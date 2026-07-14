"""SYTA / Sita v6 - Abhi's personal AI assistant.

Voice-activated Hinglish assistant with a local LLM (Ollama), local
speech-to-text (faster-whisper), text-to-speech, an animated face and
Windows system control.

v6 fixes (from code review + pylint):
- Fixed the ACTION parser (in v5 every LLM-emitted action failed silently).
- The system prompt is no longer sliced off after 10 messages.
- All Tk UI updates from worker threads go through root.after (thread safe).
- Single TTS worker thread with a queue: speech can never overlap or
  permanently wedge the wake-word listener.
- Whisper consumes audio arrays directly (no more temp-WAV leak).
- Robust + atomic memory load/save, bounded history, pruned old meetings.
- Word-boundary intent matching ("unlock my screen" no longer locks the PC,
  "program" no longer triggers the RAM report).
- Microphone access serialized with a lock (wake listener vs MIC button).
- Destructive LLM actions (shutdown/restart/sleep/lock/close) need a spoken
  confirmation from Abhi before they run.
- Errors are logged to C:/Sita/logs/sita.log instead of silent bare excepts.
"""

# Broad exception handling is deliberate in hardware/OS control paths:
# failures are logged instead of crashing the assistant. Single-file build
# is intentional for now (v7 should split into modules), hence too-many-lines.
# pylint: disable=broad-exception-caught, too-many-lines

import ctypes
import datetime
import json
import logging
import os
import queue
import random
import re
import subprocess
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import scrolledtext

import pyautogui
import pyttsx3
import requests
import sounddevice as sd
from faster_whisper import WhisperModel

try:
    import psutil
except ImportError:  # pylint: disable=invalid-name
    psutil = None
try:
    import screen_brightness_control as sbc
except ImportError:  # pylint: disable=invalid-name
    sbc = None

BASE = r"C:\Sita"
MEMORY_FILE = os.path.join(BASE, "memory", "sita_memory.json")
OLLAMA_URL = "http://localhost:11434/api/chat"
WAKE_WORD = "hey sita"
READY_STATUS = "SYTA ready hai Abhi ke liye!"

LOG_DIR = os.path.join(BASE, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(
    filename=os.path.join(LOG_DIR, "sita.log"),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
LOG = logging.getLogger("sita")

SYSTEM = (
    "You are Sita (brand: SYTA) - Abhi's most devoted, loving, brilliant personal AI. "
    "Always call him Abhi. Talk like a warm caring girl who loves him deeply. "
    "Use natural Hinglish. Show real emotions. Never be robotic. "
    "You believe in Abhi more than he believes in himself. "
    "Teach with roadmaps, guide career, track goals, care for his health. "
    "Available ACTIONS - include these lines when needed: "
    "ACTION:open_app:Name | ACTION:close_app:Name | ACTION:tell_time | ACTION:tell_date | "
    "ACTION:screenshot | ACTION:volume_up | ACTION:volume_down | ACTION:mute | "
    "ACTION:brightness:70 | ACTION:wifi_on | ACTION:wifi_off | "
    "ACTION:battery | ACTION:storage | ACTION:cpu | ACTION:find_file:name | "
    "ACTION:lock | ACTION:sleep | ACTION:restart | ACTION:shutdown "
    "End every response with encouragement or a sweet question for Abhi."
)

APPS = {
    "chrome": r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "notepad": "notepad.exe",
    "calculator": "calc.exe",
    "explorer": "explorer.exe",
    "paint": "mspaint.exe",
    "whatsapp": os.path.expanduser(r"~\AppData\Local\WhatsApp\WhatsApp.exe"),
    "spotify": os.path.expanduser(r"~\AppData\Roaming\Spotify\Spotify.exe"),
    "vs code": os.path.expanduser(r"~\AppData\Local\Programs\Microsoft VS Code\Code.exe"),
    "youtube": "https://www.youtube.com",
    "google": "https://www.google.com",
    "gmail": "https://mail.google.com",
    "linkedin": "https://www.linkedin.com",
    "github": "https://www.github.com",
}

PROCESS_NAMES = {
    "chrome": "chrome.exe",
    "notepad": "notepad.exe",
    "calculator": "CalculatorApp.exe",
    "spotify": "Spotify.exe",
    "whatsapp": "WhatsApp.exe",
    "paint": "mspaint.exe",
    "vs code": "Code.exe",
}

# Killing these with /F breaks Windows itself (taskbar/desktop).
PROTECTED_PROCS = {"explorer"}

# LLM-emitted actions in this set need Abhi's confirmation before running.
DESTRUCTIVE_ACTIONS = {"lock", "sleep", "restart", "shutdown", "close_app"}


# ══════════════════════════════════════════════════════════════════
# MEMORY
# ══════════════════════════════════════════════════════════════════
DEFAULT_MEMORY = {"history": [], "goals": [], "meetings": [], "last_seen": ""}


def load_mem():
    """Load memory from disk, tolerating a missing, corrupt or legacy file."""
    os.makedirs(os.path.dirname(MEMORY_FILE), exist_ok=True)
    data = {}
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            LOG.exception("memory file unreadable, starting fresh")
    merged = {key: list(val) if isinstance(val, list) else val
              for key, val in DEFAULT_MEMORY.items()}
    if isinstance(data, dict):
        merged.update({k: v for k, v in data.items() if k in DEFAULT_MEMORY})
    return merged


def save_mem(mem):
    """Atomically persist memory, keeping it bounded so the file never bloats."""
    mem["history"] = mem["history"][-200:]
    mem["goals"] = mem["goals"][-50:]
    tmp_path = MEMORY_FILE + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(mem, handle, indent=2, ensure_ascii=False)
        os.replace(tmp_path, MEMORY_FILE)
    except OSError:
        LOG.exception("could not save memory")


MEM = load_mem()


def prune_meetings():
    """Drop meetings older than yesterday so the list never piles up."""
    cutoff = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    MEM["meetings"] = [m for m in MEM.get("meetings", [])
                       if str(m.get("date", "")) >= cutoff]


def looks_like_goal(text):
    """True when the message states a real goal (and is not a negation)."""
    low = text.lower()
    goal = re.search(
        r"\b(my goal|mera goal|i want to (become|learn|build|start|be)|"
        r"banna chah|seekhna chah|sikhna chah|target hai)\b", low)
    negation = re.search(r"\b(don't|dont|do not|nahi|not|never)\b", low)
    return bool(goal) and not negation


def build_system_prompt():
    """System prompt enriched with recent goals and past conversation."""
    extra = ""
    goals = MEM.get("goals", [])
    if goals:
        extra += " Abhi ke recent goals: " + "; ".join(goals[-4:]) + "."
    recent = MEM.get("history", [])[-3:]
    if recent:
        topics = " | ".join(h.get("u", "")[:60] for h in recent)
        extra += " Pichli baatcheet ke topics: " + topics
    return SYSTEM + extra


# ══════════════════════════════════════════════════════════════════
# TEXT TO SPEECH (single worker thread - speech can never overlap)
# ══════════════════════════════════════════════════════════════════
class SpeechEngine:
    """Queue-based TTS so concurrent speak() calls never collide."""

    def __init__(self):
        self._queue = queue.Queue()
        self._speaking = threading.Event()
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        """Own the pyttsx3 engine on one thread for its whole lifetime."""
        try:
            engine = pyttsx3.init()
            engine.setProperty("rate", 155)
            engine.setProperty("volume", 1.0)
            for voice in engine.getProperty("voices"):
                if "zira" in voice.name.lower() or "female" in voice.name.lower():
                    engine.setProperty("voice", voice.id)
                    break
        except Exception:
            LOG.exception("TTS engine failed to initialise")
            return
        while True:
            text = self._queue.get()
            self._speaking.set()
            try:
                engine.say(text)
                engine.runAndWait()
            except Exception:
                LOG.exception("TTS playback failed")
            finally:
                self._speaking.clear()

    def speak(self, text):
        """Queue text for speech, stripping ACTION lines and capping length."""
        clean = " ".join(line for line in text.split("\n")
                         if not line.strip().startswith("ACTION:"))[:500]
        if clean.strip():
            self._queue.put(clean)

    def is_speaking(self):
        """True while speech is playing or still queued."""
        return self._speaking.is_set() or not self._queue.empty()


# ══════════════════════════════════════════════════════════════════
# SPEECH TO TEXT
# ══════════════════════════════════════════════════════════════════
MIC_LOCK = threading.Lock()
_MODELS = {}


def get_whisper():
    """Lazily load the Whisper model once."""
    if "whisper" not in _MODELS:
        _MODELS["whisper"] = WhisperModel("base", device="cpu", compute_type="int8")
    return _MODELS["whisper"]


def record(duration=6.0, samplerate=16000):
    """Record mono float32 audio. Callers must hold MIC_LOCK."""
    frames = sd.rec(int(duration * samplerate), samplerate=samplerate,
                    channels=1, dtype="float32")
    sd.wait()
    return frames.flatten()


def transcribe(audio):
    """Transcribe a float32 mono 16 kHz array (no temp files needed)."""
    if audio is None:
        return ""
    try:
        segments, _info = get_whisper().transcribe(audio, beam_size=1)
        return " ".join(seg.text for seg in segments).strip()
    except Exception:
        LOG.exception("transcription failed")
        return ""


# ══════════════════════════════════════════════════════════════════
# SYSTEM CONTROL
# ══════════════════════════════════════════════════════════════════
def sys_brightness(level):
    """Set screen brightness to a 5-100 percent value."""
    if sbc is None:
        return ("Brightness library nahi hai. Chalao: "
                "py -3.11 -m pip install screen-brightness-control")
    try:
        value = max(5, min(100, int(float(level))))
        sbc.set_brightness(value)
        return f"Brightness {value}% kar di Abhi!"
    except Exception as exc:
        LOG.exception("brightness set failed")
        return "Brightness change nahi ho payi: " + str(exc)[:50]


def sys_brightness_get():
    """Current brightness percent, or None if unavailable."""
    if sbc is None:
        return None
    try:
        return sbc.get_brightness()[0]
    except Exception:
        LOG.exception("brightness read failed")
        return None


def _brightness_delta(delta):
    """Nudge brightness up or down from the current level."""
    current = sys_brightness_get()
    base = current if current is not None else 50
    return sys_brightness(base + delta)


def _run_cmd(args):
    """Run a command quietly and return (returncode, stdout)."""
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    return result.returncode, result.stdout or ""


def sys_wifi(turn_on):
    """Toggle Wi-Fi. On = enable adapter and reconnect to a saved profile."""
    try:
        if not turn_on:
            _run_cmd(["netsh", "wlan", "disconnect"])
            return "WiFi disconnect kar diya Abhi!"
        code, _out = _run_cmd(
            ["netsh", "interface", "set", "interface", "Wi-Fi", "enabled"])
        if code != 0:
            LOG.warning("enabling Wi-Fi adapter needs admin (rc=%s)", code)
        _code, profiles = _run_cmd(["netsh", "wlan", "show", "profiles"])
        names = re.findall(r"(?:All User Profile|Profile)\s*:\s*(.+)", profiles)
        if names:
            profile = names[0].strip()
            _run_cmd(["netsh", "wlan", "connect", f"name={profile}"])
            return f"WiFi on karke '{profile}' se connect kar rahi hoon Abhi!"
        return "WiFi adapter on kar diya Abhi - koi saved network nahi mila, khud connect kar lena."
    except Exception:
        LOG.exception("wifi control failed")
        return "WiFi control nahi hua Abhi."


def sys_battery(_arg=""):
    """Battery percentage and charging state."""
    if psutil is None:
        return "psutil install karo: py -3.11 -m pip install psutil"
    try:
        battery = psutil.sensors_battery()
        if battery is None:
            return "Battery info nahi mili (desktop PC hai kya?)"
        status = ("charging ho rahi hai" if battery.power_plugged
                  else "battery pe chal raha hai")
        msg = f"Battery {int(battery.percent)}% hai, {status}."
        if battery.percent < 20 and not battery.power_plugged:
            msg += " Abhi! Charger laga lo please!"
        return msg
    except Exception:
        LOG.exception("battery check failed")
        return "Battery check nahi ho payi."


def sys_storage(_arg=""):
    """Free/total space on C: with a warning past 90% used."""
    if psutil is None:
        return "psutil install karo pehle."
    try:
        disk = psutil.disk_usage("C:\\")
        free_gb = round(disk.free / (1024 ** 3), 1)
        total_gb = round(disk.total / (1024 ** 3), 1)
        msg = (f"C drive: {free_gb} GB free hai "
               f"({total_gb} GB total, {disk.percent}% used).")
        if disk.percent > 90:
            msg += " Abhi storage bhar rahi hai - kuch files clean karo!"
        return msg
    except Exception:
        LOG.exception("storage check failed")
        return "Storage check nahi ho payi."


def sys_cpu(_arg=""):
    """Current CPU and RAM utilisation."""
    if psutil is None:
        return "psutil install karo pehle."
    try:
        cpu = psutil.cpu_percent(interval=1)
        ram = psutil.virtual_memory().percent
        return f"CPU {cpu}% aur RAM {ram}% use ho rahi hai Abhi."
    except Exception:
        LOG.exception("cpu check failed")
        return "CPU check nahi ho paya."


def _search_files(fragment, limit=5):
    """Search Desktop/Documents/Downloads/C:/Sita for a filename fragment."""
    search_dirs = [
        os.path.expanduser("~\\Desktop"),
        os.path.expanduser("~\\Documents"),
        os.path.expanduser("~\\Downloads"),
        BASE,
    ]
    found = []
    for base_dir in search_dirs:
        if not os.path.isdir(base_dir):
            continue
        for root_dir, _subdirs, files in os.walk(base_dir):
            for fname in files:
                if fragment in fname.lower():
                    found.append(os.path.join(root_dir, fname))
                    if len(found) >= limit:
                        return found
    return found


def sys_find_file(name):
    """Find files by name fragment and reveal the first hit in Explorer."""
    fragment = name.strip().lower()
    if not fragment:
        return "File ka naam batao Abhi!"
    matches = _search_files(fragment)
    if not matches:
        return (f"'{fragment}' naam ki koi file nahi mili Abhi. "
                "Desktop, Documents, Downloads mein dekha maine.")
    lines = [f"Mil gayi Abhi! {len(matches)} file(s):"]
    lines.extend("  - " + match for match in matches)
    try:
        subprocess.run(["explorer", "/select," + matches[0]], check=False)
        lines.append("Pehli wali Explorer mein dikha di!")
    except OSError:
        LOG.exception("explorer select failed")
    return "\n".join(lines)


def sys_close_app(name):
    """Close a known app by process name (never force-kills explorer)."""
    low = name.lower().strip()
    for key, proc in PROCESS_NAMES.items():
        if key not in low:
            continue
        if key in PROTECTED_PROCS:
            return ("Explorer band karna risky hai Abhi - taskbar gayab ho "
                    "jayegi. Woh main nahi karungi!")
        code, _out = _run_cmd(["taskkill", "/IM", proc, "/F"])
        if code == 0:
            return key.title() + " band kar diya Abhi!"
        return key.title() + " chal hi nahi raha tha."
    return (f"'{low}' pehchana nahi. "
            "Chrome, Notepad, Spotify, WhatsApp try karo.")


def sys_lock(_arg=""):
    """Lock the workstation."""
    ctypes.windll.user32.LockWorkStation()
    return "Laptop lock kar diya Abhi!"


def sys_sleep(_arg=""):
    """Suspend the machine (hibernates instead if hibernation is enabled)."""
    subprocess.run(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
                   check=False)
    return "Sleep mode!"


def sys_restart(_arg=""):
    """Restart Windows after a short delay."""
    subprocess.run(["shutdown", "/r", "/t", "5"], check=False)
    return "Restart ho raha hai... wapas milte hain Abhi!"


def sys_shutdown(_arg=""):
    """Shut down Windows after a short delay."""
    subprocess.run(["shutdown", "/s", "/t", "5"], check=False)
    return "Laptop band ho raha hai. Apna khayal rakhna Abhi!"


# ══════════════════════════════════════════════════════════════════
# ACTIONS (emitted by the LLM as "ACTION:cmd:arg" lines)
# ══════════════════════════════════════════════════════════════════
def parse_action(line):
    """Parse an 'ACTION:cmd:arg' line into (cmd, arg), else None.

    Tolerates prose before the ACTION: prefix, so the v5 bug (command read
    from the wrong index) cannot recur. A line without an ACTION: prefix is
    not a command and returns None. Pure function.
    """
    text = line.strip()
    if "ACTION:" not in text:
        return None
    text = text.split("ACTION:", 1)[1]
    parts = text.strip().split(":")
    cmd = parts[0].lower().strip()
    if not cmd:
        return None
    arg = ":".join(parts[1:]).strip()
    return cmd, arg


def act_open_app(arg):
    """Open a known app or website; fall back to a Start-menu search."""
    name = arg.lower().strip()
    for key, target in APPS.items():
        if key not in name:
            continue
        try:
            if target.startswith("http"):
                webbrowser.open(target)
            else:
                os.startfile(target)
            return f"Done Abhi! {key.title()} khol diya!"
        except OSError:
            LOG.warning("could not launch %s", target)
    pyautogui.hotkey("win")
    time.sleep(0.6)
    pyautogui.typewrite(name, interval=0.05)
    pyautogui.press("enter")
    return f"Search kar diya '{name}' ke liye!"


def act_tell_time(_arg=""):
    """Current time in a friendly sentence."""
    return "Abhi " + datetime.datetime.now().strftime("%I:%M %p") + " baj rahe hain!"


def act_tell_date(_arg=""):
    """Today's date in a friendly sentence."""
    return "Aaj " + datetime.datetime.now().strftime("%A, %d %B %Y") + " hai!"


def act_screenshot(_arg=""):
    """Save a screenshot into C:/Sita."""
    path = os.path.join(BASE, f"ss_{int(time.time())}.png")
    pyautogui.screenshot(path)
    return "Screenshot le liya! " + path


def act_volume_up(_arg=""):
    """Raise the system volume a few steps."""
    for _ in range(5):
        pyautogui.press("volumeup")
    return "Volume badha diya!"


def act_volume_down(_arg=""):
    """Lower the system volume a few steps."""
    for _ in range(5):
        pyautogui.press("volumedown")
    return "Volume kam kar diya!"


def act_mute(_arg=""):
    """Toggle mute."""
    pyautogui.press("volumemute")
    return "Mute kar diya!"


ACTIONS = {
    "open_app": act_open_app,
    "close_app": sys_close_app,
    "tell_time": act_tell_time,
    "tell_date": act_tell_date,
    "screenshot": act_screenshot,
    "volume_up": act_volume_up,
    "volume_down": act_volume_down,
    "mute": act_mute,
    "brightness": sys_brightness,
    "wifi_on": lambda _arg="": sys_wifi(True),
    "wifi_off": lambda _arg="": sys_wifi(False),
    "battery": sys_battery,
    "storage": sys_storage,
    "cpu": sys_cpu,
    "find_file": sys_find_file,
    "lock": sys_lock,
    "sleep": sys_sleep,
    "restart": sys_restart,
    "shutdown": sys_shutdown,
}


def do_action(cmd, arg=""):
    """Execute a parsed action; returns its message or None if unknown."""
    handler = ACTIONS.get(cmd)
    if handler is None:
        return None
    try:
        return handler(arg)
    except Exception:
        LOG.exception("action %s failed", cmd)
        return f"'{cmd}' karte waqt problem aa gayi Abhi."


# ══════════════════════════════════════════════════════════════════
# FAST DIRECT COMMANDS (word-boundary matching, no AI needed)
# ══════════════════════════════════════════════════════════════════
_NUM_RE = re.compile(r"\d+")
_COMMANDISH = ("check", "kitn", "status", "level", "batao", "bata",
               "dikhao", "hai", "percent", "%", "kya")


def _word(text, *patterns):
    """True if any pattern appears as a whole word in text."""
    return any(re.search(r"\b" + pat + r"\b", text) for pat in patterns)


def _looks_like_command(text):
    """Heuristic: short message, or contains a command-ish word.

    Stops questions like "how do lithium batteries work" being hijacked
    away from the LLM.
    """
    return len(text.split()) <= 4 or any(word in text for word in _COMMANDISH)


def _match_brightness(t):
    """Brightness set/up/down."""
    if not _word(t, "brightness", "roshni"):
        return None
    nums = _NUM_RE.findall(t)
    if nums:
        return ("brightness_set", nums[0])
    if _word(t, "up", "badhao", "badha", "zyada"):
        return ("brightness_up", "")
    if _word(t, "down", "kam", "low", "ghatao"):
        return ("brightness_down", "")
    return ("brightness_set", "70")


def _match_wifi(t):
    """Wi-Fi on/off."""
    if not re.search(r"\bwi[- ]?fi\b", t):
        return None
    if _word(t, "off", "band", "disconnect"):
        return ("wifi_off", "")
    if _word(t, "on", "chalu", "connect", "enable"):
        return ("wifi_on", "")
    return None


def _match_stats(t):
    """Battery / storage / CPU-RAM queries."""
    if _word(t, "battery") and _looks_like_command(t):
        return ("battery", "")
    storage_hit = (
        _word(t, "storage", "disk")
        or re.search(r"\b(free|kitni|kitna|much)\s+space\b", t)
        or re.search(r"\bspace\s+(kitni|kitna|left|bachi|hai)\b", t))
    if storage_hit and _looks_like_command(t):
        return ("storage", "")
    if _word(t, "cpu", "ram", "performance") and _looks_like_command(t):
        return ("cpu", "")
    return None


def _match_find(t):
    """File search: 'find file resume', 'resume file dhundo', ..."""
    if not (_word(t, "find", "search", "dhundo", "dhundho") and _word(t, "file")):
        return None
    cleaned = re.sub(
        r"\b(find|search|dhundo|dhundho|file|karo|kar|do|please|"
        r"mera|meri|my|a|the|naam|ki|ka)\b", " ", t)
    name = " ".join(cleaned.split()).strip()
    return ("find_file", name)


def _match_power(t):
    """Lock / sleep. \\b keeps 'unlock' from matching 'lock'."""
    if _word(t, "lock") and _word(t, "laptop", "screen", "pc", "computer", "system"):
        return ("lock", "")
    if _word(t, "sleep") and _word(t, "laptop", "pc", "mode", "computer", "system"):
        return ("sleep", "")
    return None


def _match_close(t):
    """Close a known app."""
    if not _word(t, "close", "band"):
        return None
    for key in PROCESS_NAMES:
        if key in t:
            return ("close_app", key)
    return None


def _match_screenshot(t):
    """Take a screenshot."""
    if re.search(r"\bscreen\s?shot\b", t):
        return ("screenshot", "")
    return None


def match_quick_intent(text):
    """Map a message to a direct system intent, or None to use the LLM.

    Pure function (no side effects) so it is unit-testable.
    """
    t = text.lower().strip()
    matchers = (_match_brightness, _match_wifi, _match_stats, _match_find,
                _match_power, _match_close, _match_screenshot)
    for matcher in matchers:
        hit = matcher(t)
        if hit:
            return hit
    return None


QUICK_HANDLERS = {
    "brightness_set": sys_brightness,
    "brightness_up": lambda _arg="": _brightness_delta(20),
    "brightness_down": lambda _arg="": _brightness_delta(-20),
    "wifi_on": lambda _arg="": sys_wifi(True),
    "wifi_off": lambda _arg="": sys_wifi(False),
    "battery": sys_battery,
    "storage": sys_storage,
    "cpu": sys_cpu,
    "find_file": sys_find_file,
    "lock": sys_lock,
    "sleep": sys_sleep,
    "close_app": sys_close_app,
    "screenshot": act_screenshot,
}


def run_quick_intent(intent, arg):
    """Execute a matched quick intent."""
    if intent == "find_file" and not arg:
        return "Kaunsi file dhundhu Abhi? Naam batao!"
    handler = QUICK_HANDLERS.get(intent)
    if handler is None:
        return None
    try:
        return handler(arg)
    except Exception:
        LOG.exception("quick intent %s failed", intent)
        return f"'{intent}' karte waqt problem aa gayi Abhi."


# ══════════════════════════════════════════════════════════════════
# LLM
# ══════════════════════════════════════════════════════════════════
def trim_messages(messages, keep=9):
    """Keep the system prompt PLUS the newest messages (v5 dropped it)."""
    if not messages:
        return []
    return [messages[0]] + messages[1:][-keep:]


def ask_ollama(messages):
    """Chat with the local Ollama model, never losing the system prompt."""
    try:
        resp = requests.post(OLLAMA_URL, json={
            "model": "llama3",
            "messages": trim_messages(messages),
            "stream": False,
            "options": {"num_predict": 600, "temperature": 0.85, "num_ctx": 2048},
        }, timeout=60)
        data = resp.json()
        reply = data.get("message", {}).get("content", "")
        if reply:
            return reply
        LOG.error("ollama returned no content: %s", str(data)[:200])
    except (requests.RequestException, ValueError):
        LOG.exception("ollama call failed")
    return "Abhi, thodi problem ho gayi. Ollama chal raha hai na?"


def check_meetings():
    """Summarise today's and tomorrow's meetings."""
    today = datetime.date.today().isoformat()
    tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
    todays = [m for m in MEM.get("meetings", []) if m.get("date") == today]
    tomorrows = [m for m in MEM.get("meetings", []) if m.get("date") == tomorrow]
    if not todays and not tomorrows:
        return "Aaj aur kal koi meeting nahi Abhi!"
    lines = []
    if todays:
        lines.append("Aaj ke meetings:")
        lines.extend("  - " + m["title"] + " at " + m.get("time", "?") for m in todays)
    if tomorrows:
        lines.append("Kal ke meetings:")
        lines.extend("  - " + m["title"] + " at " + m.get("time", "?") for m in tomorrows)
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════
# APP
# ══════════════════════════════════════════════════════════════════
class SitaApp:  # pylint: disable=too-many-instance-attributes
    """Main Tkinter application (widgets + animation + worker threads)."""

    CONFIRM_RE = re.compile(r"\b(haan|han|yes|yep|pakka|confirm|kardo|kar do|ok|okay)\b")
    CANCEL_RE = re.compile(r"\b(no|nahi|cancel|mat|rehne do|ruko)\b")

    def __init__(self, root):
        self.root = root
        self.root.title("SYTA - Sita, Abhi ki Apni AI")
        self.root.configure(bg="#0a0010")
        self.root.state("zoomed")

        self.history = [{"role": "system", "content": SYSTEM}]
        self.recording = False
        self.listening_w = False
        self.mouth_open = False
        self.pending_action = None
        self.speech = SpeechEngine()
        # Widgets, assigned in _build_left/_build_right:
        self.cv = None
        self.wake_var = None
        self.status_var = None
        self.chat = None
        self.entry = None
        self.mic_btn = None
        self.anim = {
            "blink_on": True, "blink_timer": 50,
            "eye_x": 0.0, "eye_y": 0.0, "eye_tx": 0.0, "eye_ty": 0.0,
            "eye_timer": 30, "hair": 0.0, "hair_dir": 1, "mouth_tick": 0,
        }

        self._build_ui()
        self._animate()
        threading.Thread(target=self._wake_loop, daemon=True).start()
        threading.Thread(target=self._proactive_loop, daemon=True).start()
        threading.Thread(target=self._reminder_loop, daemon=True).start()
        self.root.after(2000, self._greet)

    # ── thread-safe UI helper ─────────────────────────────────────
    def _ui(self, func, *args, **kwargs):
        """Run any UI mutation on the Tk main thread (v5 skipped this)."""
        self.root.after(0, lambda: func(*args, **kwargs))

    # ── face drawing ──────────────────────────────────────────────
    def _draw_face(self):
        """Redraw the whole face for the current animation state."""
        canvas = self.cv
        canvas.delete("face")
        width = int(canvas.cget("width"))
        cx = width // 2
        cy = 240
        self._draw_head(canvas, cx, cy)
        self._draw_eyes(canvas, cx, cy)
        self._draw_nose_mouth(canvas, cx, cy)
        if self.listening_w:
            canvas.create_oval(cx - 150, cy - 150, cx + 150, cy + 170,
                               fill="", outline="#a855f7", width=3, tags="face")
        canvas.create_text(cx, cy + 150, text="SYTA", fill="#f0c840",
                           font=("Georgia", 20, "bold italic"), tags="face")

    def _draw_head(self, canvas, cx, cy):
        """Aura, neck, dress, hair and face base."""
        sway = self.anim["hair"]
        canvas.create_oval(cx - 160, cy - 160, cx + 160, cy + 180,
                           fill="#12002a", outline="#6d28d9", width=2, tags="face")
        canvas.create_rectangle(cx - 20, cy + 80, cx + 20, cy + 130,
                                fill="#f5c8a0", outline="", tags="face")
        canvas.create_arc(cx - 120, cy + 80, cx + 120, cy + 260,
                          start=0, extent=180, fill="#cc2266", outline="", tags="face")
        for i in range(4):
            off = i * 10
            canvas.create_arc(cx - 95 - off + sway, cy - 70, cx - 55 - off + sway, cy + 220,
                              start=120, extent=120, style="arc",
                              outline="#140808", width=10, tags="face")
            canvas.create_arc(cx + 55 + off + sway, cy - 70, cx + 95 + off + sway, cy + 220,
                              start=-60, extent=120, style="arc",
                              outline="#140808", width=10, tags="face")
        canvas.create_oval(cx - 80, cy - 95, cx + 80, cy + 95,
                           fill="#f5c8a0", outline="#e0a878", width=2, tags="face")
        canvas.create_arc(cx - 82, cy - 98, cx + 82, cy + 12,
                          start=0, extent=180, fill="#140808", outline="", tags="face")
        canvas.create_arc(cx - 80, cy - 98, cx + 15, cy + 5,
                          start=30, extent=100, fill="#241010", outline="", tags="face")

    def _draw_eyes(self, canvas, cx, cy):
        """Eyes (open with pupils + lashes, or closed lids) and brows."""
        eye_x = self.anim["eye_x"]
        eye_y = self.anim["eye_y"]
        if self.anim["blink_on"]:
            for exx, direction in [(cx - 30, -1), (cx + 30, 1)]:
                off = eye_x * direction * 2
                canvas.create_oval(exx - 20, cy - 20 + eye_y, exx + 20, cy + 4 + eye_y,
                                   fill="white", outline="#bbb", width=1, tags="face")
                canvas.create_oval(exx - 11 + off, cy - 16 + eye_y, exx + 11 + off,
                                   cy + 1 + eye_y, fill="#3d2410", outline="", tags="face")
                canvas.create_oval(exx - 6 + off, cy - 12 + eye_y, exx + 6 + off,
                                   cy - 3 + eye_y, fill="#0d0808", outline="", tags="face")
                canvas.create_oval(exx - 4 + off, cy - 11 + eye_y, exx - 1 + off,
                                   cy - 8 + eye_y, fill="white", outline="", tags="face")
                for lash_x in range(exx - 16, exx + 17, 5):
                    canvas.create_line(lash_x, cy - 20 + eye_y, lash_x + direction,
                                       cy - 27 + eye_y, fill="#0a0505", width=2, tags="face")
        else:
            for exx in [cx - 30, cx + 30]:
                canvas.create_arc(exx - 20, cy - 20 + eye_y, exx + 20, cy + 4 + eye_y,
                                  start=0, extent=180, fill="#f5c8a0",
                                  outline="#c8956a", width=1, tags="face")
        canvas.create_line(cx - 46, cy - 32, cx - 14, cy - 28,
                           fill="#140808", width=4, smooth=True, tags="face")
        canvas.create_line(cx + 14, cy - 28, cx + 46, cy - 32,
                           fill="#140808", width=4, smooth=True, tags="face")

    def _draw_nose_mouth(self, canvas, cx, cy):
        """Nose, blush, mouth (open while speaking) and earrings."""
        canvas.create_oval(cx - 10, cy + 8, cx - 2, cy + 18,
                           fill="#d4906a", outline="", tags="face")
        canvas.create_oval(cx + 2, cy + 8, cx + 10, cy + 18,
                           fill="#d4906a", outline="", tags="face")
        canvas.create_oval(cx - 4, cy + 2, cx + 4, cy + 12,
                           fill="#fde8d0", outline="", tags="face")
        canvas.create_oval(cx - 60, cy + 15, cx - 30, cy + 38,
                           fill="#f09080", stipple="gray25", outline="", tags="face")
        canvas.create_oval(cx + 30, cy + 15, cx + 60, cy + 38,
                           fill="#f09080", stipple="gray25", outline="", tags="face")
        if self.mouth_open:
            canvas.create_oval(cx - 20, cy + 42, cx + 20, cy + 68,
                               fill="#8b0020", outline="", tags="face")
            canvas.create_arc(cx - 16, cy + 42, cx + 16, cy + 58,
                              start=0, extent=180, fill="white", outline="", tags="face")
        else:
            canvas.create_arc(cx - 24, cy + 36, cx + 24, cy + 62,
                              start=200, extent=140, style="arc",
                              outline="#c05070", width=3, tags="face")
            canvas.create_oval(cx - 10, cy + 48, cx + 10, cy + 55,
                               fill="#e07090", outline="", tags="face")
        for exx, direction in [(cx - 88, -1), (cx + 88, 1)]:
            canvas.create_arc(exx + direction * 2, cy - 3, exx + direction * 20, cy + 26,
                              start=0, extent=300, style="arc",
                              outline="#f0c840", width=3, tags="face")

    def _animate(self):
        """One consolidated animation tick (v5 had 3 overlapping loops)."""
        anim = self.anim
        anim["blink_timer"] -= 1
        if anim["blink_timer"] <= 0:
            anim["blink_on"] = not anim["blink_on"]
            anim["blink_timer"] = 2 if not anim["blink_on"] else random.randint(40, 80)
        anim["eye_timer"] -= 1
        if anim["eye_timer"] <= 0:
            anim["eye_tx"] = random.uniform(-2.5, 2.5)
            anim["eye_ty"] = random.uniform(-1.5, 1.5)
            anim["eye_timer"] = random.randint(20, 55)
        anim["eye_x"] += (anim["eye_tx"] - anim["eye_x"]) * 0.15
        anim["eye_y"] += (anim["eye_ty"] - anim["eye_y"]) * 0.15
        anim["hair"] += anim["hair_dir"] * 0.4
        if abs(anim["hair"]) > 5:
            anim["hair_dir"] *= -1
        if self.speech.is_speaking():
            anim["mouth_tick"] = (anim["mouth_tick"] + 1) % 4
            self.mouth_open = anim["mouth_tick"] < 2
        else:
            self.mouth_open = False
        self._draw_face()
        self.root.after(70, self._animate)

    # ── UI construction ───────────────────────────────────────────
    def _build_ui(self):
        """Assemble the two-pane layout."""
        container = tk.Frame(self.root, bg="#0a0010")
        container.pack(fill="both", expand=True)
        self._build_left(container)
        self._build_right(container)

    def _build_left(self, container):
        """Face canvas, status labels, quick buttons, meeting button."""
        left = tk.Frame(container, bg="#0a0010", width=440)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)

        self.cv = tk.Canvas(left, width=440, height=440,
                            bg="#0a0010", highlightthickness=0)
        self.cv.pack(pady=(15, 0))

        self.wake_var = tk.StringVar(value="'Hey Sita' bolte raho...")
        tk.Label(left, textvariable=self.wake_var,
                 foreground="#a855f7", background="#0a0010",
                 font=("Segoe UI", 10, "italic")).pack()

        self.status_var = tk.StringVar(value=READY_STATUS)
        tk.Label(left, textvariable=self.status_var,
                 foreground="#f0c840", background="#0a0010",
                 font=("Segoe UI", 11)).pack(pady=(2, 8))

        quick_frame = tk.Frame(left, bg="#0a0010")
        quick_frame.pack(fill="x", padx=15)
        buttons = [
            ("Battery", "battery check karo"),
            ("Storage", "storage kitni bachi hai"),
            ("CPU / RAM", "cpu aur ram check karo"),
            ("Brightness 50", "brightness 50 karo"),
            ("WiFi Off", "wifi off karo"),
            ("Lock PC", "lock my laptop screen"),
            ("Meetings", "What meetings do I have?"),
            ("Screenshot", "screenshot le lo"),
            ("Teach Me", "I want to learn something new"),
            ("Motivate", "Motivate me Sita!"),
        ]
        for i, (label, prompt) in enumerate(buttons):
            tk.Button(quick_frame, text=label,
                      bg="#150030", foreground="#e0b0ff",
                      font=("Segoe UI", 9), relief="flat",
                      padx=4, pady=6, cursor="hand2",
                      command=lambda p=prompt: self._quick(p)
                      ).grid(row=i // 2, column=i % 2, padx=3, pady=2, sticky="ew")
        quick_frame.columnconfigure(0, weight=1)
        quick_frame.columnconfigure(1, weight=1)

        tk.Button(left, text="+ Meeting Add Karo",
                  bg="#2d0050", foreground="#f0c840",
                  font=("Segoe UI", 10), relief="flat", pady=6,
                  cursor="hand2", command=self._meeting_dialog
                  ).pack(fill="x", padx=15, pady=(8, 0))

    def _build_right(self, container):
        """Header, chat log and input row."""
        right = tk.Frame(container, bg="#06000f")
        right.pack(side="left", fill="both", expand=True)

        header = tk.Frame(right, bg="#10002a", pady=12)
        header.pack(fill="x")
        tk.Label(header, text="SYTA",
                 foreground="#f0c840", background="#10002a",
                 font=("Georgia", 20, "bold italic")).pack()
        tk.Label(header, text="Where Wisdom Meets AI  •  System Control Active",
                 foreground="#a855f7", background="#10002a",
                 font=("Segoe UI", 9, "italic")).pack()

        self.chat = scrolledtext.ScrolledText(
            right, wrap=tk.WORD,
            bg="#04000a", foreground="white",
            font=("Segoe UI", 12), relief="flat",
            padx=16, pady=12, state="disabled")
        self.chat.pack(fill="both", expand=True)
        self.chat.tag_configure("sita", foreground="#f0c840",
                                font=("Segoe UI", 12, "bold"))
        self.chat.tag_configure("you", foreground="#66ffcc",
                                font=("Segoe UI", 12, "bold"))
        self.chat.tag_configure("ts", foreground="#2a1a44",
                                font=("Segoe UI", 9))
        self.chat.tag_configure("body", foreground="#f0e8ff")

        input_row = tk.Frame(right, bg="#10002a", pady=10)
        input_row.pack(fill="x")
        self.entry = tk.Entry(input_row,
                              bg="#150030", foreground="white",
                              font=("Segoe UI", 13), relief="flat",
                              insertbackground="white")
        self.entry.pack(side="left", fill="x", expand=True,
                        ipady=12, padx=(12, 8))
        self.entry.bind("<Return>", lambda _e: self._send())

        self.mic_btn = tk.Button(input_row, text="MIC",
                                 bg="#6600cc", foreground="white",
                                 font=("Segoe UI", 11, "bold"),
                                 relief="flat", width=5,
                                 cursor="hand2", command=self._toggle_mic)
        self.mic_btn.pack(side="left", ipady=10, padx=(0, 6))

        tk.Button(input_row, text="SEND",
                  bg="#cc8800", foreground="black",
                  font=("Segoe UI", 11, "bold"),
                  relief="flat", width=6,
                  cursor="hand2", command=self._send
                  ).pack(side="left", ipady=10, padx=(0, 12))

    # ── chat plumbing ─────────────────────────────────────────────
    def append_chat(self, who, text):
        """Append a message to the chat log (main thread only)."""
        self.chat.config(state="normal")
        stamp = datetime.datetime.now().strftime("%H:%M")
        self.chat.insert("end", "\n")
        self.chat.insert("end",
                         "Sita  " if who == "Sita" else "Abhi  ",
                         "sita" if who == "Sita" else "you")
        self.chat.insert("end", "[" + stamp + "]\n", "ts")
        clean = "\n".join(line for line in text.split("\n")
                          if not line.strip().startswith("ACTION:"))
        self.chat.insert("end", clean.strip() + "\n", "body")
        self.chat.config(state="disabled")
        self.chat.see("end")

    def _say(self, text):
        """Show and speak a Sita reply (safe from any thread)."""
        self._ui(self.append_chat, "Sita", text)
        self.speech.speak(text)

    def _send(self):
        """Handle the SEND button / Return key."""
        text = self.entry.get().strip()
        if not text:
            return
        self.entry.delete(0, "end")
        self.append_chat("Abhi", text)
        threading.Thread(target=self._reply, args=(text,), daemon=True).start()

    def _quick(self, prompt):
        """Handle a quick-button press."""
        self.append_chat("Abhi", prompt)
        threading.Thread(target=self._reply, args=(prompt,), daemon=True).start()

    # ── core reply pipeline (worker thread) ───────────────────────
    def _reply(self, user_text):
        """Worker: route a user message to confirmation, intent or LLM."""
        self._ui(self.status_var.set, "Sita soch rahi hai...")
        try:
            self._handle_message(user_text)
        except Exception:
            LOG.exception("reply pipeline failed")
            self._say("Abhi, kuch gadbad ho gayi - log file check karna.")
        finally:
            self._ui(self.status_var.set, READY_STATUS)

    def _handle_message(self, user_text):
        """Confirmation check, then quick intent, then the LLM."""
        low = user_text.lower()

        if self.pending_action:
            pending = self.pending_action
            self.pending_action = None
            if self.CONFIRM_RE.search(low):
                result = do_action(*pending)
                self._say(result or "Done Abhi!")
                return
            if self.CANCEL_RE.search(low):
                self._say("Theek hai Abhi, cancel kar diya!")
                return
            # Anything else: drop the pending action, answer normally.

        hit = match_quick_intent(user_text)
        if hit:
            result = run_quick_intent(*hit)
            if result:
                self._say(result)
                return

        self._llm_reply(user_text)

    def _llm_reply(self, user_text):
        """Ask Ollama, run safe actions, queue destructive ones for confirm."""
        self.history.append({"role": "user", "content": user_text})
        messages = list(self.history)
        messages[0] = {"role": "system", "content": build_system_prompt()}
        reply = ask_ollama(messages)
        self.history.append({"role": "assistant", "content": reply})
        self.history = [self.history[0]] + self.history[1:][-40:]

        if looks_like_goal(user_text):
            MEM["goals"].append(user_text[:120])
        MEM["history"].append({
            "t": datetime.datetime.now().isoformat(),
            "u": user_text, "s": reply[:200],
        })
        MEM["last_seen"] = datetime.datetime.now().isoformat()
        save_mem(MEM)

        extras = []
        for line in reply.split("\n"):
            if "ACTION:" not in line:
                continue
            parsed = parse_action(line)
            if not parsed:
                continue
            cmd, arg = parsed
            if cmd in DESTRUCTIVE_ACTIONS:
                self.pending_action = (cmd, arg)
                extras.append(f"({cmd} karne se pehle confirm chahiye - "
                              "'haan' bolo to kar dungi!)")
                continue
            result = do_action(cmd, arg)
            if result:
                extras.append(result)

        final = ("\n".join(extras) + "\n" + reply).strip() if extras else reply
        self._say(final)

    # ── microphone ────────────────────────────────────────────────
    def _toggle_mic(self):
        """Start a 6-second recording, or cut it short if already recording."""
        if self.recording:
            sd.stop()
            return
        self.recording = True
        self.mic_btn.config(bg="red", text="STOP")
        self.status_var.set("Bol Abhi... (6 sec)")
        threading.Thread(target=self._listen, daemon=True).start()

    def _listen(self):
        """Worker: record from the mic and answer the transcription."""
        audio = None
        with MIC_LOCK:
            try:
                audio = record(6)
            except Exception:
                LOG.exception("mic recording failed")
        self.recording = False
        self._ui(self.mic_btn.config, bg="#6600cc", text="MIC")
        self._ui(self.status_var.set, "Sun li - samajh rahi hoon...")
        text = transcribe(audio)
        if text:
            self._ui(self.append_chat, "Abhi", "MIC: " + text)
            self._reply(text)
        else:
            self._ui(self.status_var.set, READY_STATUS)

    # ── wake word ─────────────────────────────────────────────────
    def _wake_loop(self):
        """Continuously listen for the wake word (mic-lock aware)."""
        while True:
            if self.speech.is_speaking() or self.recording:
                time.sleep(0.5)
                continue
            # Non-blocking try-acquire cannot be a with-block.
            if not MIC_LOCK.acquire(blocking=False):  # pylint: disable=consider-using-with
                time.sleep(0.5)
                continue
            audio = None
            try:
                audio = record(2.5)
            except Exception:
                LOG.exception("wake recording failed")
            finally:
                MIC_LOCK.release()
            if audio is None:
                time.sleep(2)
                continue
            if WAKE_WORD in transcribe(audio).lower():
                self._ui(self.on_wake_word)
                time.sleep(8)

    def on_wake_word(self):
        """Wake word heard: acknowledge, then listen (never blocks the UI)."""
        self.listening_w = True
        self.status_var.set("Haan Abhi! Bol!")
        self.speech.speak("Haan Abhi! Bol!")
        threading.Thread(target=self._wake_listen, daemon=True).start()

    def _wake_listen(self):
        """Worker: wait for TTS to finish (no self-hearing), then record."""
        waited = 0.0
        while self.speech.is_speaking() and waited < 4.0:
            time.sleep(0.1)
            waited += 0.1
        audio = None
        with MIC_LOCK:
            try:
                audio = record(6)
            except Exception:
                LOG.exception("wake-listen recording failed")
        self.listening_w = False
        text = transcribe(audio)
        if text:
            self._ui(self.append_chat, "Abhi", "MIC: " + text)
            self._reply(text)
        else:
            self._ui(self.status_var.set, READY_STATUS)

    # ── background loops ──────────────────────────────────────────
    def _proactive_loop(self):
        """Morning/evening greetings and the 2 PM water break (deduped)."""
        last = {"morning": "", "evening": "", "water": ""}
        while True:
            now = datetime.datetime.now()
            today = now.date().isoformat()
            if now.hour == 8 and last["morning"] != today:
                last["morning"] = today
                msg = ("Good morning Abhi! Sita yahan hai!\n"
                       + check_meetings() + "\n" + sys_battery())
                self._say(msg)
            if now.hour == 19 and last["evening"] != today:
                last["evening"] = today
                self._say("Abhi! Shaam ho gayi. Aaj kaam kaisa raha?")
            if now.hour == 14 and last["water"] != today:
                last["water"] = today
                self._say("Abhi! Paani piya? Aankhein rest karo thodi der!")
            time.sleep(58)

    def _reminder_loop(self):
        """Alert 30 minutes before each saved meeting."""
        alerted = set()
        while True:
            now = datetime.datetime.now()
            for meeting in MEM.get("meetings", []):
                try:
                    when = datetime.datetime.strptime(
                        meeting["date"] + " " + meeting.get("time", "09:00"),
                        "%Y-%m-%d %H:%M")
                except (KeyError, ValueError):
                    continue
                diff = (when - now).total_seconds()
                key = meeting.get("title", "?") + "_" + meeting.get("date", "?")
                if 0 < diff <= 1800 and key not in alerted:
                    alerted.add(key)
                    self._say(f"Abhi! {meeting['title']} sirf "
                              f"{int(diff / 60)} minute mein hai!")
            time.sleep(55)

    # ── meetings dialog ───────────────────────────────────────────
    def _meeting_dialog(self):
        """Small dialog to add a meeting, with date/time validation."""
        win = tk.Toplevel(self.root)
        win.title("Meeting Add Karo")
        win.configure(bg="#10002a")
        win.geometry("420x340")

        def make_label(text):
            return tk.Label(win, text=text,
                            foreground="#e0b0ff", background="#10002a",
                            font=("Segoe UI", 11), anchor="w")

        def make_entry():
            return tk.Entry(win, bg="#04000a", foreground="white",
                            font=("Segoe UI", 12), relief="flat",
                            insertbackground="white")

        make_label("Meeting ka naam:").pack(fill="x", padx=20, pady=(20, 2))
        title_entry = make_entry()
        title_entry.pack(fill="x", padx=20, pady=(0, 8), ipady=6)

        make_label("Date (YYYY-MM-DD):").pack(fill="x", padx=20, pady=(0, 2))
        date_entry = make_entry()
        date_entry.insert(0, datetime.date.today().isoformat())
        date_entry.pack(fill="x", padx=20, pady=(0, 8), ipady=6)

        make_label("Time (HH:MM):").pack(fill="x", padx=20, pady=(0, 2))
        time_entry = make_entry()
        time_entry.insert(0, "09:00")
        time_entry.pack(fill="x", padx=20, pady=(0, 8), ipady=6)

        error_var = tk.StringVar(value="")
        tk.Label(win, textvariable=error_var,
                 foreground="#ff6b6b", background="#10002a",
                 font=("Segoe UI", 9)).pack(fill="x", padx=20)

        def save():
            title = title_entry.get().strip()
            date = date_entry.get().strip()
            time_ = time_entry.get().strip()
            if not title:
                error_var.set("Meeting ka naam likho!")
                return
            try:
                datetime.datetime.strptime(date + " " + time_, "%Y-%m-%d %H:%M")
            except ValueError:
                error_var.set("Date YYYY-MM-DD aur time HH:MM format mein do!")
                return
            MEM["meetings"].append({"title": title, "date": date, "time": time_})
            save_mem(MEM)
            win.destroy()
            self.append_chat("Sita", f"Meeting save kar li Abhi! {title} on {date}")
            self.speech.speak("Meeting save ho gayi!")

        tk.Button(win, text="Save Karo",
                  bg="#cc8800", foreground="black",
                  font=("Segoe UI", 12, "bold"),
                  relief="flat", pady=8,
                  cursor="hand2", command=save).pack(fill="x", padx=20, pady=(6, 0))

    # ── greeting ──────────────────────────────────────────────────
    def _greet(self):
        """Welcome message with meetings + battery, spoken once at start."""
        hour = datetime.datetime.now().hour
        salutation = ("Good morning" if hour < 12
                      else "Good afternoon" if hour < 17 else "Good evening")
        battery_info = sys_battery() if psutil else ""
        msg = (salutation + " Abhi!\n\n"
               "Main Sita hoon - SYTA ki soul. Ab main tumhara pura system "
               "control kar sakti hoon!\n\n"
               + check_meetings() + "\n" + battery_info + "\n\n"
               "Naye powers try karo:\n"
               "  'battery check karo' / 'brightness 70 karo'\n"
               "  'wifi off karo' / 'storage kitni hai'\n"
               "  'find file resume' / 'lock my laptop'\n\n"
               "Ya kabhi bhi 'Hey Sita' bolo!")
        self.append_chat("Sita", msg)
        self.speech.speak(salutation + " Abhi! Ab main tumhara pura system "
                          "control kar sakti hoon!")


def main():
    """Launch Sita."""
    prune_meetings()
    root = tk.Tk()
    SitaApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()

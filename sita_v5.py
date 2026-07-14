import tkinter as tk
from tkinter import scrolledtext
import threading, json, os, re, time, datetime, subprocess, ctypes, queue
import requests, pyttsx3, sounddevice as sd
import numpy as np, soundfile as sf, tempfile
import pyautogui, webbrowser, random, math
from faster_whisper import WhisperModel

try:
    import psutil
except:
    psutil = None
try:
    import screen_brightness_control as sbc
except:
    sbc = None

BASE        = r"C:\Sita"
MEMORY_FILE = os.path.join(BASE, "memory", "sita_memory.json")
OLLAMA_URL  = "http://localhost:11434/api/chat"
WAKE_WORD   = "hey sita"

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

def load_mem():
    # Corrupt JSON (e.g. power loss mid-save) or a file from an older
    # version missing keys must never brick startup or KeyError later.
    os.makedirs(os.path.join(BASE, "memory"), exist_ok=True)
    default = {"history": [], "goals": [], "meetings": [], "last_seen": ""}
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                default.update(data)
        except (OSError, ValueError):
            pass
    for key in ("history", "goals", "meetings"):
        if not isinstance(default.get(key), list):
            default[key] = []
    return default

def save_mem(m):
    # Atomic write: never leave a half-written memory file behind.
    tmp = MEMORY_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(m, f, indent=2, ensure_ascii=False)
    os.replace(tmp, MEMORY_FILE)

mem = load_mem()

tts = pyttsx3.init()
tts.setProperty("rate", 155)
tts.setProperty("volume", 1.0)
for v in tts.getProperty("voices"):
    if "zira" in v.name.lower() or "female" in v.name.lower():
        tts.setProperty("voice", v.id)
        break

_speaking = False
_tts_queue = queue.Queue()

def _tts_worker():
    # Single dedicated TTS thread: overlapping speak() calls used to hit
    # "run loop already started" and leave _speaking stuck True forever,
    # which silenced the wake word for the rest of the session.
    global _speaking
    while True:
        text = _tts_queue.get()
        try:
            _speaking = True
            tts.say(text)
            tts.runAndWait()
        except Exception:
            pass
        finally:
            _speaking = False
            _tts_queue.task_done()

threading.Thread(target=_tts_worker, daemon=True).start()

def speak(text):
    clean = " ".join(l for l in text.split("\n")
                    if not l.strip().startswith("ACTION:"))[:500]
    if clean.strip():
        _tts_queue.put(clean)

_whisper = None
def get_whisper():
    global _whisper
    if _whisper is None:
        _whisper = WhisperModel("base", device="cpu", compute_type="int8")
    return _whisper

def record(dur=6, sr=16000):
    a = sd.rec(int(dur*sr), samplerate=sr, channels=1, dtype="float32")
    sd.wait()
    return a.flatten()

def transcribe(audio):
    # Wake listener calls this every ~2.5s — without the finally-delete
    # it leaked thousands of orphaned WAVs into the temp folder.
    path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            path = f.name
        sf.write(path, audio, 16000)
        segs, _ = get_whisper().transcribe(path)
        # join consumes the lazy segment generator while the file exists
        return " ".join(s.text for s in segs).strip()
    except Exception:
        return ""
    finally:
        if path:
            try:
                os.remove(path)
            except OSError:
                pass

_app_ref = None
def wake_listener():
    while True:
        if _speaking:
            time.sleep(1)
            continue
        try:
            audio = sd.rec(int(2.5*16000), samplerate=16000,
                          channels=1, dtype="float32")
            sd.wait()
            text = transcribe(audio.flatten()).lower()
            if WAKE_WORD in text and _app_ref:
                _app_ref.root.after(0, _app_ref.on_wake_word)
                time.sleep(8)
        except:
            time.sleep(2)

def ask_ollama(messages):
    # Always keep the system prompt (index 0) — slicing it off made
    # Sita lose her persona and ACTION vocabulary after 5 exchanges.
    if len(messages) > 10:
        messages = [messages[0]] + messages[-9:]
    try:
        r = requests.post(OLLAMA_URL, json={
            "model": "llama3",
            "messages": messages,
            "stream": False,
            "options": {"num_predict": 600, "temperature": 0.85,
                        "num_ctx": 2048}
        }, timeout=60)
        return r.json()["message"]["content"]
    except:
        return "Abhi, thodi problem ho gayi. Ollama chal raha hai na?"

APPS = {
    "chrome":     r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "notepad":    "notepad.exe",
    "calculator": "calc.exe",
    "explorer":   "explorer.exe",
    "paint":      "mspaint.exe",
    "whatsapp":   os.path.expanduser(r"~\AppData\Local\WhatsApp\WhatsApp.exe"),
    "spotify":    os.path.expanduser(r"~\AppData\Roaming\Spotify\Spotify.exe"),
    "vs code":    os.path.expanduser(r"~\AppData\Local\Programs\Microsoft VS Code\Code.exe"),
    "youtube":    "https://www.youtube.com",
    "google":     "https://www.google.com",
    "gmail":      "https://mail.google.com",
    "linkedin":   "https://www.linkedin.com",
    "github":     "https://www.github.com",
}

PROCESS_NAMES = {
    "chrome": "chrome.exe", "notepad": "notepad.exe",
    "calculator": "CalculatorApp.exe", "spotify": "Spotify.exe",
    "whatsapp": "WhatsApp.exe", "paint": "mspaint.exe",
    "explorer": "explorer.exe", "vs code": "Code.exe",
}

# ═══════════════════════════════════════════════
# STRONG SYSTEM CONTROL
# ═══════════════════════════════════════════════
def sys_brightness(level):
    if sbc is None:
        return "Brightness library nahi hai. Chalao: py -3.11 -m pip install screen-brightness-control"
    try:
        level = max(5, min(100, int(level)))
        sbc.set_brightness(level)
        return "Brightness " + str(level) + "% kar di Abhi!"
    except Exception as e:
        return "Brightness change nahi ho payi: " + str(e)[:50]

def sys_brightness_get():
    if sbc is None:
        return None
    try:
        return sbc.get_brightness()[0]
    except:
        return None

def sys_wifi(on):
    try:
        if on:
            r = os.system('netsh interface set interface "Wi-Fi" enabled')
            if r != 0:
                return "WiFi on karne ke liye Command Prompt ko 'Run as administrator' se chalana padega Abhi!"
            return "WiFi on kar diya Abhi!"
        else:
            r = os.system("netsh wlan disconnect")
            return "WiFi disconnect kar diya Abhi!"
    except Exception as e:
        return "WiFi control nahi hua: " + str(e)[:50]

def sys_battery():
    if psutil is None:
        return "psutil install karo: py -3.11 -m pip install psutil"
    try:
        b = psutil.sensors_battery()
        if b is None:
            return "Battery info nahi mili (desktop PC hai kya?)"
        status = "charging ho rahi hai" if b.power_plugged else "battery pe chal raha hai"
        msg = "Battery " + str(int(b.percent)) + "% hai, " + status + "."
        if b.percent < 20 and not b.power_plugged:
            msg += " Abhi! Charger laga lo please!"
        return msg
    except:
        return "Battery check nahi ho payi."

def sys_storage():
    if psutil is None:
        return "psutil install karo pehle."
    try:
        d = psutil.disk_usage("C:\\")
        free_gb  = round(d.free / (1024**3), 1)
        total_gb = round(d.total / (1024**3), 1)
        pct = d.percent
        msg = ("C drive: " + str(free_gb) + " GB free hai (" +
               str(total_gb) + " GB total, " + str(pct) + "% used).")
        if pct > 90:
            msg += " Abhi storage bhar rahi hai — kuch files clean karo!"
        return msg
    except:
        return "Storage check nahi ho payi."

def sys_cpu():
    if psutil is None:
        return "psutil install karo pehle."
    try:
        cpu = psutil.cpu_percent(interval=1)
        ram = psutil.virtual_memory().percent
        return "CPU " + str(cpu) + "% aur RAM " + str(ram) + "% use ho rahi hai Abhi."
    except:
        return "CPU check nahi ho paya."

def sys_find_file(name):
    name = name.strip().lower()
    if not name:
        return "File ka naam batao Abhi!"
    search_dirs = [
        os.path.expanduser("~\\Desktop"),
        os.path.expanduser("~\\Documents"),
        os.path.expanduser("~\\Downloads"),
        r"C:\Sita",
    ]
    matches = []
    for d in search_dirs:
        if not os.path.exists(d):
            continue
        for root_, dirs, files in os.walk(d):
            for f in files:
                if name in f.lower():
                    matches.append(os.path.join(root_, f))
                    if len(matches) >= 5:
                        break
            if len(matches) >= 5:
                break
        if len(matches) >= 5:
            break
    if not matches:
        return "'" + name + "' naam ki koi file nahi mili Abhi. Desktop, Documents, Downloads mein dekha maine."
    result = "Mil gayi Abhi! " + str(len(matches)) + " file(s):\n"
    for m_ in matches:
        result += "  - " + m_ + "\n"
    try:
        subprocess.Popen('explorer /select,"' + matches[0] + '"')
        result += "Pehli wali Explorer mein khol di!"
    except:
        pass
    return result

def sys_close_app(name):
    name = name.lower().strip()
    for k, proc in PROCESS_NAMES.items():
        if k in name:
            r = os.system("taskkill /IM " + proc + " /F >nul 2>&1")
            if r == 0:
                return k.title() + " band kar diya Abhi!"
            return k.title() + " chal hi nahi raha tha."
    return "'" + name + "' pehchana nahi. Chrome, Notepad, Spotify, WhatsApp try karo."

def sys_lock():
    ctypes.windll.user32.LockWorkStation()
    return "Laptop lock kar diya Abhi!"

def sys_sleep():
    speak("Theek hai Abhi, laptop sleep mein daal rahi hoon!")
    time.sleep(2)
    os.system("rundll32.exe powrprof.dll,SetSuspendState 0,1,0")
    return "Sleep mode!"

def sys_restart():
    speak("Laptop restart kar rahi hoon Abhi. Wapas milte hain!")
    time.sleep(3)
    os.system("shutdown /r /t 5")
    return "Restart ho raha hai..."

# Actions the LLM may hallucinate that must never run without Abhi
# saying yes first.
DANGEROUS_ACTIONS = {"shutdown", "restart", "lock", "close_app"}

def action_cmd(action_str):
    return action_str.strip().split(":")[0].lower().strip()

def is_affirmative(text):
    return re.search(
        r"\b(haan|han|yes|pakka|confirm|kar do|kardo|ok|okay)\b",
        text.lower()) is not None

def do_action(action_str):
    p = action_str.strip().split(":")
    cmd = p[0].lower().strip()
    if not cmd:
        return None

    if cmd == "open_app" and len(p) >= 2:
        name = ":".join(p[1:]).lower().strip()
        for k, path in APPS.items():
            if k in name:
                try:
                    if path.startswith("http"):
                        webbrowser.open(path)
                    else:
                        subprocess.Popen(path)
                    return "Done Abhi! " + p[1] + " khol diya!"
                except:
                    pass
        pyautogui.hotkey("win")
        time.sleep(0.6)
        pyautogui.typewrite(name, interval=0.05)
        pyautogui.press("enter")
        return "Search kar diya " + p[1] + " ke liye!"
    elif cmd == "close_app" and len(p) >= 2:
        return sys_close_app(":".join(p[1:]))
    elif cmd == "tell_time":
        return "Abhi " + datetime.datetime.now().strftime("%I:%M %p") + " baj rahe hain!"
    elif cmd == "tell_date":
        return "Aaj " + datetime.datetime.now().strftime("%A, %d %B %Y") + " hai!"
    elif cmd == "screenshot":
        path = os.path.join(BASE, "ss_" + str(int(time.time())) + ".png")
        pyautogui.screenshot(path)
        return "Screenshot le liya! " + path
    elif cmd == "volume_up":
        for _ in range(5):
            pyautogui.press("volumeup")
        return "Volume badha diya!"
    elif cmd == "volume_down":
        for _ in range(5):
            pyautogui.press("volumedown")
        return "Volume kam kar diya!"
    elif cmd == "mute":
        pyautogui.press("volumemute")
        return "Mute kar diya!"
    elif cmd == "brightness" and len(p) >= 2:
        return sys_brightness(p[1])
    elif cmd == "wifi_on":
        return sys_wifi(True)
    elif cmd == "wifi_off":
        return sys_wifi(False)
    elif cmd == "battery":
        return sys_battery()
    elif cmd == "storage":
        return sys_storage()
    elif cmd == "cpu":
        return sys_cpu()
    elif cmd == "find_file" and len(p) >= 2:
        return sys_find_file(":".join(p[1:]))
    elif cmd == "lock":
        return sys_lock()
    elif cmd == "sleep":
        return sys_sleep()
    elif cmd == "restart":
        return sys_restart()
    elif cmd == "shutdown":
        speak("Theek hai Abhi, laptop band kar rahi hoon. Apna khayal rakhna!")
        time.sleep(3)
        os.system("shutdown /s /t 5")
    return None

# ═══════════════════════════════════════════════
# FAST DIRECT COMMANDS (no AI needed = instant!)
# ═══════════════════════════════════════════════
def quick_command(text):
    t = text.lower()

    def has(*patterns):
        # Whole-word matching: bare substrings made "unlock" trigger
        # lock and "program" trigger the RAM report.
        return any(re.search(r"\b" + pat + r"\b", t) for pat in patterns)

    # Brightness with number
    if has("brightness", "roshni"):
        nums = re.findall(r"\d+", t)
        if nums:
            return sys_brightness(nums[0])
        cur = sys_brightness_get()
        if has(r"bad\w*", "up", "zyada"):
            return sys_brightness((cur or 50) + 20)
        if has("kam", "down", "low"):
            return sys_brightness((cur or 50) - 20)
        return sys_brightness(70)

    if has(r"wi[\s-]?fi"):
        if has("off", "band", "disconnect"):
            return sys_wifi(False)
        if has("on", "chalu", "connect"):
            return sys_wifi(True)

    if has("battery"):
        return sys_battery()

    if has("storage", "disk", "space"):
        return sys_storage()

    if has("cpu", "ram", "performance"):
        return sys_cpu()

    if has("find", "dhundo", "dhundho", "search file") and has("file"):
        for kw in ["file ", "find ", "dhundo ", "dhundho "]:
            if kw in t:
                name = t.split(kw)[-1].strip()
                if name and name != "file":
                    return sys_find_file(name)
        return "Kaunsi file dhundhu Abhi? Naam batao!"

    if has("lock") and has("laptop", "screen", "pc"):
        return sys_lock()

    if has("sleep") and has("laptop", "pc", "mode"):
        return sys_sleep()

    if has("close", "band karo"):
        for k in PROCESS_NAMES:
            if re.search(r"\b" + re.escape(k) + r"\b", t):
                return sys_close_app(k)

    if has("screenshot", "screen shot"):
        path = os.path.join(BASE, "ss_" + str(int(time.time())) + ".png")
        pyautogui.screenshot(path)
        return "Screenshot le liya Abhi! " + path

    return None

def check_meetings():
    today = datetime.date.today().isoformat()
    tmrw  = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
    t_m   = [m for m in mem.get("meetings", []) if m.get("date") == today]
    tm_m  = [m for m in mem.get("meetings", []) if m.get("date") == tmrw]
    if not t_m and not tm_m:
        return "Aaj aur kal koi meeting nahi Abhi!"
    lines = []
    if t_m:
        lines.append("Aaj ke meetings:")
        for m in t_m:
            lines.append("  - " + m["title"] + " at " + m.get("time", "?"))
    if tm_m:
        lines.append("Kal ke meetings:")
        for m in tm_m:
            lines.append("  - " + m["title"] + " at " + m.get("time", "?"))
    return "\n".join(lines)

def meeting_reminder_loop(app):
    alerted = set()
    while True:
        now = datetime.datetime.now()
        for m in mem.get("meetings", []):
            try:
                dt   = datetime.datetime.strptime(
                    m["date"] + " " + m.get("time", "09:00"), "%Y-%m-%d %H:%M")
                diff = (dt - now).total_seconds()
                key  = m["title"] + "_" + m["date"]
                if 0 < diff <= 1800 and key not in alerted:
                    alerted.add(key)
                    msg = "Abhi! " + m["title"] + " sirf " + str(int(diff/60)) + " minute mein hai!"
                    app.root.after(0, lambda mg=msg: app._append("Sita", mg))
                    speak(msg)
            except:
                pass
        time.sleep(55)

def proactive_loop(app):
    gm = ""; ge = ""
    while True:
        now   = datetime.datetime.now()
        today = now.date().isoformat()
        if now.hour == 8 and gm != today:
            gm  = today
            msg = "Good morning Abhi! Sita yahan hai!\n" + check_meetings() + "\n" + sys_battery()
            app.root.after(0, lambda m=msg: app._append("Sita", m))
            speak("Good morning Abhi!")
        if now.hour == 19 and ge != today:
            ge  = today
            msg = "Abhi! Shaam ho gayi. Aaj kaam kaisa raha?"
            app.root.after(0, lambda m=msg: app._append("Sita", m))
            speak("Abhi shaam ho gayi!")
        if now.hour == 14 and now.minute < 2:
            msg = "Abhi! Paani piya? Aankhein rest karo thodi der!"
            app.root.after(0, lambda m=msg: app._append("Sita", m))
            speak("Abhi paani piyo!")
        time.sleep(58)


class SitaApp:
    def __init__(self, root):
        self.root = root
        self.root.title("SYTA - Sita, Abhi ki Apni AI")
        self.root.configure(bg="#0a0010")
        self.root.state("zoomed")

        self.history     = [{"role": "system", "content": SYSTEM}]
        self.pending_action = None
        self.recording   = False
        self.blink_on    = True
        self.mouth_open  = False
        self.eye_x       = 0.0
        self.eye_y       = 0.0
        self.eye_tx      = 0.0
        self.eye_ty      = 0.0
        self.listening_w = False
        self.hair_sway   = 0.0
        self.hair_dir    = 1

        self._build_ui()
        self._blink_loop()
        self._eye_smooth_loop()
        self._hair_loop()

        global _app_ref
        _app_ref = self
        threading.Thread(target=wake_listener,         daemon=True).start()
        threading.Thread(target=proactive_loop,        args=(self,), daemon=True).start()
        threading.Thread(target=meeting_reminder_loop, args=(self,), daemon=True).start()
        threading.Thread(target=self._eye_wander,      daemon=True).start()
        self.root.after(2000, self._greet)

    def _draw_face(self):
        cv = self.cv
        cv.delete("face")
        W  = int(cv.cget("width"))
        cx = W // 2
        cy = 240
        ex = self.eye_x
        ey = self.eye_y
        hs = self.hair_sway

        cv.create_oval(cx-160, cy-160, cx+160, cy+180,
                       fill="#12002a", outline="#6d28d9", width=2, tags="face")

        cv.create_rectangle(cx-20, cy+80, cx+20, cy+130,
                            fill="#f5c8a0", outline="", tags="face")
        cv.create_arc(cx-120, cy+80, cx+120, cy+260,
                      start=0, extent=180,
                      fill="#cc2266", outline="", tags="face")

        for i in range(4):
            off = i * 10
            cv.create_arc(cx-95-off+hs, cy-70, cx-55-off+hs, cy+220,
                          start=120, extent=120, style="arc",
                          outline="#140808", width=10, tags="face")
            cv.create_arc(cx+55+off+hs, cy-70, cx+95+off+hs, cy+220,
                          start=-60, extent=120, style="arc",
                          outline="#140808", width=10, tags="face")

        cv.create_oval(cx-80, cy-95, cx+80, cy+95,
                       fill="#f5c8a0", outline="#e0a878", width=2, tags="face")

        cv.create_arc(cx-82, cy-98, cx+82, cy+12,
                      start=0, extent=180, fill="#140808", outline="", tags="face")
        cv.create_arc(cx-80, cy-98, cx+15, cy+5,
                      start=30, extent=100, fill="#241010", outline="", tags="face")

        if self.blink_on:
            for exx, dr in [(cx-30, -1), (cx+30, 1)]:
                ox = ex * dr * 2
                cv.create_oval(exx-20, cy-20+ey, exx+20, cy+4+ey,
                               fill="white", outline="#bbb", width=1, tags="face")
                cv.create_oval(exx-11+ox, cy-16+ey, exx+11+ox, cy+1+ey,
                               fill="#3d2410", outline="", tags="face")
                cv.create_oval(exx-6+ox, cy-12+ey, exx+6+ox, cy-3+ey,
                               fill="#0d0808", outline="", tags="face")
                cv.create_oval(exx-4+ox, cy-11+ey, exx-1+ox, cy-8+ey,
                               fill="white", outline="", tags="face")
                for lx in range(exx-16, exx+17, 5):
                    cv.create_line(lx, cy-20+ey, lx+dr, cy-27+ey,
                                   fill="#0a0505", width=2, tags="face")
        else:
            for exx in [cx-30, cx+30]:
                cv.create_arc(exx-20, cy-20+ey, exx+20, cy+4+ey,
                              start=0, extent=180,
                              fill="#f5c8a0", outline="#c8956a", width=1, tags="face")

        cv.create_line(cx-46, cy-32, cx-14, cy-28,
                       fill="#140808", width=4, smooth=True, tags="face")
        cv.create_line(cx+14, cy-28, cx+46, cy-32,
                       fill="#140808", width=4, smooth=True, tags="face")

        cv.create_oval(cx-10, cy+8, cx-2, cy+18,
                       fill="#d4906a", outline="", tags="face")
        cv.create_oval(cx+2, cy+8, cx+10, cy+18,
                       fill="#d4906a", outline="", tags="face")
        cv.create_oval(cx-4, cy+2, cx+4, cy+12,
                       fill="#fde8d0", outline="", tags="face")

        cv.create_oval(cx-60, cy+15, cx-30, cy+38,
                       fill="#f09080", stipple="gray25", outline="", tags="face")
        cv.create_oval(cx+30, cy+15, cx+60, cy+38,
                       fill="#f09080", stipple="gray25", outline="", tags="face")

        if self.mouth_open:
            cv.create_oval(cx-20, cy+42, cx+20, cy+68,
                           fill="#8b0020", outline="", tags="face")
            cv.create_arc(cx-16, cy+42, cx+16, cy+58,
                          start=0, extent=180, fill="white", outline="", tags="face")
        else:
            cv.create_arc(cx-24, cy+36, cx+24, cy+62,
                          start=200, extent=140, style="arc",
                          outline="#c05070", width=3, tags="face")
            cv.create_oval(cx-10, cy+48, cx+10, cy+55,
                           fill="#e07090", outline="", tags="face")

        for exx, dr in [(cx-88, -1), (cx+88, 1)]:
            cv.create_arc(exx+dr*2, cy-3, exx+dr*20, cy+26,
                          start=0, extent=300, style="arc",
                          outline="#f0c840", width=3, tags="face")

        if self.listening_w:
            cv.create_oval(cx-150, cy-150, cx+150, cy+170,
                           fill="", outline="#a855f7", width=3, tags="face")

        cv.create_text(cx, cy+150, text="SYTA",
                       fill="#f0c840",
                       font=("Georgia", 20, "bold italic"), tags="face")

    def _blink_loop(self):
        self.blink_on = not self.blink_on
        self._draw_face()
        self.root.after(160 if not self.blink_on else random.randint(3000, 6000),
                        self._blink_loop)

    def _eye_wander(self):
        while True:
            self.eye_tx = random.uniform(-2.5, 2.5)
            self.eye_ty = random.uniform(-1.5, 1.5)
            time.sleep(random.uniform(1.5, 4))

    def _eye_smooth_loop(self):
        self.eye_x += (self.eye_tx - self.eye_x) * 0.1
        self.eye_y += (self.eye_ty - self.eye_y) * 0.1
        self._draw_face()
        self.root.after(40, self._eye_smooth_loop)

    def _hair_loop(self):
        self.hair_sway += self.hair_dir * 0.4
        if abs(self.hair_sway) > 5:
            self.hair_dir *= -1
        self._draw_face()
        self.root.after(80, self._hair_loop)

    def set_talking(self, val):
        self.mouth_open = val
        self._draw_face()

    def _build_ui(self):
        main = tk.Frame(self.root, bg="#0a0010")
        main.pack(fill="both", expand=True)

        left = tk.Frame(main, bg="#0a0010", width=440)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)

        self.cv = tk.Canvas(left, width=440, height=440,
                            bg="#0a0010", highlightthickness=0)
        self.cv.pack(pady=(15, 0))

        self.wake_var = tk.StringVar(value="'Hey Sita' bolte raho...")
        tk.Label(left, textvariable=self.wake_var,
                 foreground="#a855f7", background="#0a0010",
                 font=("Segoe UI", 10, "italic")).pack()

        self.status_var = tk.StringVar(value="SYTA ready hai Abhi ke liye!")
        tk.Label(left, textvariable=self.status_var,
                 foreground="#f0c840", background="#0a0010",
                 font=("Segoe UI", 11)).pack(pady=(2, 8))

        qa = tk.Frame(left, bg="#0a0010")
        qa.pack(fill="x", padx=15)
        btns = [
            ("Battery",      "battery check karo"),
            ("Storage",      "storage kitni bachi hai"),
            ("CPU / RAM",    "cpu aur ram check karo"),
            ("Brightness 50","brightness 50 karo"),
            ("WiFi Off",     "wifi off karo"),
            ("Lock PC",      "lock my laptop screen"),
            ("Meetings",     "What meetings do I have?"),
            ("Screenshot",   "screenshot le lo"),
            ("Teach Me",     "I want to learn something new"),
            ("Motivate",     "Motivate me Sita!"),
        ]
        for i, (label, prompt) in enumerate(btns):
            tk.Button(qa, text=label,
                      bg="#150030", foreground="#e0b0ff",
                      font=("Segoe UI", 9), relief="flat",
                      padx=4, pady=6, cursor="hand2",
                      command=lambda p=prompt: self._quick(p)
                      ).grid(row=i//2, column=i%2, padx=3, pady=2, sticky="ew")
        qa.columnconfigure(0, weight=1)
        qa.columnconfigure(1, weight=1)

        tk.Button(left, text="+ Meeting Add Karo",
                  bg="#2d0050", foreground="#f0c840",
                  font=("Segoe UI", 10), relief="flat", pady=6,
                  cursor="hand2", command=self._meeting_dialog
                  ).pack(fill="x", padx=15, pady=(8, 0))

        right = tk.Frame(main, bg="#06000f")
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

        inp = tk.Frame(right, bg="#10002a", pady=10)
        inp.pack(fill="x")
        self.entry = tk.Entry(inp,
                              bg="#150030", foreground="white",
                              font=("Segoe UI", 13), relief="flat",
                              insertbackground="white")
        self.entry.pack(side="left", fill="x", expand=True,
                        ipady=12, padx=(12, 8))
        self.entry.bind("<Return>", lambda e: self._send())

        self.mic_btn = tk.Button(inp, text="MIC",
                                 bg="#6600cc", foreground="white",
                                 font=("Segoe UI", 11, "bold"),
                                 relief="flat", width=5,
                                 cursor="hand2", command=self._toggle_mic)
        self.mic_btn.pack(side="left", ipady=10, padx=(0, 6))

        tk.Button(inp, text="SEND",
                  bg="#cc8800", foreground="black",
                  font=("Segoe UI", 11, "bold"),
                  relief="flat", width=6,
                  cursor="hand2", command=self._send
                  ).pack(side="left", ipady=10, padx=(0, 12))

    def ui(self, fn, *args, **kwargs):
        # Tkinter is not thread-safe: _reply/_listen/_wake_listen run on
        # worker threads, so every widget touch must hop to the main
        # thread via root.after.
        self.root.after(0, lambda: fn(*args, **kwargs))

    def _append(self, who, text):
        self.chat.config(state="normal")
        t = datetime.datetime.now().strftime("%H:%M")
        self.chat.insert("end", "\n")
        self.chat.insert("end",
                         "Sita  " if who == "Sita" else "Abhi  ",
                         "sita" if who == "Sita" else "you")
        self.chat.insert("end", "[" + t + "]\n", "ts")
        clean = "\n".join(l for l in text.split("\n")
                          if not l.strip().startswith("ACTION:"))
        self.chat.insert("end", clean.strip() + "\n", "body")
        self.chat.config(state="disabled")
        self.chat.see("end")

    def _reply(self, user_text):
        self.ui(self.status_var.set, "Sita soch rahi hai...")

        # STEP 0 — Confirmation gate: a dangerous ACTION suggested by
        # the LLM runs only after Abhi explicitly says haan.
        if self.pending_action:
            pending, self.pending_action = self.pending_action, None
            if is_affirmative(user_text):
                res = do_action(pending) or "Ho gaya Abhi!"
                self.ui(self._append, "Sita", res)
                speak(res)
                self.ui(self.status_var.set, "SYTA ready hai Abhi ke liye!")
                return
            # anything else cancels; message continues normally below

        # STEP 1 — Try instant system command (no AI = super fast!)
        quick = quick_command(user_text)
        if quick:
            self.ui(self._append, "Sita", quick)
            self.ui(self.set_talking, True)
            speak(quick)
            self.root.after(max(2000, len(quick.split()) * 100),
                            lambda: self.set_talking(False))
            self.ui(self.status_var.set, "SYTA ready hai Abhi ke liye!")
            return

        # STEP 2 — Otherwise ask AI
        self.history.append({"role": "user", "content": user_text})
        msgs = self.history.copy()
        if mem.get("goals"):
            msgs[0] = {"role": "system",
                       "content": SYSTEM + "\nAbhi goals: " + ", ".join(mem["goals"][-4:])}
        reply = ask_ollama(msgs)
        self.history.append({"role": "assistant", "content": reply})

        if any(w in user_text.lower() for w in
               ["want", "goal", "dream", "plan", "career", "study", "learn"]):
            mem["goals"].append(user_text[:120])
        mem["history"].append({
            "t": datetime.datetime.now().isoformat(),
            "u": user_text, "s": reply[:200]
        })
        save_mem(mem)

        extras = []
        for line in reply.split("\n"):
            if "ACTION:" in line:
                action_str = line.split("ACTION:")[-1]
                if action_cmd(action_str) in DANGEROUS_ACTIONS:
                    self.pending_action = action_str
                    extras.append("Yeh kaam (" + action_cmd(action_str) +
                                  ") pakka karun Abhi? 'haan' bolo to kar dungi!")
                    continue
                res = do_action(action_str)
                if res:
                    extras.append(res)
        final = ("\n".join(extras) + "\n" + reply).strip() if extras else reply
        self.ui(self._append, "Sita", final)
        self.ui(self.set_talking, True)
        speak(final)
        self.root.after(max(2500, len(final.split()) * 100),
                        lambda: self.set_talking(False))
        self.ui(self.status_var.set, "SYTA ready hai Abhi ke liye!")

    def _send(self):
        txt = self.entry.get().strip()
        if not txt:
            return
        self.entry.delete(0, "end")
        self._append("Abhi", txt)
        threading.Thread(target=self._reply, args=(txt,), daemon=True).start()

    def _quick(self, prompt):
        self._append("Abhi", prompt)
        threading.Thread(target=self._reply, args=(prompt,), daemon=True).start()

    def _toggle_mic(self):
        if self.recording:
            return
        self.recording = True
        self.mic_btn.config(bg="red", text="STOP")
        self.status_var.set("Bol Abhi... (6 sec)")
        threading.Thread(target=self._listen, daemon=True).start()

    def _listen(self):
        audio = record(dur=6)
        self.ui(self.status_var.set, "Sun li - samajh rahi hoon...")
        text = transcribe(audio)
        self.recording = False
        self.ui(self.mic_btn.config, bg="#6600cc", text="MIC")
        if text:
            self.ui(self._append, "Abhi", "MIC: " + text)
            self._reply(text)
        else:
            self.ui(self.status_var.set, "SYTA ready hai Abhi ke liye!")

    def on_wake_word(self):
        self.listening_w = True
        self._draw_face()
        self.status_var.set("Haan Abhi! Bol!")
        speak("Haan Abhi! Bol!")
        time.sleep(0.9)
        threading.Thread(target=self._wake_listen, daemon=True).start()

    def _wake_listen(self):
        audio = record(dur=6)
        text  = transcribe(audio)
        self.listening_w = False
        self.ui(self._draw_face)
        if text:
            self.ui(self._append, "Abhi", "MIC: " + text)
            self._reply(text)
        else:
            self.ui(self.status_var.set, "SYTA ready hai Abhi ke liye!")

    def _meeting_dialog(self):
        win = tk.Toplevel(self.root)
        win.title("Meeting Add Karo")
        win.configure(bg="#10002a")
        win.geometry("420x300")

        def lbl(t):
            return tk.Label(win, text=t,
                            foreground="#e0b0ff", background="#10002a",
                            font=("Segoe UI", 11), anchor="w")

        def ent():
            return tk.Entry(win, bg="#04000a", foreground="white",
                            font=("Segoe UI", 12), relief="flat",
                            insertbackground="white")

        lbl("Meeting ka naam:").pack(fill="x", padx=20, pady=(20, 2))
        t = ent(); t.pack(fill="x", padx=20, pady=(0, 8), ipady=6)
        lbl("Date (YYYY-MM-DD):").pack(fill="x", padx=20, pady=(0, 2))
        d = ent(); d.insert(0, datetime.date.today().isoformat())
        d.pack(fill="x", padx=20, pady=(0, 8), ipady=6)
        lbl("Time (HH:MM):").pack(fill="x", padx=20, pady=(0, 2))
        ti = ent(); ti.insert(0, "09:00")
        ti.pack(fill="x", padx=20, pady=(0, 12), ipady=6)

        def save():
            title = t.get().strip()
            date  = d.get().strip()
            time_ = ti.get().strip()
            if not title or not date:
                return
            mem["meetings"].append({"title": title, "date": date, "time": time_})
            save_mem(mem)
            win.destroy()
            msg = "Meeting save kar li Abhi! " + title + " on " + date
            self._append("Sita", msg)
            speak("Meeting save ho gayi!")

        tk.Button(win, text="Save Karo",
                  bg="#cc8800", foreground="black",
                  font=("Segoe UI", 12, "bold"),
                  relief="flat", pady=8,
                  cursor="hand2", command=save).pack(fill="x", padx=20)

    def _greet(self):
        h   = datetime.datetime.now().hour
        sal = "Good morning" if h < 12 else "Good afternoon" if h < 17 else "Good evening"
        battery_info = sys_battery() if psutil else ""
        msg = (sal + " Abhi!\n\n"
               "Main Sita hoon - SYTA ki soul. Ab main tumhara pura system control kar sakti hoon!\n\n"
               + check_meetings() + "\n" + battery_info + "\n\n"
               "Naye powers try karo:\n"
               "  'battery check karo' / 'brightness 70 karo'\n"
               "  'wifi off karo' / 'storage kitni hai'\n"
               "  'find file resume' / 'lock my laptop'\n\n"
               "Ya kabhi bhi 'Hey Sita' bolo!")
        self._append("Sita", msg)
        self.set_talking(True)
        speak(sal + " Abhi! Ab main tumhara pura system control kar sakti hoon!")
        self.root.after(5500, lambda: self.set_talking(False))
        self.status_var.set("SYTA ready hai Abhi ke liye!")


if __name__ == "__main__":
    root = tk.Tk()
    app  = SitaApp(root)
    root.mainloop()
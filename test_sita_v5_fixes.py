"""Regression tests for the critical-bug fixes applied to sita_v5.py.

Run with:  py -3.11 -m pytest C:\\Sita\\test_sita_v5_fixes.py -v

Every handler that touches real system state (lock, apps, battery, TTS,
mic, Ollama) is monkeypatched — these tests never launch anything.
"""
import os
import time

import sita_v5 as sita


# ═══ Fix 1 — do_action parses p[0] as the command ═══════════════════

def test_tell_time_single_token_action():
    """v5 returned None for any single-token action like tell_time."""
    res = sita.do_action("tell_time")
    assert res is not None and "baj" in res


def test_tell_date_single_token_action():
    res = sita.do_action("tell_date")
    assert res is not None and "Aaj" in res


def test_open_app_launches_known_app(monkeypatch):
    """ACTION:open_app:Chrome must route to the Chrome path (was dead in v5)."""
    launched = []

    class FakeSubprocess:
        @staticmethod
        def Popen(path):
            launched.append(path)

    monkeypatch.setattr(sita, "subprocess", FakeSubprocess)
    res = sita.do_action("open_app:Chrome")
    assert launched == [sita.APPS["chrome"]]
    assert "Chrome" in res


def test_open_app_website(monkeypatch):
    opened = []

    class FakeWeb:
        @staticmethod
        def open(url):
            opened.append(url)

    monkeypatch.setattr(sita, "webbrowser", FakeWeb)
    res = sita.do_action("open_app:YouTube")
    assert opened == [sita.APPS["youtube"]]
    assert res is not None


def test_close_app_arg_passed_through(monkeypatch):
    got = []
    monkeypatch.setattr(sita, "sys_close_app", lambda n: got.append(n) or "closed")
    assert sita.do_action("close_app:notepad") == "closed"
    assert got == ["notepad"]


def test_battery_routes_to_handler(monkeypatch):
    monkeypatch.setattr(sita, "sys_battery", lambda: "BATT-OK")
    assert sita.do_action("battery") == "BATT-OK"


def test_brightness_arg(monkeypatch):
    got = []
    monkeypatch.setattr(sita, "sys_brightness", lambda v: got.append(v) or "bright")
    assert sita.do_action("brightness:70") == "bright"
    assert got == ["70"]


def test_find_file_arg_with_colon(monkeypatch):
    got = []
    monkeypatch.setattr(sita, "sys_find_file", lambda n: got.append(n) or "found")
    assert sita.do_action("find_file:notes:v2") == "found"
    assert got == ["notes:v2"]


def test_lock_routes_to_handler(monkeypatch):
    monkeypatch.setattr(sita, "sys_lock", lambda: "LOCKED-TEST")
    assert sita.do_action("lock") == "LOCKED-TEST"


def test_empty_and_unknown_actions_return_none():
    assert sita.do_action("") is None
    assert sita.do_action("   ") is None
    assert sita.do_action("no_such_command") is None


# ═══ Fix 2 — system prompt survives history trimming ════════════════

class _FakeResponse:
    def json(self):
        return {"message": {"content": "ok"}}


def _capture_post(captured):
    def post(url, json=None, timeout=None):
        captured.append(json["messages"])
        return _FakeResponse()
    return post


def test_system_prompt_kept_after_long_history(monkeypatch):
    """v5 sent messages[-10:], slicing off the system prompt at index 0."""
    captured = []
    monkeypatch.setattr(sita.requests, "post", _capture_post(captured))
    msgs = [{"role": "system", "content": "SYS"}]
    msgs += [{"role": "user", "content": str(i)} for i in range(30)]
    assert sita.ask_ollama(msgs) == "ok"
    sent = captured[0]
    assert sent[0] == {"role": "system", "content": "SYS"}
    assert len(sent) == 10
    assert sent[-1]["content"] == "29"


def test_short_history_sent_unchanged(monkeypatch):
    captured = []
    monkeypatch.setattr(sita.requests, "post", _capture_post(captured))
    msgs = [{"role": "system", "content": "SYS"},
            {"role": "user", "content": "hi"}]
    sita.ask_ollama(msgs)
    assert captured[0] == msgs


# ═══ Fix 3 — TTS queue, _speaking always resets ═════════════════════

def _drain_tts(timeout=5.0):
    deadline = time.time() + timeout
    while sita._tts_queue.unfinished_tasks and time.time() < deadline:
        time.sleep(0.02)
    assert sita._tts_queue.unfinished_tasks == 0, "TTS worker stalled"


class _FakeTTS:
    def __init__(self, fail=False):
        self.spoken = []
        self.fail = fail

    def say(self, text):
        if self.fail:
            raise RuntimeError("run loop already started")
        self.spoken.append(text)

    def runAndWait(self):
        pass


def test_speak_goes_through_queue(monkeypatch):
    fake = _FakeTTS()
    monkeypatch.setattr(sita, "tts", fake)
    sita.speak("hello Abhi")
    _drain_tts()
    assert fake.spoken == ["hello Abhi"]
    assert sita._speaking is False


def test_speaking_resets_even_when_tts_raises(monkeypatch):
    """The v5 killer: a crashed speak() left _speaking True forever,
    so wake_listener never listened again."""
    fake = _FakeTTS(fail=True)
    monkeypatch.setattr(sita, "tts", fake)
    sita.speak("this will crash")
    _drain_tts()
    assert sita._speaking is False


def test_overlapping_speak_calls_serialize(monkeypatch):
    fake = _FakeTTS()
    monkeypatch.setattr(sita, "tts", fake)
    for i in range(5):
        sita.speak("line " + str(i))
    _drain_tts()
    assert fake.spoken == ["line " + str(i) for i in range(5)]
    assert sita._speaking is False


def test_speak_strips_action_lines(monkeypatch):
    fake = _FakeTTS()
    monkeypatch.setattr(sita, "tts", fake)
    sita.speak("ACTION:lock\nbaat sun Abhi")
    sita.speak("ACTION:shutdown")          # nothing speakable — not queued
    _drain_tts()
    assert fake.spoken == ["baat sun Abhi"]


# ═══ Fix 4 — all Tk updates hop to the main thread via root.after ═══

class _FakeRoot:
    """Records after() calls and runs zero-delay callbacks immediately."""
    def __init__(self):
        self.after_calls = []

    def after(self, delay, fn=None):
        self.after_calls.append(delay)
        if fn is not None and delay == 0:
            fn()


def _bare_app():
    app = object.__new__(sita.SitaApp)
    app.root = _FakeRoot()
    return app


def test_ui_helper_routes_through_root_after():
    app = _bare_app()
    hits = []
    app.ui(lambda a, b=None: hits.append((a, b)), 1, b=2)
    assert hits == [(1, 2)]
    assert app.root.after_calls == [0]


def test_threaded_methods_do_not_touch_widgets_directly():
    """_reply/_listen/_wake_listen run on worker threads; any direct
    widget call there is the random-crash bug from the review."""
    import inspect
    import re as _re
    for meth in (sita.SitaApp._reply, sita.SitaApp._listen,
                 sita.SitaApp._wake_listen):
        src = inspect.getsource(meth)
        # calls wrapped in a lambda handed to root.after already run on
        # the main thread — only bare calls are violations
        src = _re.sub(r"lambda[^\n]*", "", src)
        assert "self.ui(" in src, meth.__name__
        assert "self.status_var.set(" not in src, meth.__name__
        assert "self._append(" not in src, meth.__name__
        assert "self.mic_btn.config(" not in src, meth.__name__
        assert "self.set_talking(" not in src, meth.__name__
        assert "self._draw_face()" not in src, meth.__name__


# ═══ Fix 5 — word-boundary matching in quick_command ════════════════

def _patch_all_handlers(monkeypatch):
    """Replace every system handler with a harmless sentinel."""
    monkeypatch.setattr(sita, "sys_lock", lambda: "LOCKED")
    monkeypatch.setattr(sita, "sys_sleep", lambda: "SLEPT")
    monkeypatch.setattr(sita, "sys_cpu", lambda: "CPU")
    monkeypatch.setattr(sita, "sys_battery", lambda: "BATT")
    monkeypatch.setattr(sita, "sys_storage", lambda: "DISK")
    monkeypatch.setattr(sita, "sys_wifi", lambda on: "WIFI-ON" if on else "WIFI-OFF")
    monkeypatch.setattr(sita, "sys_brightness", lambda v: "BRIGHT:" + str(v))
    monkeypatch.setattr(sita, "sys_brightness_get", lambda: 50)
    monkeypatch.setattr(sita, "sys_close_app", lambda n: "CLOSE:" + n)
    monkeypatch.setattr(sita, "sys_find_file", lambda n: "FIND:" + n)


def test_unlock_does_not_lock_the_pc(monkeypatch):
    """The review headline: 'unlock my screen' locked the PC."""
    _patch_all_handlers(monkeypatch)
    assert sita.quick_command("unlock my screen please") is None


def test_lock_still_locks(monkeypatch):
    _patch_all_handlers(monkeypatch)
    assert sita.quick_command("lock my laptop screen") == "LOCKED"


def test_program_is_not_ram(monkeypatch):
    _patch_all_handlers(monkeypatch)
    assert sita.quick_command("sita mujhe ek program likhna sikhao") is None


def test_cpu_ram_command_still_works(monkeypatch):
    _patch_all_handlers(monkeypatch)
    assert sita.quick_command("cpu aur ram check karo") == "CPU"


def test_battery_command(monkeypatch):
    _patch_all_handlers(monkeypatch)
    assert sita.quick_command("battery check karo") == "BATT"


def test_wifi_off_and_on(monkeypatch):
    _patch_all_handlers(monkeypatch)
    assert sita.quick_command("wifi off karo") == "WIFI-OFF"
    assert sita.quick_command("wi-fi on karo") == "WIFI-ON"


def test_brightness_with_number(monkeypatch):
    _patch_all_handlers(monkeypatch)
    assert sita.quick_command("brightness 50 karo") == "BRIGHT:50"


def test_brightness_up_hinglish(monkeypatch):
    _patch_all_handlers(monkeypatch)
    assert sita.quick_command("brightness badha do") == "BRIGHT:70"


def test_close_app_word_bounded(monkeypatch):
    _patch_all_handlers(monkeypatch)
    assert sita.quick_command("close chrome") == "CLOSE:chrome"


def test_find_file(monkeypatch):
    _patch_all_handlers(monkeypatch)
    assert sita.quick_command("find file resume") == "FIND:resume"


def test_plain_chat_falls_through_to_llm(monkeypatch):
    _patch_all_handlers(monkeypatch)
    assert sita.quick_command("aaj mera din accha tha") is None


# ═══ Fix 6 — confirmation gate for dangerous LLM actions ════════════

def test_dangerous_action_set_is_correct():
    assert sita.DANGEROUS_ACTIONS == {"shutdown", "restart", "lock", "close_app"}


def test_action_cmd_helper():
    assert sita.action_cmd("shutdown") == "shutdown"
    assert sita.action_cmd(" close_app:chrome ") == "close_app"
    assert sita.action_cmd("tell_time") == "tell_time"


def test_is_affirmative():
    assert sita.is_affirmative("haan pakka kar do")
    assert sita.is_affirmative("Yes")
    assert not sita.is_affirmative("nahi rehne do")
    assert not sita.is_affirmative("yesterday was fun")   # no bare-substring 'yes'
    assert not sita.is_affirmative("khan sahab aaye the") # no bare-substring 'han'


def _gate_app(monkeypatch, llm_reply):
    """Bare SitaApp wired for _reply flow tests — no real Tk, TTS,
    Ollama, or system calls anywhere."""
    import types
    app = _bare_app()
    app.pending_action = None
    app.history = [{"role": "system", "content": "S"}]
    app.status_var = types.SimpleNamespace(set=lambda s: None)
    app.transcript = []
    app._append = lambda who, txt: app.transcript.append((who, txt))
    app.set_talking = lambda v: None

    executed = []
    monkeypatch.setattr(sita, "quick_command", lambda t: None)
    monkeypatch.setattr(sita, "ask_ollama", lambda m: llm_reply)
    monkeypatch.setattr(sita, "speak", lambda t: None)
    monkeypatch.setattr(sita, "save_mem", lambda m: None)
    monkeypatch.setattr(sita, "mem", {"history": [], "goals": [],
                                      "meetings": [], "last_seen": ""})
    monkeypatch.setattr(sita, "do_action",
                        lambda a: executed.append(a) or "DONE:" + a)
    return app, executed


def test_llm_shutdown_is_held_for_confirmation(monkeypatch):
    app, executed = _gate_app(monkeypatch, "Theek hai Abhi!\nACTION:shutdown")
    app._reply("pc band kar do")
    assert executed == []                      # nothing ran
    assert app.pending_action == "shutdown"    # held instead
    assert any("pakka" in txt for _, txt in app.transcript)


def test_confirmed_pending_action_executes(monkeypatch):
    app, executed = _gate_app(monkeypatch, "should not be asked")
    app.pending_action = "shutdown"
    app._reply("haan pakka")
    assert executed == ["shutdown"]
    assert app.pending_action is None
    assert ("Sita", "DONE:shutdown") in app.transcript


def test_declined_pending_action_is_dropped(monkeypatch):
    app, executed = _gate_app(monkeypatch, "Koi baat nahi Abhi!")
    app.pending_action = "restart"
    app._reply("nahi rehne do")
    assert executed == []
    assert app.pending_action is None


def test_safe_llm_action_runs_without_gate(monkeypatch):
    app, executed = _gate_app(monkeypatch, "Abhi time ye raha!\nACTION:tell_time")
    app._reply("time batao")
    assert executed == ["tell_time"]
    assert app.pending_action is None


# ═══ Fix 7 — transcribe deletes its temp WAV ════════════════════════

class _FakeSeg:
    def __init__(self, text):
        self.text = text


class _FakeWhisper:
    def __init__(self):
        self.paths = []

    def transcribe(self, path):
        self.paths.append(path)
        assert os.path.exists(path), "WAV must exist while whisper reads it"
        return iter([_FakeSeg("hello"), _FakeSeg("abhi")]), None


def test_transcribe_deletes_temp_wav(monkeypatch):
    import numpy as np
    fake = _FakeWhisper()
    monkeypatch.setattr(sita, "get_whisper", lambda: fake)
    out = sita.transcribe(np.zeros(16000, dtype="float32"))
    assert out == "hello abhi"
    assert len(fake.paths) == 1
    assert not os.path.exists(fake.paths[0]), "temp WAV leaked"


def test_transcribe_deletes_temp_wav_even_on_error(monkeypatch):
    import numpy as np

    class _Boom:
        def __init__(self):
            self.paths = []

        def transcribe(self, path):
            self.paths.append(path)
            raise RuntimeError("whisper exploded")

    fake = _Boom()
    monkeypatch.setattr(sita, "get_whisper", lambda: fake)
    out = sita.transcribe(np.zeros(16000, dtype="float32"))
    assert out == ""
    assert not os.path.exists(fake.paths[0]), "temp WAV leaked on error"


# ═══ Fix 8 — defensive memory load, atomic save ═════════════════════

DEFAULT_KEYS = {"history", "goals", "meetings", "last_seen"}


def _use_mem_file(monkeypatch, tmp_path, content=None):
    memfile = tmp_path / "sita_memory.json"
    if content is not None:
        memfile.write_text(content, encoding="utf-8")
    monkeypatch.setattr(sita, "MEMORY_FILE", str(memfile))
    return memfile


def test_load_mem_corrupt_json_returns_defaults(monkeypatch, tmp_path):
    """v5 crashed at startup on a half-written memory file."""
    _use_mem_file(monkeypatch, tmp_path, '{"history": [1,2,')
    m = sita.load_mem()
    assert DEFAULT_KEYS <= set(m)
    assert m["history"] == [] and m["goals"] == []


def test_load_mem_missing_keys_are_merged(monkeypatch, tmp_path):
    """Old memory file without 'goals' caused KeyError on append."""
    _use_mem_file(monkeypatch, tmp_path, '{"history": [{"u": "hi"}]}')
    m = sita.load_mem()
    assert m["history"] == [{"u": "hi"}]
    assert m["goals"] == [] and m["meetings"] == []
    m["goals"].append("no KeyError")


def test_load_mem_wrong_types_reset(monkeypatch, tmp_path):
    _use_mem_file(monkeypatch, tmp_path, '{"goals": null, "meetings": "x"}')
    m = sita.load_mem()
    assert m["goals"] == [] and m["meetings"] == []


def test_load_mem_no_file_returns_defaults(monkeypatch, tmp_path):
    _use_mem_file(monkeypatch, tmp_path)
    m = sita.load_mem()
    assert DEFAULT_KEYS <= set(m)


def test_save_mem_round_trip_and_no_tmp_left(monkeypatch, tmp_path):
    memfile = _use_mem_file(monkeypatch, tmp_path)
    data = {"history": [], "goals": ["GATE 2027"], "meetings": [],
            "last_seen": "2026-07-14"}
    sita.save_mem(data)
    assert sita.load_mem() == data
    assert not os.path.exists(str(memfile) + ".tmp")

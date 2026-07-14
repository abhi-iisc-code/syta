"""Unit tests for the pure logic in sita_v6.

Run with:  py -3.11 -m pytest C:\\Sita\\test_sita_v6.py -v
Only pure functions are tested - nothing here touches the mic, TTS,
Ollama or Windows system state.
"""
import sita_v6 as sita


# ── ACTION parsing (the v5 killer bug) ────────────────────────────
def test_parse_action_with_prose_prefix():
    """LLM output usually has prose before the ACTION line."""
    assert sita.parse_action("Sure Abhi! ACTION:open_app:Chrome") == ("open_app", "Chrome")


def test_parse_action_no_arg():
    """Single-part actions like tell_time must parse too (v5 returned None)."""
    assert sita.parse_action("ACTION:tell_time") == ("tell_time", "")


def test_parse_action_arg_with_colon():
    """Arguments containing ':' survive parsing."""
    assert sita.parse_action("ACTION:find_file:notes:v2") == ("find_file", "notes:v2")


def test_parse_action_without_prefix_is_not_a_command():
    """A line with no ACTION: prefix is prose, not a command -> None."""
    assert sita.parse_action("no action here") is None
    assert sita.parse_action("just chatting with Abhi") is None
    assert sita.parse_action("ACTION:") is None


def test_parse_action_known_commands_are_registered():
    """Every command advertised in the system prompt has a handler."""
    for cmd in ["open_app", "close_app", "tell_time", "tell_date", "screenshot",
                "volume_up", "volume_down", "mute", "brightness", "wifi_on",
                "wifi_off", "battery", "storage", "cpu", "find_file",
                "lock", "sleep", "restart", "shutdown"]:
        assert cmd in sita.ACTIONS


# ── system prompt retention (v5 dropped it after 10 messages) ─────
def test_trim_messages_keeps_system_prompt():
    msgs = [{"role": "system", "content": "S"}]
    msgs += [{"role": "user", "content": str(i)} for i in range(30)]
    trimmed = sita.trim_messages(msgs)
    assert trimmed[0]["role"] == "system"
    assert len(trimmed) == 10
    assert trimmed[-1]["content"] == "29"


def test_trim_messages_short_history_unchanged():
    msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "hi"}]
    assert sita.trim_messages(msgs) == msgs


# ── quick-intent false positives fixed in v6 ──────────────────────
def test_unlock_does_not_lock_the_pc():
    assert sita.match_quick_intent("unlock my screen please") is None


def test_program_is_not_ram():
    assert sita.match_quick_intent("sita mujhe ek program likhna sikhao") is None


def test_space_question_goes_to_llm():
    assert sita.match_quick_intent("i want to learn about space") is None


def test_battery_science_question_goes_to_llm():
    assert sita.match_quick_intent(
        "can you explain how a lithium ion battery degrades over many years") is None


# ── quick intents that SHOULD match ───────────────────────────────
def test_battery_command():
    assert sita.match_quick_intent("battery check karo") == ("battery", "")


def test_brightness_with_number():
    assert sita.match_quick_intent("brightness 50 karo") == ("brightness_set", "50")


def test_brightness_up():
    assert sita.match_quick_intent("brightness thodi up karo") == ("brightness_up", "")


def test_lock_command():
    assert sita.match_quick_intent("lock my laptop screen") == ("lock", "")


def test_wifi_off():
    assert sita.match_quick_intent("wifi off karo") == ("wifi_off", "")


def test_storage_command():
    assert sita.match_quick_intent("storage kitni bachi hai") == ("storage", "")


def test_find_file_extracts_name():
    intent, arg = sita.match_quick_intent("find file resume")
    assert intent == "find_file"
    assert arg == "resume"


def test_find_file_hinglish_order():
    intent, arg = sita.match_quick_intent("resume file dhundo")
    assert intent == "find_file"
    assert arg == "resume"


def test_close_app():
    assert sita.match_quick_intent("close chrome please") == ("close_app", "chrome")


def test_screenshot():
    assert sita.match_quick_intent("screenshot le lo") == ("screenshot", "")


# ── goal capture (v5 saved negations as goals) ────────────────────
def test_negation_is_not_a_goal():
    assert not sita.looks_like_goal("i don't want to study today")


def test_real_goal_is_captured():
    assert sita.looks_like_goal("i want to become a software engineer")


def test_random_sentence_is_not_a_goal():
    assert not sita.looks_like_goal("what is the weather today")


# ── memory safety ─────────────────────────────────────────────────
def test_default_memory_has_all_keys():
    mem = sita.load_mem()
    for key in ("history", "goals", "meetings", "last_seen"):
        assert key in mem


def test_explorer_is_protected():
    result = sita.sys_close_app("close explorer")
    assert "risky" in result.lower() or "nahi" in result.lower()

"""Get text into whatever window has focus, without destroying the clipboard.

Pasting is the only insertion method fast enough to feel instant, but it means
borrowing the user's clipboard. Borrowing it correctly is fiddly: restore too
early and the target app pastes the *restored* value; restore too late and the
user's own Ctrl+V picks up the transcript. Both are real, reported bugs, so the
timing here is deliberate and configurable.

The other rule this module enforces: the words are never lost. If the paste
cannot land, the transcript stays on the clipboard and is retrievable from the
last-transcript buffer.
"""

import logging
import re
import threading
import time

logger = logging.getLogger(__name__)

# How long to let the target app read the clipboard before putting the user's
# own contents back.
DEFAULT_RESTORE_DELAY = 0.25

_last_transcript = ""
_last_lock = threading.Lock()

# Trailing voice commands that fire a keystroke after the text is inserted.
# Recognised only at the very end of a dictation — mid-sentence they are words.
_TRAILING_ACTIONS = [
    (re.compile(r"[\s,.]*\b(?:press|hit|push)\s+enter\b[\s.!?]*$", re.IGNORECASE), "enter"),
    (re.compile(r"[\s,.]*\b(?:press|hit|push)\s+return\b[\s.!?]*$", re.IGNORECASE), "enter"),
    (re.compile(r"[\s,.]*\b(?:press|hit|push)\s+tab\b[\s.!?]*$", re.IGNORECASE), "tab"),
]


def _pyperclip():
    import pyperclip
    return pyperclip


def _pyautogui():
    import pyautogui
    pyautogui.FAILSAFE = False   # a corner-of-screen mouse must not abort a paste
    return pyautogui


# ---------------------------------------------------------------------------
# Last transcript buffer
# ---------------------------------------------------------------------------
def set_last_transcript(text):
    global _last_transcript
    with _last_lock:
        _last_transcript = text or ""


def get_last_transcript():
    with _last_lock:
        return _last_transcript


# ---------------------------------------------------------------------------
# Trailing voice actions
# ---------------------------------------------------------------------------
def extract_trailing_action(text):
    """Split a trailing keystroke command off the end of a transcript.

    Returns (text_without_command, action_or_None). "Sounds good, press enter"
    yields ("Sounds good", "enter"); "the period between 1990 and 2000" is
    untouched because the phrase is not at the end.
    """
    if not text:
        return text, None
    for pattern, action in _TRAILING_ACTIONS:
        stripped = pattern.sub("", text)
        if stripped != text:
            return stripped.rstrip(), action
    return text, None


# ---------------------------------------------------------------------------
# Clipboard
# ---------------------------------------------------------------------------
def read_clipboard():
    try:
        return _pyperclip().paste() or ""
    except Exception as e:  # noqa: BLE001 — clipboard access fails in many ways
        logger.debug("Could not read clipboard: %s", e)
        return ""


def write_clipboard(text):
    try:
        _pyperclip().copy(text)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not write clipboard: %s", e)
        return False


def read_selection(copy_delay=0.12):
    """Read the currently selected text via a clipboard round-trip.

    Used by command mode. The user's clipboard is restored before returning, so
    invoking command mode with nothing selected costs them nothing.
    """
    saved = read_clipboard()
    sentinel = "\x00__local_dictation_probe__\x00"
    write_clipboard(sentinel)

    try:
        _pyautogui().hotkey("ctrl", "c")
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not send copy keystroke: %s", e)
        write_clipboard(saved)
        return ""

    time.sleep(copy_delay)
    selection = read_clipboard()
    write_clipboard(saved)

    # An unchanged sentinel means the copy produced nothing — no selection.
    if selection == sentinel:
        return ""
    return selection or ""


# ---------------------------------------------------------------------------
# Insertion
# ---------------------------------------------------------------------------
def paste_text(text, restore_delay=DEFAULT_RESTORE_DELAY, restore_clipboard=True):
    """Insert text by clipboard paste. Returns True if the keystroke was sent."""
    if not text:
        return False

    saved = read_clipboard() if restore_clipboard else None

    if not write_clipboard(text):
        return False

    try:
        _pyautogui().hotkey("ctrl", "v")
    except Exception as e:  # noqa: BLE001
        logger.warning("Paste keystroke failed: %s", e)
        return False        # text is still on the clipboard for manual pasting

    if restore_clipboard and saved is not None:
        # Restore off-thread so the caller is not blocked waiting on a delay
        # whose only purpose is to let the target app finish reading.
        def _restore():
            time.sleep(restore_delay)
            write_clipboard(saved)

        threading.Thread(target=_restore, daemon=True, name="clipboard-restore").start()

    return True


def type_text(text, interval=0.01):
    """Insert text by synthesising keystrokes — slower, but works where paste is blocked."""
    if not text:
        return False
    try:
        _pyautogui().write(text, interval=interval)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("Typing failed: %s", e)
        return False


def send_key(action):
    """Fire a trailing keystroke action such as Enter or Tab."""
    if not action:
        return False
    try:
        _pyautogui().press(action)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not send %s keystroke: %s", action, e)
        return False


def insert(text, mode="clipboard", restore_delay=DEFAULT_RESTORE_DELAY,
           restore_clipboard=True, trailing_action=None, key_delay=0.08):
    """Insert text and optionally fire a trailing keystroke.

    Returns (ok, message). On failure the text is left on the clipboard, and the
    message says so — the caller surfaces it as a notification.
    """
    if not text:
        return True, ""

    set_last_transcript(text)

    if mode == "type":
        ok = type_text(text)
        if not ok:
            write_clipboard(text)
            return False, "Could not type the text. It is on your clipboard."
    else:
        ok = paste_text(text, restore_delay=restore_delay,
                        restore_clipboard=restore_clipboard)
        if not ok:
            # paste_text already left the transcript on the clipboard.
            logger.info("Paste failed; falling back to typing")
            if type_text(text):
                ok = True
            else:
                return False, "Could not paste. The text is on your clipboard."

    if trailing_action:
        time.sleep(key_delay)   # let the paste settle before submitting
        send_key(trailing_action)

    return True, ""


def paste_last_transcript(**kwargs):
    """Re-insert the most recent transcript — the recovery path after a failure."""
    text = get_last_transcript()
    if not text:
        return False, "No transcript to paste yet."
    return insert(text, **kwargs)


# ---------------------------------------------------------------------------
# Seam handling
# ---------------------------------------------------------------------------
def join_with_context(text, preceding=""):
    """Adjust a block so it reads as typed in place rather than pasted in.

    Continuing mid-sentence lowercases the opener and adds the missing space;
    starting fresh leaves the capital alone. This is the detail that separates
    "obviously dictated" from "looks typed".
    """
    if not text:
        return text

    tail = (preceding or "").rstrip()
    if not tail:
        return text

    ends_sentence = tail[-1] in ".!?:\n"
    result = text

    if not ends_sentence and result[:1].isupper():
        # Only lowercase an ordinary word — never an acronym or a proper noun.
        first_word = result.split(" ", 1)[0].strip(".,!?")
        if not first_word.isupper() and first_word.lower() == first_word.lower():
            if len(first_word) > 1 and not first_word[1:].isupper():
                result = result[0].lower() + result[1:]

    if preceding and not preceding[-1].isspace():
        result = " " + result

    return result

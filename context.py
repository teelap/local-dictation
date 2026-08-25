"""Detect where the user is dictating and adapt the output to suit it.

The single biggest reason a subscription dictation tool reads better than raw
Whisper is that it knows the difference between a Slack box and an email draft.
This module supplies that knowledge: it resolves the foreground window to an
application, maps the application to a category, and hands the caller the style
that category should be written in.

Detection is deliberately cheap and local — window class and title only. No
screen capture, no accessibility-tree scraping, nothing leaves the machine. The
research notes that reading on-screen text is where competing products drew
their sharpest privacy criticism; window titles get most of the formatting
benefit at none of that cost.
"""

import logging
import os
import re
import sys

logger = logging.getLogger(__name__)

# App categories. Wispr Flow ships four and assigns them for you; we ship five
# (splitting out code) and let the user remap any app, which is the obvious
# improvement over a fixed assignment.
CAT_PERSONAL = "personal"
CAT_WORK = "work"
CAT_EMAIL = "email"
CAT_CODE = "code"
CAT_OTHER = "other"

CATEGORIES = [CAT_PERSONAL, CAT_WORK, CAT_EMAIL, CAT_CODE, CAT_OTHER]

CATEGORY_LABELS = {
    CAT_PERSONAL: "Personal messaging",
    CAT_WORK: "Work messaging",
    CAT_EMAIL: "Email",
    CAT_CODE: "Code & terminal",
    CAT_OTHER: "Documents & everything else",
}

CATEGORY_EXAMPLES = {
    CAT_PERSONAL: "iMessage, WhatsApp, Telegram, Discord",
    CAT_WORK: "Slack, Teams, Zoom chat",
    CAT_EMAIL: "Gmail, Outlook, Superhuman, Thunderbird",
    CAT_CODE: "VS Code, Cursor, terminals, JetBrains IDEs",
    CAT_OTHER: "Word, Notion, Docs, browsers, note apps",
}

# Writing styles. These govern punctuation, capitalization, and register — they
# never license rewording. That boundary is deliberate: users tolerate a missing
# period far better than a sentence they did not write.
STYLE_VERY_CASUAL = "very_casual"
STYLE_CASUAL = "casual"
STYLE_EXCITED = "excited"
STYLE_FORMAL = "formal"
STYLE_TECHNICAL = "technical"

STYLES = [STYLE_VERY_CASUAL, STYLE_CASUAL, STYLE_EXCITED, STYLE_FORMAL, STYLE_TECHNICAL]

STYLE_LABELS = {
    STYLE_VERY_CASUAL: "Very casual — lowercase, no trailing periods",
    STYLE_CASUAL: "Casual — contractions, light punctuation",
    STYLE_EXCITED: "Excited — casual with energy",
    STYLE_FORMAL: "Formal — complete sentences, full punctuation",
    STYLE_TECHNICAL: "Technical — preserve identifiers and symbols",
}

STYLE_PREVIEWS = {
    STYLE_VERY_CASUAL: "hey did you get a chance to look at the doc",
    STYLE_CASUAL: "Hey — did you get a chance to look at the doc?",
    STYLE_EXCITED: "Hey! Did you get a chance to look at the doc?",
    STYLE_FORMAL: "Hi, I wanted to check whether you had a chance to review the document.",
    STYLE_TECHNICAL: "check whether `fetchUserData` handles the null case",
}

# How each style bends the deterministic formatter.
STYLE_OPTIONS = {
    STYLE_VERY_CASUAL: {
        "auto_capitalize": False,
        "auto_punctuate": False,
        "smart_quotes": False,
        "tone": "casual",
    },
    STYLE_CASUAL: {
        "auto_capitalize": True,
        "auto_punctuate": False,
        "smart_quotes": False,
        "tone": "casual",
    },
    STYLE_EXCITED: {
        "auto_capitalize": True,
        "auto_punctuate": True,
        "smart_quotes": False,
        "tone": "casual",
    },
    STYLE_FORMAL: {
        "auto_capitalize": True,
        "auto_punctuate": True,
        "smart_quotes": False,
        "tone": "professional",
    },
    STYLE_TECHNICAL: {
        "auto_capitalize": False,
        "auto_punctuate": False,
        "smart_numbers": False,
        "smart_quotes": False,
        "tone": "technical",
    },
}

# Extra guidance handed to the LLM pass per style.
STYLE_LLM_HINTS = {
    STYLE_VERY_CASUAL: "Write it the way someone texts: lowercase openers are fine, "
                       "drop trailing periods on short messages, keep it brief.",
    STYLE_CASUAL: "Write it as a relaxed chat message. Keep contractions. Do not add "
                  "a greeting or sign-off.",
    STYLE_EXCITED: "Write it as an upbeat chat message. An exclamation mark is fine "
                   "where the speaker sounded enthusiastic; do not add more than one.",
    STYLE_FORMAL: "Write it as professional prose: complete sentences, correct "
                  "punctuation. Do not add a greeting or sign-off the speaker did not say.",
    STYLE_TECHNICAL: "This is going into code or a terminal. Preserve identifiers, "
                     "casing, paths, flags, and symbols exactly. Do not add prose "
                     "punctuation to commands or identifiers.",
}

DEFAULT_CATEGORY_STYLES = {
    CAT_PERSONAL: STYLE_CASUAL,
    CAT_WORK: STYLE_CASUAL,
    CAT_EMAIL: STYLE_FORMAL,
    CAT_CODE: STYLE_TECHNICAL,
    CAT_OTHER: STYLE_FORMAL,
}

# Executable name -> category. Matched on the lowercase basename.
DEFAULT_APP_CATEGORIES = {
    # Personal messaging
    "discord.exe": CAT_PERSONAL, "telegram.exe": CAT_PERSONAL,
    "whatsapp.exe": CAT_PERSONAL, "signal.exe": CAT_PERSONAL,
    "messenger.exe": CAT_PERSONAL, "imessage.exe": CAT_PERSONAL,
    # Work messaging
    "slack.exe": CAT_WORK, "teams.exe": CAT_WORK, "ms-teams.exe": CAT_WORK,
    "zoom.exe": CAT_WORK, "webex.exe": CAT_WORK,
    # Email
    "outlook.exe": CAT_EMAIL, "thunderbird.exe": CAT_EMAIL,
    "hxoutlook.exe": CAT_EMAIL, "mailbird.exe": CAT_EMAIL, "em client.exe": CAT_EMAIL,
    # Code and terminals
    "code.exe": CAT_CODE, "code - insiders.exe": CAT_CODE, "cursor.exe": CAT_CODE,
    "windsurf.exe": CAT_CODE, "devenv.exe": CAT_CODE, "rider64.exe": CAT_CODE,
    "idea64.exe": CAT_CODE, "pycharm64.exe": CAT_CODE, "webstorm64.exe": CAT_CODE,
    "goland64.exe": CAT_CODE, "clion64.exe": CAT_CODE, "sublime_text.exe": CAT_CODE,
    "windowsterminal.exe": CAT_CODE, "wt.exe": CAT_CODE, "cmd.exe": CAT_CODE,
    "powershell.exe": CAT_CODE, "pwsh.exe": CAT_CODE, "conemu64.exe": CAT_CODE,
    "alacritty.exe": CAT_CODE, "wezterm-gui.exe": CAT_CODE, "putty.exe": CAT_CODE,
    "nvim.exe": CAT_CODE, "vim.exe": CAT_CODE, "emacs.exe": CAT_CODE,
    # Documents and notes
    "winword.exe": CAT_OTHER, "notepad.exe": CAT_OTHER, "notepad++.exe": CAT_OTHER,
    "onenote.exe": CAT_OTHER, "notion.exe": CAT_OTHER, "obsidian.exe": CAT_OTHER,
    "evernote.exe": CAT_OTHER, "typora.exe": CAT_OTHER, "bear.exe": CAT_OTHER,
}

BROWSER_EXES = {
    "chrome.exe", "firefox.exe", "msedge.exe", "brave.exe", "opera.exe",
    "vivaldi.exe", "arc.exe", "chromium.exe", "librewolf.exe",
}

# A browser is whatever site is open, so refine by window title. Ordered —
# first match wins.
BROWSER_TITLE_RULES = [
    (re.compile(r"\b(gmail|inbox|outlook|proton\s*mail|fastmail|superhuman|zoho mail)\b", re.I), CAT_EMAIL),
    (re.compile(r"\bslack\b", re.I), CAT_WORK),
    (re.compile(r"\b(teams|microsoft teams)\b", re.I), CAT_WORK),
    (re.compile(r"\b(discord|whatsapp|telegram|messenger)\b", re.I), CAT_PERSONAL),
    (re.compile(r"\b(github|gitlab|stack overflow|codepen|replit|codesandbox|jupyter)\b", re.I), CAT_CODE),
    (re.compile(r"\b(linear|jira|asana|notion|confluence|google docs|docs\.google)\b", re.I), CAT_OTHER),
]


class AppContext:
    """A resolved snapshot of where the text is about to land."""

    def __init__(self, exe="", title="", category=CAT_OTHER, style=STYLE_FORMAL, source="unknown"):
        self.exe = exe
        self.title = title
        self.category = category
        self.style = style
        self.source = source

    @property
    def app_name(self):
        """A human-readable name for the app, for logs and the LLM prompt."""
        if not self.exe:
            return ""
        base = re.sub(r"\.exe$", "", self.exe, flags=re.IGNORECASE)
        return base.replace("-", " ").replace("_", " ").title()

    def __repr__(self):
        return (f"AppContext(exe={self.exe!r}, category={self.category!r}, "
                f"style={self.style!r})")


# ---------------------------------------------------------------------------
# Foreground window detection
# ---------------------------------------------------------------------------
def _foreground_windows():
    """Return (exe_basename, window_title) for the focused window on Windows."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return "", ""

    length = user32.GetWindowTextLengthW(hwnd)
    title_buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, title_buf, length + 1)
    title = title_buf.value or ""

    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return "", title

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return "", title
    try:
        buf = ctypes.create_unicode_buffer(4096)
        size = wintypes.DWORD(4096)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value), title
    finally:
        kernel32.CloseHandle(handle)
    return "", title


def _foreground_x11():
    """Best-effort X11 detection, so the feature is testable off Windows."""
    import subprocess

    try:
        win_id = subprocess.run(
            ["xdotool", "getactivewindow"], capture_output=True, text=True, timeout=1.5)
        if win_id.returncode != 0:
            return "", ""
        wid = win_id.stdout.strip()
        name = subprocess.run(
            ["xdotool", "getwindowname", wid], capture_output=True, text=True, timeout=1.5)
        cls = subprocess.run(
            ["xprop", "-id", wid, "WM_CLASS"], capture_output=True, text=True, timeout=1.5)
        title = name.stdout.strip() if name.returncode == 0 else ""
        exe = ""
        if cls.returncode == 0:
            found = re.findall(r'"([^"]+)"', cls.stdout)
            if found:
                exe = found[-1].lower()
        return exe, title
    except (OSError, subprocess.SubprocessError):
        return "", ""


def get_foreground_window():
    """Return (exe, title) for the focused window, or ("", "") if unavailable."""
    try:
        if sys.platform == "win32":
            return _foreground_windows()
        return _foreground_x11()
    except Exception as e:  # noqa: BLE001 — detection must never break dictation
        logger.debug("Foreground window detection failed: %s", e)
        return "", ""


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------
def categorize(exe, title, overrides=None):
    """Map an executable and window title to a category.

    User overrides win, then browser-title rules, then the built-in table.
    """
    exe_lower = (exe or "").lower()
    overrides = {k.lower(): v for k, v in (overrides or {}).items()}

    if exe_lower in overrides:
        return overrides[exe_lower], "override"

    if exe_lower in BROWSER_EXES:
        for pattern, category in BROWSER_TITLE_RULES:
            if pattern.search(title or ""):
                return category, "browser-title"
        return CAT_OTHER, "browser-default"

    if exe_lower in DEFAULT_APP_CATEGORIES:
        return DEFAULT_APP_CATEGORIES[exe_lower], "builtin"

    return CAT_OTHER, "default"


def resolve(config):
    """Resolve the current foreground window into an :class:`AppContext`.

    Honours the master Context Awareness switch: with it off, every window looks
    like the "other" category and no window title is ever read.
    """
    settings = config.get("context", {}) or {}
    if not settings.get("enabled", True):
        styles = settings.get("category_styles", {}) or {}
        return AppContext(
            category=CAT_OTHER,
            style=styles.get(CAT_OTHER, DEFAULT_CATEGORY_STYLES[CAT_OTHER]),
            source="disabled",
        )

    exe, title = get_foreground_window()
    category, source = categorize(exe, title, settings.get("app_overrides"))

    styles = {**DEFAULT_CATEGORY_STYLES, **(settings.get("category_styles") or {})}
    style = styles.get(category, STYLE_FORMAL)

    ctx = AppContext(exe=exe, title=title, category=category, style=style, source=source)
    logger.debug("Context: %r (title=%r, via %s)", ctx, title[:60], source)
    return ctx


def profile_for(context, config):
    """Build the formatter overrides implied by a context.

    Merges the style preset with any per-category customisation the user has
    saved, so a user who wants "casual but keep my periods" gets exactly that.
    """
    profile = dict(STYLE_OPTIONS.get(context.style, {}))

    settings = config.get("context", {}) or {}
    per_category = (settings.get("category_overrides") or {}).get(context.category, {})
    profile.update({k: v for k, v in per_category.items() if v is not None})

    hint = STYLE_LLM_HINTS.get(context.style, "")
    existing = profile.get("custom_instructions", "") or ""
    global_instructions = (config.get("formatting", {}) or {}).get("custom_instructions", "") or ""
    profile["custom_instructions"] = " ".join(
        part for part in (hint, global_instructions, existing) if part).strip()

    return profile

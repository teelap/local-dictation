"""Usage statistics — the numbers that make invisible work legible.

Filler removal and dictionary corrections happen silently, so users never see
what the app did for them. Counting those edits and surfacing them turns the
formatter from "it types what I say" into "it fixed 1,240 words for me". The
same goes for streaks and words-per-minute: they are the reason anyone opens the
main window at all.

Everything is bucketed by local calendar day and stored in a single JSON file.
"""

import json
import logging
import os
import threading
from datetime import date, datetime, timedelta

logger = logging.getLogger(__name__)

# Average sustained typing speed, used as the baseline for "time saved".
DEFAULT_TYPING_WPM = 40

# Playful scale comparisons for the word counter, smallest first.
WORD_BENCHMARKS = [
    (100, "a thank-you note"),
    (500, "a blog post"),
    (1_500, "a news article"),
    (5_000, "a short story"),
    (20_000, "a novella"),
    (50_000, "a novel"),
]

_lock = threading.RLock()
_data = {"days": {}}
_path = None


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
def init(app_dir):
    global _path
    _path = os.path.join(app_dir, "stats.json")
    load()


def load():
    global _data
    if not _path or not os.path.exists(_path):
        return
    try:
        with open(_path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict) and isinstance(loaded.get("days"), dict):
            with _lock:
                _data = loaded
            logger.info("Loaded stats for %d days", len(_data["days"]))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Could not load stats: %s", e)


def save():
    """Write stats atomically.

    record_session runs on the dictation worker while Reset runs on the Tk
    thread; truncating the real file in place lets one of them read a
    half-written file, or lose the other's write entirely.
    """
    if not _path:
        return
    temp_path = f"{_path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with _lock:
            snapshot = json.dumps(_data, indent=2)
            with open(temp_path, "w", encoding="utf-8") as f:
                f.write(snapshot)
            os.replace(temp_path, _path)
    except OSError as e:
        logger.warning("Could not save stats: %s", e)
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass


def _blank_day():
    return {"words": 0, "sessions": 0, "audio_seconds": 0.0,
            "cleaned_words": 0, "replacements": 0, "apps": {}}


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------
def record_session(words, audio_seconds, cleaned_words=0, replacements=0, app=""):
    """Log one completed dictation.

    Args:
        words: Word count of the text that was actually inserted.
        audio_seconds: Length of the recording.
        cleaned_words: Words the formatter removed or rewrote.
        replacements: Dictionary corrections and snippet expansions applied.
        app: Executable name the text was inserted into.
    """
    key = date.today().isoformat()
    with _lock:
        day = _data["days"].setdefault(key, _blank_day())
        day["words"] += max(0, int(words))
        day["sessions"] += 1
        day["audio_seconds"] = round(day["audio_seconds"] + max(0.0, float(audio_seconds)), 1)
        day["cleaned_words"] += max(0, int(cleaned_words))
        day["replacements"] += max(0, int(replacements))
        if app:
            day["apps"][app] = day["apps"].get(app, 0) + 1
    save()


def reset():
    with _lock:
        _data["days"] = {}
    save()


# ---------------------------------------------------------------------------
# Derived metrics
# ---------------------------------------------------------------------------
def _days():
    with _lock:
        return dict(_data["days"])


def totals():
    """Aggregate every recorded day."""
    result = {"words": 0, "sessions": 0, "audio_seconds": 0.0,
              "cleaned_words": 0, "replacements": 0, "apps": {}, "active_days": 0}
    for day in _days().values():
        result["words"] += day.get("words", 0)
        result["sessions"] += day.get("sessions", 0)
        result["audio_seconds"] += day.get("audio_seconds", 0.0)
        result["cleaned_words"] += day.get("cleaned_words", 0)
        result["replacements"] += day.get("replacements", 0)
        if day.get("words", 0) > 0:
            result["active_days"] += 1
        for app, count in (day.get("apps") or {}).items():
            result["apps"][app] = result["apps"].get(app, 0) + count
    return result


def words_per_minute():
    """Dictation speed across all recorded audio."""
    agg = totals()
    minutes = agg["audio_seconds"] / 60.0
    if minutes <= 0:
        return 0.0
    return round(agg["words"] / minutes, 1)


def time_saved_seconds(typing_wpm=DEFAULT_TYPING_WPM):
    """Seconds saved versus typing the same words by hand.

    Deliberately conservative: it credits only the difference between typing
    time and the time actually spent speaking, and never goes negative.
    """
    agg = totals()
    if agg["words"] <= 0 or typing_wpm <= 0:
        return 0
    typing_seconds = agg["words"] / typing_wpm * 60.0
    return max(0, int(typing_seconds - agg["audio_seconds"]))


def streak():
    """Consecutive days with at least one dictation, ending today or yesterday.

    Yesterday counts as the anchor so the streak does not appear broken first
    thing in the morning before the user has dictated anything.
    """
    days = _days()
    active = {key for key, value in days.items() if value.get("words", 0) > 0}
    if not active:
        return 0

    today = date.today()
    anchor = today if today.isoformat() in active else today - timedelta(days=1)
    if anchor.isoformat() not in active:
        return 0

    count = 0
    cursor = anchor
    while cursor.isoformat() in active:
        count += 1
        cursor -= timedelta(days=1)
    return count


def longest_streak():
    days = _days()
    active = sorted(key for key, value in days.items() if value.get("words", 0) > 0)
    if not active:
        return 0

    best = run = 1
    previous = datetime.strptime(active[0], "%Y-%m-%d").date()
    for key in active[1:]:
        current = datetime.strptime(key, "%Y-%m-%d").date()
        run = run + 1 if (current - previous).days == 1 else 1
        best = max(best, run)
        previous = current
    return best


def top_apps(limit=5):
    """Most-dictated-into applications, as (app, session_count) pairs."""
    agg = totals()
    ranked = sorted(agg["apps"].items(), key=lambda item: item[1], reverse=True)
    return ranked[:limit]


def heatmap(days_back=182):
    """Daily word counts for the last ``days_back`` days, oldest first.

    Returns a list of (date, words) — the calendar grid on the Insights page.
    """
    days = _days()
    today = date.today()
    return [
        ((today - timedelta(days=offset)).isoformat(),
         days.get((today - timedelta(days=offset)).isoformat(), {}).get("words", 0))
        for offset in range(days_back - 1, -1, -1)
    ]


def recent_days(count=7):
    days = _days()
    today = date.today()
    return [
        ((today - timedelta(days=offset)).isoformat(),
         days.get((today - timedelta(days=offset)).isoformat(), _blank_day()))
        for offset in range(count - 1, -1, -1)
    ]


def word_comparison(words=None):
    """Turn a raw word count into something a person can picture."""
    if words is None:
        words = totals()["words"]
    if words < WORD_BENCHMARKS[0][0]:
        return ""
    for threshold, label in reversed(WORD_BENCHMARKS):
        if words >= threshold:
            multiple = words / threshold
            if multiple < 2:
                return f"about {label}"
            return f"about {int(multiple)} × {label}"
    return ""


def format_duration(seconds):
    """Render a duration the way a dashboard should: coarse and readable."""
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {secs}s" if secs else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h" if hours else f"{days}d"


def summary(typing_wpm=DEFAULT_TYPING_WPM):
    """One call for everything the Home and Insights pages display."""
    agg = totals()
    return {
        "words": agg["words"],
        "sessions": agg["sessions"],
        "audio_seconds": agg["audio_seconds"],
        "cleaned_words": agg["cleaned_words"],
        "replacements": agg["replacements"],
        "active_days": agg["active_days"],
        "wpm": words_per_minute(),
        "streak": streak(),
        "longest_streak": longest_streak(),
        "time_saved": time_saved_seconds(typing_wpm),
        "time_saved_label": format_duration(time_saved_seconds(typing_wpm)),
        "top_apps": top_apps(),
        "comparison": word_comparison(agg["words"]),
    }

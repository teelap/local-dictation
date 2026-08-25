"""Transcript history — the record of what was said and what was typed.

Every entry stores **both** the raw transcript and the cleaned text. That is the
single most important trust feature in the whole app: it means "undo AI edit" is
always possible, the user can see exactly what the formatter changed, and a
cleanup pass that gets something wrong is recoverable rather than silent.

This module is the data layer only — the browsing UI lives in the Flow Hub.
"""

import csv
import io
import json
import logging
import os
import re
import threading
import uuid
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

MAX_ENTRIES = 2000

MODE_DICTATION = "dictation"
MODE_COMMAND = "command"

_lock = threading.RLock()
_entries = []
_path = None
_persist = True


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------
def init(app_dir, persist=True, retention_days=0):
    global _path, _persist
    _persist = persist
    _path = os.path.join(app_dir, "history.json")
    if persist and os.path.exists(_path):
        load()
    if retention_days:
        prune(retention_days)


def load():
    global _entries
    try:
        with open(_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            with _lock:
                _entries = [_normalize(e) for e in data][-MAX_ENTRIES:]
            logger.info("Loaded %d history entries", len(_entries))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Could not load history: %s", e)


def save():
    if not _persist or not _path:
        return
    try:
        with _lock:
            payload = json.dumps(list(_entries), indent=2, ensure_ascii=False)
        temp_path = _path + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(temp_path, _path)
    except OSError as e:
        logger.warning("Could not save history: %s", e)


def _normalize(entry):
    """Fill in fields added after an entry was written, so old files still load."""
    text = entry.get("text", "")
    return {
        "id": entry.get("id") or uuid.uuid4().hex[:12],
        "timestamp": entry.get("timestamp") or datetime.now().isoformat(timespec="seconds"),
        "duration": float(entry.get("duration", 0.0) or 0.0),
        "raw": entry.get("raw", text),
        "text": text,
        "app": entry.get("app", ""),
        "app_title": entry.get("app_title", ""),
        "category": entry.get("category", ""),
        "cleanup_level": entry.get("cleanup_level", ""),
        "used_llm": bool(entry.get("used_llm", False)),
        "words": int(entry.get("words") or len(text.split())),
        "cleaned_words": int(entry.get("cleaned_words", 0) or 0),
        "replacements": int(entry.get("replacements", 0) or 0),
        "mode": entry.get("mode", MODE_DICTATION),
        "reverted": bool(entry.get("reverted", False)),
    }


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------
def add_entry(raw, text, duration=0.0, app="", app_title="", category="",
              cleanup_level="", used_llm=False, cleaned_words=0, replacements=0,
              mode=MODE_DICTATION):
    """Record one dictation. Returns the stored entry."""
    entry = _normalize({
        "raw": raw, "text": text, "duration": duration, "app": app,
        "app_title": app_title, "category": category, "cleanup_level": cleanup_level,
        "used_llm": used_llm, "cleaned_words": cleaned_words,
        "replacements": replacements, "mode": mode,
    })
    with _lock:
        _entries.append(entry)
        if len(_entries) > MAX_ENTRIES:
            del _entries[:len(_entries) - MAX_ENTRIES]
    save()
    logger.debug("History: %s", text[:60])
    return entry


def revert_ai_edit(entry_id):
    """Swap an entry's cleaned text back to the raw transcript.

    Returns the raw text so the caller can re-paste it, or None if not found.
    """
    with _lock:
        for entry in _entries:
            if entry["id"] == entry_id:
                if entry["reverted"]:
                    return entry["text"]
                entry["text"], entry["raw"] = entry["raw"], entry["raw"]
                entry["reverted"] = True
                entry["words"] = len(entry["text"].split())
                save()
                return entry["text"]
    return None


def update_text(entry_id, text):
    """Edit an entry's text in place — used when the user fixes a transcript."""
    with _lock:
        for entry in _entries:
            if entry["id"] == entry_id:
                entry["text"] = text
                entry["words"] = len(text.split())
                save()
                return True
    return False


def delete(entry_id):
    with _lock:
        before = len(_entries)
        _entries[:] = [e for e in _entries if e["id"] != entry_id]
        changed = len(_entries) != before
    if changed:
        save()
    return changed


def clear():
    with _lock:
        _entries.clear()
    save()


def prune(retention_days):
    """Drop entries older than the retention window. 0 keeps everything."""
    if not retention_days:
        return 0
    cutoff = datetime.now() - timedelta(days=retention_days)
    with _lock:
        before = len(_entries)
        kept = []
        for entry in _entries:
            try:
                stamp = datetime.fromisoformat(entry["timestamp"])
            except (ValueError, KeyError):
                kept.append(entry)     # unparseable timestamp: keep it
                continue
            if stamp >= cutoff:
                kept.append(entry)
        _entries[:] = kept
        removed = before - len(_entries)
    if removed:
        save()
        logger.info("Pruned %d history entries older than %d days", removed, retention_days)
    return removed


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------
def get_entries(newest_first=True):
    with _lock:
        entries = [dict(e) for e in _entries]
    return list(reversed(entries)) if newest_first else entries


def get_entry(entry_id):
    with _lock:
        for entry in _entries:
            if entry["id"] == entry_id:
                return dict(entry)
    return None


def latest():
    with _lock:
        return dict(_entries[-1]) if _entries else None


def search(query, category=None, mode=None):
    """Filter history by free text, category, and mode.

    Matching is on whole words where possible so searching "the" does not return
    every entry containing "there".
    """
    entries = get_entries()
    query = (query or "").strip()

    if category:
        entries = [e for e in entries if e["category"] == category]
    if mode:
        entries = [e for e in entries if e["mode"] == mode]

    if not query:
        return entries

    try:
        pattern = re.compile(re.escape(query), re.IGNORECASE)
    except re.error:
        return entries

    return [e for e in entries
            if pattern.search(e["text"]) or pattern.search(e["raw"])
            or pattern.search(e["app"])]


def group_by_day(entries=None):
    """Group entries under Today / Yesterday / explicit dates, newest first."""
    entries = entries if entries is not None else get_entries()
    today = datetime.now().date()
    groups = []
    index = {}

    for entry in entries:
        try:
            stamp = datetime.fromisoformat(entry["timestamp"]).date()
        except (ValueError, KeyError):
            stamp = today

        delta = (today - stamp).days
        if delta == 0:
            label = "Today"
        elif delta == 1:
            label = "Yesterday"
        else:
            label = stamp.strftime("%B %d, %Y")

        if label not in index:
            index[label] = []
            groups.append((label, index[label]))
        index[label].append(entry)

    return groups


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
def export(fmt="txt", entries=None):
    """Serialise history as plain text, JSON, or CSV."""
    entries = entries if entries is not None else get_entries(newest_first=False)

    if fmt == "json":
        return json.dumps(entries, indent=2, ensure_ascii=False)

    if fmt == "csv":
        buffer = io.StringIO()
        fields = ["timestamp", "duration", "app", "category", "mode",
                  "words", "used_llm", "text", "raw"]
        writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for entry in entries:
            writer.writerow(entry)
        return buffer.getvalue()

    lines = []
    for entry in entries:
        lines.append(f"[{entry['timestamp']}] ({entry['duration']:.1f}s"
                     + (f", {entry['app']}" if entry["app"] else "") + ")")
        lines.append(entry["text"])
        lines.append("")
    return "\n".join(lines)


def stats_snapshot():
    """Quick counts for the Home page header."""
    entries = get_entries()
    return {
        "count": len(entries),
        "words": sum(e["words"] for e in entries),
        "with_llm": sum(1 for e in entries if e["used_llm"]),
    }

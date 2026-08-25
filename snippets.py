"""Voice-triggered text expansion: say a short phrase, type a saved block.

Say "my work email" and your address appears; say "standup template" and the
whole block does. Expansion is deterministic — the trigger is matched as a
literal phrase rather than inferred by a model — because a snippet that fires
when you did not mean it is far more annoying than one that occasionally does
not fire.

The only subtle part is splicing: the trigger phrase itself has to disappear and
the expansion has to sit correctly in the surrounding sentence.
"""

import json
import logging
import os
import re
import threading
from datetime import datetime

logger = logging.getLogger(__name__)

MAX_TRIGGER_LENGTH = 60
MAX_EXPANSION_LENGTH = 4000

_lock = threading.RLock()
_snippets = []        # [{trigger, expansion, enabled}]
_path = None


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
def init(app_dir):
    global _path
    _path = os.path.join(app_dir, "snippets.json")
    load()


def load():
    global _snippets
    if not _path or not os.path.exists(_path):
        return
    try:
        with open(_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        with _lock:
            _snippets = [_normalize(s) for s in data if s.get("trigger")]
        logger.info("Loaded %d snippets", len(_snippets))
    except (OSError, json.JSONDecodeError, AttributeError) as e:
        logger.warning("Could not load snippets: %s", e)


def save():
    if not _path:
        return
    try:
        with _lock:
            snapshot = list(_snippets)
        with open(_path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, indent=2, ensure_ascii=False)
    except OSError as e:
        logger.warning("Could not save snippets: %s", e)


def _normalize(entry):
    return {
        "trigger": str(entry.get("trigger", ""))[:MAX_TRIGGER_LENGTH].strip(),
        "expansion": str(entry.get("expansion", ""))[:MAX_EXPANSION_LENGTH],
        "enabled": bool(entry.get("enabled", True)),
    }


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------
def get_snippets():
    with _lock:
        return [dict(s) for s in _snippets]


def find(trigger):
    lowered = (trigger or "").strip().lower()
    with _lock:
        for snippet in _snippets:
            if snippet["trigger"].lower() == lowered:
                return dict(snippet)
    return None


def add(trigger, expansion, enabled=True):
    trigger = (trigger or "").strip()
    expansion = expansion or ""
    if not trigger:
        return False, "Trigger phrase cannot be empty."
    if len(trigger) > MAX_TRIGGER_LENGTH:
        return False, f"Trigger phrases are limited to {MAX_TRIGGER_LENGTH} characters."
    if not expansion.strip():
        return False, "Expansion cannot be empty."
    if len(expansion) > MAX_EXPANSION_LENGTH:
        return False, f"Expansions are limited to {MAX_EXPANSION_LENGTH} characters."
    if find(trigger):
        return False, f"A snippet for {trigger!r} already exists."

    with _lock:
        _snippets.append(_normalize({
            "trigger": trigger, "expansion": expansion, "enabled": enabled}))
    save()
    return True, f"Added snippet {trigger!r}."


def update(original_trigger, trigger=None, expansion=None, enabled=None):
    lowered = (original_trigger or "").strip().lower()
    with _lock:
        for snippet in _snippets:
            if snippet["trigger"].lower() == lowered:
                if trigger is not None:
                    snippet["trigger"] = trigger.strip()[:MAX_TRIGGER_LENGTH]
                if expansion is not None:
                    snippet["expansion"] = expansion[:MAX_EXPANSION_LENGTH]
                if enabled is not None:
                    snippet["enabled"] = bool(enabled)
                save()
                return True
    return False


def remove(trigger):
    lowered = (trigger or "").strip().lower()
    with _lock:
        before = len(_snippets)
        _snippets[:] = [s for s in _snippets if s["trigger"].lower() != lowered]
        changed = len(_snippets) != before
    if changed:
        save()
    return changed


def clear():
    with _lock:
        _snippets.clear()
    save()


def import_snippets(payload):
    """Import a JSON array of {name|trigger, text|expansion} objects."""
    try:
        data = json.loads(payload) if isinstance(payload, str) else payload
    except json.JSONDecodeError as e:
        return 0, 0, f"Not valid JSON: {e}"

    if isinstance(data, dict):
        data = [{"trigger": k, "expansion": v} for k, v in data.items()]
    if not isinstance(data, list):
        return 0, 0, "Expected a JSON array of snippets."

    added = skipped = 0
    for raw in data:
        if not isinstance(raw, dict):
            skipped += 1
            continue
        trigger = raw.get("trigger") or raw.get("name") or ""
        expansion = raw.get("expansion") or raw.get("text") or ""
        ok, _ = add(trigger, expansion)
        added += 1 if ok else 0
        skipped += 0 if ok else 1
    return added, skipped, None


def export_snippets():
    return json.dumps(get_snippets(), indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Expansion
# ---------------------------------------------------------------------------
def _resolve_variables(expansion, clipboard_text=""):
    """Substitute {date}, {time}, {datetime} and {clipboard} placeholders."""
    now = datetime.now()
    values = {
        "date": now.strftime("%B %-d, %Y") if os.name != "nt" else now.strftime("%B %d, %Y"),
        "time": now.strftime("%-I:%M %p") if os.name != "nt" else now.strftime("%I:%M %p"),
        "datetime": now.strftime("%Y-%m-%d %H:%M"),
        "iso_date": now.strftime("%Y-%m-%d"),
        "clipboard": clipboard_text or "",
    }

    def _replace(match):
        return values.get(match.group(1).lower(), match.group(0))

    return re.sub(r"\{(\w+)\}", _replace, expansion)


def expand(text, clipboard_text=""):
    """Replace any trigger phrases in ``text`` with their expansions.

    Longer triggers are matched first so "my work email" wins over "my work".
    Returns (text, [triggers_fired]).
    """
    if not text:
        return text, []

    with _lock:
        active = [dict(s) for s in _snippets if s["enabled"] and s["trigger"]]
    if not active:
        return text, []

    fired = []
    for snippet in sorted(active, key=lambda s: len(s["trigger"]), reverse=True):
        # Tolerate the punctuation and spacing variance of dictated speech.
        pattern = r"\s+".join(re.escape(word) for word in snippet["trigger"].split())
        regex = re.compile(rf"\b{pattern}\b", re.IGNORECASE)
        if not regex.search(text):
            continue
        expansion = _resolve_variables(snippet["expansion"], clipboard_text)
        text = regex.sub(lambda _m, e=expansion: e, text)
        fired.append(snippet["trigger"])

    if fired:
        # Splicing an expansion mid-sentence tends to leave doubled spaces or a
        # space stranded before punctuation.
        text = re.sub(r"[ \t]{2,}", " ", text)
        text = re.sub(r"\s+([,.;:!?])", r"\1", text)
        logger.info("Snippets fired: %s", ", ".join(fired))

    return text, fired

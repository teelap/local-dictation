"""Custom vocabulary: teach the recognizer your names, jargon, and acronyms.

Two mechanisms, because one is not enough:

1. **Biasing** — terms are fed to Whisper as ``hotwords``/``initial_prompt`` so
   the acoustic model is nudged toward them while decoding. This is what
   actually fixes accuracy.
2. **Correction** — a post-transcription fuzzy pass that repairs terms the model
   mangled anyway ("to lapek" -> "Tlapek"). This catches what biasing misses.

Entries can be starred. That is not decoration: the bias prompt has a bounded
length, so when the list outgrows the budget, starred entries are the ones that
still get injected.
"""

import difflib
import json
import logging
import os
import re
import threading

logger = logging.getLogger(__name__)

MAX_TERM_LENGTH = 60          # matches the cap users are used to
MAX_BIAS_CHARS = 900          # keeps the Whisper prompt well inside its window
MIN_FUZZY_RATIO = 0.82        # below this, a "correction" is usually a new word

_lock = threading.RLock()
_entries = []                 # [{term, sounds_like, starred, auto, created}]
_path = None


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
def init(app_dir):
    global _path
    _path = os.path.join(app_dir, "dictionary.json")
    load()


def load():
    global _entries
    if not _path or not os.path.exists(_path):
        return
    try:
        with open(_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        with _lock:
            _entries = [_normalize(e) for e in data if e.get("term")]
        logger.info("Loaded %d dictionary entries", len(_entries))
    except (OSError, json.JSONDecodeError, AttributeError) as e:
        logger.warning("Could not load dictionary: %s", e)


def save():
    if not _path:
        return
    try:
        with _lock:
            snapshot = list(_entries)
        with open(_path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, indent=2, ensure_ascii=False)
    except OSError as e:
        logger.warning("Could not save dictionary: %s", e)


def _normalize(entry):
    return {
        "term": str(entry.get("term", ""))[:MAX_TERM_LENGTH].strip(),
        "sounds_like": [str(s).strip() for s in entry.get("sounds_like", []) if str(s).strip()],
        "starred": bool(entry.get("starred", False)),
        "auto": bool(entry.get("auto", False)),
    }


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------
def get_entries():
    with _lock:
        return [dict(e) for e in _entries]


def find(term):
    lowered = (term or "").strip().lower()
    with _lock:
        for entry in _entries:
            if entry["term"].lower() == lowered:
                return dict(entry)
    return None


def add(term, sounds_like=None, starred=False, auto=False):
    """Add a term. Returns (ok, message) — duplicates are rejected, not merged."""
    term = (term or "").strip()
    if not term:
        return False, "Term cannot be empty."
    if len(term) > MAX_TERM_LENGTH:
        return False, f"Terms are limited to {MAX_TERM_LENGTH} characters."
    if find(term):
        return False, f"{term!r} is already in your dictionary."

    entry = _normalize({
        "term": term,
        "sounds_like": sounds_like or [],
        "starred": starred,
        "auto": auto,
    })
    with _lock:
        _entries.append(entry)
    save()
    logger.info("Dictionary: added %r%s", term, " (auto-learned)" if auto else "")
    return True, f"Added {term!r}."


def update(original_term, term=None, sounds_like=None, starred=None):
    lowered = (original_term or "").strip().lower()
    with _lock:
        for entry in _entries:
            if entry["term"].lower() == lowered:
                if term is not None:
                    entry["term"] = term.strip()[:MAX_TERM_LENGTH]
                if sounds_like is not None:
                    entry["sounds_like"] = [s.strip() for s in sounds_like if s.strip()]
                if starred is not None:
                    entry["starred"] = bool(starred)
                entry["auto"] = False   # an edited entry is now user-owned
                save()
                return True
    return False


def remove(term):
    lowered = (term or "").strip().lower()
    with _lock:
        before = len(_entries)
        _entries[:] = [e for e in _entries if e["term"].lower() != lowered]
        changed = len(_entries) != before
    if changed:
        save()
    return changed


def clear():
    with _lock:
        _entries.clear()
    save()


def toggle_star(term):
    entry = find(term)
    if not entry:
        return False
    return update(term, starred=not entry["starred"])


# ---------------------------------------------------------------------------
# Bulk import / export
# ---------------------------------------------------------------------------
def import_entries(payload):
    """Import a JSON array of {name|term, text|sounds_like} objects.

    Returns (added, skipped, error). Accepts the shape competing tools export,
    so migrating a glossary does not mean retyping it.
    """
    try:
        data = json.loads(payload) if isinstance(payload, str) else payload
    except json.JSONDecodeError as e:
        return 0, 0, f"Not valid JSON: {e}"

    if isinstance(data, dict):
        data = [{"term": k, "sounds_like": v} for k, v in data.items()]
    if not isinstance(data, list):
        return 0, 0, "Expected a JSON array of entries."

    added = skipped = 0
    for raw in data:
        if isinstance(raw, str):
            raw = {"term": raw}
        if not isinstance(raw, dict):
            skipped += 1
            continue
        term = raw.get("term") or raw.get("name") or raw.get("word") or ""
        sounds = raw.get("sounds_like") or raw.get("text") or raw.get("aliases") or []
        if isinstance(sounds, str):
            sounds = [sounds]
        ok, _ = add(term, sounds_like=sounds, starred=bool(raw.get("starred")))
        added += 1 if ok else 0
        skipped += 0 if ok else 1
    return added, skipped, None


def export_entries():
    return json.dumps(get_entries(), indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Biasing
# ---------------------------------------------------------------------------
def bias_terms(limit_chars=MAX_BIAS_CHARS):
    """The term list to bias decoding with, starred entries first.

    Truncated to a character budget — an overlong prompt degrades transcription
    rather than improving it, which is the whole reason starring exists.
    """
    with _lock:
        ordered = sorted(_entries, key=lambda e: (not e["starred"], e["term"].lower()))

    chosen, used = [], 0
    for entry in ordered:
        term = entry["term"]
        cost = len(term) + 2
        if used + cost > limit_chars:
            break
        chosen.append(term)
        used += cost
    return chosen


def initial_prompt(extra_terms=None):
    """Build the Whisper ``initial_prompt`` string, or None when there is nothing.

    Phrasing it as a sentence of vocabulary works better than a bare CSV — the
    prompt is interpreted as preceding transcript text, so it should read like
    natural language.
    """
    terms = bias_terms()
    if extra_terms:
        terms = terms + [t for t in extra_terms if t not in terms]
    if not terms:
        return None
    return "Vocabulary: " + ", ".join(terms) + "."


# ---------------------------------------------------------------------------
# Correction
# ---------------------------------------------------------------------------
def _fuzzy_key(value):
    """Reduce a string to letters and digits so hyphens and spacing don't count."""
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def _match_case(source, replacement):
    """Carry the casing of what was transcribed onto the replacement."""
    if source.isupper() and len(source) > 1:
        return replacement.upper()
    if source[:1].isupper():
        return replacement[:1].upper() + replacement[1:]
    return replacement


def correct(text):
    """Repair near-misses of dictionary terms in a transcript.

    Explicit "sounds like" aliases are applied first and exactly; then each term
    is fuzzily matched against same-length word windows so a term dictated as
    two words ("to lapek") still resolves to one ("Tlapek").
    """
    if not text:
        return text

    with _lock:
        entries = [dict(e) for e in _entries]
    if not entries:
        return text

    # Pass 1 — explicit aliases, highest confidence.
    for entry in entries:
        for alias in entry["sounds_like"]:
            if not alias:
                continue
            text = re.sub(rf"\b{re.escape(alias)}\b",
                          lambda m, t=entry["term"]: _match_case(m.group(0), t),
                          text, flags=re.IGNORECASE)

    # Pass 2 — fuzzy match over word windows.
    tokens = re.findall(r"\w+|\W+", text)
    word_positions = [i for i, tok in enumerate(tokens) if tok.isalnum() or "_" in tok]
    if not word_positions:
        return text

    # One matcher reused across every window: SequenceMatcher caches an index of
    # its second sequence, so holding the term in seq2 and varying seq1 avoids
    # rebuilding that index hundreds of thousands of times on a long transcript.
    matcher = difflib.SequenceMatcher(autojunk=False)

    for entry in entries:
        term = entry["term"]
        # Count word tokens, not space-separated chunks: "faster-whisper" is
        # dictated as two words and must match a two-word window.
        span = max(1, len(re.findall(r"\w+", term)))
        if len(term) < 4:
            continue        # short terms fuzzy-match far too eagerly

        term_key = _fuzzy_key(term)
        if not term_key:
            continue
        matcher.set_seq2(term_key)

        # A ratio of 2M/(la+lb) cannot reach the threshold unless the two
        # lengths are within this band, so most windows are rejected by a
        # comparison instead of a full diff.
        min_len = len(term_key) * MIN_FUZZY_RATIO / (2 - MIN_FUZZY_RATIO)
        max_len = len(term_key) * (2 - MIN_FUZZY_RATIO) / MIN_FUZZY_RATIO

        index = 0
        while index <= len(word_positions) - span:
            start = word_positions[index]
            end = word_positions[index + span - 1]
            candidate = "".join(tokens[start:end + 1])
            if candidate.lower() == term.lower():
                index += span
                continue

            candidate_key = _fuzzy_key(candidate)
            if not min_len <= len(candidate_key) <= max_len:
                index += 1
                continue

            matcher.set_seq1(candidate_key)
            # Both quick ratios are cheap upper bounds on the real one.
            if (matcher.real_quick_ratio() < MIN_FUZZY_RATIO
                    or matcher.quick_ratio() < MIN_FUZZY_RATIO):
                index += 1
                continue

            ratio = matcher.ratio()
            if ratio >= MIN_FUZZY_RATIO:
                logger.debug("Dictionary: %r -> %r (%.2f)", candidate, term, ratio)
                tokens[start:end + 1] = [_match_case(candidate, term)]
                tokens_changed = end - start
                word_positions = [i for i, tok in enumerate(tokens)
                                  if tok.isalnum() or "_" in tok]
                index = max(0, index - tokens_changed)
                continue
            index += 1

    return "".join(tokens)


# ---------------------------------------------------------------------------
# Auto-learn
# ---------------------------------------------------------------------------
def learn_from_correction(original, corrected, enabled=True):
    """Notice that the user retyped a word we produced, and remember the fix.

    Only single-word substitutions that are *similar* to what we emitted count —
    that similarity is what distinguishes "fixed my spelling" from "rewrote the
    sentence", and it is the difference between a dictionary that gets smarter
    and one that fills up with noise.
    """
    if not enabled or not original or not corrected:
        return []

    original_words = re.findall(r"[\w'-]+", original)
    corrected_words = re.findall(r"[\w'-]+", corrected)
    learned = []

    matcher = difflib.SequenceMatcher(None,
                                      [w.lower() for w in original_words],
                                      [w.lower() for w in corrected_words])
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "replace" or (i2 - i1) != 1 or (j2 - j1) != 1:
            continue
        was, now = original_words[i1], corrected_words[j1]
        if len(now) < 3 or was.lower() == now.lower():
            continue
        ratio = difflib.SequenceMatcher(None, was.lower(), now.lower()).ratio()
        if ratio < 0.55 or ratio >= 1.0:
            continue      # too different to be a spelling fix, or identical
        if find(now):
            continue
        ok, _ = add(now, sounds_like=[was], auto=True)
        if ok:
            learned.append(now)

    if learned:
        logger.info("Dictionary auto-learned: %s", ", ".join(learned))
    return learned

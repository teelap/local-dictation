"""Pluggable LLM backends used by the AI formatting and voice-command layers.

The app is local-first: the default provider is ``off``, which means every
feature that can degrade to deterministic rules does so and nothing ever leaves
the machine. Users who want Wispr-Flow-grade cleanup can point this at a local
Ollama instance (still fully offline), any OpenAI-compatible server such as
LM Studio or llama.cpp, or a hosted API.

Every entry point is synchronous, bounded by a timeout, and raises
:class:`LLMError` on failure — callers are expected to fall back to the
rule-based path rather than surfacing an error to the user mid-dictation.
"""

import json
import logging
import os
import threading
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

# Provider identifiers, in the order they are presented in the settings UI.
PROVIDER_OFF = "off"
PROVIDER_OLLAMA = "ollama"
PROVIDER_OPENAI = "openai"
PROVIDER_ANTHROPIC = "anthropic"

PROVIDERS = [PROVIDER_OFF, PROVIDER_OLLAMA, PROVIDER_OPENAI, PROVIDER_ANTHROPIC]

PROVIDER_LABELS = {
    PROVIDER_OFF: "Off — rules only (fully offline)",
    PROVIDER_OLLAMA: "Ollama — local model (offline)",
    PROVIDER_OPENAI: "OpenAI-compatible endpoint",
    PROVIDER_ANTHROPIC: "Anthropic (Claude)",
}

# Suggested models per provider. These populate an editable combobox, so the
# user is never limited to what is listed here.
SUGGESTED_MODELS = {
    PROVIDER_OLLAMA: ["llama3.2:3b", "llama3.1:8b", "qwen2.5:7b", "mistral:7b", "phi4"],
    PROVIDER_OPENAI: ["gpt-4o-mini", "local-model"],
    PROVIDER_ANTHROPIC: [
        "claude-opus-5",
        "claude-sonnet-5",
        "claude-haiku-4-5",
    ],
}

DEFAULT_MODELS = {
    PROVIDER_OLLAMA: "llama3.2:3b",
    PROVIDER_OPENAI: "gpt-4o-mini",
    PROVIDER_ANTHROPIC: "claude-opus-5",
}

DEFAULT_OLLAMA_HOST = "http://localhost:11434"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"

# Anthropic models that accept output_config.effort and server-side fallbacks.
# Sending either to a model that does not support it is a 400, so gate on these.
_ANTHROPIC_EFFORT_MODELS = (
    "claude-fable-5", "claude-mythos-5", "claude-opus-5", "claude-opus-4-8",
    "claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-5", "claude-sonnet-4-6",
)
_ANTHROPIC_FALLBACK_MODELS = ("claude-fable-5", "claude-mythos-5", "claude-opus-5")


class LLMError(Exception):
    """Raised when a provider is unreachable, misconfigured, or errors out."""


# ---------------------------------------------------------------------------
# Config access
# ---------------------------------------------------------------------------
def get_provider(config):
    return (config.get("llm_provider") or PROVIDER_OFF).strip().lower()


def is_enabled(config):
    """True when a provider other than 'off' is selected."""
    return get_provider(config) != PROVIDER_OFF


def get_model(config):
    provider = get_provider(config)
    model = (config.get("llm_model") or "").strip()
    return model or DEFAULT_MODELS.get(provider, "")


def get_api_key(config):
    """Resolve the API key, preferring the environment over config.json.

    Keeping the key in an environment variable means it never has to be written
    to disk in plaintext, which matters because config.json sits next to the
    executable.
    """
    provider = get_provider(config)
    env_names = {
        PROVIDER_ANTHROPIC: ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
        PROVIDER_OPENAI: ("OPENAI_API_KEY",),
    }.get(provider, ())
    for name in env_names:
        value = os.environ.get(name)
        if value:
            return value.strip()
    return (config.get("llm_api_key") or "").strip()


def get_base_url(config):
    provider = get_provider(config)
    explicit = (config.get("llm_base_url") or "").strip().rstrip("/")
    if explicit:
        return explicit
    if provider == PROVIDER_OLLAMA:
        return DEFAULT_OLLAMA_HOST
    if provider == PROVIDER_OPENAI:
        return DEFAULT_OPENAI_BASE_URL
    return ""


def get_timeout(config):
    try:
        return max(1.0, float(config.get("llm_timeout_seconds", 12.0)))
    except (TypeError, ValueError):
        return 12.0


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------
def _post_json(url, payload, headers=None, timeout=12.0):
    """POST JSON and return the decoded response body.

    Uses urllib rather than requests so the LLM layer adds no dependency of its
    own — the feature stays usable in a minimal install.
    """
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")[:400]
        except Exception:
            pass
        raise LLMError(f"HTTP {e.code} from {url}: {body or e.reason}") from e
    except urllib.error.URLError as e:
        raise LLMError(f"Could not reach {url}: {e.reason}") from e
    except (TimeoutError, OSError) as e:
        raise LLMError(f"Request to {url} failed: {e}") from e
    except json.JSONDecodeError as e:
        raise LLMError(f"Malformed JSON from {url}: {e}") from e


def _get_json(url, headers=None, timeout=8.0):
    req = urllib.request.Request(url, method="GET")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise LLMError(f"HTTP {e.code} from {url}: {e.reason}") from e
    except urllib.error.URLError as e:
        raise LLMError(f"Could not reach {url}: {e.reason}") from e
    except (TimeoutError, OSError, json.JSONDecodeError) as e:
        raise LLMError(f"Request to {url} failed: {e}") from e


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------
def _complete_ollama(system, user, config, max_tokens, timeout):
    url = f"{get_base_url(config)}/api/chat"
    payload = {
        "model": get_model(config),
        "stream": False,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "options": {
            "temperature": 0.1,   # cleanup is a rewrite, not a creative task
            "num_predict": max_tokens,
        },
    }
    data = _post_json(url, payload, timeout=timeout)
    text = (data.get("message") or {}).get("content", "")
    if not text:
        raise LLMError("Ollama returned an empty response")
    return text.strip()


def _complete_openai(system, user, config, max_tokens, timeout):
    url = f"{get_base_url(config)}/chat/completions"
    key = get_api_key(config)
    headers = {}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    payload = {
        "model": get_model(config),
        "temperature": 0.1,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    data = _post_json(url, payload, headers=headers, timeout=timeout)
    choices = data.get("choices") or []
    if not choices:
        raise LLMError("OpenAI-compatible endpoint returned no choices")
    text = (choices[0].get("message") or {}).get("content", "")
    if not text:
        raise LLMError("OpenAI-compatible endpoint returned empty content")
    return text.strip()


def _complete_anthropic(system, user, config, max_tokens, timeout):
    try:
        import anthropic
    except ImportError as e:
        raise LLMError(
            "The 'anthropic' package is not installed. Run: pip install anthropic"
        ) from e

    key = get_api_key(config)
    client = anthropic.Anthropic(api_key=key, timeout=timeout) if key \
        else anthropic.Anthropic(timeout=timeout)

    model = get_model(config)
    kwargs = dict(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    # Cleanup is latency-sensitive, so ask for the shallowest thinking the model
    # supports rather than disabling thinking outright (disabling it on Opus 5
    # can leak internal tags into the visible response).
    if model.startswith(_ANTHROPIC_EFFORT_MODELS):
        kwargs["output_config"] = {"effort": "low"}

    use_fallbacks = model.startswith(_ANTHROPIC_FALLBACK_MODELS)
    try:
        if use_fallbacks:
            response = client.beta.messages.create(
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                **kwargs,
            )
        else:
            response = client.messages.create(**kwargs)
    except anthropic.BadRequestError:
        # A model we did not recognise may reject effort/fallbacks. Retry once
        # with the plain request shape before giving up.
        kwargs.pop("output_config", None)
        try:
            response = client.messages.create(**kwargs)
        except Exception as e:  # noqa: BLE001 — surfaced to the caller as LLMError
            raise LLMError(f"Anthropic request failed: {e}") from e
    except Exception as e:  # noqa: BLE001
        raise LLMError(f"Anthropic request failed: {e}") from e

    if getattr(response, "stop_reason", None) == "refusal":
        raise LLMError("Model declined to process this text")

    parts = [b.text for b in response.content if getattr(b, "type", None) == "text"]
    text = "".join(parts).strip()
    if not text:
        raise LLMError("Anthropic returned an empty response")
    return text


_DISPATCH = {
    PROVIDER_OLLAMA: _complete_ollama,
    PROVIDER_OPENAI: _complete_openai,
    PROVIDER_ANTHROPIC: _complete_anthropic,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def complete(system, user, config, max_tokens=1024, timeout=None):
    """Run a single completion. Raises LLMError; never returns an empty string."""
    provider = get_provider(config)
    if provider == PROVIDER_OFF:
        raise LLMError("No LLM provider is configured")

    handler = _DISPATCH.get(provider)
    if handler is None:
        raise LLMError(f"Unknown LLM provider: {provider}")

    if timeout is None:
        timeout = get_timeout(config)

    logger.debug("LLM request via %s (%s), max_tokens=%d", provider, get_model(config), max_tokens)
    text = handler(system, user, config, max_tokens, timeout)
    logger.debug("LLM response: %s", text[:200])
    return text


def complete_or_none(system, user, config, max_tokens=1024, timeout=None):
    """Like :func:`complete` but returns None instead of raising.

    This is what the dictation hot path uses: a failing LLM must degrade to the
    rule-based result rather than losing the user's words.
    """
    try:
        return complete(system, user, config, max_tokens=max_tokens, timeout=timeout)
    except LLMError as e:
        logger.warning("LLM call failed, falling back to rules: %s", e)
        return None


def list_models(config):
    """Best-effort model discovery for the settings UI.

    Only Ollama is queried live — it is local, fast, and the list is genuinely
    per-machine. Other providers fall back to the suggested list.
    """
    provider = get_provider(config)
    if provider == PROVIDER_OLLAMA:
        try:
            data = _get_json(f"{get_base_url(config)}/api/tags", timeout=4.0)
            names = [m.get("name") for m in data.get("models", []) if m.get("name")]
            if names:
                return sorted(names)
        except LLMError as e:
            logger.debug("Ollama model listing failed: %s", e)
    return list(SUGGESTED_MODELS.get(provider, []))


def test_connection(config):
    """Round-trip the configured provider. Returns (ok: bool, message: str)."""
    provider = get_provider(config)
    if provider == PROVIDER_OFF:
        return True, "AI cleanup is off — using rules only."

    model = get_model(config)
    if not model:
        return False, "No model selected."
    if provider in (PROVIDER_ANTHROPIC,) and not get_api_key(config):
        return False, "No API key. Set ANTHROPIC_API_KEY or paste a key below."

    try:
        reply = complete(
            "You are a text utility. Reply with exactly one word.",
            "Reply with the single word: ok",
            config,
            max_tokens=2048,
            timeout=min(get_timeout(config), 20.0),
        )
    except LLMError as e:
        return False, str(e)
    return True, f"Connected to {model} — replied {reply[:40]!r}"


def warm_up(config):
    """Fire a tiny request in the background so the first dictation is not slow.

    Local models in particular pay a multi-second load cost on first token; doing
    it at startup keeps that out of the user's way.
    """
    if not is_enabled(config):
        return

    def _run():
        try:
            complete("You are a text utility.", "ok", config, max_tokens=1024, timeout=60.0)
            logger.info("LLM warm-up complete (%s)", get_model(config))
        except LLMError as e:
            logger.info("LLM warm-up skipped: %s", e)

    threading.Thread(target=_run, daemon=True, name="llm-warmup").start()

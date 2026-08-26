"""Configuration: defaults, disk persistence, and migration from older layouts.

Config grew from a flat dictionary of nine keys into a nested one covering
hotkeys, formatting, context, audio, and the LLM backend. :func:`load_config`
migrates the old flat shape forward, so an existing install keeps its hotkey and
model choice instead of silently reverting to defaults on first launch.
"""

import copy
import json
import logging
import os
import sys

logger = logging.getLogger(__name__)

CONFIG_VERSION = 2


def _get_app_dir():
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        # PyInstaller frozen build: use the directory containing the .exe
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


APP_DIR = _get_app_dir()
CONFIG_FILE = os.path.join(APP_DIR, "config.json")

DEFAULT_CONFIG = {
    "config_version": CONFIG_VERSION,
    "first_run_complete": False,

    # ---- Hotkeys -------------------------------------------------------
    "hotkeys": {
        # Hold to dictate. Ctrl+Win is the Windows convention for this and
        # collides with almost nothing.
        "push_to_talk": "ctrl+windows",
        # Toggle-style hands-free dictation. Optional: double-tapping
        # push_to_talk latches too, so this can be left blank.
        "hands_free": "ctrl+windows+space",
        # Speak an instruction about the selected text.
        "command_mode": "ctrl+windows+alt",
        # Re-insert the most recent transcript.
        "paste_last": "shift+alt+z",
        # Open the scratchpad.
        "scratchpad": "windows+alt+s",
        # Cancel an in-flight dictation.
        "cancel": "esc",
    },
    # Hold shorter than this reads as an accidental tap and cancels silently.
    "min_hold_seconds": 0.35,
    # Two taps inside this window latch into hands-free mode.
    "double_tap_seconds": 0.4,

    # ---- Model ---------------------------------------------------------
    "model_size": "base.en",
    "device": "auto",
    "compute_type": None,
    # Narrowing the pool is what buys accuracy — an open set of 100 languages
    # is a far harder classification than a set of two.
    "language": None,
    "language_pool": ["en"],

    # ---- Audio ---------------------------------------------------------
    "audio": {
        "device_index": None,
        "sample_rate": 16000,
        "silence_threshold_seconds": 0.0,     # 0 disables auto-stop
        "silence_rms": 0.003,
        "max_session_seconds": 1200,          # 20 minutes, then end gracefully
        "warn_before_limit_seconds": 60,
        "no_audio_warn_seconds": 5,
        "mic_dead_warn_seconds": 15,
    },

    # ---- Text output ---------------------------------------------------
    "output": {
        "paste_mode": "clipboard",            # "clipboard" or "type"
        "restore_clipboard": True,
        "clipboard_restore_delay": 0.25,
        "trailing_space": True,               # the single source of truth
        "trailing_actions": True,             # honour "press enter"
    },

    # ---- Formatting ----------------------------------------------------
    "formatting": {
        "cleanup_level": "medium",            # none | light | medium | high
        "custom_instructions": "",
        "writing_samples": [],
    },
    "word_substitutions": {},

    # ---- Context awareness ---------------------------------------------
    "context": {
        "enabled": True,
        "app_overrides": {},                  # exe name -> category
        "category_styles": {},                # category -> style
        "category_overrides": {},             # category -> formatter overrides
    },

    # ---- Vocabulary ----------------------------------------------------
    "dictionary": {
        "enabled": True,
        "bias_decoding": True,                # feed terms to Whisper
        "auto_learn": True,                   # learn from typed corrections
    },
    "snippets_enabled": True,

    # ---- LLM backend ---------------------------------------------------
    "llm_provider": "off",                    # off | ollama | openai | anthropic
    "llm_model": "",
    "llm_base_url": "",
    "llm_api_key": "",
    "llm_timeout_seconds": 12.0,
    "llm_warm_up": True,

    # ---- Interface -----------------------------------------------------
    "ui": {
        "show_overlay": True,
        "overlay_dock": "bottom",             # bottom | left | right
        "overlay_hidden_until": 0.0,          # epoch seconds for "hide for 1 hour"
        "theme": "light",                     # light | dark
        "sounds": True,
        "sound_volume": 0.5,
        "notifications": {
            "permissions": True,
            "session_limits": True,
            "errors": True,
            "tips": True,
        },
    },

    # ---- System --------------------------------------------------------
    "launch_at_startup": False,
    "history_persist": True,
    "history_retention_days": 0,              # 0 keeps everything
    "typing_wpm_baseline": 40,
    "log_level": "INFO",
}

# Old flat key -> path in the new nested config.
_MIGRATION_MAP = {
    "trigger_hotkey": ("hotkeys", "push_to_talk"),
    "paste_mode": ("output", "paste_mode"),
    "silence_threshold_seconds": ("audio", "silence_threshold_seconds"),
    "audio_device_index": ("audio", "device_index"),
}


def _deep_merge(defaults, overrides):
    """Merge saved values over defaults, recursing into nested dicts.

    Free-form maps (substitutions, per-app overrides) are replaced wholesale
    rather than merged — merging them would make a deleted entry immortal.
    """
    result = copy.deepcopy(defaults)
    for key, value in (overrides or {}).items():
        if key not in result:
            result[key] = value
        elif isinstance(result[key], dict) and isinstance(value, dict):
            if key in ("word_substitutions", "app_overrides", "category_styles",
                       "category_overrides", "notifications"):
                result[key] = {**result[key], **value} if key == "notifications" else value
            else:
                result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _migrate(data):
    """Bring a pre-v2 config forward without losing the user's choices."""
    if data.get("config_version", 1) >= CONFIG_VERSION:
        return data, False

    logger.info("Migrating config from version %s to %s",
                data.get("config_version", 1), CONFIG_VERSION)
    migrated = dict(data)

    for old_key, (section, new_key) in _MIGRATION_MAP.items():
        if old_key in migrated:
            migrated.setdefault(section, {})
            migrated[section].setdefault(new_key, migrated.pop(old_key))

    # The old app had a single hotkey and a toggle/push-to-talk switch. Map that
    # onto the new two-binding model so muscle memory survives the upgrade.
    interaction = migrated.pop("interaction_mode", None)
    hotkey = (migrated.get("hotkeys") or {}).get("push_to_talk")
    if interaction == "toggle" and hotkey:
        migrated.setdefault("hotkeys", {})["hands_free"] = hotkey
        migrated["hotkeys"]["push_to_talk"] = ""

    migrated.pop("show_history", None)
    # An existing config means an existing user; do not put them through the
    # first-run wizard on upgrade.
    migrated.setdefault("first_run_complete", True)
    migrated["config_version"] = CONFIG_VERSION
    return migrated, True


def load_config():
    """Load config from disk, applying defaults and migrations."""
    if not os.path.exists(CONFIG_FILE):
        config = copy.deepcopy(DEFAULT_CONFIG)
        save_config(config)
        return config

    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.error("Could not read config, using defaults: %s", e)
        return copy.deepcopy(DEFAULT_CONFIG)

    if not isinstance(data, dict):
        logger.error("Config is not an object, using defaults")
        return copy.deepcopy(DEFAULT_CONFIG)

    data, migrated = _migrate(data)
    config = _deep_merge(DEFAULT_CONFIG, data)

    if migrated or config != data:
        save_config(config)
    return config


def save_config(config_data):
    """Write config to disk atomically, so a crash cannot truncate it."""
    temp_path = CONFIG_FILE + ".tmp"
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(config_data, f, indent=4)
        os.replace(temp_path, CONFIG_FILE)
        return True
    except OSError as e:
        logger.error("Could not save config: %s", e)
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass
        return False


def get_value(config, path, default=None):
    """Read a dotted path such as "audio.device_index"."""
    node = config
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def set_value(config, path, value):
    """Write a dotted path, creating intermediate dicts as needed."""
    parts = path.split(".")
    node = config
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value
    return config

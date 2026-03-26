import json
import os
import sys

# Anchor config file to the app directory, PyInstaller-aware
def _get_app_dir():
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        # PyInstaller frozen build: use the directory containing the .exe
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

APP_DIR = _get_app_dir()
CONFIG_FILE = os.path.join(APP_DIR, "config.json")

DEFAULT_CONFIG = {
    "trigger_hotkey": "ctrl+shift+f12",
    "model_size": "base.en",
    "device": "auto",
    "language": None,
    "paste_mode": "clipboard",
    "interaction_mode": "toggle",
    "silence_threshold_seconds": 3.0,
    "audio_device_index": None,
    "word_substitutions": {},
    "launch_at_startup": False,
    "show_history": True,
    "history_persist": True
}

def load_config():
    if not os.path.exists(CONFIG_FILE):
        save_config(DEFAULT_CONFIG)
        return dict(DEFAULT_CONFIG)

    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Merge in any missing keys from defaults
        updated = False
        for k, v in DEFAULT_CONFIG.items():
            if k not in data:
                data[k] = v
                updated = True
        if updated:
            save_config(data)
        return data
    except Exception as e:
        print(f"Error loading config: {e}")
        return dict(DEFAULT_CONFIG)

def save_config(config_data):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config_data, f, indent=4)
    except Exception as e:
        print(f"Error saving config: {e}")

def get_config_value(key, default=None):
    config = load_config()
    return config.get(key, default if default is not None else DEFAULT_CONFIG.get(key))

def set_config_value(key, value):
    config = load_config()
    config[key] = value
    save_config(config)

# Convenience helpers kept for backward compat
def get_hotkey():
    return get_config_value("trigger_hotkey", "ctrl+shift+f12")

def set_hotkey(new_hotkey):
    set_config_value("trigger_hotkey", new_hotkey)

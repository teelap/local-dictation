"""LocalDictation — main entry point.

Architecture: all state lives in DictationApp. Modules are pure functions/classes
that the app wires together.
"""

import os
import sys
import time
import threading
import logging
import logging.handlers

# ---------------------------------------------------------------------------
# Logging setup — must happen before any other imports that use logging
# ---------------------------------------------------------------------------
def _setup_logging(app_dir, level_name="INFO"):
    log_path = os.path.join(app_dir, "dictation.log")
    level = getattr(logging, level_name.upper(), logging.INFO)
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)  # capture everything; handlers filter

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")

    # Rotating file handler — 1 MB, 2 backups
    fh = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=1_048_576, backupCount=2, encoding="utf-8")
    fh.setLevel(level)
    fh.setFormatter(fmt)
    root_logger.addHandler(fh)

    # Also capture unhandled exceptions
    def _exc_hook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        logging.getLogger("main").critical("Unhandled exception", exc_info=(exc_type, exc_value, exc_tb))

    sys.excepthook = _exc_hook


# Determine app dir before imports so config paths are correct
def _get_app_dir():
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


APP_DIR = _get_app_dir()
_setup_logging(APP_DIR)
logger = logging.getLogger("main")

# ---------------------------------------------------------------------------
# Application imports
# ---------------------------------------------------------------------------
import keyboard
import pyperclip
import pyautogui
import winsound
import winreg

from config_manager import load_config, save_config, APP_DIR as CFG_APP_DIR
import audio as audio_mod
import transcription as transcription_mod
from tray import TrayIcon, STATE_LOADING, STATE_IDLE, STATE_RECORDING, STATE_TRANSCRIBING
from settings_ui import open_settings
import history as history_mod
from overlay import RecordingOverlay

# ---------------------------------------------------------------------------
# Voice command map
# ---------------------------------------------------------------------------
VOICE_COMMANDS = {
    "new line":          ("\n", "type"),
    "new paragraph":     ("\n\n", "type"),
    "delete that":       ("ctrl+z", "hotkey"),
    "scratch that":      ("ctrl+z", "hotkey"),
    "period":            (".", "type"),
    "full stop":         (".", "type"),
    "comma":             (",", "type"),
    "question mark":     ("?", "type"),
    "exclamation mark":  ("!", "type"),
    "exclamation point": ("!", "type"),
    "open bracket":      ("(", "type"),
    "close bracket":     (")", "type"),
    "open parenthesis":  ("(", "type"),
    "close parenthesis": (")", "type"),
    "open brace":        ("{", "type"),
    "close brace":       ("}", "type"),
    "colon":             (":", "type"),
    "semicolon":         (";", "type"),
    "hyphen":            ("-", "type"),
    "dash":              ("-", "type"),
    "tab key":           ("\t", "type"),
}


class DictationApp:
    """All application state and logic in one place."""

    def __init__(self):
        self.config = load_config()
        self._toggle_lock = threading.Lock()
        self._model_ready = threading.Event()
        self._is_recording = False
        self._recording_start_time = None
        self._hotkey_handle = None
        self._ptt_active = False  # push-to-talk state
        self.tray: TrayIcon = None

        # Hidden Tk root for after() scheduling (history window needs it)
        self._tk_root = None

        history_mod.init(APP_DIR, persist=self.config.get("history_persist", True))
        self.overlay = RecordingOverlay()

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------
    def start(self):
        logger.info("--- LocalDictation starting ---")

        # Start tray icon in background thread FIRST so it's visible immediately
        self.tray = TrayIcon(
            on_quit=self.quit,
            on_settings=self._open_settings_threaded,
            on_history=self._open_history_threaded,
        )
        tray_thread = threading.Thread(target=self.tray.run, daemon=True, name="tray")
        tray_thread.start()

        # Apply startup registration if configured
        self._apply_startup_setting()

        # Load model in background; hotkey only activates once ready
        model_thread = threading.Thread(target=self._load_model, daemon=True, name="model-loader")
        model_thread.start()

        # Block main thread keeping keyboard listener alive
        keyboard.wait()

    def _load_model(self):
        cfg = self.config
        model_size = cfg.get("model_size", "base.en")
        device = cfg.get("device", "auto")
        log_level = cfg.get("log_level", "INFO")

        # Update logging level from config
        logging.getLogger().handlers[0].setLevel(getattr(logging, log_level.upper(), logging.INFO))

        try:
            transcription_mod.init_model(model_size=model_size, device=device)
        except Exception as e:
            logger.error("Model loading failed: %s", e)
            self.tray.notify("LocalDictation Error", f"Model failed to load: {e}")
            return

        self._model_ready.set()
        self._register_hotkey()
        self.tray.set_state(STATE_IDLE)

        hotkey = self.config.get("trigger_hotkey", "ctrl+shift+f12")
        self.tray.notify("LocalDictation Ready",
                         f"Press {hotkey} to start dictating.")
        logger.info("Model ready. App is listening.")

    # ------------------------------------------------------------------
    # Hotkey registration
    # ------------------------------------------------------------------
    def _register_hotkey(self):
        hotkey = self.config.get("trigger_hotkey", "ctrl+shift+f12")
        interaction = self.config.get("interaction_mode", "toggle")
        logger.info("Registering hotkey '%s' (mode=%s)", hotkey, interaction)

        # Remove previous binding if any
        self._unregister_hotkey()

        if interaction == "push_to_talk":
            keyboard.on_press_key(hotkey.split("+")[-1], self._ptt_press, suppress=True)
            keyboard.on_release_key(hotkey.split("+")[-1], self._ptt_release, suppress=True)
            self._hotkey_handle = hotkey  # store the key name for cleanup
        else:
            self._hotkey_handle = keyboard.add_hotkey(hotkey, self._on_toggle, suppress=True)

    def _unregister_hotkey(self):
        if self._hotkey_handle is not None:
            try:
                if isinstance(self._hotkey_handle, str):
                    # PTT mode stored the key string
                    keyboard.unhook_all_hotkeys()
                else:
                    keyboard.remove_hotkey(self._hotkey_handle)
            except Exception as e:
                logger.warning("Error removing hotkey: %s", e)
            self._hotkey_handle = None

    # ------------------------------------------------------------------
    # Toggle (default mode)
    # ------------------------------------------------------------------
    def _on_toggle(self):
        if not self._model_ready.is_set():
            self.tray.notify("LocalDictation", "Model is still loading, please wait.")
            return
        # Dispatch to a worker thread immediately so the keyboard hook
        # callback returns fast — Windows kills low-level hooks that block.
        threading.Thread(target=self._toggle_worker, daemon=True, name="toggle").start()

    def _toggle_worker(self):
        if not self._toggle_lock.acquire(blocking=False):
            return  # Already processing
        try:
            if not self._is_recording:
                self._start_recording()
            else:
                self._stop_and_transcribe()
        finally:
            self._toggle_lock.release()

    # ------------------------------------------------------------------
    # Push-to-talk
    # ------------------------------------------------------------------
    def _ptt_press(self, event):
        if not self._model_ready.is_set():
            return
        if not self._ptt_active and not self._is_recording:
            self._ptt_active = True
            self._start_recording()

    def _ptt_release(self, event):
        if self._ptt_active and self._is_recording:
            self._ptt_active = False
            self._stop_and_transcribe()

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------
    def _start_recording(self):
        logger.info("Recording started")
        self._is_recording = True
        self._recording_start_time = time.monotonic()
        self.tray.set_state(STATE_RECORDING)
        self.overlay.show_recording()
        winsound.Beep(800, 100)  # start beep

        silence_secs = self.config.get("silence_threshold_seconds", 3.0)
        device_index = self.config.get("audio_device_index", None)

        on_silence = None
        if silence_secs and silence_secs > 0:
            on_silence = self._on_silence_triggered

        audio_mod.start_recording(
            sample_rate=16000,
            channels=1,
            device_index=device_index,
            silence_threshold_seconds=silence_secs,
            on_silence_callback=on_silence,
        )

    def _on_silence_triggered(self):
        """Called from silence-monitor background thread."""
        if not self._toggle_lock.acquire(blocking=False):
            return  # toggle_worker already handling stop
        try:
            if self._is_recording:
                logger.info("Auto-stop: silence detected")
                self._stop_and_transcribe()
        finally:
            self._toggle_lock.release()

    def _stop_and_transcribe(self):
        if not self._is_recording:
            return

        self._is_recording = False
        duration = time.monotonic() - (self._recording_start_time or time.monotonic())
        winsound.Beep(600, 100)  # stop beep
        self.tray.set_state(STATE_TRANSCRIBING)
        self.overlay.show_transcribing()
        logger.info("Recording stopped (%.1fs). Transcribing...", duration)

        wav_path = audio_mod.stop_recording_and_save(output_file=None, sample_rate=16000)
        if not wav_path:
            logger.warning("No audio captured")
            self.tray.set_state(STATE_IDLE)
            self.overlay.hide()
            return

        language = self.config.get("language", None)
        text = transcription_mod.transcribe_audio(wav_path, language=language)

        if text and text.strip():
            processed = self._process_text(text.strip())
            if processed is not None:
                self._output_text(processed)
                history_mod.add_entry(text.strip(), duration)
        else:
            logger.info("No speech detected")

        self.tray.set_state(STATE_IDLE)
        self.overlay.hide()

    # ------------------------------------------------------------------
    # Text processing pipeline
    # ------------------------------------------------------------------
    def _process_text(self, text):
        """Apply substitutions, then check for voice commands.

        Returns final text to output, or None if a command was handled inline.
        """
        # 1. Word substitutions
        subs = self.config.get("word_substitutions", {})
        for find, replace in subs.items():
            text = text.replace(find, replace)

        # 2. Voice commands (whole-text match, case-insensitive)
        lower = text.lower().strip().rstrip(".,!?")
        if lower in VOICE_COMMANDS:
            action, action_type = VOICE_COMMANDS[lower]
            logger.info("Voice command: '%s' -> %s '%s'", lower, action_type, action)
            if action_type == "hotkey":
                pyautogui.hotkey(*action.split("+"))
            else:
                self._paste_or_type(action)
            return None  # signal: already handled

        return text

    def _output_text(self, text):
        """Paste or type the text into the active window."""
        paste_mode = self.config.get("paste_mode", "clipboard")
        if paste_mode == "type":
            pyautogui.write(text + " ", interval=0.01)
        else:
            self._paste_or_type(text + " ")

    def _paste_or_type(self, text):
        try:
            pyperclip.copy(text)
            pyautogui.hotkey("ctrl", "v")
        except Exception as e:
            logger.warning("Clipboard paste failed, falling back to type: %s", e)
            pyautogui.write(text, interval=0.01)

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------
    def _open_settings_threaded(self):
        def _run():
            open_settings(self._on_settings_saved)
        threading.Thread(target=_run, daemon=True, name="settings-ui").start()

    def _on_settings_saved(self, new_config):
        logger.info("Settings saved — applying changes")
        self.config = new_config

        # Apply startup registration
        self._apply_startup_setting()

        # Re-register hotkey (mode or key may have changed)
        if self._model_ready.is_set():
            self._register_hotkey()

        # Note: model size/device changes require restart to take effect
        # We notify the user
        self.tray.notify("LocalDictation", "Settings saved. Model changes take effect on next restart.")

    def _open_history_threaded(self):
        def _run():
            # Tkinter windows need a root — create a hidden one if needed
            import tkinter as tk
            root = tk.Tk()
            root.withdraw()
            history_mod.open_history_window(root)
            root.mainloop()
        threading.Thread(target=_run, daemon=True, name="history-ui").start()

    # ------------------------------------------------------------------
    # Windows startup registry
    # ------------------------------------------------------------------
    def _apply_startup_setting(self):
        enabled = self.config.get("launch_at_startup", False)
        app_name = "LocalDictation"
        reg_path = r"Software\Microsoft\Windows\CurrentVersion\Run"

        if getattr(sys, 'frozen', False):
            exe_path = f'"{sys.executable}"'
        else:
            exe_path = f'"{sys.executable}" "{os.path.abspath(__file__)}"'

        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, reg_path, 0, winreg.KEY_SET_VALUE)
            if enabled:
                winreg.SetValueEx(key, app_name, 0, winreg.REG_SZ, exe_path)
                logger.info("Startup registry key set")
            else:
                try:
                    winreg.DeleteValue(key, app_name)
                    logger.info("Startup registry key removed")
                except FileNotFoundError:
                    pass  # already absent
            winreg.CloseKey(key)
        except Exception as e:
            logger.warning("Could not update startup registry: %s", e)

    # ------------------------------------------------------------------
    # Quit
    # ------------------------------------------------------------------
    def quit(self):
        logger.info("Application quitting")
        self._unregister_hotkey()
        os._exit(0)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    app = DictationApp()
    app.start()


if __name__ == "__main__":
    main()

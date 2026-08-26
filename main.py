"""LocalDictation — hold a key, talk, and get written text wherever you type.

Architecture: a single :class:`DictationApp` owns all state. Tk runs on the main
thread and owns every window (the overlay, the Hub, the onboarding wizard);
pystray and the keyboard hooks run on background threads; each dictation runs on
its own worker so a slow transcription never blocks the hotkey.

The pipeline, in order:

    audio -> whisper (biased by your dictionary) -> dictionary correction
          -> snippet expansion -> formatter (per-app style) -> injector

Every stage after transcription is optional and degrades to a no-op, so a
failure anywhere still ends with your words in the text field.
"""

import logging
import logging.handlers
import os
import sys
import threading
import time


# ---------------------------------------------------------------------------
# Paths and logging — configured before anything else imports logging
# ---------------------------------------------------------------------------
def _get_app_dir():
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


APP_DIR = _get_app_dir()


def _setup_logging(app_dir, level_name="INFO"):
    log_path = os.path.join(app_dir, "dictation.log")
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S")

    handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=1_048_576, backupCount=2, encoding="utf-8")
    handler.setLevel(getattr(logging, level_name.upper(), logging.INFO))
    handler.setFormatter(formatter)
    root_logger.addHandler(handler)

    def _excepthook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        logging.getLogger("main").critical(
            "Unhandled exception", exc_info=(exc_type, exc_value, exc_tb))

    sys.excepthook = _excepthook
    return handler


_log_handler = _setup_logging(APP_DIR)
logger = logging.getLogger("main")

# ---------------------------------------------------------------------------
import tkinter as tk

import audio as audio_mod
import context as context_mod
import dictionary as dictionary_mod
import formatter as formatter_mod
import history as history_mod
import injector
import llm
import snippets as snippets_mod
import stats as stats_mod
import transcription as transcription_mod
import transforms as transforms_mod
from config_manager import load_config, save_config
from overlay import RecordingOverlay
from tray import (STATE_COMMAND, STATE_ERROR, STATE_IDLE, STATE_PAUSED,
                  STATE_RECORDING, STATE_TRANSCRIBING, TrayIcon)

# Modes a session can be in.
MODE_DICTATION = "dictation"
MODE_COMMAND = "command"


class DictationApp:
    """All application state and the dictation pipeline."""

    def __init__(self):
        self.config = load_config()
        self._apply_log_level()

        self._session_lock = threading.Lock()
        self._model_ready = threading.Event()
        self._recording = False
        self._processing = False
        self._paused = False
        self._mode = MODE_DICTATION
        self._session_started = 0.0
        self._latched = False           # hands-free lock during a PTT session
        self._press_time = 0.0
        self._last_tap_time = 0.0
        self._pending_selection = ""
        self._cancelled = False

        self._hooks = []
        self.tray = None
        self.overlay = None
        self.hub = None
        self.root = None

        history_mod.init(APP_DIR,
                         persist=self.config.get("history_persist", True),
                         retention_days=self.config.get("history_retention_days", 0))
        dictionary_mod.init(APP_DIR)
        snippets_mod.init(APP_DIR)
        stats_mod.init(APP_DIR)
        transforms_mod.init(APP_DIR)

    def _apply_log_level(self):
        level = self.config.get("log_level", "INFO")
        _log_handler.setLevel(getattr(logging, str(level).upper(), logging.INFO))

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------
    def start(self):
        logger.info("--- LocalDictation starting ---")

        # One Tk root for the whole process, kept hidden. Every window is a
        # Toplevel of it; a second root makes teardown unpredictable.
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("LocalDictation")

        self.overlay = RecordingOverlay(
            self.root, config=self.config,
            on_stop=self.stop_from_ui, on_cancel=self.cancel,
            on_settings=lambda: self.open_hub("settings"))

        self.tray = TrayIcon(callbacks={
            "open_hub": lambda: self.open_hub("home"),
            "open_settings": lambda: self.open_hub("settings"),
            "open_history": lambda: self.open_hub("home"),
            "open_insights": lambda: self.open_hub("insights"),
            "paste_last": self.paste_last_transcript,
            "toggle_pause": self.toggle_pause,
            "set_microphone": self.set_microphone,
            "quit": self.quit,
        }, config=self.config)
        threading.Thread(target=self.tray.run, daemon=True, name="tray").start()

        self._apply_startup_setting()

        threading.Thread(target=self._load_model, daemon=True, name="model-loader").start()

        if self.config.get("llm_warm_up", True):
            llm.warm_up(self.config)

        if not self.config.get("first_run_complete"):
            self.root.after(600, self._show_onboarding)

        self.root.mainloop()

    def _load_model(self):
        try:
            info = transcription_mod.init_model(
                model_size=self.config.get("model_size", "base.en"),
                device=self.config.get("device", "auto"),
                compute_type=self.config.get("compute_type"))
        except Exception as e:  # noqa: BLE001
            logger.exception("Model loading failed: %s", e)
            self.tray.set_state(STATE_ERROR)
            self.tray.notify("LocalDictation", f"Speech model failed to load: {e}")
            return

        self._model_ready.set()
        self._register_hotkeys()
        self.tray.set_state(STATE_IDLE)

        hotkey = (self.config.get("hotkeys") or {}).get("push_to_talk") or "your hotkey"
        self.tray.notify("LocalDictation is ready",
                         f"Hold {hotkey} anywhere and start talking.", category="tips")
        logger.info("Ready — model %s on %s", info.get("model_size"), info.get("device"))

    def model_ready(self):
        return self._model_ready.is_set()

    # ------------------------------------------------------------------
    # Hotkeys
    # ------------------------------------------------------------------
    def _register_hotkeys(self):
        try:
            import keyboard
        except ImportError as e:
            logger.error("The 'keyboard' package is unavailable: %s", e)
            self.tray.notify("LocalDictation",
                             "Global hotkeys are unavailable on this system.")
            return

        self._unregister_hotkeys()
        hotkeys = self.config.get("hotkeys") or {}

        def bind(binding, handler, trigger="down"):
            if not binding:
                return
            try:
                self._hooks.append(
                    keyboard.add_hotkey(binding, handler, suppress=False,
                                        trigger_on_release=(trigger == "up")))
            except Exception as e:  # noqa: BLE001 — an invalid binding must not abort the rest
                logger.warning("Could not bind %r: %s", binding, e)

        # Push-to-talk needs press *and* release, so it is hooked directly
        # rather than through add_hotkey, which only fires once per combination.
        ptt = hotkeys.get("push_to_talk")
        if ptt:
            try:
                self._hooks.append(
                    keyboard.add_hotkey(ptt, self._on_ptt_press, suppress=False))
                # The release edge is detected by watching the last key of the
                # combination; add_hotkey has no release callback.
                last_key = ptt.split("+")[-1].strip()
                keyboard.on_release_key(last_key, self._on_ptt_release)
                self._hooks.append(("release", last_key))
            except Exception as e:  # noqa: BLE001
                logger.warning("Could not bind push-to-talk %r: %s", ptt, e)

        bind(hotkeys.get("hands_free"), self._on_hands_free)
        bind(hotkeys.get("command_mode"), self._on_command_mode)
        bind(hotkeys.get("paste_last"), self.paste_last_transcript)
        bind(hotkeys.get("scratchpad"), lambda: self.open_hub("home"))
        bind(hotkeys.get("cancel", "esc"), self._on_cancel_key)

        for binding, name in transforms_mod.hotkey_bindings():
            bind(binding, lambda n=name: self.run_transform(n))

        logger.info("Hotkeys registered: %s", {k: v for k, v in hotkeys.items() if v})

    def _unregister_hotkeys(self):
        if not self._hooks:
            return
        try:
            import keyboard
            # unhook_all_hotkeys() only clears add_hotkey registrations; the
            # push-to-talk release handler is a key hook and would survive,
            # stacking a duplicate every time settings are saved.
            keyboard.unhook_all()
        except Exception as e:  # noqa: BLE001
            logger.debug("Could not unhook hotkeys: %s", e)
        self._hooks = []

    # ------------------------------------------------------------------
    # Hotkey handlers
    # ------------------------------------------------------------------
    def _on_ptt_press(self):
        if not self._ready_to_record():
            return

        now = time.monotonic()
        double_tap_window = self.config.get("double_tap_seconds", 0.4)

        # A second press inside the window latches recording on, so one key does
        # both hold-to-talk and hands-free. The first tap has usually already
        # been discarded as too short by then, so this has to latch whether or
        # not a session is still live — otherwise tap-then-hold never locks.
        if now - self._last_tap_time < double_tap_window:
            self._last_tap_time = 0.0
            self._latched = True
            if self._recording:
                logger.info("Live session latched to hands-free")
                return
            logger.info("Double tap — starting a latched session")
            self._press_time = now
            self._start_session(MODE_DICTATION)
            return

        self._last_tap_time = now

        if self._recording:
            return
        self._press_time = now
        self._start_session(MODE_DICTATION)

    def _is_prefix_echo(self):
        """True when a session was started microseconds ago by a shorter binding.

        The default bindings are nested — push-to-talk is Ctrl+Win, command mode
        is Ctrl+Win+Alt — and a hotkey library fires the shorter combination the
        moment its keys are down, before the extra key is even pressed. So
        reaching a superset handler with a brand-new session in flight means the
        prefix fired first, not that the user wants to stop.
        """
        if not self._recording:
            return False
        threshold = max(0.2, float(self.config.get("min_hold_seconds", 0.35)))
        return (time.monotonic() - self._session_started) < threshold

    def _on_ptt_release(self, _event=None):
        if not self._recording or self._latched:
            return

        held = time.monotonic() - self._press_time
        minimum = self.config.get("min_hold_seconds", 0.35)
        if held < minimum:
            # A jab at the key is an accident, not a dictation.
            logger.info("Hold too short (%.2fs) — cancelling", held)
            self._abort_session(silent=True)
            return

        self._finish_session()

    def _on_hands_free(self):
        if self._recording:
            # Ctrl+Win+Space arrives just after Ctrl+Win already started a
            # push-to-talk session; latch that session rather than ending it.
            if self._is_prefix_echo():
                self._latched = True
                logger.info("Hands-free latched the push-to-talk session")
                return
            self._latched = False
            self._finish_session()
            return
        if not self._ready_to_record():
            return
        self._latched = True
        self._start_session(MODE_DICTATION)

    def _on_command_mode(self):
        if self._recording:
            if self._is_prefix_echo() and self._mode == MODE_DICTATION:
                # Same nesting, but this session is the wrong mode. Throw away
                # the fraction of a second of audio and start a command session.
                logger.info("Converting a prefix-triggered session to command mode")
                self._abort_session(silent=True)
            else:
                self._latched = False
                self._finish_session()
                return
        if not self._ready_to_record():
            return
        self._latched = True
        self._start_session(MODE_COMMAND)

    def _on_cancel_key(self):
        if self._recording:
            self.cancel()

    def _ready_to_record(self):
        if self._paused:
            return False
        if not self._model_ready.is_set():
            self.tray.notify("LocalDictation", "The speech model is still loading.",
                             category="tips")
            return False
        if self._processing:
            # Only one dictation is in flight at a time; starting a second would
            # race for the same cursor.
            self.tray.notify("LocalDictation",
                             "Still finishing your last dictation.", category="tips")
            return False
        return True

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------
    def _start_session(self, mode):
        with self._session_lock:
            if self._recording:
                return
            self._recording = True
            self._cancelled = False
            self._mode = mode
            self._session_started = time.monotonic()

        # Command mode operates on the selection, so grab it before the user's
        # focus can move.
        self._pending_selection = ""
        if mode == MODE_COMMAND:
            try:
                self._pending_selection = injector.read_selection()
            except Exception as e:  # noqa: BLE001
                logger.warning("Could not read selection: %s", e)

        self.tray.set_state(STATE_COMMAND if mode == MODE_COMMAND else STATE_RECORDING)
        if mode == MODE_COMMAND:
            self.overlay.show_command()
        else:
            self.overlay.show_recording()
        self._play_sound(start=True)

        audio_config = dict(self.config.get("audio") or {})
        if not self._latched:
            # Auto-stop on silence only makes sense hands-free. While the key is
            # held, a pause for thought is not the end of the dictation — and an
            # upgraded config carries this value over from the old toggle-only
            # model, where it always applied.
            audio_config["silence_threshold_seconds"] = 0.0

        started = audio_mod.start_recording(
            sample_rate=audio_config.get("sample_rate", 16000),
            device_index=audio_config.get("device_index"),
            config=audio_config,
            callbacks={
                "silence": self._on_auto_stop,
                "no_audio": self._on_no_audio,
                "mic_dead": self._on_mic_dead,
                "limit_warning": self._on_limit_warning,
                "limit_reached": self._on_limit_reached,
                "error": self._on_audio_error,
            })

        if not started:
            # _latched must be cleared too: leaving it set makes the next
            # push-to-talk release a no-op, so recording never stops again.
            self._recording = False
            self._latched = False
            self.overlay.hide()
            self.tray.set_state(STATE_ERROR)
            self.tray.notify("Microphone unavailable",
                             "LocalDictation could not open your microphone.")

    def stop_from_ui(self):
        """Clicking the overlay ends the session."""
        if self._recording:
            self._latched = False
            self._finish_session()

    def cancel(self):
        """Discard the current dictation without inserting anything.

        Also works during transcription: the worker checks the flag before it
        touches the text field, so Esc still saves you once you have realised
        you were dictating into the wrong window.
        """
        if self._recording:
            logger.info("Session cancelled by user")
            self._abort_session(silent=False)
            return
        if self._processing:
            logger.info("Cancelled while transcribing — output will be suppressed")
            self._cancelled = True
            self.tray.notify("Dictation cancelled", "Nothing was inserted.",
                             category="tips")

    def _abort_session(self, silent=False):
        with self._session_lock:
            if not self._recording:
                return
            self._recording = False
            self._cancelled = True
            self._latched = False

        audio_mod.discard_recording()
        self.overlay.hide()
        self.tray.set_state(STATE_IDLE)
        if not silent:
            self.tray.notify("Dictation cancelled", "Nothing was inserted.",
                             category="tips")

    def _finish_session(self):
        with self._session_lock:
            if not self._recording:
                return
            self._recording = False
            self._latched = False
            self._processing = True

        duration = time.monotonic() - self._session_started
        self._play_sound(start=False)
        self.tray.set_state(STATE_TRANSCRIBING)
        self.overlay.show_transcribing()

        threading.Thread(target=self._process_session, args=(duration,),
                         daemon=True, name="dictation").start()

    # ------------------------------------------------------------------
    # Processing
    # ------------------------------------------------------------------
    def _process_session(self, duration):
        mode = self._mode
        try:
            wav_path = audio_mod.stop_recording_and_save(
                sample_rate=(self.config.get("audio") or {}).get("sample_rate", 16000))
            if not wav_path:
                logger.info("Nothing captured")
                return

            raw, info = self._transcribe(wav_path)
            if info.get("error"):
                self.tray.notify("Transcription failed", str(info["error"])[:140])
                return
            if not raw:
                logger.info("No speech detected")
                return

            if self._cancelled:
                logger.info("Discarding transcript — cancelled during transcription")
                return

            if mode == MODE_COMMAND:
                self._handle_command(raw, duration)
            else:
                self._handle_dictation(raw, duration)

        except Exception as e:  # noqa: BLE001 — the loop must survive any single failure
            logger.exception("Dictation failed: %s", e)
            self.tray.notify("Dictation failed", str(e)[:120])
        finally:
            self._processing = False
            self.overlay.hide()
            self.tray.set_state(STATE_PAUSED if self._paused else STATE_IDLE)
            self._refresh_hub()

    def _transcribe(self, wav_path):
        dictionary_config = self.config.get("dictionary") or {}
        prompt = hotwords = None
        if dictionary_config.get("enabled", True) and dictionary_config.get("bias_decoding", True):
            prompt = dictionary_mod.initial_prompt()
            terms = dictionary_mod.bias_terms()
            hotwords = " ".join(terms) if terms else None

        text, info = transcription_mod.transcribe_audio(
            wav_path,
            language=self.config.get("language"),
            language_pool=self.config.get("language_pool"),
            initial_prompt=prompt,
            hotwords=hotwords)
        return (text or "").strip(), info

    def _handle_dictation(self, raw, duration):
        app_context = context_mod.resolve(self.config)
        profile = context_mod.profile_for(app_context, self.config)
        options = formatter_mod.FormatOptions.from_config(self.config, profile)
        options.app_name = app_context.app_name

        text = raw
        replacements = 0

        # Vocabulary correction first: later stages should reason about the
        # words the user actually said, correctly spelled.
        if (self.config.get("dictionary") or {}).get("enabled", True):
            corrected = dictionary_mod.correct(text)
            if corrected != text:
                replacements += 1
            text = corrected

        if self.config.get("snippets_enabled", True):
            text, fired = snippets_mod.expand(text, injector.read_clipboard())
            replacements += len(fired)

        result = formatter_mod.format_text(
            text, config=self.config, options=options,
            vocabulary=dictionary_mod.bias_terms())
        final = result.text

        trailing_action = None
        if (self.config.get("output") or {}).get("trailing_actions", True):
            final, trailing_action = injector.extract_trailing_action(final)

        if not final.strip():
            logger.info("Nothing left after formatting")
            return

        if options.trailing_space and not trailing_action:
            final += " "

        output = self.config.get("output") or {}
        ok, message = injector.insert(
            final,
            mode=output.get("paste_mode", "clipboard"),
            restore_delay=output.get("clipboard_restore_delay", 0.25),
            restore_clipboard=output.get("restore_clipboard", True),
            trailing_action=trailing_action)

        if not ok:
            hotkey = (self.config.get("hotkeys") or {}).get("paste_last", "")
            suffix = f" Press {hotkey} to paste it." if hotkey else ""
            self.tray.notify("Could not insert text", message + suffix)

        words = len(final.split())
        cleaned = max(0, len(raw.split()) - words)
        history_mod.add_entry(
            raw=raw, text=final.strip(), duration=duration,
            app=app_context.exe, app_title=app_context.title,
            category=app_context.category,
            cleanup_level=(self.config.get("formatting") or {}).get("cleanup_level", ""),
            used_llm=result.used_llm, cleaned_words=cleaned, replacements=replacements)
        stats_mod.record_session(words=words, audio_seconds=duration,
                                 cleaned_words=cleaned, replacements=replacements,
                                 app=app_context.exe)
        logger.info("Inserted %d words into %s", words, app_context.exe or "unknown")

    def _handle_command(self, instruction, duration):
        selection = self._pending_selection
        self._pending_selection = ""

        if not selection.strip():
            # Nothing selected: fall back to inserting what was said. Silently
            # sending the utterance to a web search — as some tools do — would
            # be a surprising thing for an offline app to do.
            logger.info("Command mode with no selection — inserting as dictation")
            self._handle_dictation(instruction, duration)
            return

        result, message = transforms_mod.run(selection, instruction, self.config)
        if result is None:
            self.tray.notify("Command mode", message or "Nothing was changed.")
            return

        output = self.config.get("output") or {}
        ok, insert_message = injector.insert(
            result, mode=output.get("paste_mode", "clipboard"),
            restore_delay=output.get("clipboard_restore_delay", 0.25),
            restore_clipboard=output.get("restore_clipboard", True))
        if not ok:
            self.tray.notify("Could not replace selection", insert_message)
            return

        history_mod.add_entry(raw=instruction, text=result, duration=duration,
                              mode=history_mod.MODE_COMMAND)
        logger.info("Command applied: %r", instruction[:60])

    def run_transform(self, name):
        """Apply a saved transform to the current selection, no speaking needed."""
        if self._processing:
            return

        def _run():
            selection = injector.read_selection()
            if not selection.strip():
                self.tray.notify("Transform", "Select some text first.")
                return
            result, message = transforms_mod.run_named(selection, name, self.config)
            if result is None:
                self.tray.notify("Transform", message or "Nothing was changed.")
                return
            output = self.config.get("output") or {}
            injector.insert(result, mode=output.get("paste_mode", "clipboard"),
                            restore_clipboard=output.get("restore_clipboard", True))
            history_mod.add_entry(raw=selection, text=result,
                                  mode=history_mod.MODE_COMMAND)

        threading.Thread(target=_run, daemon=True, name="transform").start()

    # ------------------------------------------------------------------
    # Audio watchdog callbacks
    # ------------------------------------------------------------------
    def _on_auto_stop(self):
        if self._recording:
            logger.info("Auto-stopping after silence")
            self._finish_session()

    def _on_no_audio(self, seconds):
        self.tray.notify("No audio received",
                         f"Nothing heard for {int(seconds)}s. Check your microphone.",
                         category="errors")

    def _on_mic_dead(self, _seconds):
        self.tray.notify("Microphone is not working",
                         "Still silent. Pick a different input in Settings.",
                         category="errors")

    def _on_limit_warning(self, remaining):
        self.tray.notify("Session ending soon",
                         f"Less than {max(1, remaining // 60)} minute(s) left.",
                         category="session_limits")

    def _on_limit_reached(self):
        # End gracefully and insert what was said rather than discarding it.
        if self._recording:
            self.tray.notify("Session limit reached",
                             "Transcribing what you said so far.",
                             category="session_limits")
            self._latched = False
            self._finish_session()

    def _on_audio_error(self, message):
        self.tray.notify("Microphone error", message[:120], category="errors")

    # ------------------------------------------------------------------
    # Sounds
    # ------------------------------------------------------------------
    def _play_sound(self, start=True):
        """Two distinct tones: one confirms the mic is live, one confirms insertion."""
        if not (self.config.get("ui") or {}).get("sounds", True):
            return
        if sys.platform != "win32":
            return

        def _beep():
            try:
                import winsound
                winsound.Beep(880 if start else 620, 90)
            except Exception as e:  # noqa: BLE001
                logger.debug("Sound failed: %s", e)

        threading.Thread(target=_beep, daemon=True, name="sound").start()

    # ------------------------------------------------------------------
    # Tray and window actions
    # ------------------------------------------------------------------
    def toggle_pause(self):
        self._paused = not self._paused
        if self._paused and self._recording:
            self._abort_session(silent=True)
        self.tray.set_paused(self._paused)
        logger.info("Dictation %s", "paused" if self._paused else "resumed")

    def set_microphone(self, index):
        self.config.setdefault("audio", {})["device_index"] = index
        save_config(self.config)
        self.tray.refresh_menu()
        logger.info("Microphone set to %s", audio_mod.get_device_name(index))

    def paste_last_transcript(self):
        output = self.config.get("output") or {}
        ok, message = injector.paste_last_transcript(
            mode=output.get("paste_mode", "clipboard"),
            restore_clipboard=output.get("restore_clipboard", True))
        if not ok:
            self.tray.notify("Paste last transcript", message)

    def open_hub(self, page="home"):
        """Open the main window. Must run on the Tk thread."""
        def _open():
            try:
                if self.hub is None or not self.hub.winfo_exists():
                    from ui.hub import Hub
                    self.hub = Hub(self.root, self.config,
                                   on_config_changed=self.apply_config, app=self)
                self.hub.show(page)
            except Exception as e:  # noqa: BLE001
                logger.exception("Could not open the Hub: %s", e)
                self.tray.notify("LocalDictation", "Could not open the main window.")

        self.root.after(0, _open)

    def _refresh_hub(self):
        if self.hub is None:
            return

        def _refresh():
            try:
                if self.hub.winfo_exists() and self.hub.state() != "withdrawn":
                    self.hub.refresh_current()
            except Exception as e:  # noqa: BLE001
                logger.debug("Hub refresh failed: %s", e)

        self.root.after(0, _refresh)

    def _show_onboarding(self):
        try:
            from ui import onboarding
            onboarding.maybe_run(self.root, self.config,
                                 on_finish=self.apply_config, app=self)
        except Exception as e:  # noqa: BLE001
            logger.exception("Onboarding failed: %s", e)

    # ------------------------------------------------------------------
    # Config changes
    # ------------------------------------------------------------------
    def apply_config(self, new_config):
        """Re-apply everything that can change without a restart."""
        self.config = new_config
        save_config(self.config)
        self._apply_log_level()
        self._apply_startup_setting()

        if self._model_ready.is_set():
            self._register_hotkeys()

        history_mod.set_persist(self.config.get("history_persist", True))

        self.overlay.set_enabled((self.config.get("ui") or {}).get("show_overlay", True))
        if self.tray:
            self.tray.refresh_menu()
        logger.info("Configuration applied")

    def _apply_startup_setting(self):
        """Register or remove the Run key so the app can start with Windows."""
        if sys.platform != "win32":
            return
        try:
            import winreg
        except ImportError:
            return

        enabled = self.config.get("launch_at_startup", False)
        app_name = "LocalDictation"
        reg_path = r"Software\Microsoft\Windows\CurrentVersion\Run"

        if getattr(sys, "frozen", False):
            command = f'"{sys.executable}"'
        else:
            command = f'"{sys.executable}" "{os.path.abspath(__file__)}"'

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, reg_path, 0,
                                winreg.KEY_SET_VALUE) as key:
                if enabled:
                    winreg.SetValueEx(key, app_name, 0, winreg.REG_SZ, command)
                else:
                    try:
                        winreg.DeleteValue(key, app_name)
                    except FileNotFoundError:
                        pass
        except OSError as e:
            logger.warning("Could not update the startup entry: %s", e)

    # ------------------------------------------------------------------
    def quit(self):
        logger.info("Shutting down")
        self._unregister_hotkeys()
        if self._recording:
            audio_mod.discard_recording()
        save_config(self.config)
        # A paste borrows the clipboard and hands it back on a short timer.
        # os._exit skips that thread, so quitting right after dictating would
        # otherwise leave the transcript sitting in the user's clipboard.
        injector.flush_pending()
        if self.tray:
            self.tray.stop()
        try:
            self.root.after(0, self.root.quit)
        except Exception:  # noqa: BLE001
            pass
        os._exit(0)


def main():
    DictationApp().start()


if __name__ == "__main__":
    main()

"""First-run setup — from install to a first successful dictation.

Two patterns from the products that do this well are worth keeping. First,
never fire a system dialog cold: explain the ask in the app before the OS
prompt appears, so a refusal is recoverable rather than mysterious. Second,
make the person actually dictate before the wizard closes — a tool nobody has
successfully used once is a tool they will not come back to.

The whole thing is skippable. Every choice here also lives in Settings.
"""

import logging
import threading
import tkinter as tk

import audio as audio_mod
import formatter
from . import theme as th
from . import widgets as w

logger = logging.getLogger(__name__)


class Onboarding(tk.Toplevel):
    """A modal, stepped setup window."""

    def __init__(self, master, config, on_finish=None, app=None, theme=None):
        super().__init__(master)
        self.config_data = config
        self.on_finish = on_finish
        self.app = app
        self.theme = theme or th.Theme((config.get("ui") or {}).get("theme", "light"))

        self.title("Welcome to LocalDictation")
        self.geometry("640x540")
        self.resizable(False, False)
        self.configure(bg=self.theme.bg)
        self.transient(master)

        self._step = 0
        self._steps = [
            ("Welcome", self._step_welcome),
            ("Microphone", self._step_microphone),
            ("Hotkey", self._step_hotkey),
            ("Cleanup", self._step_cleanup),
            ("Try it", self._step_try),
        ]

        self._build_chrome()
        self._render()

        self.protocol("WM_DELETE_WINDOW", self._finish)
        self.bind("<Escape>", lambda _e: self._finish())

    # ------------------------------------------------------------------
    # Chrome
    # ------------------------------------------------------------------
    def _build_chrome(self):
        self._dots = tk.Frame(self, bg=self.theme.bg)
        self._dots.pack(fill=tk.X, padx=th.SPACE_XL, pady=(th.SPACE_LG, 0))

        self._body = tk.Frame(self, bg=self.theme.bg)
        self._body.pack(fill=tk.BOTH, expand=True, padx=th.SPACE_XL, pady=th.SPACE_MD)

        footer = tk.Frame(self, bg=self.theme.bg)
        footer.pack(fill=tk.X, padx=th.SPACE_XL, pady=(0, th.SPACE_LG))

        self._skip = w.Button(footer, self.theme, text="Skip setup", variant="secondary",
                              size=9, height=32, command=self._finish)
        self._skip.pack(side=tk.LEFT)

        self._next = w.Button(footer, self.theme, text="Continue", size=10,
                              height=36, command=self._advance)
        self._next.pack(side=tk.RIGHT)

        self._back = w.Button(footer, self.theme, text="Back", variant="secondary",
                              size=9, height=32, command=self._retreat)
        self._back.pack(side=tk.RIGHT, padx=(0, 8))

    def _render_dots(self):
        for child in self._dots.winfo_children():
            child.destroy()
        for index, (label, _) in enumerate(self._steps):
            done = index <= self._step
            chip = tk.Canvas(self._dots, width=max(56, len(label) * 8 + 20), height=8,
                             bg=self.theme.bg, highlightthickness=0, bd=0)
            chip.pack(side=tk.LEFT, padx=(0, 6))
            chip.create_rectangle(
                0, 2, int(chip["width"]), 6,
                fill=self.theme.accent if done else self.theme.grid_empty, outline="")

    def _render(self):
        for child in self._body.winfo_children():
            child.destroy()
        self._render_dots()

        label, builder = self._steps[self._step]
        builder(self._body)

        self._back.set_enabled(self._step > 0)
        self._next.set_text("Finish" if self._step == len(self._steps) - 1 else "Continue")

    def _advance(self):
        if self._step >= len(self._steps) - 1:
            self._finish()
            return
        self._step += 1
        self._render()

    def _retreat(self):
        if self._step > 0:
            self._step -= 1
            self._render()

    def _finish(self):
        self.config_data["first_run_complete"] = True
        self._stop_meter()
        if self.on_finish:
            try:
                self.on_finish(self.config_data)
            except Exception as e:  # noqa: BLE001
                logger.exception("Onboarding finish handler failed: %s", e)
        self.destroy()

    # ------------------------------------------------------------------
    # Steps
    # ------------------------------------------------------------------
    def _heading(self, parent, title, subtitle):
        w.Label(parent, self.theme, text=title, size=20, weight="bold",
                serif=True).pack(anchor="w", pady=(th.SPACE_MD, 4))
        w.Label(parent, self.theme, text=subtitle, role="ink_soft", size=11,
                wraplength=540, justify=tk.LEFT).pack(anchor="w", pady=(0, th.SPACE_MD))

    def _step_welcome(self, parent):
        self._heading(parent, "Dictation that stays on this machine",
                      "Hold a key, talk, let go. Your words are transcribed and "
                      "cleaned up locally and typed wherever your cursor is.")

        for glyph, title, detail in (
            ("◉", "Nothing leaves your computer",
             "Speech recognition runs on your own hardware. There is no account, "
             "no server, and no subscription."),
            ("✦", "It writes, not transcribes",
             "Filler words, stutters, and false starts are removed. Numbers, times, "
             "and email addresses are written the way you would type them."),
            ("☷", "It learns your words",
             "Names and jargon you add to the dictionary stop coming out wrong."),
        ):
            card = w.Card(parent, self.theme, radius=th.RADIUS_SMALL, padding=14, height=86)
            card.pack(fill=tk.X, pady=4)
            row = tk.Frame(card.body, bg=self.theme.surface)
            row.pack(fill=tk.X)
            w.Label(row, self.theme, text=glyph, size=15, bg="surface").pack(
                side=tk.LEFT, padx=(0, 12))
            column = tk.Frame(row, bg=self.theme.surface)
            column.pack(side=tk.LEFT, fill=tk.X, expand=True)
            w.Label(column, self.theme, text=title, size=11, weight="bold",
                    bg="surface").pack(anchor="w")
            w.Label(column, self.theme, text=detail, role="ink_soft", size=10,
                    bg="surface", wraplength=440, justify=tk.LEFT).pack(anchor="w")

    def _step_microphone(self, parent):
        self._heading(parent, "Pick your microphone",
                      "Speak normally for a moment and watch the meter. If it does "
                      "not move, choose a different input.")

        try:
            devices = [("system_default", "System default")] + [
                (str(index), name) for index, name in audio_mod.query_input_devices()]
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not list microphones: %s", e)
            devices = [("system_default", "System default")]

        current = (self.config_data.get("audio") or {}).get("device_index")
        value = "system_default" if current is None else str(current)

        w.Dropdown(parent, self.theme, options=devices, value=value,
                   command=self._set_device, width=520).pack(anchor="w", pady=(0, th.SPACE_MD))

        self._meter = tk.Canvas(parent, height=48, bg=self.theme.bg,
                                highlightthickness=0, bd=0)
        self._meter.pack(fill=tk.X, pady=(0, 8))

        self._meter_note = w.Label(parent, self.theme, text="Listening…",
                                   role="ink_faint", size=10)
        self._meter_note.pack(anchor="w")

        self._meter_running = True
        self._start_meter()

    def _set_device(self, value):
        index = None if value == "system_default" else int(value)
        self.config_data.setdefault("audio", {})["device_index"] = index
        self._restart_meter()

    def _start_meter(self):
        """Open a short-lived stream so the level meter is live during setup."""
        def _run():
            try:
                audio_mod.start_recording(
                    device_index=(self.config_data.get("audio") or {}).get("device_index"),
                    config={"silence_threshold_seconds": 0, "max_session_seconds": 0,
                            "no_audio_warn_seconds": 0, "mic_dead_warn_seconds": 0})
            except Exception as e:  # noqa: BLE001
                logger.warning("Meter stream failed: %s", e)

        threading.Thread(target=_run, daemon=True, name="onboarding-meter").start()
        self.after(120, self._paint_meter)

    def _restart_meter(self):
        self._stop_meter()
        self.after(150, self._start_meter)

    def _stop_meter(self):
        self._meter_running = False
        try:
            audio_mod.discard_recording()
        except Exception as e:  # noqa: BLE001
            logger.debug("Could not stop meter stream: %s", e)

    def _paint_meter(self):
        if not getattr(self, "_meter_running", False):
            return
        try:
            if not self._meter.winfo_exists():
                return
            level = min(1.0, (audio_mod.get_rms() ** 0.25) * 1.4)
            width = max(1, self._meter.winfo_width())
            self._meter.delete("all")
            th.rounded_rect(self._meter, 1, 16, width - 1, 32,
                            radius=th.RADIUS_PILL, fill=self.theme.grid_empty,
                            outline=self.theme.ink, width=th.BORDER_WIDTH)
            if level > 0.02:
                filled = max(6, int((width - 6) * level))
                th.rounded_rect(self._meter, 3, 18, 3 + filled, 30,
                                radius=th.RADIUS_PILL,
                                fill=self.theme.secondary, outline="", width=0)
            self._meter_note.configure(
                text="Sounds good." if level > 0.15
                else "Speak a little louder…" if level > 0.02
                else "No signal yet — try another input.")
        except tk.TclError:
            return
        self.after(80, self._paint_meter)

    def _step_hotkey(self, parent):
        hotkeys = self.config_data.get("hotkeys", {}) or {}
        self._heading(parent, "Your dictation key",
                      "Hold this anywhere to dictate. Let go and the text appears "
                      "at your cursor.")

        card = w.Card(parent, self.theme, padding=20, height=120)
        card.pack(fill=tk.X, pady=(0, th.SPACE_MD))
        w.Label(card.body, self.theme, text=hotkeys.get("push_to_talk") or "not set",
                size=22, weight="bold", serif=True, bg="surface").pack(anchor="w")
        w.Label(card.body, self.theme, size=10, role="ink_soft", bg="surface",
                text="Hold to talk. Double-tap it while talking to lock recording on, "
                     "so you can take your hands off the keyboard.").pack(
            anchor="w", pady=(6, 0))

        for label, key, detail in (
            ("Hands-free", "hands_free", "Toggle recording on and off"),
            ("Command mode", "command_mode", "Select text, then speak an instruction"),
            ("Paste last", "paste_last", "Re-insert the transcript you just dictated"),
        ):
            row = tk.Frame(parent, bg=self.theme.bg)
            row.pack(fill=tk.X, pady=3)
            w.Label(row, self.theme, text=label, size=10, weight="bold").pack(side=tk.LEFT)
            w.Label(row, self.theme, text=hotkeys.get(key) or "not set",
                    role="ink_soft", size=10).pack(side=tk.RIGHT)
            w.Label(row, self.theme, text=f"  {detail}", role="ink_faint",
                    size=9).pack(side=tk.LEFT)

        w.Label(parent, self.theme, role="ink_faint", size=9, wraplength=540,
                justify=tk.LEFT,
                text="You can change any of these later in Settings → Shortcuts."
                ).pack(anchor="w", pady=(th.SPACE_MD, 0))

    def _step_cleanup(self, parent):
        self._heading(parent, "How much should it tidy up?",
                      "Raw speech has filler words and false starts in it. Pick how "
                      "much of that to remove. You can change this any time.")

        options = [(level, formatter.CLEANUP_LABELS[level])
                   for level in formatter.CLEANUP_LEVELS]
        current = (self.config_data.get("formatting") or {}).get("cleanup_level", "medium")

        control = w.SegmentedControl(parent, self.theme, options=options, value=current,
                                     command=self._set_cleanup, height=42)
        control.pack(fill=tk.X, pady=(0, th.SPACE_MD))

        self._cleanup_desc = w.Label(parent, self.theme, text="", size=11,
                                     wraplength=540, justify=tk.LEFT)
        self._cleanup_desc.pack(anchor="w")

        example = w.Card(parent, self.theme, radius=th.RADIUS_SMALL, padding=14, height=92)
        example.pack(fill=tk.X, pady=th.SPACE_MD)
        w.Label(example.body, self.theme, text="YOU SAY", role="ink_faint", size=8,
                weight="bold", bg="surface").pack(anchor="w")
        w.Label(example.body, self.theme, bg="surface", role="ink_soft", size=10,
                text="um so i think we should uh ship it on friday").pack(anchor="w")
        w.Label(example.body, self.theme, text="YOU GET", role="ink_faint", size=8,
                weight="bold", bg="surface").pack(anchor="w", pady=(8, 0))
        self._cleanup_example = w.Label(example.body, self.theme, text="", size=10,
                                        weight="bold", bg="surface")
        self._cleanup_example.pack(anchor="w")

        self._set_cleanup(current, persist=False)

    def _set_cleanup(self, level, persist=True):
        if persist:
            self.config_data.setdefault("formatting", {})["cleanup_level"] = level
        self._cleanup_desc.configure(text=formatter.CLEANUP_DESCRIPTIONS.get(level, ""))
        self._cleanup_example.configure(text=formatter.CLEANUP_EXAMPLES.get(level, ""))

    def _step_try(self, parent):
        hotkey = (self.config_data.get("hotkeys") or {}).get("push_to_talk") or "your hotkey"
        self._heading(parent, "Try it right now",
                      f"Click into the box below, hold {hotkey}, and say something. "
                      "Your words should land in the box.")

        self._try_box = w.TextArea(parent, self.theme, height=7)
        self._try_box.pack(fill=tk.BOTH, expand=True, pady=(0, th.SPACE_MD))
        self._try_box.text.focus_set()

        note = ("The model loads the first time you dictate, so the very first "
                "attempt can take a few seconds. After that it is immediate.")
        if self.app is not None and not getattr(self.app, "model_ready", lambda: False)():
            note = "The speech model is still loading — give it a moment, then try."
        w.Label(parent, self.theme, text=note, role="ink_faint", size=9,
                wraplength=540, justify=tk.LEFT).pack(anchor="w")


def maybe_run(master, config, on_finish=None, app=None, theme=None):
    """Show onboarding only on a genuine first run."""
    if config.get("first_run_complete"):
        return None
    try:
        return Onboarding(master, config, on_finish=on_finish, app=app, theme=theme)
    except Exception as e:  # noqa: BLE001 — never block startup on the wizard
        logger.exception("Could not start onboarding: %s", e)
        return None

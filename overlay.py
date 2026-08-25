"""The floating recording pill — the visual anchor of the dictation loop.

There is no text field to watch while you speak, so this bar carries all the
feedback. Its most important job is diagnostic, and it comes from two separate
signals drawn at once:

* **Bar height** follows raw amplitude — proof the stream is open.
* **Bar colour** follows voice activity — proof it is hearing *you*.

Bars that move but never light up mean the mic is picking up a room, not a
person. A bar that never appears at all means the hotkey never fired. That
distinction is the difference between a two-minute wasted dictation and an
immediate fix.

The pill is draggable to three edge docks and remembers where it was put — the
single loudest complaint about tools like this is an overlay that sits on top of
the button you were trying to press.
"""

import logging
import math
import sys
import time
import tkinter as tk

import audio as audio_mod

logger = logging.getLogger(__name__)

# States
STATE_HIDDEN = "hidden"
STATE_RECORDING = "recording"
STATE_TRANSCRIBING = "transcribing"
STATE_COMMAND = "command"

# Geometry
PILL_W, PILL_H = 148, 46
BAR_COUNT = 9
BAR_W = 4
BAR_GAP = 3
BAR_MAX = 26
BAR_MIN = 3

DOCK_BOTTOM = "bottom"
DOCK_LEFT = "left"
DOCK_RIGHT = "right"
DOCK_MARGIN = 40

FRAME_MS = 40          # ~25 fps; enough for a waveform, cheap enough to ignore


def _apply_window_effects(root, width, height, alpha=0.96):
    """Round the window, make it translucent, and keep it off the taskbar.

    Windows-only; everything here degrades to a plain borderless window
    elsewhere, which is enough for the overlay to remain usable.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        hwnd = ctypes.windll.user32.GetParent(root.winfo_id()) or root.winfo_id()
        gdi32, user32 = ctypes.windll.gdi32, ctypes.windll.user32

        region = gdi32.CreateRoundRectRgn(0, 0, width + 1, height + 1, height, height)
        user32.SetWindowRgn(hwnd, region, True)

        GWL_EXSTYLE = -20
        WS_EX_LAYERED = 0x00080000
        WS_EX_TOOLWINDOW = 0x00000080    # keeps it out of Alt-Tab and the taskbar
        WS_EX_NOACTIVATE = 0x08000000    # clicking it must not steal focus
        LWA_ALPHA = 0x00000002

        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE,
                              style | WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
        user32.SetLayeredWindowAttributes(hwnd, 0, int(alpha * 255), LWA_ALPHA)
    except Exception as e:  # noqa: BLE001 — cosmetic only
        logger.debug("Window effects unavailable: %s", e)


class RecordingOverlay:
    """A borderless Toplevel that animates the live input level.

    It shares the application's single Tk root rather than creating one of its
    own — a second root in the same process makes window ownership and teardown
    unpredictable. Public methods are safe to call from worker threads; each one
    marshals onto the Tk thread with ``after``.
    """

    def __init__(self, master, config=None, on_stop=None, on_cancel=None,
                 on_settings=None):
        self._config = config or {}
        self._on_stop = on_stop
        self._on_cancel = on_cancel
        self._on_settings = on_settings

        self._root = None
        self._canvas = None
        self._bar_ids = []
        self._dot_id = None
        self._heights = [float(BAR_MIN)] * BAR_COUNT
        self._targets = [float(BAR_MIN)] * BAR_COUNT
        self._state = STATE_HIDDEN
        self._phase = 0.0

        self._dock = (self._config.get("ui") or {}).get("overlay_dock", DOCK_BOTTOM)
        self._vertical = self._dock in (DOCK_LEFT, DOCK_RIGHT)
        self._drag_origin = None
        self._hidden_until = float((self._config.get("ui") or {}).get(
            "overlay_hidden_until", 0.0) or 0.0)

        self._build(master)

    def _build(self, master):
        try:
            root = tk.Toplevel(master)
            self._root = root
            root.overrideredirect(True)
            root.wm_attributes("-topmost", True)
            root.configure(bg=self._palette()["bg"])

            self._canvas = tk.Canvas(root, highlightthickness=0, bd=0,
                                     bg=self._palette()["bg"])
            self._canvas.pack(fill=tk.BOTH, expand=True)

            self._apply_geometry()
            root.update_idletasks()
            _apply_window_effects(root, *self._size())

            self._bind_events()
            self._build_items()

            root.withdraw()
            logger.info("Overlay ready (dock=%s)", self._dock)
            root.after(FRAME_MS, self._tick)
        except Exception as e:  # noqa: BLE001 — never take the app down with the overlay
            logger.exception("Overlay failed to start: %s", e)
            self._root = None

    def _palette(self):
        """Overlay colours are fixed dark chrome — it floats over other apps."""
        return {
            "bg": "#1B1A16",
            "idle": "#6E6A5C",
            "voice": "#7BD1C0",        # hearing you
            "silent": "#9A9484",       # capturing, but nothing to hear
            "recording": "#FF5A5F",
            "transcribing": "#FFB84A",
            "command": "#D9BCF0",
            "text": "#FFFFEB",
        }

    def _size(self):
        return (PILL_H, PILL_W) if self._vertical else (PILL_W, PILL_H)

    def _apply_geometry(self):
        root = self._root
        width, height = self._size()
        screen_w = root.winfo_screenwidth()
        screen_h = root.winfo_screenheight()

        if self._dock == DOCK_LEFT:
            x, y = DOCK_MARGIN, (screen_h - height) // 2
        elif self._dock == DOCK_RIGHT:
            x, y = screen_w - width - DOCK_MARGIN, (screen_h - height) // 2
        else:
            x, y = (screen_w - width) // 2, screen_h - height - 80

        root.geometry(f"{width}x{height}+{x}+{y}")
        self._canvas.configure(width=width, height=height)

    def _bind_events(self):
        canvas = self._canvas
        canvas.bind("<ButtonPress-1>", self._on_press)
        canvas.bind("<B1-Motion>", self._on_drag)
        canvas.bind("<ButtonRelease-1>", self._on_release)
        canvas.bind("<Button-3>", self._on_right_click)

    def _build_items(self):
        """Lay out the bars along the pill's long axis."""
        canvas = self._canvas
        canvas.delete("all")
        self._bar_ids = []

        width, height = self._size()
        span = BAR_COUNT * BAR_W + (BAR_COUNT - 1) * BAR_GAP
        colors = self._palette()

        if self._vertical:
            start = (height - span) / 2
            centre = width / 2
            for index in range(BAR_COUNT):
                offset = start + index * (BAR_W + BAR_GAP)
                self._bar_ids.append(canvas.create_rectangle(
                    centre - BAR_MIN / 2, offset, centre + BAR_MIN / 2,
                    offset + BAR_W, fill=colors["silent"], outline=""))
        else:
            start = (width - span) / 2 - 10
            centre = height / 2
            for index in range(BAR_COUNT):
                offset = start + index * (BAR_W + BAR_GAP)
                self._bar_ids.append(canvas.create_rectangle(
                    offset, centre - BAR_MIN / 2, offset + BAR_W,
                    centre + BAR_MIN / 2, fill=colors["silent"], outline=""))

        # Status dot at the trailing end.
        if self._vertical:
            dot_x, dot_y = width / 2, height - 16
        else:
            dot_x, dot_y = width - 20, height / 2
        self._dot_id = canvas.create_oval(dot_x - 5, dot_y - 5, dot_x + 5, dot_y + 5,
                                          fill=colors["recording"], outline="")

    # ------------------------------------------------------------------
    # Animation
    # ------------------------------------------------------------------
    def _tick(self):
        if not self._root:
            return
        try:
            self._phase += 0.18
            colors = self._palette()

            if self._state in (STATE_RECORDING, STATE_COMMAND):
                rms = audio_mod.get_rms()
                hearing_voice = audio_mod.has_voice()
                # A steep power curve: quiet speech should still move the bars,
                # or the meter reads as broken.
                scaled = (rms ** 0.25) * 78
                base = min(BAR_MAX, max(4, scaled))

                for index in range(BAR_COUNT):
                    wave = math.sin(self._phase * 1.3 + index * 0.7) * 0.28 + 0.72
                    self._targets[index] = max(BAR_MIN, min(BAR_MAX, base * wave))

                accent = colors["command"] if self._state == STATE_COMMAND else colors["voice"]
                color = accent if hearing_voice else colors["silent"]
                for bar_id in self._bar_ids:
                    self._canvas.itemconfig(bar_id, fill=color)

                pulse = math.sin(self._phase * 0.9) * 0.5 + 0.5
                self._draw_dot(4 + pulse * 3,
                               colors["command"] if self._state == STATE_COMMAND
                               else colors["recording"])

            elif self._state == STATE_TRANSCRIBING:
                for index in range(BAR_COUNT):
                    wave = math.sin(self._phase * 1.6 + index * 0.8) * 0.5 + 0.5
                    self._targets[index] = BAR_MIN + wave * (BAR_MAX * 0.45)
                for bar_id in self._bar_ids:
                    self._canvas.itemconfig(bar_id, fill=colors["transcribing"])
                self._draw_dot(5, colors["transcribing"])

            self._apply_bar_heights()
        except tk.TclError:
            return      # window torn down mid-frame
        except Exception as e:  # noqa: BLE001
            logger.debug("Overlay tick error: %s", e)

        self._root.after(FRAME_MS, self._tick)

    def _apply_bar_heights(self):
        width, height = self._size()
        span = BAR_COUNT * BAR_W + (BAR_COUNT - 1) * BAR_GAP

        for index, bar_id in enumerate(self._bar_ids):
            current = self._heights[index]
            # Ease toward the target so the meter reads as motion, not noise.
            current += (self._targets[index] - current) * 0.45
            self._heights[index] = current

            if self._vertical:
                start = (height - span) / 2
                offset = start + index * (BAR_W + BAR_GAP)
                centre = width / 2
                self._canvas.coords(bar_id, centre - current / 2, offset,
                                    centre + current / 2, offset + BAR_W)
            else:
                start = (width - span) / 2 - 10
                offset = start + index * (BAR_W + BAR_GAP)
                centre = height / 2
                self._canvas.coords(bar_id, offset, centre - current / 2,
                                    offset + BAR_W, centre + current / 2)

    def _draw_dot(self, radius, color):
        width, height = self._size()
        if self._vertical:
            cx, cy = width / 2, height - 16
        else:
            cx, cy = width - 20, height / 2
        self._canvas.coords(self._dot_id, cx - radius, cy - radius,
                            cx + radius, cy + radius)
        self._canvas.itemconfig(self._dot_id, fill=color)

    # ------------------------------------------------------------------
    # Interaction
    # ------------------------------------------------------------------
    def _on_press(self, event):
        self._drag_origin = (event.x_root, event.y_root, time.monotonic())

    def _on_drag(self, event):
        if not self._drag_origin:
            return
        origin_x, origin_y, _ = self._drag_origin
        if abs(event.x_root - origin_x) < 4 and abs(event.y_root - origin_y) < 4:
            return
        width, height = self._size()
        self._root.geometry(f"{width}x{height}+{event.x_root - width // 2}"
                            f"+{event.y_root - height // 2}")

    def _on_release(self, event):
        if not self._drag_origin:
            return
        origin_x, origin_y, pressed_at = self._drag_origin
        self._drag_origin = None
        moved = abs(event.x_root - origin_x) > 6 or abs(event.y_root - origin_y) > 6

        if not moved:
            # A click on the bar stops the current dictation.
            if self._state in (STATE_RECORDING, STATE_COMMAND) and self._on_stop:
                self._on_stop()
            return

        self._snap_to_nearest_dock(event.x_root, event.y_root)

    def _snap_to_nearest_dock(self, x, y):
        screen_w = self._root.winfo_screenwidth()
        screen_h = self._root.winfo_screenheight()

        distances = {
            DOCK_LEFT: x,
            DOCK_RIGHT: screen_w - x,
            DOCK_BOTTOM: screen_h - y,
        }
        dock = min(distances, key=distances.get)
        self.set_dock(dock)

    def set_dock(self, dock):
        """Move the pill to an edge, reflowing horizontal/vertical as needed."""
        if dock not in (DOCK_BOTTOM, DOCK_LEFT, DOCK_RIGHT):
            return
        was_vertical = self._vertical
        self._dock = dock
        self._vertical = dock in (DOCK_LEFT, DOCK_RIGHT)

        self._apply_geometry()
        if was_vertical != self._vertical:
            self._build_items()
        self._root.update_idletasks()
        _apply_window_effects(self._root, *self._size())

        self._config.setdefault("ui", {})["overlay_dock"] = dock
        logger.info("Overlay docked %s", dock)

    def _on_right_click(self, event):
        """A short context menu — the escape hatches people actually need."""
        menu = tk.Menu(self._root, tearoff=0)
        menu.add_command(label="Hide for 1 hour", command=lambda: self.hide_for(3600))
        menu.add_separator()
        for dock, label in ((DOCK_BOTTOM, "Dock bottom"), (DOCK_LEFT, "Dock left"),
                            (DOCK_RIGHT, "Dock right")):
            menu.add_command(label=label, command=lambda d=dock: self.set_dock(d))
        if self._on_settings:
            menu.add_separator()
            menu.add_command(label="Settings…", command=self._on_settings)
        if self._state in (STATE_RECORDING, STATE_COMMAND) and self._on_cancel:
            menu.add_separator()
            menu.add_command(label="Cancel dictation", command=self._on_cancel)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    # ------------------------------------------------------------------
    # Public API — safe from any thread
    # ------------------------------------------------------------------
    def _schedule(self, func):
        if not self._root:
            return
        try:
            self._root.after(0, func)
        except (tk.TclError, RuntimeError):
            pass

    def show_recording(self):
        self._schedule(lambda: self._show(STATE_RECORDING))

    def show_command(self):
        self._schedule(lambda: self._show(STATE_COMMAND))

    def show_transcribing(self):
        self._schedule(lambda: self._set_state(STATE_TRANSCRIBING))

    def hide(self):
        self._schedule(self._do_hide)

    def hide_for(self, seconds):
        """Snooze the overlay. Dictation keeps working without it."""
        self._hidden_until = time.time() + seconds
        self._config.setdefault("ui", {})["overlay_hidden_until"] = self._hidden_until
        self.hide()
        logger.info("Overlay hidden for %d seconds", seconds)

    def is_snoozed(self):
        return time.time() < self._hidden_until

    def set_enabled(self, enabled):
        self._config.setdefault("ui", {})["show_overlay"] = bool(enabled)
        if not enabled:
            self.hide()

    def _show(self, state):
        if not (self._config.get("ui") or {}).get("show_overlay", True):
            return
        if self.is_snoozed():
            return
        self._set_state(state)
        try:
            self._root.deiconify()
            self._root.lift()
            self._root.wm_attributes("-topmost", True)
        except tk.TclError:
            pass

    def _set_state(self, state):
        self._state = state

    def _do_hide(self):
        self._state = STATE_HIDDEN
        self._heights = [float(BAR_MIN)] * BAR_COUNT
        self._targets = [float(BAR_MIN)] * BAR_COUNT
        try:
            self._root.withdraw()
        except tk.TclError:
            pass

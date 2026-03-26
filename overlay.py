"""Pill-shaped always-on-top overlay with live audio meter."""

import math
import random
import threading
import tkinter as tk
import ctypes
import logging

import audio as audio_mod

logger = logging.getLogger(__name__)

# ── Layout ────────────────────────────────────────────────────────────────────
W, H       = 122, 48
RADIUS     = H // 2          # 24 — full pill end-caps

BAR_COUNT  = 7
BAR_W      = 4
BAR_GAP    = 3
BAR_MAX_H  = 30
BAR_MIN_H  = 3
BARS_LEFT  = RADIUS           # first bar starts right after left cap
BARS_TOTAL = BAR_COUNT * BAR_W + (BAR_COUNT - 1) * BAR_GAP   # 46 px

# Red dot sits to the right of the bars
DOT_CX     = BARS_LEFT + BARS_TOTAL + 10   # 80
DOT_CY     = H // 2                         # 24
DOT_R      = 6

# ── Colours ───────────────────────────────────────────────────────────────────
BG        = '#1c1c1e'
RED       = '#ff453a'
ORANGE    = '#ff9f0a'


def _apply_pill_region(hwnd, w, h):
    gdi32  = ctypes.windll.gdi32
    user32 = ctypes.windll.user32
    hrgn = gdi32.CreateRoundRectRgn(0, 0, w + 1, h + 1, h, h)
    user32.SetWindowRgn(hwnd, hrgn, True)


def _apply_layered_alpha(hwnd, alpha=0.93):
    user32        = ctypes.windll.user32
    GWL_EXSTYLE   = -20
    WS_EX_LAYERED = 0x00080000
    LWA_ALPHA     = 0x00000002
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED)
    user32.SetLayeredWindowAttributes(hwnd, 0, int(alpha * 255), LWA_ALPHA)


def _apply_click_through(hwnd):
    user32            = ctypes.windll.user32
    GWL_EXSTYLE       = -20
    WS_EX_LAYERED     = 0x00080000
    WS_EX_TRANSPARENT = 0x00000020
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE,
                          style | WS_EX_LAYERED | WS_EX_TRANSPARENT)


class RecordingOverlay:
    def __init__(self):
        self._root    = None
        self._canvas  = None
        self._bar_ids = []
        self._dot_id  = None
        self._heights = [float(BAR_MIN_H)] * BAR_COUNT
        self._targets = [float(BAR_MIN_H)] * BAR_COUNT
        self._state   = 'hidden'
        self._phase   = 0.0
        self._ready   = threading.Event()

        t = threading.Thread(target=self._run, daemon=True, name="overlay")
        t.start()
        self._ready.wait(timeout=5)

    # ── Tk thread ─────────────────────────────────────────────────────────────

    def _run(self):
        try:
            root = tk.Tk()
            self._root = root

            root.overrideredirect(True)
            root.wm_attributes('-topmost', True)
            root.configure(bg=BG)

            sw = root.winfo_screenwidth()
            sh = root.winfo_screenheight()
            root.geometry(f'{W}x{H}+{(sw - W) // 2}+{sh - H - 80}')
            root.update_idletasks()

            hwnd = root.winfo_id()
            _apply_layered_alpha(hwnd, 0.93)
            _apply_pill_region(hwnd, W, H)
            _apply_click_through(hwnd)

            canvas = tk.Canvas(root, width=W, height=H,
                               bg=BG, highlightthickness=0)
            canvas.pack()
            self._canvas = canvas

            # Audio bars
            cy = H / 2
            for i in range(BAR_COUNT):
                bx  = BARS_LEFT + i * (BAR_W + BAR_GAP)
                bid = canvas.create_rectangle(
                    bx, cy - BAR_MIN_H / 2,
                    bx + BAR_W, cy + BAR_MIN_H / 2,
                    fill=RED, outline='',
                )
                self._bar_ids.append(bid)

            # Recording dot
            self._dot_id = canvas.create_oval(
                DOT_CX - DOT_R, DOT_CY - DOT_R,
                DOT_CX + DOT_R, DOT_CY + DOT_R,
                fill=RED, outline='',
            )

            root.withdraw()
            self._ready.set()
            logger.info("Overlay initialised at %dx%d+%d+%d",
                        W, H, (sw - W) // 2, sh - H - 80)

            root.after(40, self._tick)
            root.mainloop()

        except Exception as exc:
            logger.exception("Overlay crashed: %s", exc)
            self._ready.set()

    # ── Animation (~25 fps) ───────────────────────────────────────────────────

    def _tick(self):
        if not self._root:
            return

        self._phase += 0.16

        if self._state == 'recording':
            rms  = audio_mod.get_rms()
            # Aggressive power-curve so even quiet speech drives big movement
            scaled = (rms ** 0.25) * 90
            base   = min(BAR_MAX_H, max(5, scaled))
            for i in range(BAR_COUNT):
                spread = random.uniform(0.25, 1.0) if rms > 0.004 else random.uniform(0.3, 0.7)
                self._targets[i] = max(BAR_MIN_H, min(BAR_MAX_H, base * spread))

            # Pulse dot size
            pulse = math.sin(self._phase * 0.9) * 0.5 + 0.5
            r = DOT_R - 1 + pulse * 2.5
            self._canvas.coords(self._dot_id,
                                DOT_CX - r, DOT_CY - r,
                                DOT_CX + r, DOT_CY + r)

        elif self._state == 'transcribing':
            for i in range(BAR_COUNT):
                wave = math.sin(self._phase + i * 0.85) * 0.5 + 0.5
                self._targets[i] = BAR_MIN_H + wave * (BAR_MAX_H * 0.4 - BAR_MIN_H)

        # Spring-lerp bar heights
        cy = H / 2
        for i, bid in enumerate(self._bar_ids):
            h = self._heights[i] + (self._targets[i] - self._heights[i]) * 0.45
            self._heights[i] = h
            bx = BARS_LEFT + i * (BAR_W + BAR_GAP)
            self._canvas.coords(bid, bx, cy - h / 2, bx + BAR_W, cy + h / 2)

        self._root.after(40, self._tick)

    # ── Public API ────────────────────────────────────────────────────────────

    def show_recording(self):
        self._schedule(self._do_show_recording)

    def show_transcribing(self):
        self._schedule(self._do_show_transcribing)

    def hide(self):
        self._schedule(self._do_hide)

    def _schedule(self, fn):
        if self._root:
            try:
                self._root.after(0, fn)
            except Exception:
                pass

    def _do_show_recording(self):
        self._state = 'recording'
        c = self._canvas
        for bid in self._bar_ids:
            c.itemconfig(bid, fill=RED)
        c.itemconfig(self._dot_id, fill=RED)
        self._root.deiconify()
        self._root.lift()
        self._root.wm_attributes('-topmost', True)

    def _do_show_transcribing(self):
        self._state = 'transcribing'
        c = self._canvas
        for bid in self._bar_ids:
            c.itemconfig(bid, fill=ORANGE)
        c.itemconfig(self._dot_id, fill=ORANGE)
        # Reset dot to normal size
        c.coords(self._dot_id,
                 DOT_CX - DOT_R, DOT_CY - DOT_R,
                 DOT_CX + DOT_R, DOT_CY + DOT_R)

    def _do_hide(self):
        self._state = 'hidden'
        self._root.withdraw()

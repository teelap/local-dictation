"""System tray presence: state at a glance, and the actions people reach for.

The tray icon is the app's only permanent surface — the Hub can be closed and
the overlay can be hidden — so it carries the live state. While recording it
animates a waveform driven by the real input level, which is the one status
signal that still works when everything else is out of the way.

The menu is deliberately action-first. The most common thing after a failed
paste is "paste that again", so it sits near the top rather than behind
Preferences.
"""

import logging
import threading
import time

import pystray
from PIL import Image, ImageDraw

import audio as audio_mod

logger = logging.getLogger(__name__)

STATE_LOADING = "loading"
STATE_IDLE = "idle"
STATE_RECORDING = "recording"
STATE_TRANSCRIBING = "transcribing"
STATE_COMMAND = "command"
STATE_PAUSED = "paused"
STATE_ERROR = "error"

ICON_SIZE = 64

_STATE_COLORS = {
    STATE_LOADING: (150, 150, 140),
    STATE_IDLE: (26, 26, 26),
    STATE_RECORDING: (229, 72, 77),
    STATE_TRANSCRIBING: (240, 160, 32),
    STATE_COMMAND: (170, 130, 220),
    STATE_PAUSED: (140, 140, 130),
    STATE_ERROR: (196, 52, 43),
}

_STATE_TOOLTIPS = {
    STATE_LOADING: "LocalDictation — loading model…",
    STATE_IDLE: "LocalDictation — ready",
    STATE_RECORDING: "LocalDictation — recording",
    STATE_TRANSCRIBING: "LocalDictation — transcribing…",
    STATE_COMMAND: "LocalDictation — command mode",
    STATE_PAUSED: "LocalDictation — paused",
    STATE_ERROR: "LocalDictation — needs attention",
}

# Redraw rate for the recording animation. Fast enough to read as live, slow
# enough that repainting a tray icon stays free.
ANIMATION_INTERVAL = 0.12


def _mic_icon(color, level=0.0, bars=None):
    """Draw the tray glyph: a mic at rest, a waveform while recording."""
    image = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    if bars is None:
        # Idle glyph: a rounded microphone capsule on a stand.
        capsule_w, capsule_h = 20, 30
        left = (ICON_SIZE - capsule_w) // 2
        top = 8
        draw.rounded_rectangle((left, top, left + capsule_w, top + capsule_h),
                               radius=capsule_w // 2, fill=color)
        draw.arc((left - 8, top + 12, left + capsule_w + 8, top + capsule_h + 10),
                 start=0, end=180, fill=color, width=4)
        draw.line((ICON_SIZE // 2, top + capsule_h + 10, ICON_SIZE // 2, ICON_SIZE - 10),
                  fill=color, width=4)
        return image

    # Recording glyph: five bars whose heights track the live level.
    count = len(bars)
    bar_w = 7
    gap = 4
    total = count * bar_w + (count - 1) * gap
    start = (ICON_SIZE - total) // 2
    centre = ICON_SIZE // 2

    for index, height_ratio in enumerate(bars):
        height = max(6, int(height_ratio * (ICON_SIZE - 16)))
        x = start + index * (bar_w + gap)
        draw.rounded_rectangle(
            (x, centre - height // 2, x + bar_w, centre + height // 2),
            radius=bar_w // 2, fill=color)
    return image


class TrayIcon:
    """Wraps pystray, exposing state changes and a dynamic menu."""

    def __init__(self, callbacks=None, config=None):
        self._callbacks = callbacks or {}
        self._config = config or {}
        self._icon = None
        self._state = STATE_LOADING
        self._paused = False
        self._animating = False
        self._animation_thread = None
        self._phase = 0.0

    # ------------------------------------------------------------------
    # Menu
    # ------------------------------------------------------------------
    def _call(self, name, *args):
        handler = self._callbacks.get(name)
        if not handler:
            return
        # Menu handlers run on pystray's thread; anything slow must not block it.
        threading.Thread(target=lambda: self._safe(handler, *args), daemon=True,
                         name=f"tray-{name}").start()

    @staticmethod
    def _safe(handler, *args):
        try:
            handler(*args)
        except Exception as e:  # noqa: BLE001
            logger.exception("Tray action failed: %s", e)

    def _microphone_items(self):
        """Built on open, so a mic plugged in a minute ago shows up."""
        try:
            devices = audio_mod.query_input_devices()
        except Exception as e:  # noqa: BLE001
            logger.debug("Could not list microphones: %s", e)
            return [pystray.MenuItem("No devices found", None, enabled=False)]

        current = (self._config.get("audio") or {}).get("device_index")
        items = [pystray.MenuItem(
            "System default",
            lambda *_: self._call("set_microphone", None),
            checked=lambda _i, c=current: c is None, radio=True)]
        for index, name in devices[:12]:
            items.append(pystray.MenuItem(
                name[:44],
                lambda *_, i=index: self._call("set_microphone", i),
                checked=lambda _i, i=index, c=current: c == i, radio=True))
        return items

    def _build_menu(self):
        return pystray.Menu(
            pystray.MenuItem("Open LocalDictation",
                             lambda *_: self._call("open_hub"), default=True),
            pystray.MenuItem("Paste last transcript",
                             lambda *_: self._call("paste_last")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Pause dictation" if not self._paused else "Resume dictation",
                             lambda *_: self._call("toggle_pause")),
            pystray.MenuItem("Microphone", pystray.Menu(*self._microphone_items())),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Shortcuts…", lambda *_: self._call("open_settings")),
            pystray.MenuItem("History", lambda *_: self._call("open_history")),
            pystray.MenuItem("Insights", lambda *_: self._call("open_insights")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", lambda *_: self._call("quit")),
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def run(self):
        """Blocking — run this on a background thread."""
        self._icon = pystray.Icon(
            "LocalDictation",
            _mic_icon(_STATE_COLORS[STATE_LOADING]),
            _STATE_TOOLTIPS[STATE_LOADING],
            self._build_menu())
        self._icon.run()

    def stop(self):
        self._animating = False
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception as e:  # noqa: BLE001
                logger.debug("Tray stop failed: %s", e)

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------
    def set_state(self, state):
        self._state = state
        if self._icon is None:
            return

        if state in (STATE_RECORDING, STATE_COMMAND):
            self._start_animation()
        else:
            self._stop_animation()
            try:
                self._icon.icon = _mic_icon(_STATE_COLORS.get(state, (26, 26, 26)))
                self._icon.title = _STATE_TOOLTIPS.get(state, "LocalDictation")
            except Exception as e:  # noqa: BLE001
                logger.debug("Could not update tray icon: %s", e)

    def set_paused(self, paused):
        self._paused = bool(paused)
        self.refresh_menu()
        self.set_state(STATE_PAUSED if paused else STATE_IDLE)

    def refresh_menu(self):
        if self._icon is None:
            return
        try:
            self._icon.menu = self._build_menu()
            self._icon.update_menu()
        except Exception as e:  # noqa: BLE001
            logger.debug("Could not rebuild tray menu: %s", e)

    # ------------------------------------------------------------------
    # Recording animation
    # ------------------------------------------------------------------
    def _start_animation(self):
        if self._animating:
            return
        self._animating = True
        self._animation_thread = threading.Thread(
            target=self._animate, daemon=True, name="tray-animation")
        self._animation_thread.start()

    def _stop_animation(self):
        self._animating = False

    def _animate(self):
        import math

        color = _STATE_COLORS.get(self._state, _STATE_COLORS[STATE_RECORDING])
        tooltip = _STATE_TOOLTIPS.get(self._state, "LocalDictation — recording")
        device = ""
        try:
            device = audio_mod.get_device_name(
                (self._config.get("audio") or {}).get("device_index"))
        except Exception:  # noqa: BLE001
            pass
        if device:
            tooltip = f"{tooltip} · {device}"

        while self._animating:
            try:
                rms = audio_mod.get_rms()
                level = min(1.0, (rms ** 0.25) * 1.5)
                self._phase += 0.5
                bars = [
                    max(0.12, min(1.0, level * (math.sin(self._phase + i * 0.8) * 0.3 + 0.8)))
                    for i in range(5)
                ]
                self._icon.icon = _mic_icon(color, bars=bars)
                self._icon.title = tooltip
            except Exception as e:  # noqa: BLE001 — never let the animation crash the app
                logger.debug("Tray animation stopped: %s", e)
                return
            time.sleep(ANIMATION_INTERVAL)

    # ------------------------------------------------------------------
    def notify(self, title, message, category="errors"):
        """Show a balloon notification, honouring per-category muting."""
        if self._icon is None:
            return
        categories = ((self._config.get("ui") or {}).get("notifications") or {})
        if not categories.get(category, True):
            logger.debug("Notification muted (%s): %s", category, message)
            return
        try:
            self._icon.notify(message, title)
        except Exception as e:  # noqa: BLE001
            logger.debug("Tray notification failed: %s", e)

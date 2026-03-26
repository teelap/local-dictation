import logging
import pystray
from PIL import Image, ImageDraw

logger = logging.getLogger(__name__)

# State constants
STATE_LOADING = "loading"
STATE_IDLE = "idle"
STATE_RECORDING = "recording"
STATE_TRANSCRIBING = "transcribing"

_ICON_SIZE = 64

# Color palette for each state
_STATE_COLORS = {
    STATE_LOADING:     (180, 180, 180),   # grey
    STATE_IDLE:        (100, 180, 255),   # calm blue
    STATE_RECORDING:   (220,  50,  50),   # red
    STATE_TRANSCRIBING:(255, 165,   0),   # orange/amber
}

_STATE_TOOLTIPS = {
    STATE_LOADING:     "LocalDictation - Loading...",
    STATE_IDLE:        "LocalDictation - Idle",
    STATE_RECORDING:   "LocalDictation - Recording...",
    STATE_TRANSCRIBING:"LocalDictation - Transcribing...",
}


def _make_icon_image(state):
    """Programmatically generate a simple colored circle icon for the given state."""
    color = _STATE_COLORS.get(state, (200, 200, 200))
    size = _ICON_SIZE
    image = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    pad = 6
    # Outer ring — slightly darker shade
    ring_color = tuple(max(0, c - 50) for c in color)
    draw.ellipse((pad, pad, size - pad, size - pad), fill=ring_color)
    # Inner fill
    inner_pad = pad + 6
    draw.ellipse((inner_pad, inner_pad, size - inner_pad, size - inner_pad), fill=color)
    # Small white dot in center to suggest a microphone
    dot_pad = size // 2 - 5
    draw.ellipse((dot_pad, dot_pad, dot_pad + 10, dot_pad + 10), fill=(255, 255, 255, 200))
    return image


class TrayIcon:
    """Wraps pystray and exposes state-driven icon/tooltip updates."""

    def __init__(self, on_quit, on_settings, on_history):
        self._on_quit = on_quit
        self._on_settings = on_settings
        self._on_history = on_history
        self._icon = None
        self._current_state = STATE_LOADING

    def _build_menu(self):
        return pystray.Menu(
            pystray.MenuItem('Settings', lambda icon, item: self._on_settings()),
            pystray.MenuItem('History', lambda icon, item: self._on_history()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('Quit', lambda icon, item: self._handle_quit(icon)),
        )

    def _handle_quit(self, icon):
        icon.stop()
        self._on_quit()

    def run(self):
        """Blocking call — run in a background thread."""
        image = _make_icon_image(STATE_LOADING)
        tooltip = _STATE_TOOLTIPS[STATE_LOADING]
        self._icon = pystray.Icon(
            "LocalDictation",
            image,
            tooltip,
            self._build_menu()
        )
        self._icon.run()

    def set_state(self, state):
        """Update the tray icon and tooltip to reflect the new state."""
        if self._icon is None:
            return
        self._current_state = state
        try:
            self._icon.icon = _make_icon_image(state)
            self._icon.title = _STATE_TOOLTIPS.get(state, "LocalDictation")
        except Exception as e:
            logger.warning("Failed to update tray icon state: %s", e)

    def notify(self, title, message):
        """Show a balloon/toast notification via pystray."""
        if self._icon is None:
            return
        try:
            self._icon.notify(message, title)
        except Exception as e:
            logger.warning("Tray notification failed: %s", e)


# Legacy function signature kept so nothing outside main breaks if called directly
def run_tray(on_quit, on_settings):
    """Compatibility shim. Prefer TrayIcon class directly."""
    icon = TrayIcon(on_quit, on_settings, lambda: None)
    icon.run()

"""Design tokens and drawing primitives for the interface.

The look is warm and editorial rather than the usual blue-gradient utility
chrome: a cream ground, near-black ink, and a pale lilac used as a *fill* with
dark text on top. Contrast comes from a heavy 2px border on every interactive
element and from generous corner radii, not from saturated colour or shadows.

Tk has no rounded corners, so :func:`rounded_rect` draws them as canvas
polygons. Everything visual in the app is built from that primitive.
"""

import math
import tkinter.font as tkfont

# ---------------------------------------------------------------------------
# Palette
# ---------------------------------------------------------------------------
LIGHT = {
    "bg": "#FFFFEB",            # Lumen Cream — warm off-white, never pure white
    "surface": "#FFFFFF",
    "surface_alt": "#FAF6E4",
    "ink": "#1A1A1A",           # Vast Ink — text and every border
    "ink_soft": "#5A5A52",
    "ink_faint": "#8C8C82",
    "accent": "#F0D7FF",        # Lavender Whisper — CTA fill, dark label on top
    "accent_hover": "#E4C2FF",
    "accent_ink": "#1A1A1A",
    "secondary": "#034F46",     # deep teal, for small details
    "danger": "#C4342B",
    "danger_soft": "#FBE6E4",
    "success": "#2E7D32",
    "warning": "#B26A00",
    "recording": "#E5484D",
    "transcribing": "#F0A020",
    "grid_empty": "#EDE9D6",
    "shadow": "#E8E3CE",
}

DARK = {
    # Warm near-black rather than pure black, so the cream identity survives.
    "bg": "#16150F",
    "surface": "#211F16",
    "surface_alt": "#1B1A12",
    "ink": "#FFFFEB",
    "ink_soft": "#C4C0AC",
    "ink_faint": "#8C897A",
    "accent": "#D9BCF0",
    "accent_hover": "#E7D2FA",
    "accent_ink": "#16150F",
    "secondary": "#6BC2B4",
    "danger": "#FF6B62",
    "danger_soft": "#3A211F",
    "success": "#7BC97F",
    "warning": "#E2A03C",
    "recording": "#FF5A5F",
    "transcribing": "#FFB84A",
    "grid_empty": "#2A2820",
    "shadow": "#0E0D08",
}

# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------
RADIUS_CARD = 28
RADIUS_SMALL = 14
RADIUS_PILL = 999          # resolved to height/2 by the drawing helper

BORDER_WIDTH = 2

SPACE_XS = 4
SPACE_SM = 8
SPACE_MD = 16
SPACE_LG = 24
SPACE_XL = 36

SIDEBAR_WIDTH = 216

# Figtree is the intended face; the rest are the realistic fallbacks on a
# Windows or Linux box that has never heard of it.
FONT_STACK = ["Figtree", "Segoe UI Variable Text", "Segoe UI",
              "DejaVu Sans", "Helvetica", "sans-serif"]
SERIF_STACK = ["Georgia", "Iowan Old Style", "DejaVu Serif", "Times New Roman", "serif"]

_resolved_font = None
_resolved_serif = None


def ui_font(root=None, size=11, weight="normal"):
    """Resolve the UI face once, then reuse it."""
    global _resolved_font
    if _resolved_font is None:
        _resolved_font = _first_available(FONT_STACK, root)
    return (_resolved_font, size, weight)


def display_font(root=None, size=22, weight="normal"):
    """The editorial serif used for display numerals and page titles."""
    global _resolved_serif
    if _resolved_serif is None:
        _resolved_serif = _first_available(SERIF_STACK, root)
    return (_resolved_serif, size, weight)


def _first_available(stack, root):
    try:
        available = {name.lower() for name in tkfont.families(root)}
    except Exception:  # noqa: BLE001 — no Tk root yet
        return stack[-1]
    for name in stack:
        if name.lower() in available:
            return name
    return stack[-1]


class Theme:
    """Resolved colours for one appearance mode."""

    def __init__(self, mode="light"):
        self.mode = mode if mode in ("light", "dark") else "light"
        self.colors = dict(DARK if self.mode == "dark" else LIGHT)

    def __getattr__(self, name):
        try:
            return self.__dict__["colors"][name]
        except KeyError as e:
            raise AttributeError(name) from e

    def get(self, name, default=None):
        return self.colors.get(name, default)


# ---------------------------------------------------------------------------
# Drawing primitives
# ---------------------------------------------------------------------------
def rounded_points(x1, y1, x2, y2, radius, steps=10):
    """Trace a rounded rectangle as a flat list of polygon coordinates.

    Corners are real arcs rather than Tk's spline smoothing, which visibly
    distorts at the large radii this design uses.
    """
    width = abs(x2 - x1)
    height = abs(y2 - y1)
    radius = max(0, min(radius, width / 2, height / 2))

    if radius <= 0:
        return [x1, y1, x2, y1, x2, y2, x1, y2]

    corners = (
        (x1 + radius, y1 + radius, 180),   # top-left
        (x2 - radius, y1 + radius, 270),   # top-right
        (x2 - radius, y2 - radius, 0),     # bottom-right
        (x1 + radius, y2 - radius, 90),    # bottom-left
    )

    points = []
    for cx, cy, start in corners:
        for step in range(steps + 1):
            angle = math.radians(start + 90 * step / steps)
            points.extend([cx + radius * math.cos(angle),
                           cy + radius * math.sin(angle)])
    return points


def rounded_rect(canvas, x1, y1, x2, y2, radius=RADIUS_CARD, fill="", outline="",
                 width=BORDER_WIDTH, steps=10, **kwargs):
    """Draw a rounded rectangle on a canvas and return the item id.

    ``radius=RADIUS_PILL`` produces a true pill: the radius becomes half the
    shorter side.
    """
    if radius >= RADIUS_PILL:
        radius = min(abs(x2 - x1), abs(y2 - y1)) / 2

    points = rounded_points(x1, y1, x2, y2, radius, steps=steps)
    return canvas.create_polygon(
        points, fill=fill, outline=outline, width=width if outline else 0,
        joinstyle="round", **kwargs)


def measure(root, text, font):
    """Pixel width of a string in a given font."""
    try:
        return tkfont.Font(root=root, font=font).measure(text)
    except Exception:  # noqa: BLE001
        return len(text) * 7


def truncate(root, text, font, max_width, ellipsis="…"):
    """Shorten text with an ellipsis so it fits inside ``max_width`` pixels."""
    if measure(root, text, font) <= max_width:
        return text
    low, high = 0, len(text)
    while low < high:
        mid = (low + high) // 2
        if measure(root, text[:mid] + ellipsis, font) <= max_width:
            low = mid + 1
        else:
            high = mid
    return text[:max(0, low - 1)] + ellipsis


def mix(color_a, color_b, ratio=0.5):
    """Blend two ``#rrggbb`` colours — used for hover and heatmap ramps."""
    def _parts(value):
        value = value.lstrip("#")
        return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))

    a, b = _parts(color_a), _parts(color_b)
    blended = tuple(round(a[i] + (b[i] - a[i]) * ratio) for i in range(3))
    return "#%02x%02x%02x" % blended

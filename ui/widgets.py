"""Canvas-drawn widgets in the app's visual language.

Tk's stock widgets cannot express this design — no rounded corners, no control
over border weight, no hover states worth the name. Everything interactive here
is therefore drawn on a Canvas: a rounded body, a 2px ink border, and a label,
with the whole canvas acting as the hit target.

Each widget follows the same contract: construct with a parent and a
:class:`~ui.theme.Theme`, call ``set_theme`` to restyle in place, and read or
write state through plain methods rather than Tk variables.
"""

import tkinter as tk

from . import theme as th


class ThemedFrame(tk.Frame):
    """A plain frame that tracks the theme background."""

    def __init__(self, parent, theme, color="bg", **kwargs):
        self.theme = theme
        self._color_key = color
        super().__init__(parent, bg=theme.get(color), highlightthickness=0,
                         bd=0, **kwargs)

    def set_theme(self, theme):
        self.theme = theme
        self.configure(bg=theme.get(self._color_key))
        for child in self.winfo_children():
            if hasattr(child, "set_theme"):
                child.set_theme(theme)


class Label(tk.Label):
    """A text label bound to a theme colour role."""

    def __init__(self, parent, theme, text="", role="ink", size=11,
                 weight="normal", bg="bg", serif=False, **kwargs):
        self.theme = theme
        self._role = role
        self._bg = bg
        self._size = size
        self._weight = weight
        self._serif = serif
        font = (th.display_font(parent, size, weight) if serif
                else th.ui_font(parent, size, weight))
        super().__init__(parent, text=text, fg=theme.get(role), bg=theme.get(bg),
                         font=font, bd=0, highlightthickness=0, **kwargs)

    def set_theme(self, theme):
        self.theme = theme
        self.configure(fg=theme.get(self._role), bg=theme.get(self._bg))


class Card(tk.Canvas):
    """A rounded, bordered surface that hosts other widgets.

    The body is drawn on the canvas and the content frame is placed on top with
    ``create_window``, so children lay out normally inside the rounded shape.
    """

    def __init__(self, parent, theme, radius=th.RADIUS_CARD, padding=th.SPACE_MD,
                 fill="surface", border="ink", bg="bg", **kwargs):
        self.theme = theme
        self._radius = radius
        self._padding = padding
        self._fill_key = fill
        self._border_key = border
        self._bg_key = bg
        # A caller-supplied height is treated as a minimum, not a ceiling. A
        # canvas does not grow with its contents, so without this any label that
        # wraps to an extra line is silently clipped at the card's edge.
        self._min_height = int(kwargs.pop("height", 0) or 0)
        # Guards the height override below against the auto-sizer's own writes,
        # which would otherwise ratchet the floor up and never let it back down.
        self._sizing = False
        super().__init__(parent, highlightthickness=0, bd=0,
                         bg=theme.get(bg), height=max(1, self._min_height), **kwargs)

        self._shape = None
        self.body = tk.Frame(self, bg=theme.get(fill), bd=0, highlightthickness=0)
        self._window = self.create_window(padding, padding, window=self.body,
                                          anchor="nw")
        self.bind("<Configure>", self._redraw)
        self.body.bind("<Configure>", lambda _e: self._sync_height())

    def configure(self, cnf=None, **kwargs):
        """Treat an explicit height as a new floor, not a one-off.

        Otherwise a caller shrinking a card (a section collapsing, say) is
        immediately overruled by the auto-sizer, which still holds the height
        the card was constructed with.
        """
        if not self._sizing and "height" in kwargs:
            self._min_height = int(kwargs["height"] or 0)
        return super().configure(cnf, **kwargs)

    config = configure

    def _sync_height(self):
        """Resize the canvas to fit whatever the body needs."""
        needed = max(self._min_height,
                     self.body.winfo_reqheight() + self._padding * 2)
        try:
            if abs(needed - int(self.cget("height"))) > 1:
                self._sizing = True
                try:
                    self.configure(height=needed)
                finally:
                    self._sizing = False
        except (tk.TclError, ValueError):
            self._sizing = False

    def _redraw(self, _event=None):
        width, height = self.winfo_width(), self.winfo_height()
        if width <= 1 or height <= 1:
            return
        if self._shape is not None:
            self.delete(self._shape)
        inset = th.BORDER_WIDTH / 2
        self._shape = th.rounded_rect(
            self, inset, inset, width - inset, height - inset,
            radius=self._radius, fill=self.theme.get(self._fill_key),
            outline=self.theme.get(self._border_key), width=th.BORDER_WIDTH)
        self.tag_lower(self._shape)
        # Width is constrained so children wrap; height is left to the body so
        # the card can report how tall it actually needs to be.
        self.itemconfigure(self._window, width=max(1, width - self._padding * 2))
        self._sync_height()

    def set_theme(self, theme):
        self.theme = theme
        self.configure(bg=theme.get(self._bg_key))
        self.body.configure(bg=theme.get(self._fill_key))
        for child in self.body.winfo_children():
            if hasattr(child, "set_theme"):
                child.set_theme(theme)
        self._redraw()


class Button(tk.Canvas):
    """A pill button. ``primary`` fills with the accent; others are outline-only."""

    def __init__(self, parent, theme, text="", command=None, variant="primary",
                 width=None, height=34, size=10, bg="bg", **kwargs):
        self.theme = theme
        self._text = text
        self._command = command
        self._variant = variant
        self._bg_key = bg
        self._height = height
        self._size = size
        self._enabled = True
        self._hovering = False
        self._pressed = False

        font = th.ui_font(parent, size, "bold")
        measured = th.measure(parent, text, font) + th.SPACE_LG * 2
        super().__init__(parent, height=height, width=width or measured,
                         highlightthickness=0, bd=0, bg=theme.get(bg), **kwargs)

        self._shape = None
        self._label = None
        self.bind("<Configure>", self._redraw)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)

    # -- appearance ------------------------------------------------------
    def _palette(self):
        theme = self.theme
        if not self._enabled:
            return theme.get("surface_alt"), theme.get("ink_faint"), theme.get("ink_faint")
        if self._variant == "primary":
            fill = theme.get("accent_hover") if self._hovering else theme.get("accent")
            return fill, theme.get("ink"), theme.get("accent_ink")
        if self._variant == "danger":
            fill = theme.get("danger_soft") if self._hovering else theme.get("bg")
            return fill, theme.get("danger"), theme.get("danger")
        fill = th.mix(theme.get("bg"), theme.get("ink"), 0.06) if self._hovering \
            else theme.get("bg")
        return fill, theme.get("ink"), theme.get("ink")

    def _redraw(self, _event=None):
        width, height = self.winfo_width(), self.winfo_height()
        if width <= 1 or height <= 1:
            return
        self.delete("all")

        fill, border, ink = self._palette()
        offset = 1 if self._pressed and self._enabled else 0
        inset = th.BORDER_WIDTH / 2
        self._shape = th.rounded_rect(
            self, inset, inset + offset, width - inset, height - inset + offset,
            radius=th.RADIUS_PILL, fill=fill, outline=border, width=th.BORDER_WIDTH)
        self._label = self.create_text(
            width / 2, height / 2 + offset, text=self._text, fill=ink,
            font=th.ui_font(self, self._size, "bold"))

    # -- interaction -----------------------------------------------------
    def _on_enter(self, _event):
        if self._enabled:
            self._hovering = True
            self.configure(cursor="hand2")
            self._redraw()

    def _on_leave(self, _event):
        self._hovering = False
        self._pressed = False
        self.configure(cursor="")
        self._redraw()

    def _on_press(self, _event):
        if self._enabled:
            self._pressed = True
            self._redraw()

    def _on_release(self, _event):
        was_pressed = self._pressed
        self._pressed = False
        self._redraw()
        if was_pressed and self._enabled and self._command:
            self._command()

    # -- state -----------------------------------------------------------
    def set_text(self, text):
        self._text = text
        self._redraw()

    def set_enabled(self, enabled):
        self._enabled = bool(enabled)
        self._redraw()

    def set_theme(self, theme):
        self.theme = theme
        self.configure(bg=theme.get(self._bg_key))
        self._redraw()


class Toggle(tk.Canvas):
    """An on/off switch with an ink border, matching the button language."""

    WIDTH = 46
    HEIGHT = 26

    def __init__(self, parent, theme, value=False, command=None, bg="bg", **kwargs):
        self.theme = theme
        self._value = bool(value)
        self._command = command
        self._bg_key = bg
        super().__init__(parent, width=self.WIDTH, height=self.HEIGHT,
                         highlightthickness=0, bd=0, bg=theme.get(bg), **kwargs)
        self.bind("<Button-1>", self._on_click)
        self.bind("<Enter>", lambda _e: self.configure(cursor="hand2"))
        self.bind("<Leave>", lambda _e: self.configure(cursor=""))
        self._redraw()

    def _redraw(self):
        self.delete("all")
        theme = self.theme
        inset = th.BORDER_WIDTH / 2
        fill = theme.get("accent") if self._value else theme.get("surface_alt")
        th.rounded_rect(self, inset, inset, self.WIDTH - inset, self.HEIGHT - inset,
                        radius=th.RADIUS_PILL, fill=fill,
                        outline=theme.get("ink"), width=th.BORDER_WIDTH)

        knob_radius = (self.HEIGHT - 10) / 2
        centre_x = (self.WIDTH - knob_radius - 5) if self._value else (knob_radius + 5)
        centre_y = self.HEIGHT / 2
        self.create_oval(centre_x - knob_radius, centre_y - knob_radius,
                         centre_x + knob_radius, centre_y + knob_radius,
                         fill=theme.get("ink"), outline="")

    def _on_click(self, _event):
        self._value = not self._value
        self._redraw()
        if self._command:
            self._command(self._value)

    def get(self):
        return self._value

    def set(self, value, notify=False):
        self._value = bool(value)
        self._redraw()
        if notify and self._command:
            self._command(self._value)

    def set_theme(self, theme):
        self.theme = theme
        self.configure(bg=theme.get(self._bg_key))
        self._redraw()


class SegmentedControl(tk.Canvas):
    """A row of mutually exclusive options in one bordered pill.

    Used for the cleanup dial, where seeing all four levels at once is the
    point — a dropdown would hide the range.
    """

    def __init__(self, parent, theme, options, value=None, command=None,
                 height=38, bg="bg", size=10, **kwargs):
        self.theme = theme
        self._items = list(options)          # [(value, label)]
        self._value = value or (self._items[0][0] if self._items else None)
        self._command = command
        self._bg_key = bg
        self._height = height
        self._size = size
        self._hover_index = -1
        super().__init__(parent, height=height, highlightthickness=0, bd=0,
                         bg=theme.get(bg), **kwargs)
        self.bind("<Configure>", lambda _e: self._redraw())
        self.bind("<Button-1>", self._on_click)
        self.bind("<Motion>", self._on_motion)
        self.bind("<Leave>", self._on_leave)

    def _segment_width(self):
        width = max(1, self.winfo_width())
        return width / max(1, len(self._items))

    def _redraw(self):
        width, height = self.winfo_width(), self.winfo_height()
        if width <= 1 or height <= 1:
            return
        self.delete("all")
        theme = self.theme
        inset = th.BORDER_WIDTH / 2

        th.rounded_rect(self, inset, inset, width - inset, height - inset,
                        radius=th.RADIUS_PILL, fill=theme.get("bg"),
                        outline=theme.get("ink"), width=th.BORDER_WIDTH)

        segment = self._segment_width()
        for index, (value, label) in enumerate(self._items):
            left = index * segment
            right = left + segment
            selected = value == self._value

            if selected:
                th.rounded_rect(self, left + 2, 2, right - 2, height - 2,
                                radius=th.RADIUS_PILL, fill=theme.get("accent"),
                                outline=theme.get("ink"), width=th.BORDER_WIDTH)
            elif index == self._hover_index:
                th.rounded_rect(self, left + 2, 2, right - 2, height - 2,
                                radius=th.RADIUS_PILL,
                                fill=th.mix(theme.get("bg"), theme.get("ink"), 0.05),
                                outline="", width=0)

            # Text sitting on the accent fill needs the accent's own ink, or it
            # vanishes in dark mode where `ink` is cream.
            self.create_text(left + segment / 2, height / 2, text=label,
                             fill=theme.get("accent_ink") if selected else theme.get("ink"),
                             font=th.ui_font(self, self._size,
                                             "bold" if selected else "normal"))

    def _index_at(self, x):
        segment = self._segment_width()
        index = int(x // segment)
        return index if 0 <= index < len(self._items) else -1

    def _on_click(self, event):
        index = self._index_at(event.x)
        if index < 0:
            return
        value = self._items[index][0]
        if value == self._value:
            return
        self._value = value
        self._redraw()
        if self._command:
            self._command(value)

    def _on_motion(self, event):
        index = self._index_at(event.x)
        if index != self._hover_index:
            self._hover_index = index
            self.configure(cursor="hand2" if index >= 0 else "")
            self._redraw()

    def _on_leave(self, _event):
        self._hover_index = -1
        self.configure(cursor="")
        self._redraw()

    def get(self):
        return self._value

    def set(self, value, notify=False):
        self._value = value
        self._redraw()
        if notify and self._command:
            self._command(value)

    def set_theme(self, theme):
        self.theme = theme
        self.configure(bg=theme.get(self._bg_key))
        self._redraw()


class Entry(tk.Frame):
    """A single-line text field wrapped in the app's bordered pill."""

    def __init__(self, parent, theme, value="", placeholder="", width=24,
                 on_change=None, bg="bg", show=None, **kwargs):
        self.theme = theme
        self._bg_key = bg
        self._placeholder = placeholder
        self._on_change = on_change
        super().__init__(parent, bg=theme.get(bg), highlightthickness=0, bd=0, **kwargs)

        self._canvas = tk.Canvas(self, height=36, highlightthickness=0, bd=0,
                                 bg=theme.get(bg))
        self._canvas.pack(fill=tk.BOTH, expand=True)

        self.var = tk.StringVar(value=value)
        self._show = show
        # The placeholder lives inside the entry rather than on the canvas
        # behind it — the entry paints its own opaque background and would
        # cover any canvas text.
        self._showing_placeholder = False

        self._entry = tk.Entry(
            self._canvas, textvariable=self.var, width=width, bd=0,
            highlightthickness=0, bg=theme.get("surface"), fg=theme.get("ink"),
            insertbackground=theme.get("ink"), font=th.ui_font(parent, 11), show=show)
        self._window = self._canvas.create_window(14, 18, window=self._entry, anchor="w")

        self._canvas.bind("<Configure>", lambda _e: self._redraw())
        self._canvas.bind("<Button-1>", lambda _e: self.focus())
        self._entry.bind("<FocusIn>", self._on_focus_in)
        self._entry.bind("<FocusOut>", self._on_focus_out)
        self.var.trace_add("write", self._on_write)

        if not value:
            self._show_placeholder()

    def _redraw(self):
        canvas = self._canvas
        width, height = canvas.winfo_width(), canvas.winfo_height()
        if width <= 1 or height <= 1:
            return
        canvas.delete("shape")
        inset = th.BORDER_WIDTH / 2
        shape = th.rounded_rect(
            canvas, inset, inset, width - inset, height - inset,
            radius=th.RADIUS_SMALL, fill=self.theme.get("surface"),
            outline=self.theme.get("ink"), width=th.BORDER_WIDTH, tags="shape")
        canvas.tag_lower(shape)
        canvas.itemconfigure(self._window, width=max(1, width - 28))

    def _show_placeholder(self):
        if not self._placeholder or self._showing_placeholder:
            return
        self._showing_placeholder = True
        self._entry.configure(fg=self.theme.get("ink_faint"), show="")
        self.var.set(self._placeholder)

    def _hide_placeholder(self):
        if not self._showing_placeholder:
            return
        self._showing_placeholder = False
        self.var.set("")
        self._entry.configure(fg=self.theme.get("ink"), show=self._show or "")

    def _on_focus_in(self, _event):
        self._hide_placeholder()

    def _on_focus_out(self, _event):
        if not self.var.get():
            self._show_placeholder()

    def _on_write(self, *_args):
        if self._on_change and not self._showing_placeholder:
            self._on_change(self.var.get())

    def get(self):
        """The real value — never the placeholder text."""
        return "" if self._showing_placeholder else self.var.get()

    def set(self, value):
        self._showing_placeholder = False
        self._entry.configure(fg=self.theme.get("ink"), show=self._show or "")
        self.var.set(value or "")
        if not value and not self._entry.focus_get() == self._entry:
            self._show_placeholder()

    def focus(self):
        self._entry.focus_set()

    def set_theme(self, theme):
        self.theme = theme
        self.configure(bg=theme.get(self._bg_key))
        self._canvas.configure(bg=theme.get(self._bg_key))
        self._entry.configure(bg=theme.get("surface"), fg=theme.get("ink"),
                              insertbackground=theme.get("ink"))
        self._redraw()


class Dropdown(tk.Canvas):
    """A bordered select. Opens a themed popup rather than a native combobox."""

    def __init__(self, parent, theme, options, value=None, command=None,
                 width=200, height=36, bg="bg", **kwargs):
        self.theme = theme
        self._items = list(options)          # [(value, label)]
        self._value = value if value is not None else (
            self._items[0][0] if self._items else None)
        self._command = command
        self._bg_key = bg
        self._popup = None
        super().__init__(parent, width=width, height=height, highlightthickness=0,
                         bd=0, bg=theme.get(bg), **kwargs)
        self.bind("<Configure>", lambda _e: self._redraw())
        self.bind("<Button-1>", self._toggle_popup)
        self.bind("<Enter>", lambda _e: self.configure(cursor="hand2"))
        self.bind("<Leave>", lambda _e: self.configure(cursor=""))

    def _label_for(self, value):
        for option_value, label in self._items:
            if option_value == value:
                return label
        return ""

    def _redraw(self):
        width, height = self.winfo_width(), self.winfo_height()
        if width <= 1 or height <= 1:
            return
        self.delete("all")
        theme = self.theme
        inset = th.BORDER_WIDTH / 2
        th.rounded_rect(self, inset, inset, width - inset, height - inset,
                        radius=th.RADIUS_SMALL, fill=theme.get("surface"),
                        outline=theme.get("ink"), width=th.BORDER_WIDTH)

        font = th.ui_font(self, 11)
        label = th.truncate(self, self._label_for(self._value), font, width - 46)
        self.create_text(14, height / 2, text=label, anchor="w",
                         fill=theme.get("ink"), font=font)
        # Chevron
        cx, cy = width - 18, height / 2
        self.create_line(cx - 5, cy - 2, cx, cy + 3, cx + 5, cy - 2,
                         fill=theme.get("ink"), width=2, capstyle="round")

    def _toggle_popup(self, _event=None):
        if self._popup is not None:
            self._close_popup()
            return
        if not self._items:
            return

        self._popup = tk.Toplevel(self)
        self._popup.overrideredirect(True)
        self._popup.configure(bg=self.theme.get("ink"))
        self._popup.attributes("-topmost", True)

        inner = tk.Frame(self._popup, bg=self.theme.get("surface"), bd=0,
                         highlightthickness=0)
        inner.pack(padx=th.BORDER_WIDTH, pady=th.BORDER_WIDTH, fill=tk.BOTH, expand=True)

        # A long list must not run off the screen.
        max_rows = 12
        rows = self._items[:max_rows * 4]
        canvas = None
        if len(rows) > max_rows:
            canvas = tk.Canvas(inner, bg=self.theme.get("surface"), bd=0,
                               highlightthickness=0, height=max_rows * 30,
                               width=self.winfo_width() - 4)
            scrollbar = tk.Scrollbar(inner, orient="vertical", command=canvas.yview)
            holder = tk.Frame(canvas, bg=self.theme.get("surface"))
            holder.bind("<Configure>",
                        lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
            canvas.create_window((0, 0), window=holder, anchor="nw")
            canvas.configure(yscrollcommand=scrollbar.set)
            canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
            container = holder
        else:
            container = inner

        for option_value, label in rows:
            selected = option_value == self._value
            row = tk.Label(
                container, text=label, anchor="w", padx=12, pady=6,
                bg=self.theme.get("accent") if selected else self.theme.get("surface"),
                # On the accent fill the label needs the accent's own ink, or it
                # is cream-on-lilac in dark mode.
                fg=self.theme.get("accent_ink") if selected else self.theme.get("ink"),
                font=th.ui_font(self, 11),
                width=max(18, int(self.winfo_width() / 8)))
            row.pack(fill=tk.X)
            row.bind("<Button-1>", lambda _e, v=option_value: self._choose(v))
            row.bind("<Enter>", lambda e, w=row, s=selected: w.configure(
                bg=self.theme.get("accent") if s
                else th.mix(self.theme.get("surface"), self.theme.get("ink"), 0.07)))
            row.bind("<Leave>", lambda e, w=row, s=selected: w.configure(
                bg=self.theme.get("accent") if s else self.theme.get("surface")))

        self._popup.update_idletasks()
        x = self.winfo_rootx()
        y = self.winfo_rooty() + self.winfo_height() + 4
        self._popup.geometry(f"+{x}+{y}")
        self._popup.bind("<FocusOut>", lambda _e: self._close_popup())
        self._popup.focus_set()

    def _close_popup(self):
        if self._popup is not None:
            self._popup.destroy()
            self._popup = None

    def _choose(self, value):
        self._close_popup()
        if value == self._value:
            return
        self._value = value
        self._redraw()
        if self._command:
            self._command(value)

    def get(self):
        return self._value

    def set(self, value, notify=False):
        self._value = value
        self._redraw()
        if notify and self._command:
            self._command(value)

    def set_options(self, options, keep_value=True):
        self._items = list(options)
        if not keep_value or self._value not in [v for v, _ in self._items]:
            self._value = self._items[0][0] if self._items else None
        self._redraw()

    def set_theme(self, theme):
        self.theme = theme
        self.configure(bg=theme.get(self._bg_key))
        self._redraw()


class TextArea(tk.Frame):
    """A multi-line field with the same bordered treatment as :class:`Entry`."""

    def __init__(self, parent, theme, value="", height=8, bg="bg", **kwargs):
        self.theme = theme
        self._bg_key = bg
        super().__init__(parent, bg=theme.get(bg), highlightthickness=0,
                         bd=th.BORDER_WIDTH, **kwargs)
        self.configure(highlightbackground=theme.get("ink"),
                       highlightcolor=theme.get("ink"), highlightthickness=th.BORDER_WIDTH,
                       bd=0)

        self.text = tk.Text(self, height=height, wrap=tk.WORD, bd=0,
                            highlightthickness=0, padx=12, pady=10,
                            bg=theme.get("surface"), fg=theme.get("ink"),
                            insertbackground=theme.get("ink"),
                            font=th.ui_font(parent, 11))
        scrollbar = tk.Scrollbar(self, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=scrollbar.set)
        self.text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        if value:
            self.text.insert("1.0", value)

    def get(self):
        return self.text.get("1.0", tk.END).rstrip("\n")

    def set(self, value):
        self.text.delete("1.0", tk.END)
        self.text.insert("1.0", value or "")

    def set_theme(self, theme):
        self.theme = theme
        self.configure(bg=theme.get(self._bg_key),
                       highlightbackground=theme.get("ink"),
                       highlightcolor=theme.get("ink"))
        self.text.configure(bg=theme.get("surface"), fg=theme.get("ink"),
                            insertbackground=theme.get("ink"))


class ScrollFrame(tk.Frame):
    """A vertically scrolling container that tracks its parent's width."""

    def __init__(self, parent, theme, bg="bg", **kwargs):
        self.theme = theme
        self._bg_key = bg
        super().__init__(parent, bg=theme.get(bg), highlightthickness=0, bd=0, **kwargs)

        self.canvas = tk.Canvas(self, bg=theme.get(bg), highlightthickness=0, bd=0)
        self._scrollbar = tk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self._style_scrollbar()
        self.body = tk.Frame(self.canvas, bg=theme.get(bg), bd=0, highlightthickness=0)

        self._window = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.canvas.configure(yscrollcommand=self._scrollbar.set)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.body.bind("<Configure>", self._on_body_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)

        # The wheel is bound application-wide and filtered by pointer position.
        # Binding on the canvas's Enter/Leave does not work: moving the pointer
        # onto the embedded body window counts as leaving the canvas, so the
        # binding was dropped the moment the pointer reached the content.
        self.bind("<Destroy>", lambda _e: self._unbind_wheel())
        self._bind_wheel()

    def _style_scrollbar(self):
        """Tk scrollbars default to system grey, which fights the cream ground."""
        try:
            self._scrollbar.configure(
                bg=self.theme.get("surface_alt"),
                activebackground=self.theme.get("ink_faint"),
                troughcolor=self.theme.get("bg"),
                highlightthickness=0, bd=0, relief="flat",
                elementborderwidth=0, width=10)
        except tk.TclError:
            pass    # platform themes reject some of these options

    def _on_body_configure(self, _event):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event):
        self.canvas.itemconfigure(self._window, width=event.width)

    def _bind_wheel(self):
        self.canvas.bind_all("<MouseWheel>", self._on_wheel)
        self.canvas.bind_all("<Button-4>", self._on_wheel)
        self.canvas.bind_all("<Button-5>", self._on_wheel)

    def _unbind_wheel(self):
        self.canvas.unbind_all("<MouseWheel>")
        self.canvas.unbind_all("<Button-4>")
        self.canvas.unbind_all("<Button-5>")

    def _pointer_is_inside(self, event):
        """True when the pointer sits over this frame or one of its children."""
        try:
            widget = self.winfo_containing(event.x_root, event.y_root)
        except (tk.TclError, KeyError):
            return False
        while widget is not None:
            if widget is self:
                return True
            widget = getattr(widget, "master", None)
        return False

    def _on_wheel(self, event):
        if not self._pointer_is_inside(event):
            return
        if getattr(event, "num", None) == 4:
            delta = -1
        elif getattr(event, "num", None) == 5:
            delta = 1
        else:
            delta = -1 if event.delta > 0 else 1
        self.canvas.yview_scroll(delta, "units")

    def clear(self):
        for child in self.body.winfo_children():
            child.destroy()

    def scroll_to_top(self):
        self.canvas.yview_moveto(0)

    def set_theme(self, theme):
        self.theme = theme
        color = theme.get(self._bg_key)
        self.configure(bg=color)
        self.canvas.configure(bg=color)
        self.body.configure(bg=color)
        self._style_scrollbar()
        for child in self.body.winfo_children():
            if hasattr(child, "set_theme"):
                child.set_theme(theme)


class StatTile(tk.Canvas):
    """A single large numeral with a small-caps label — the stats vocabulary."""

    def __init__(self, parent, theme, value="0", label="", caption="",
                 width=180, height=118, bg="bg", accent=False, **kwargs):
        self.theme = theme
        self._value = value
        self._label = label
        self._caption = caption
        self._accent = accent
        self._bg_key = bg
        super().__init__(parent, width=width, height=height, highlightthickness=0,
                         bd=0, bg=theme.get(bg), **kwargs)
        self.bind("<Configure>", lambda _e: self._redraw())

    def _redraw(self):
        width, height = self.winfo_width(), self.winfo_height()
        if width <= 1 or height <= 1:
            return
        self.delete("all")
        theme = self.theme
        inset = th.BORDER_WIDTH / 2
        th.rounded_rect(
            self, inset, inset, width - inset, height - inset,
            radius=th.RADIUS_CARD,
            fill=theme.get("accent") if self._accent else theme.get("surface"),
            outline=theme.get("ink"), width=th.BORDER_WIDTH)

        # On the accent fill, every text role collapses to the accent's ink —
        # the soft/faint greys are tuned for the surface colour, not this one.
        if self._accent:
            numeral = label_color = theme.get("accent_ink")
            caption_color = th.mix(theme.get("accent_ink"), theme.get("accent"), 0.45)
        else:
            numeral = theme.get("ink")
            label_color = theme.get("ink_soft")
            caption_color = theme.get("ink_faint")

        # Step the numeral down for long values ("8h 25m") so it stays inside
        # the tile instead of running under the border.
        size = 30
        for candidate in (30, 26, 22, 19):
            size = candidate
            if th.measure(self, self._value,
                          th.display_font(self, candidate, "bold")) <= width - 28:
                break

        self.create_text(width / 2, height / 2 - (10 if self._caption else 4),
                         text=self._value, fill=numeral,
                         font=th.display_font(self, size, "bold"))
        self.create_text(width / 2, height / 2 + (16 if self._caption else 20),
                         text=self._label.upper(), fill=label_color,
                         font=th.ui_font(self, 8, "bold"))
        if self._caption:
            self.create_text(width / 2, height - 16, text=self._caption,
                             fill=caption_color, font=th.ui_font(self, 8),
                             width=width - 20)

    def update_values(self, value=None, label=None, caption=None):
        if value is not None:
            self._value = value
        if label is not None:
            self._label = label
        if caption is not None:
            self._caption = caption
        self._redraw()

    def set_theme(self, theme):
        self.theme = theme
        self.configure(bg=theme.get(self._bg_key))
        self._redraw()


class Heatmap(tk.Canvas):
    """A calendar grid of daily activity, densest column last."""

    CELL = 12
    GAP = 3

    def __init__(self, parent, theme, data=None, bg="bg", **kwargs):
        self.theme = theme
        self._data = data or []
        self._bg_key = bg
        rows = 7
        columns = max(1, (len(self._data) + rows - 1) // rows)
        super().__init__(parent, highlightthickness=0, bd=0, bg=theme.get(bg),
                         height=rows * (self.CELL + self.GAP) + 4,
                         width=columns * (self.CELL + self.GAP) + 4, **kwargs)
        self.bind("<Configure>", lambda _e: self._redraw())

    def set_data(self, data):
        self._data = data or []
        self._redraw()

    def _redraw(self):
        self.delete("all")
        if not self._data:
            return
        theme = self.theme
        peak = max((count for _, count in self._data), default=0)

        for index, (day, count) in enumerate(self._data):
            column, row = divmod(index, 7)
            x = 2 + column * (self.CELL + self.GAP)
            y = 2 + row * (self.CELL + self.GAP)
            if count <= 0 or peak <= 0:
                color = theme.get("grid_empty")
            else:
                # Square-root ramp so a single busy day does not flatten the rest.
                intensity = min(1.0, (count / peak) ** 0.5)
                color = th.mix(theme.get("grid_empty"), theme.get("secondary"),
                               0.25 + 0.75 * intensity)
            self.create_rectangle(x, y, x + self.CELL, y + self.CELL,
                                  fill=color, outline="", tags=(f"day-{day}",))

    def set_theme(self, theme):
        self.theme = theme
        self.configure(bg=theme.get(self._bg_key))
        self._redraw()


class Divider(tk.Frame):
    def __init__(self, parent, theme, bg="bg", **kwargs):
        self.theme = theme
        self._bg_key = bg
        super().__init__(parent, height=1, bg=theme.get("shadow"),
                         highlightthickness=0, bd=0, **kwargs)

    def set_theme(self, theme):
        self.theme = theme
        self.configure(bg=theme.get("shadow"))


class Badge(tk.Canvas):
    """A small pill label used for tags such as the source app or cleanup level."""

    def __init__(self, parent, theme, text="", role="ink_soft", bg="bg", **kwargs):
        self.theme = theme
        self._text = text
        self._role = role
        self._bg_key = bg
        font = th.ui_font(parent, 8, "bold")
        width = th.measure(parent, text, font) + 18
        super().__init__(parent, width=width, height=20, highlightthickness=0,
                         bd=0, bg=theme.get(bg), **kwargs)
        self.bind("<Configure>", lambda _e: self._redraw())

    def _redraw(self):
        width, height = self.winfo_width(), self.winfo_height()
        if width <= 1 or height <= 1:
            return
        self.delete("all")
        th.rounded_rect(self, 1, 1, width - 1, height - 1, radius=th.RADIUS_PILL,
                        fill=self.theme.get("surface_alt"),
                        outline=self.theme.get("shadow"), width=1)
        self.create_text(width / 2, height / 2, text=self._text,
                         fill=self.theme.get(self._role),
                         font=th.ui_font(self, 8, "bold"))

    def set_theme(self, theme):
        self.theme = theme
        self.configure(bg=theme.get(self._bg_key))
        self._redraw()

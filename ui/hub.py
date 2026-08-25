"""The Flow Hub — the app's main window.

A desktop utility with seven destinations reads better as a console with a
persistent left sidebar than as a stack of preference tabs, so that is what this
is: a fixed sidebar, a scrolling content area, and one page per destination.

Pages are self-contained. Each subclasses :class:`Page`, is constructed lazily
the first time it is shown, and refreshes its data in ``on_show`` rather than at
construction — so the Hub opens instantly and pages never display stale state.
"""

import logging
import tkinter as tk

import config_manager
from . import theme as th
from . import widgets as w

logger = logging.getLogger(__name__)


class Page(w.ThemedFrame):
    """Base class for a Hub destination."""

    title = ""
    subtitle = ""

    def __init__(self, parent, hub):
        super().__init__(parent, hub.theme)
        self.hub = hub
        self.build()

    def build(self):
        """Construct the page once. Subclasses override."""

    def on_show(self):
        """Called every time the page becomes visible. Refresh data here."""


class SidebarItem(tk.Canvas):
    """One navigation row: an icon glyph, a label, and a filled active state."""

    HEIGHT = 40

    def __init__(self, parent, theme, glyph, label, command, **kwargs):
        self.theme = theme
        self._glyph = glyph
        self._label = label
        self._command = command
        self._active = False
        self._hovering = False
        super().__init__(parent, height=self.HEIGHT, highlightthickness=0, bd=0,
                         bg=theme.get("surface_alt"), **kwargs)
        self.bind("<Configure>", lambda _e: self._redraw())
        self.bind("<Button-1>", lambda _e: self._command())
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)

    def _redraw(self):
        width, height = self.winfo_width(), self.winfo_height()
        if width <= 1 or height <= 1:
            return
        self.delete("all")
        theme = self.theme

        if self._active:
            th.rounded_rect(self, 1, 2, width - 8, height - 2,
                            radius=th.RADIUS_SMALL, fill=theme.get("accent"),
                            outline=theme.get("ink"), width=th.BORDER_WIDTH)
            ink = theme.get("accent_ink")
        else:
            if self._hovering:
                th.rounded_rect(self, 1, 2, width - 8, height - 2,
                                radius=th.RADIUS_SMALL,
                                fill=th.mix(theme.get("surface_alt"),
                                            theme.get("ink"), 0.06),
                                outline="", width=0)
            ink = theme.get("ink")

        self.create_text(20, height / 2, text=self._glyph, fill=ink,
                         font=th.ui_font(self, 12))
        self.create_text(40, height / 2, text=self._label, anchor="w", fill=ink,
                         font=th.ui_font(self, 11, "bold" if self._active else "normal"))

    def _on_enter(self, _event):
        self._hovering = True
        self.configure(cursor="hand2")
        self._redraw()

    def _on_leave(self, _event):
        self._hovering = False
        self.configure(cursor="")
        self._redraw()

    def set_active(self, active):
        self._active = bool(active)
        self._redraw()

    def set_theme(self, theme):
        self.theme = theme
        self.configure(bg=theme.get("surface_alt"))
        self._redraw()


class Hub(tk.Toplevel):
    """The main window. One instance at a time; re-opening raises the existing one."""

    # (key, glyph, label, page factory path)
    DESTINATIONS = [
        ("home", "◉", "Home"),
        ("dictionary", "☷", "Dictionary"),
        ("snippets", "⊕", "Snippets"),
        ("styles", "✿", "Styles"),
        ("insights", "░", "Insights"),
        ("settings", "⚙", "Settings"),
    ]

    def __init__(self, master, config, on_config_changed=None, app=None):
        super().__init__(master)
        self.config_data = config
        self.on_config_changed = on_config_changed
        self.app = app
        self.theme = th.Theme((config.get("ui") or {}).get("theme", "light"))

        self.title("LocalDictation")
        self.geometry("1040x700")
        self.minsize(880, 560)
        self.configure(bg=self.theme.bg)

        self._pages = {}
        self._items = {}
        self._current = None
        self._toast = None
        self._toast_job = None

        self._build()
        self.navigate("home")

        self.protocol("WM_DELETE_WINDOW", self.hide)
        self.bind("<Escape>", lambda _e: self.hide())

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------
    def _build(self):
        self.sidebar = tk.Frame(self, bg=self.theme.surface_alt,
                                width=th.SIDEBAR_WIDTH, bd=0, highlightthickness=0)
        self.sidebar.pack(side=tk.LEFT, fill=tk.Y)
        self.sidebar.pack_propagate(False)

        header = tk.Frame(self.sidebar, bg=self.theme.surface_alt)
        header.pack(fill=tk.X, padx=th.SPACE_MD, pady=(th.SPACE_LG, th.SPACE_MD))
        w.Label(header, self.theme, text="LocalDictation", size=13, weight="bold",
                bg="surface_alt").pack(anchor="w")
        self._hotkey_label = w.Label(header, self.theme, text="", role="ink_faint",
                                     size=9, bg="surface_alt")
        self._hotkey_label.pack(anchor="w", pady=(2, 0))

        nav = tk.Frame(self.sidebar, bg=self.theme.surface_alt)
        nav.pack(fill=tk.X, padx=(th.SPACE_MD, 0))
        for key, glyph, label in self.DESTINATIONS:
            item = SidebarItem(nav, self.theme, glyph, label,
                               command=lambda k=key: self.navigate(k))
            item.pack(fill=tk.X, pady=1)
            self._items[key] = item

        footer = tk.Frame(self.sidebar, bg=self.theme.surface_alt)
        footer.pack(side=tk.BOTTOM, fill=tk.X, padx=th.SPACE_MD, pady=th.SPACE_MD)
        self._theme_button = w.Button(
            footer, self.theme, text="Dark mode", variant="secondary",
            command=self.toggle_theme, bg="surface_alt", size=9, height=30)
        self._theme_button.pack(fill=tk.X)
        self._sync_theme_button()

        # Content column
        self.content = tk.Frame(self, bg=self.theme.bg)
        self.content.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.header = tk.Frame(self.content, bg=self.theme.bg)
        self.header.pack(fill=tk.X, padx=th.SPACE_XL, pady=(th.SPACE_LG, th.SPACE_SM))
        self._title = w.Label(self.header, self.theme, text="", size=20,
                              weight="bold", serif=True)
        self._title.pack(anchor="w")
        self._subtitle = w.Label(self.header, self.theme, text="", role="ink_soft",
                                 size=10)
        self._subtitle.pack(anchor="w", pady=(2, 0))

        self.page_host = tk.Frame(self.content, bg=self.theme.bg)
        self.page_host.pack(fill=tk.BOTH, expand=True)

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------
    def _page_class(self, key):
        # Imported lazily so a broken page cannot stop the Hub from opening.
        if key == "home":
            from .page_home import HomePage
            return HomePage
        if key == "dictionary":
            from .page_lists import DictionaryPage
            return DictionaryPage
        if key == "snippets":
            from .page_lists import SnippetsPage
            return SnippetsPage
        if key == "styles":
            from .page_styles import StylesPage
            return StylesPage
        if key == "insights":
            from .page_insights import InsightsPage
            return InsightsPage
        if key == "settings":
            from .page_settings import SettingsPage
            return SettingsPage
        return None

    def navigate(self, key):
        if key not in self._items:
            return

        if key not in self._pages:
            page_class = self._page_class(key)
            if page_class is None:
                return
            try:
                self._pages[key] = page_class(self.page_host, self)
            except Exception as e:  # noqa: BLE001 — one bad page must not wedge the app
                logger.exception("Could not build page %r: %s", key, e)
                self.toast(f"Could not open {key}.")
                return

        if self._current is not None:
            self._current.pack_forget()

        page = self._pages[key]
        page.pack(fill=tk.BOTH, expand=True)
        self._current = page

        for item_key, item in self._items.items():
            item.set_active(item_key == key)

        self._title.configure(text=page.title)
        self._subtitle.configure(text=page.subtitle)

        try:
            page.on_show()
        except Exception as e:  # noqa: BLE001
            logger.exception("on_show failed for %r: %s", key, e)

        self.refresh_hotkey_label()

    def refresh_hotkey_label(self):
        hotkeys = self.config_data.get("hotkeys", {}) or {}
        binding = hotkeys.get("push_to_talk") or hotkeys.get("hands_free") or "not set"
        self._hotkey_label.configure(text=f"Hold {binding} to dictate")

    # ------------------------------------------------------------------
    # Config plumbing
    # ------------------------------------------------------------------
    def save(self, notify=True):
        """Persist config and let the running app apply the change."""
        config_manager.save_config(self.config_data)
        if notify and self.on_config_changed:
            try:
                self.on_config_changed(self.config_data)
            except Exception as e:  # noqa: BLE001
                logger.exception("Config change handler failed: %s", e)

    def set_config(self, path, value, notify=True):
        config_manager.set_value(self.config_data, path, value)
        self.save(notify=notify)

    def get_config(self, path, default=None):
        return config_manager.get_value(self.config_data, path, default)

    # ------------------------------------------------------------------
    # Theme
    # ------------------------------------------------------------------
    def toggle_theme(self):
        previous = self.theme
        mode = "dark" if self.theme.mode == "light" else "light"
        self.theme = th.Theme(mode)
        self.set_config("ui.theme", mode)
        self._apply_theme(previous=previous)

    def _sync_theme_button(self):
        self._theme_button.set_text(
            "Light mode" if self.theme.mode == "dark" else "Dark mode")

    def _apply_theme(self, previous=None):
        # Plain frames carry no theme knowledge, so their new colour is derived
        # from their old one. Repainting them all with the page background would
        # flatten every card interior — a frame inside a Card is "surface", not
        # "bg", and only the old palette can tell them apart.
        translation = {}
        if previous is not None:
            for role, old_color in previous.colors.items():
                translation.setdefault(old_color, self.theme.get(role))

        def walk(widget):
            for child in widget.winfo_children():
                if hasattr(child, "set_theme"):
                    child.set_theme(self.theme)
                else:
                    try:
                        replacement = translation.get(str(child.cget("bg")))
                        if replacement:
                            child.configure(bg=replacement)
                    except tk.TclError:
                        pass
                walk(child)

        walk(self)

        # The chrome is set last, deliberately. Setting it first and then
        # walking would feed those widgets' brand-new colours back into the
        # translation table, which maps the new background to whatever role held
        # that value in the old palette — repainting the page with the ink colour.
        self.configure(bg=self.theme.bg)
        self.sidebar.configure(bg=self.theme.surface_alt)
        self.content.configure(bg=self.theme.bg)
        self.header.configure(bg=self.theme.bg)
        self.page_host.configure(bg=self.theme.bg)

        self._sync_theme_button()

    # ------------------------------------------------------------------
    # Feedback
    # ------------------------------------------------------------------
    def toast(self, message, duration=2600):
        """A transient message strip at the bottom of the content area."""
        if self._toast is not None:
            self._toast.destroy()
            self._toast = None
        if self._toast_job is not None:
            try:
                self.after_cancel(self._toast_job)
            except (ValueError, tk.TclError):
                pass
            self._toast_job = None

        holder = tk.Frame(self.content, bg=self.theme.bg)
        holder.place(relx=0.5, rely=1.0, anchor="s", y=-18)
        card = w.Card(holder, self.theme, radius=th.RADIUS_PILL, padding=10,
                      height=42, width=max(220, len(message) * 8 + 60))
        card.pack()
        w.Label(card.body, self.theme, text=message, size=10,
                bg="surface").pack(anchor="center")
        self._toast = holder
        self._toast_job = self.after(duration, self._clear_toast)

    def _clear_toast(self):
        if self._toast is not None:
            self._toast.destroy()
            self._toast = None
        self._toast_job = None

    # ------------------------------------------------------------------
    # Window lifecycle
    # ------------------------------------------------------------------
    def show(self, page=None):
        self.deiconify()
        self.lift()
        self.focus_force()
        if page:
            self.navigate(page)
        elif self._current is not None:
            try:
                self._current.on_show()
            except Exception as e:  # noqa: BLE001
                logger.exception("on_show failed: %s", e)

    def hide(self):
        self.withdraw()

    def refresh_current(self):
        if self._current is not None:
            self._current.on_show()

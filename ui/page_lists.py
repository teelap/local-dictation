"""Dictionary and Snippets — the two hand-maintained lists.

Both pages are the same object with different nouns: a filtered, sorted list of
short records, each row editable in place, the whole thing importable and
exportable as JSON. So the chrome — search, add, import/export, the scrolling
body, the modal editor — lives once in :class:`ListPage`, and the subclasses
supply only their data layer and their row.

Neither data layer stores timestamps. Storage order *is* insertion order, so
"newest" means "further down the file", which is why the sorts below work off
list position rather than a date field.
"""

import logging
import tkinter as tk

import dictionary
import snippets
from . import theme as th
from . import widgets as w
from .hub import Page

logger = logging.getLogger(__name__)

# How much of a snippet's expansion is worth showing in a row.
PREVIEW_CHARS = 90


def _capped_entry(parent, theme, maximum, **kwargs):
    """An :class:`~ui.widgets.Entry` that will not hold more than ``maximum`` chars.

    The data layers truncate silently on save; clamping as the user types means
    the field always shows exactly what is going to be stored.
    """
    box = {}

    def clamp(value):
        if len(value) > maximum:
            box["entry"].set(value[:maximum])

    box["entry"] = w.Entry(parent, theme, on_change=clamp, **kwargs)
    return box["entry"]


class ListPage(Page):
    """Shared chrome for the two list pages. Not registered with the Hub itself."""

    # -- subclass contract ---------------------------------------------
    key_field = "term"              # the dict key holding the row's identity
    search_placeholder = "Search…"
    add_label = "Add new"
    export_name = "export.json"
    sort_options = ()               # [(value, label)]
    default_sort = None

    def fetch(self):
        """Every record, in storage order. Subclasses override."""
        return []

    def matches(self, entry, query):
        return query in entry.get(self.key_field, "").lower()

    def build_row(self, parent, entry):
        raise NotImplementedError

    def render_empty(self, parent):
        raise NotImplementedError

    def open_editor(self, entry=None):
        raise NotImplementedError

    def import_text(self, payload):
        """-> (added, skipped, error)"""
        return 0, 0, "Import is not available here."

    def export_text(self):
        return "[]"

    # ------------------------------------------------------------------
    def build(self):
        theme = self.hub.theme

        # -- Search, sort and actions -------------------------------------
        controls = tk.Frame(self, bg=theme.bg)
        controls.pack(fill=tk.X, padx=th.SPACE_XL, pady=(0, th.SPACE_SM))

        # The fixed-width controls are packed to the right first so they reserve
        # their space; the search box then expands into whatever is left. Packing
        # the expanding widget first pushes the last buttons off the edge.
        w.Button(controls, theme, text="Export", variant="secondary", size=9,
                 height=32, command=self._export).pack(side=tk.RIGHT, padx=(6, 0))
        w.Button(controls, theme, text="Import", variant="secondary", size=9,
                 height=32, command=self._import).pack(side=tk.RIGHT, padx=(6, 0))
        w.Button(controls, theme, text=self.add_label, size=9, height=32,
                 command=self.open_editor).pack(side=tk.RIGHT, padx=(th.SPACE_SM, 0))

        self._sort = None
        if self.sort_options:
            self._sort = w.Dropdown(controls, theme, options=list(self.sort_options),
                                    value=self.default_sort, width=142, height=32,
                                    command=lambda _v: self.refresh())
            self._sort.pack(side=tk.RIGHT, padx=(th.SPACE_SM, 0))

        self._search = w.Entry(controls, theme, placeholder=self.search_placeholder,
                               on_change=lambda _v: self._schedule_refresh())
        self._search.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # -- List ---------------------------------------------------------
        self._list = w.ScrollFrame(self, theme)
        self._list.pack(fill=tk.BOTH, expand=True, padx=th.SPACE_XL,
                        pady=(0, th.SPACE_MD))

        self._refresh_job = None

    def on_show(self):
        self.refresh()

    def _schedule_refresh(self):
        """Debounce search typing so each keystroke does not rebuild the list."""
        if self._refresh_job is not None:
            try:
                self.after_cancel(self._refresh_job)
            except (ValueError, tk.TclError):
                pass
        self._refresh_job = self.after(180, self.refresh)

    # ------------------------------------------------------------------
    def refresh(self):
        self._refresh_job = None
        self._list.clear()

        try:
            entries = self.fetch()
        except Exception as e:  # noqa: BLE001 — a broken store must not blank the Hub
            logger.exception("Could not read the list: %s", e)
            self.hub.toast("Could not read that list.")
            return

        query = self._search.get().strip().lower()
        visible = [e for e in entries if not query or self.matches(e, query)]

        if not visible:
            if entries:
                self._render_no_matches(query)
            else:
                self.render_empty(self._list.body)
            return

        for entry in self._sorted(visible):
            self.build_row(self._list.body, entry)

    def _sorted(self, entries):
        """Order rows by the dropdown. Input is in storage (oldest-first) order."""
        key = self._sort.get() if self._sort else "newest"
        if key == "oldest":
            return list(entries)
        if key == "alpha":
            return sorted(entries, key=lambda e: e.get(self.key_field, "").lower())
        newest = list(reversed(entries))
        if key == "starred":
            # Stable, so starred entries stay newest-first among themselves.
            return sorted(newest, key=lambda e: not e.get("starred"))
        return newest

    def _render_no_matches(self, query):
        theme = self.hub.theme
        holder = tk.Frame(self._list.body, bg=theme.bg)
        holder.pack(fill=tk.X, pady=60)
        w.Label(holder, theme, text="No matches", size=13, weight="bold").pack()
        w.Label(holder, theme, role="ink_faint", size=10,
                text=f"Nothing here contains “{query}”.").pack(pady=(4, 0))

    def _empty_state(self, parent, headline, body_text):
        theme = self.hub.theme
        holder = tk.Frame(parent, bg=theme.bg)
        holder.pack(fill=tk.X, pady=56)
        w.Label(holder, theme, text=headline, size=13, weight="bold").pack()
        w.Label(holder, theme, role="ink_faint", size=10, text=body_text,
                wraplength=460, justify=tk.CENTER).pack(pady=(6, th.SPACE_MD))
        w.Button(holder, theme, text=self.add_label, size=9, height=32,
                 command=self.open_editor).pack()

    # ------------------------------------------------------------------
    # Modal editor
    # ------------------------------------------------------------------
    def _open_modal(self, heading, width, height):
        """A themed, application-modal dialog centred over the Hub.

        Tk offers no dialog primitive that can be styled like the rest of this
        app, so the modal is an ordinary Toplevel: ``transient`` keeps it above
        the Hub, ``grab_set`` makes it modal.
        """
        theme = self.hub.theme
        window = tk.Toplevel(self.hub)
        window.title(heading)
        window.configure(bg=theme.bg)
        window.resizable(False, False)
        window.transient(self.hub)

        self.hub.update_idletasks()
        x = self.hub.winfo_rootx() + max(0, (self.hub.winfo_width() - width) // 2)
        y = self.hub.winfo_rooty() + max(0, (self.hub.winfo_height() - height) // 3)
        window.geometry(f"{width}x{height}+{x}+{y}")

        body = tk.Frame(window, bg=theme.bg)
        body.pack(fill=tk.BOTH, expand=True, padx=th.SPACE_LG, pady=th.SPACE_LG)
        w.Label(body, theme, text=heading, size=15, weight="bold",
                serif=True).pack(anchor="w")

        window.bind("<Escape>", lambda _e: window.destroy())
        try:
            # Grabbing an unmapped window raises; wait for it to appear first.
            window.wait_visibility()
            window.grab_set()
        except tk.TclError as e:
            logger.debug("Modal grab failed: %s", e)
        return window, body

    def _modal_buttons(self, body, window, on_save):
        theme = self.hub.theme
        row = tk.Frame(body, bg=theme.bg)
        row.pack(side=tk.BOTTOM, fill=tk.X, pady=(th.SPACE_LG, 0))
        w.Button(row, theme, text="Save", size=10, command=on_save).pack(side=tk.RIGHT)
        w.Button(row, theme, text="Cancel", variant="secondary", size=10,
                 command=window.destroy).pack(side=tk.RIGHT, padx=(0, th.SPACE_SM))
        return row

    @staticmethod
    def _field_label(parent, theme, text):
        w.Label(parent, theme, text=text, role="ink_soft", size=9,
                weight="bold").pack(anchor="w", pady=(th.SPACE_MD, 4))

    # ------------------------------------------------------------------
    # Import / export
    # ------------------------------------------------------------------
    def _import(self):
        from tkinter import filedialog

        path = filedialog.askopenfilename(
            parent=self.hub, filetypes=[("JSON", "*.json"), ("All files", "*.*")])
        if not path:
            return

        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = handle.read()
        except OSError as e:
            logger.warning("Import failed: %s", e)
            self.hub.toast("Could not read that file.")
            return

        added, skipped, error = self.import_text(payload)
        self.refresh()
        self.hub.toast(error or f"Imported {added}, skipped {skipped}.")

    def _export(self):
        from tkinter import filedialog

        path = filedialog.asksaveasfilename(
            parent=self.hub, defaultextension=".json",
            filetypes=[("JSON", "*.json")], initialfile=self.export_name)
        if not path:
            return

        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(self.export_text())
            self.hub.toast(f"Exported to {path}")
        except OSError as e:
            logger.warning("Export failed: %s", e)
            self.hub.toast("Could not write that file.")

    def _confirm(self, heading, question):
        from tkinter import messagebox

        return messagebox.askyesno(heading, question, parent=self.hub)


class DictionaryPage(ListPage):
    title = "Dictionary"
    subtitle = "Words, names, and jargon that should always come out right"

    key_field = "term"
    search_placeholder = "Search your dictionary…"
    export_name = "dictionary.json"
    sort_options = (("starred", "Starred first"), ("newest", "Newest"),
                    ("oldest", "Oldest"), ("alpha", "A–Z"))
    default_sort = "starred"

    # ------------------------------------------------------------------
    def fetch(self):
        return dictionary.get_entries()

    def matches(self, entry, query):
        haystack = [entry.get("term", "")] + list(entry.get("sounds_like") or [])
        return any(query in part.lower() for part in haystack)

    def import_text(self, payload):
        return dictionary.import_entries(payload)

    def export_text(self):
        return dictionary.export_entries()

    # ------------------------------------------------------------------
    def build_row(self, parent, entry):
        theme = self.hub.theme
        aliases = [a for a in (entry.get("sounds_like") or []) if a]

        card = w.Card(parent, theme, radius=th.RADIUS_SMALL, padding=14,
                      height=76 if aliases else 56)
        card.pack(fill=tk.X, pady=4)

        line = tk.Frame(card.body, bg=theme.surface)
        line.pack(fill=tk.X)

        # Actions claim the right edge before the term label expands into it.
        actions = tk.Frame(line, bg=theme.surface)
        actions.pack(side=tk.RIGHT)
        w.Button(actions, theme, text="Edit", variant="secondary", size=9,
                 height=26, bg="surface",
                 command=lambda e=entry: self.open_editor(e)).pack(side=tk.LEFT, padx=3)
        w.Button(actions, theme, text="✕", variant="secondary", size=9, height=26,
                 width=30, bg="surface",
                 command=lambda e=entry: self._delete(e)).pack(side=tk.LEFT, padx=3)

        starred = bool(entry.get("starred"))
        star = w.Label(line, theme, text="★" if starred else "☆",
                       role="warning" if starred else "ink_faint", size=14,
                       bg="surface", cursor="hand2")
        star.pack(side=tk.LEFT, padx=(0, th.SPACE_SM))
        star.bind("<Button-1>", lambda _e, t=entry["term"]: self._toggle_star(t))

        w.Label(line, theme, text=entry["term"], size=11, weight="bold",
                bg="surface").pack(side=tk.LEFT)

        # "auto" means the entry was learned from a correction rather than typed;
        # saying so is what stops the list feeling like it grew things by itself.
        if entry.get("auto"):
            w.Badge(line, theme, text="AUTO", bg="surface").pack(side=tk.LEFT,
                                                                 padx=(th.SPACE_SM, 0))

        if aliases:
            w.Label(card.body, theme, text="sounds like: " + ", ".join(aliases),
                    role="ink_faint", size=9, bg="surface", anchor="w",
                    wraplength=620, justify=tk.LEFT).pack(anchor="w", padx=(26, 0),
                                                          pady=(4, 0))

    def render_empty(self, parent):
        self._empty_state(
            parent, "Your dictionary is empty",
            "Terms you add here are fed to the recognizer before it decodes, so "
            "names, jargon and acronyms stop coming out wrong — and anything it "
            "mangles anyway gets corrected afterwards.")

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------
    def _toggle_star(self, term):
        entry = dictionary.find(term)
        if entry is None or not dictionary.toggle_star(term):
            self.hub.toast("Could not update that term.")
            return
        self.refresh()
        self.hub.toast(f"Unstarred {term}." if entry["starred"] else f"Starred {term}.")

    def _delete(self, entry):
        term = entry["term"]
        if not self._confirm("Remove term",
                             f"Remove “{term}” from your dictionary?"):
            return
        if not dictionary.remove(term):
            self.hub.toast("Could not remove that term.")
            return
        self.refresh()
        self.hub.toast(f"Removed {term}.")

    def open_editor(self, entry=None):
        theme = self.hub.theme
        editing = isinstance(entry, dict)
        window, body = self._open_modal("Edit term" if editing else "New term",
                                        width=460, height=350)

        self._field_label(body, theme, "TERM")
        term_field = _capped_entry(body, theme, dictionary.MAX_TERM_LENGTH,
                                   value=entry["term"] if editing else "",
                                   placeholder="Tlapek")
        term_field.pack(fill=tk.X)

        self._field_label(body, theme, "SOUNDS LIKE")
        alias_field = w.Entry(
            body, theme, placeholder="to lapek, tuh lapek",
            value=", ".join(entry.get("sounds_like") or []) if editing else "")
        alias_field.pack(fill=tk.X)
        w.Label(body, theme, role="ink_faint", size=9, wraplength=400,
                justify=tk.LEFT, anchor="w",
                text="Comma-separated. Anything transcribed as one of these is "
                     "replaced with the term exactly.").pack(anchor="w", pady=(4, 0))

        star_row = tk.Frame(body, bg=theme.bg)
        star_row.pack(fill=tk.X, pady=(th.SPACE_MD, 0))
        w.Label(star_row, theme, text="Always bias the recognizer",
                size=10).pack(side=tk.LEFT)
        star_toggle = w.Toggle(star_row, theme, value=bool(entry.get("starred"))
                               if editing else False)
        star_toggle.pack(side=tk.RIGHT)
        w.Label(body, theme, role="ink_faint", size=9, anchor="w", wraplength=400,
                justify=tk.LEFT,
                text="The bias prompt has a length budget; starred terms are the "
                     "ones that survive it once the list grows.").pack(anchor="w",
                                                                       pady=(4, 0))

        def save():
            term = term_field.get().strip()
            aliases = [part.strip() for part in alias_field.get().split(",")
                       if part.strip()]
            if not term:
                self.hub.toast("Term cannot be empty.")
                return

            if editing:
                renamed = term.lower() != entry["term"].lower()
                if renamed and dictionary.find(term):
                    self.hub.toast(f"“{term}” is already in your dictionary.")
                    return
                if not dictionary.update(entry["term"], term=term,
                                         sounds_like=aliases,
                                         starred=star_toggle.get()):
                    self.hub.toast("Could not save that term.")
                    return
                message = f"Saved {term}."
            else:
                # `add` refuses duplicates and over-long terms with a message
                # worth showing verbatim.
                ok, message = dictionary.add(term, sounds_like=aliases,
                                             starred=star_toggle.get())
                if not ok:
                    self.hub.toast(message)
                    return

            window.destroy()
            self.refresh()
            self.hub.toast(message)

        self._modal_buttons(body, window, save)
        window.bind("<Return>", lambda _e: save())
        window.after(60, term_field.focus)


class SnippetsPage(ListPage):
    title = "Snippets"
    subtitle = "Say a short phrase, type a saved block of text"

    key_field = "trigger"
    search_placeholder = "Search your snippets…"
    export_name = "snippets.json"
    sort_options = (("newest", "Newest"), ("oldest", "Oldest"), ("alpha", "A–Z"))
    default_sort = "newest"

    # ------------------------------------------------------------------
    def fetch(self):
        return snippets.get_snippets()

    def matches(self, entry, query):
        return (query in entry.get("trigger", "").lower()
                or query in entry.get("expansion", "").lower())

    def import_text(self, payload):
        return snippets.import_snippets(payload)

    def export_text(self):
        return snippets.export_snippets()

    # ------------------------------------------------------------------
    def build_row(self, parent, entry):
        theme = self.hub.theme

        card = w.Card(parent, theme, radius=th.RADIUS_SMALL, padding=14, height=76)
        card.pack(fill=tk.X, pady=4)

        line = tk.Frame(card.body, bg=theme.surface)
        line.pack(fill=tk.X)

        actions = tk.Frame(line, bg=theme.surface)
        actions.pack(side=tk.RIGHT)
        w.Toggle(actions, theme, value=bool(entry.get("enabled", True)),
                 bg="surface",
                 command=lambda v, t=entry["trigger"]: self._set_enabled(t, v)
                 ).pack(side=tk.LEFT, padx=(0, th.SPACE_SM))
        w.Button(actions, theme, text="Edit", variant="secondary", size=9,
                 height=26, bg="surface",
                 command=lambda e=entry: self.open_editor(e)).pack(side=tk.LEFT, padx=3)
        w.Button(actions, theme, text="✕", variant="secondary", size=9, height=26,
                 width=30, bg="surface",
                 command=lambda e=entry: self._delete(e)).pack(side=tk.LEFT, padx=3)

        w.Label(line, theme, text=entry["trigger"], size=11, weight="bold",
                bg="surface").pack(side=tk.LEFT)

        expansion = " ".join((entry.get("expansion") or "").split())
        preview = expansion[:PREVIEW_CHARS] + ("…" if len(expansion) > PREVIEW_CHARS
                                               else "")
        w.Label(card.body, theme, text=preview or "(empty)", role="ink_faint",
                size=9, bg="surface", anchor="w", justify=tk.LEFT,
                wraplength=620).pack(anchor="w", pady=(4, 0))

    def render_empty(self, parent):
        self._empty_state(
            parent, "No snippets yet",
            "Say “my work email” and your address appears; say “standup "
            "template” and the whole three-line block does. Triggers are matched "
            "literally, so they only fire when you actually say them.")

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------
    def _set_enabled(self, trigger, value):
        if not snippets.update(trigger, enabled=value):
            self.hub.toast("Could not update that snippet.")
            self.refresh()
            return
        self.refresh()
        self.hub.toast(f"“{trigger}” {'enabled' if value else 'disabled'}.")

    def _delete(self, entry):
        trigger = entry["trigger"]
        if not self._confirm("Remove snippet", f"Remove the “{trigger}” snippet?"):
            return
        if not snippets.remove(trigger):
            self.hub.toast("Could not remove that snippet.")
            return
        self.refresh()
        self.hub.toast(f"Removed {trigger}.")

    def open_editor(self, entry=None):
        theme = self.hub.theme
        editing = isinstance(entry, dict)
        window, body = self._open_modal("Edit snippet" if editing else "New snippet",
                                        width=540, height=520)

        self._field_label(body, theme, "WHEN I SAY")
        trigger_field = _capped_entry(body, theme, snippets.MAX_TRIGGER_LENGTH,
                                      value=entry["trigger"] if editing else "",
                                      placeholder="my work email")
        trigger_field.pack(fill=tk.X)

        self._field_label(body, theme, "TYPE THIS")
        area = w.TextArea(body, theme, height=8,
                          value=entry.get("expansion", "") if editing else "")
        area.pack(fill=tk.BOTH, expand=True)

        meta = tk.Frame(body, bg=theme.bg)
        meta.pack(fill=tk.X, pady=(6, 0))
        w.Label(meta, theme, role="ink_faint", size=9, anchor="w", justify=tk.LEFT,
                wraplength=340,
                text="Variables: {date}, {time}, {datetime}, {iso_date}, "
                     "{clipboard}").pack(side=tk.LEFT)
        counter = w.Label(meta, theme, text="", role="ink_faint", size=9)
        counter.pack(side=tk.RIGHT)

        def update_count(_event=None):
            length = len(area.get())
            counter.configure(
                text=f"{length} / {snippets.MAX_EXPANSION_LENGTH}",
                fg=theme.get("danger" if length > snippets.MAX_EXPANSION_LENGTH
                             else "ink_faint"))

        area.text.bind("<KeyRelease>", update_count)
        # A mouse paste never fires KeyRelease, and the text lands after the
        # event, so the count has to be read on the next idle pass.
        area.text.bind("<<Paste>>", lambda _e: window.after(0, update_count))
        update_count()

        def save():
            trigger = trigger_field.get().strip()
            expansion = area.get()
            if not trigger:
                self.hub.toast("Trigger phrase cannot be empty.")
                return
            if not expansion.strip():
                self.hub.toast("Expansion cannot be empty.")
                return
            if len(expansion) > snippets.MAX_EXPANSION_LENGTH:
                self.hub.toast(
                    f"Expansions are limited to {snippets.MAX_EXPANSION_LENGTH} "
                    "characters.")
                return

            if editing:
                renamed = trigger.lower() != entry["trigger"].lower()
                if renamed and snippets.find(trigger):
                    self.hub.toast(f"A snippet for “{trigger}” already exists.")
                    return
                if not snippets.update(entry["trigger"], trigger=trigger,
                                       expansion=expansion):
                    self.hub.toast("Could not save that snippet.")
                    return
                message = f"Saved “{trigger}”."
            else:
                ok, message = snippets.add(trigger, expansion)
                if not ok:
                    self.hub.toast(message)
                    return

            window.destroy()
            self.refresh()
            self.hub.toast(message)

        self._modal_buttons(body, window, save)
        window.after(60, trigger_field.focus)

"""Home — the stats strip and the transcript feed.

This is the landing page and the one people actually open the Hub for. The stats
row at the top is the hook; the day-grouped transcript feed below it is the
record. Every row exposes the actions that matter after the fact: copy it again,
re-insert it, see what the AI changed, and put the raw version back.
"""

import logging
import tkinter as tk

import history
import injector
import stats
from . import theme as th
from . import widgets as w
from .hub import Page

logger = logging.getLogger(__name__)

# Rendering thousands of rows locks the UI; the feed pages instead.
PAGE_SIZE = 40


class HomePage(Page):
    title = "Home"
    subtitle = "Everything you have dictated"

    def build(self):
        theme = self.hub.theme

        # -- Stats strip --------------------------------------------------
        self._stats_row = tk.Frame(self, bg=theme.bg)
        self._stats_row.pack(fill=tk.X, padx=th.SPACE_XL, pady=(0, th.SPACE_MD))

        self._tiles = {}
        for key, label, accent in (("words", "words dictated", True),
                                   ("wpm", "words per minute", False),
                                   ("streak", "day streak", False),
                                   ("saved", "time saved", False)):
            tile = w.StatTile(self._stats_row, theme, value="0", label=label,
                              accent=accent, width=178, height=112)
            tile.pack(side=tk.LEFT, padx=(0, th.SPACE_SM))
            self._tiles[key] = tile

        # -- Search and actions -------------------------------------------
        controls = tk.Frame(self, bg=theme.bg)
        controls.pack(fill=tk.X, padx=th.SPACE_XL, pady=(0, th.SPACE_SM))

        # Fixed controls reserve their width first; the search box expands into
        # what remains. The other order clips the rightmost button.
        w.Button(controls, theme, text="Clear all", variant="danger", size=9,
                 height=32, command=self._clear_all).pack(side=tk.RIGHT, padx=(6, 0))
        w.Button(controls, theme, text="Export", variant="secondary", size=9,
                 height=32, command=self._export).pack(side=tk.RIGHT, padx=(th.SPACE_SM, 0))

        self._search = w.Entry(controls, theme, placeholder="Search transcripts…",
                               on_change=lambda _v: self._schedule_refresh())
        self._search.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # -- Feed ---------------------------------------------------------
        self._feed = w.ScrollFrame(self, theme)
        self._feed.pack(fill=tk.BOTH, expand=True, padx=th.SPACE_XL,
                        pady=(0, th.SPACE_MD))

        self._visible = PAGE_SIZE
        self._refresh_job = None

    # ------------------------------------------------------------------
    def on_show(self):
        self._refresh_stats()
        self._refresh_feed()

    def _schedule_refresh(self):
        """Debounce search typing so each keystroke does not rebuild the feed."""
        if self._refresh_job is not None:
            try:
                self.after_cancel(self._refresh_job)
            except (ValueError, tk.TclError):
                pass
        self._visible = PAGE_SIZE
        self._refresh_job = self.after(180, self._refresh_feed)

    def _refresh_stats(self):
        summary = stats.summary(self.hub.get_config("typing_wpm_baseline", 40))
        self._tiles["words"].update_values(
            value=f"{summary['words']:,}", caption=summary["comparison"])
        self._tiles["wpm"].update_values(value=f"{summary['wpm']:.0f}")
        self._tiles["streak"].update_values(value=str(summary["streak"]))
        self._tiles["saved"].update_values(value=summary["time_saved_label"] or "0s",
                                           caption="vs typing")

    def _refresh_feed(self):
        self._refresh_job = None
        theme = self.hub.theme
        self._feed.clear()

        entries = history.search(self._search.get())
        if not entries:
            empty = tk.Frame(self._feed.body, bg=theme.bg)
            empty.pack(fill=tk.X, pady=60)
            w.Label(empty, theme, text="Nothing here yet", size=13,
                    weight="bold").pack()
            w.Label(empty, theme, role="ink_faint", size=10,
                    text="Hold your dictation hotkey anywhere and start talking."
                    ).pack(pady=(4, 0))
            return

        shown = entries[:self._visible]
        for label, group in history.group_by_day(shown):
            w.Label(self._feed.body, theme, text=label.upper(), role="ink_faint",
                    size=9, weight="bold").pack(anchor="w", pady=(th.SPACE_MD, 6))
            for entry in group:
                self._build_row(entry)

        if len(entries) > self._visible:
            remaining = len(entries) - self._visible
            w.Button(self._feed.body, theme,
                     text=f"Show {min(PAGE_SIZE, remaining)} more",
                     variant="secondary", size=9, height=32,
                     command=self._show_more).pack(pady=th.SPACE_MD)

    def _show_more(self):
        self._visible += PAGE_SIZE
        self._refresh_feed()

    def _build_row(self, entry):
        theme = self.hub.theme
        text = entry["text"]
        lines = max(1, min(6, len(text) // 78 + 1))

        card = w.Card(self._feed.body, theme, radius=th.RADIUS_SMALL, padding=14,
                      height=74 + lines * 18)
        card.pack(fill=tk.X, pady=4)

        body = card.body
        w.Label(body, theme, text=text, size=11, bg="surface", justify=tk.LEFT,
                anchor="w", wraplength=760).pack(anchor="w", fill=tk.X)

        meta = tk.Frame(body, bg=theme.surface)
        meta.pack(anchor="w", fill=tk.X, pady=(8, 0))

        stamp = entry["timestamp"].split("T")[-1][:5]
        detail = f"{stamp} · {entry['duration']:.1f}s"
        if entry["app"]:
            detail += f" · {entry['app']}"
        w.Label(meta, theme, text=detail, role="ink_faint", size=9,
                bg="surface").pack(side=tk.LEFT)

        if entry["used_llm"] and not entry["reverted"]:
            w.Badge(meta, theme, text="AI POLISHED", bg="surface").pack(
                side=tk.LEFT, padx=(8, 0))
        if entry["reverted"]:
            w.Badge(meta, theme, text="RAW", bg="surface").pack(side=tk.LEFT, padx=(8, 0))

        actions = tk.Frame(meta, bg=theme.surface)
        actions.pack(side=tk.RIGHT)

        w.Button(actions, theme, text="Copy", variant="secondary", size=9,
                 height=26, bg="surface",
                 command=lambda t=text: self._copy(t)).pack(side=tk.LEFT, padx=3)
        w.Button(actions, theme, text="Paste", variant="secondary", size=9,
                 height=26, bg="surface",
                 command=lambda t=text: self._paste(t)).pack(side=tk.LEFT, padx=3)

        # Only offer the revert when there is actually a different raw version.
        if entry["raw"] and entry["raw"] != entry["text"] and not entry["reverted"]:
            w.Button(actions, theme, text="Undo AI edit", variant="secondary",
                     size=9, height=26, bg="surface",
                     command=lambda e=entry: self._revert(e)).pack(side=tk.LEFT, padx=3)

        w.Button(actions, theme, text="✕", variant="secondary", size=9, height=26,
                 width=30, bg="surface",
                 command=lambda e=entry: self._delete(e)).pack(side=tk.LEFT, padx=3)

    # ------------------------------------------------------------------
    # Row actions
    # ------------------------------------------------------------------
    def _copy(self, text):
        try:
            self.hub.clipboard_clear()
            self.hub.clipboard_append(text)
            self.hub.toast("Copied to clipboard.")
        except tk.TclError as e:
            logger.warning("Clipboard copy failed: %s", e)
            self.hub.toast("Could not copy.")

    def _paste(self, text):
        """Hide the Hub first, so the paste lands in the window behind it."""
        self.hub.hide()
        self.hub.after(220, lambda: injector.insert(
            text, mode=self.hub.get_config("output.paste_mode", "clipboard"),
            restore_clipboard=self.hub.get_config("output.restore_clipboard", True)))

    def _revert(self, entry):
        raw = history.revert_ai_edit(entry["id"])
        if raw is None:
            self.hub.toast("Could not restore the original.")
            return
        self.hub.toast("Restored the raw transcript.")
        self._refresh_feed()

    def _delete(self, entry):
        history.delete(entry["id"])
        self._refresh_feed()

    def _export(self):
        from tkinter import filedialog

        path = filedialog.asksaveasfilename(
            parent=self.hub, defaultextension=".txt",
            filetypes=[("Text", "*.txt"), ("JSON", "*.json"), ("CSV", "*.csv")],
            initialfile="dictation-history.txt")
        if not path:
            return

        fmt = "json" if path.endswith(".json") else "csv" if path.endswith(".csv") else "txt"
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(history.export(fmt))
            self.hub.toast(f"Exported to {path}")
        except OSError as e:
            logger.warning("Export failed: %s", e)
            self.hub.toast("Could not write that file.")

    def _clear_all(self):
        from tkinter import messagebox

        if not messagebox.askyesno(
                "Clear history",
                "Delete every saved transcript? This cannot be undone.",
                parent=self.hub):
            return
        history.clear()
        self._refresh_feed()
        self.hub.toast("History cleared.")

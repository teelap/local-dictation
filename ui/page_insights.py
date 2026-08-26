"""Insights — the payoff page.

Home shows what you dictated; this shows what it bought you. Everything here is
derived from :mod:`stats`, which buckets by calendar day, so the whole page is
one ``summary()`` call plus the heatmap series. The page rebuilds from scratch on
every ``on_show`` — the numbers change while the Hub is closed, and a stale
"time saved" is worse than a slow redraw.
"""

import logging
import tkinter as tk

import context
import stats
from . import theme as th
from . import widgets as w
from .hub import Page

logger = logging.getLogger(__name__)

# Cards are canvases and cannot size themselves to their contents, so every
# card height is computed from what goes inside it.
HEATMAP_DAYS = 182
HEATMAP_CARD_HEIGHT = 214
APP_ROW_HEIGHT = 50

# Legend steps mirror the ramp inside w.Heatmap: empty, then the 0.25→1.0 band.
LEGEND_RATIOS = [0.0, 0.25, 0.5, 0.75, 1.0]


class InsightsPage(Page):
    title = "Insights"
    subtitle = "What dictating has actually saved you"

    def build(self):
        # Only the scroller is permanent; its body is rebuilt per refresh.
        self._scroll = w.ScrollFrame(self, self.hub.theme)
        self._scroll.pack(fill=tk.BOTH, expand=True, padx=th.SPACE_XL,
                          pady=(0, th.SPACE_MD))

    # ------------------------------------------------------------------
    def on_show(self):
        self._refresh()

    def _baseline(self):
        """Typing speed to measure against — a bad config value must not divide by zero."""
        try:
            value = float(self.hub.get_config("typing_wpm_baseline", 40))
        except (TypeError, ValueError):
            value = float(stats.DEFAULT_TYPING_WPM)
        return value if value > 0 else float(stats.DEFAULT_TYPING_WPM)

    def _refresh(self):
        theme = self.hub.theme
        self._scroll.clear()
        self._scroll.scroll_to_top()

        baseline = self._baseline()
        try:
            summary = stats.summary(baseline)
        except Exception as e:  # noqa: BLE001 — a corrupt stats file must not blank the Hub
            logger.exception("Could not build the stats summary: %s", e)
            self.hub.toast("Could not read your stats.")
            return

        if summary["sessions"] == 0:
            self._build_empty_state()
            return

        self._build_tiles(summary, baseline)
        self._build_heatmap()
        self._build_apps(summary)

        w.Divider(self._scroll.body, theme).pack(fill=tk.X, pady=th.SPACE_LG)
        self._build_reset_row(summary)

    # ------------------------------------------------------------------
    # Sections
    # ------------------------------------------------------------------
    def _build_empty_state(self):
        theme = self.hub.theme
        empty = tk.Frame(self._scroll.body, bg=theme.bg)
        empty.pack(fill=tk.X, pady=80)

        w.Label(empty, theme, text="No numbers yet", size=15, weight="bold",
                serif=True).pack()
        w.Label(empty, theme, role="ink_soft", size=10, justify=tk.CENTER,
                wraplength=460,
                text="Once you start dictating, this page fills in with your word "
                     "count, your speaking speed, how much time you saved against "
                     "typing, and a six-month activity grid.").pack(pady=(8, 0))
        w.Label(empty, theme, role="ink_faint", size=10,
                text="Hold your dictation hotkey anywhere and start talking."
                ).pack(pady=(th.SPACE_MD, 0))

    def _build_tiles(self, summary, baseline):
        theme = self.hub.theme
        grid = tk.Frame(self._scroll.body, bg=theme.bg)
        grid.pack(fill=tk.X, pady=(0, th.SPACE_LG))
        for column in range(3):
            grid.columnconfigure(column, weight=1, uniform="tiles")

        wpm = summary["wpm"]
        speed_caption = (f"{wpm / baseline:.1f}× typing speed" if wpm > 0
                         else f"vs {baseline:.0f} wpm typing")
        longest = summary["longest_streak"]
        streak_caption = (f"longest run {longest} days" if longest > 1
                          else "your first run")

        cells = [
            (f"{summary['words']:,}", "words dictated", summary["comparison"], True),
            (f"{wpm:.0f}", "words per minute", speed_caption, False),
            (summary["time_saved_label"] or "0s", "time saved",
             f"vs typing at {baseline:.0f} wpm", False),
            (f"{summary['cleaned_words']:,}", "words cleaned up",
             "fillers and fixes", False),
            (f"{summary['replacements']:,}", "smart replacements",
             "dictionary + snippets", False),
            (str(summary["streak"]), "current streak", streak_caption, False),
        ]

        for index, (value, label, caption, accent) in enumerate(cells):
            tile = w.StatTile(grid, theme, value=value, label=label,
                              caption=caption, accent=accent, height=124)
            tile.grid(row=index // 3, column=index % 3, sticky="ew",
                      padx=(0 if index % 3 == 0 else th.SPACE_SM, 0),
                      pady=(0 if index < 3 else th.SPACE_SM, 0))

    def _build_heatmap(self):
        theme = self.hub.theme
        w.Label(self._scroll.body, theme, text="ACTIVITY", role="ink_faint",
                size=9, weight="bold").pack(anchor="w", pady=(0, 6))

        card = w.Card(self._scroll.body, theme, height=HEATMAP_CARD_HEIGHT)
        card.pack(fill=tk.X)
        body = card.body

        w.Label(body, theme, text="Last 6 months", size=11, weight="bold",
                bg="surface").pack(anchor="w")

        try:
            data = stats.heatmap(HEATMAP_DAYS)
        except Exception as e:  # noqa: BLE001
            logger.exception("Could not build the heatmap series: %s", e)
            self.hub.toast("Could not read your activity history.")
            data = []

        w.Heatmap(body, theme, data=data, bg="surface").pack(anchor="w",
                                                             pady=(th.SPACE_MD, 0))

        legend = tk.Frame(body, bg=theme.surface)
        legend.pack(anchor="w", pady=(th.SPACE_SM, 0))
        w.Label(legend, theme, text="Less", role="ink_faint", size=8,
                bg="surface").pack(side=tk.LEFT, padx=(0, 6))
        for ratio in LEGEND_RATIOS:
            color = (theme.grid_empty if ratio <= 0
                     else th.mix(theme.grid_empty, theme.secondary, ratio))
            swatch = tk.Frame(legend, bg=color, width=w.Heatmap.CELL,
                              height=w.Heatmap.CELL, bd=0, highlightthickness=0)
            swatch.pack(side=tk.LEFT, padx=1)
        w.Label(legend, theme, text="More", role="ink_faint", size=8,
                bg="surface").pack(side=tk.LEFT, padx=(6, 0))

    def _build_apps(self, summary):
        theme = self.hub.theme
        entries = summary["top_apps"]
        if not entries:
            return

        w.Label(self._scroll.body, theme, text="WHERE YOU DICTATE",
                role="ink_faint", size=9, weight="bold").pack(
                    anchor="w", pady=(th.SPACE_LG, 6))

        card = w.Card(self._scroll.body, theme,
                      height=th.SPACE_MD * 2 + len(entries) * APP_ROW_HEIGHT)
        card.pack(fill=tk.X)

        # Bars are relative to the busiest app, not to the total — the shape of
        # the ranking is the point, not each app's share of everything.
        peak = max(count for _, count in entries)
        for exe, count in entries:
            self._build_app_row(card.body, exe, count, peak)

    def _build_app_row(self, parent, exe, count, peak):
        theme = self.hub.theme
        row = tk.Frame(parent, bg=theme.surface)
        row.pack(fill=tk.X, pady=(0, th.SPACE_MD))

        header = tk.Frame(row, bg=theme.surface)
        header.pack(fill=tk.X)

        # AppContext owns the exe → display-name rule; reuse it rather than
        # re-implementing the ".exe" stripping here.
        name = context.AppContext(exe=exe).app_name or exe or "Unknown app"
        w.Label(header, theme, text=name, size=10, weight="bold",
                bg="surface").pack(side=tk.LEFT)

        category, _source = context.categorize(exe, "")
        label = context.CATEGORY_LABELS.get(category)
        if label:
            w.Label(header, theme, text=label, role="ink_faint", size=9,
                    bg="surface").pack(side=tk.LEFT, padx=(th.SPACE_SM, 0))

        plural = "session" if count == 1 else "sessions"
        w.Label(header, theme, text=f"{count:,} {plural}", role="ink_soft",
                size=9, bg="surface").pack(side=tk.RIGHT)

        # The track is a plain frame with the bar placed on top: `place` sets no
        # size request on the parent, so the track keeps its 8px height.
        track = tk.Frame(row, bg=theme.grid_empty, height=8, bd=0,
                         highlightthickness=0)
        track.pack(fill=tk.X, pady=(6, 0))
        bar = tk.Frame(track, bg=theme.secondary, bd=0, highlightthickness=0)
        bar.place(x=0, y=0, relheight=1.0,
                  relwidth=max(0.02, count / peak) if peak > 0 else 0.02)

    def _build_reset_row(self, summary):
        theme = self.hub.theme
        row = tk.Frame(self._scroll.body, bg=theme.bg)
        row.pack(fill=tk.X, pady=(0, th.SPACE_LG))

        copy = tk.Frame(row, bg=theme.bg)
        copy.pack(side=tk.LEFT, fill=tk.X, expand=True)
        w.Label(copy, theme, text="Reset statistics", size=11,
                weight="bold").pack(anchor="w")
        w.Label(copy, theme, role="ink_faint", size=9,
                text=f"Clears {summary['sessions']:,} sessions across "
                     f"{summary['active_days']:,} days. Your transcripts stay."
                ).pack(anchor="w", pady=(2, 0))

        w.Button(row, theme, text="Reset", variant="danger", size=9, height=32,
                 command=self._reset).pack(side=tk.RIGHT)

    # ------------------------------------------------------------------
    def _reset(self):
        from tkinter import messagebox

        if not messagebox.askyesno(
                "Reset statistics",
                "Erase every counted word, streak, and saved minute? "
                "This cannot be undone.",
                parent=self.hub):
            return
        try:
            stats.reset()
        except Exception as e:  # noqa: BLE001
            logger.exception("Could not reset stats: %s", e)
            self.hub.toast("Could not reset your statistics.")
            return
        self._refresh()
        self.hub.toast("Statistics reset.")

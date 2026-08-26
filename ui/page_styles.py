"""Styles — the cleanup dial, the per-app writing styles, and the guardrails.

Everything on this page answers one question: how much is the app allowed to
change what you said? The cleanup dial at the top is the global answer; the
per-app styles below it are the local ones, and the context switch is what lets
the two interact at all. Each control shows its own consequence — a description,
an example, a preview — because "Medium" and "Casual" mean nothing on their own.

Every change here writes straight through to the config; the only exception is
the custom-instruction box, which saves on a button so a half-typed sentence is
never handed to the LLM.
"""

import logging
import tkinter as tk
import tkinter.font as tkfont

import context
import formatter
import llm
from . import theme as th
from . import widgets as w
from .hub import Page

logger = logging.getLogger(__name__)

# Wide enough for the content column at the Hub's minimum width, so nothing
# reflows when the window is resized down.
WRAP = 660


def _mono_font(size=10):
    """Tk's stock fixed face — sample output should not read as prose."""
    try:
        return (tkfont.nametofont("TkFixedFont").actual("family"), size)
    except tk.TclError:
        return ("Courier", size)


class StylesPage(Page):
    title = "Styles"
    subtitle = "How your words come out, per app and overall"

    def build(self):
        theme = self.hub.theme

        self._scroll = w.ScrollFrame(self, theme)
        self._scroll.pack(fill=tk.BOTH, expand=True, padx=th.SPACE_XL,
                          pady=(0, th.SPACE_MD))

        self._style_dropdowns = {}
        self._style_previews = {}

        self._build_cleanup(self._scroll.body)
        self._build_per_app(self._scroll.body)
        self._build_context(self._scroll.body)
        self._build_instructions(self._scroll.body)
        self._build_replacements(self._scroll.body)

    # ------------------------------------------------------------------
    def on_show(self):
        self._sync_cleanup()
        self._sync_styles()
        self._sync_context()
        self._sync_instructions()
        self._sync_replacements()

    # ------------------------------------------------------------------
    # 5. Text replacements
    # ------------------------------------------------------------------
    def _build_replacements(self, parent):
        """Plain find/replace rules.

        These survive from the previous version of the app, where they had their
        own tab. They still run on every dictation, so they need an editor —
        rules the user cannot see or remove are worse than no rules.
        """
        theme = self.hub.theme
        body = self._section(
            parent, "Text replacements",
            "Literal find-and-replace, applied to every dictation before "
            "punctuation and capitalization.",
            height=316)

        self._replacements = w.TextArea(body, theme, height=5, bg="surface")
        self._replacements.pack(fill=tk.X)
        self._saved_replacements = ""

        w.Label(body, theme, role="ink_faint", size=9, bg="surface",
                wraplength=WRAP, justify=tk.LEFT, anchor="w",
                text="One rule per line, written as  find -> replace.  For example: "
                     "“ok -> OK”. Matching ignores case and only whole words count. "
                     "For names and jargon, prefer the Dictionary — it also improves "
                     "recognition rather than just fixing the text afterwards."
                ).pack(anchor="w", fill=tk.X, pady=(6, th.SPACE_SM))

        w.Button(body, theme, text="Save replacements", size=9, height=32,
                 bg="surface", command=self._save_replacements).pack(anchor="w")

    @staticmethod
    def _format_replacements(mapping):
        return "\n".join(f"{find} -> {replace}" for find, replace in mapping.items())

    @staticmethod
    def _parse_replacements(text):
        rules = {}
        for line in (text or "").splitlines():
            if "->" not in line:
                continue
            find, _, replace = line.partition("->")
            find = find.strip()
            if find:
                rules[find] = replace.strip()
        return rules

    def _sync_replacements(self):
        stored = self._format_replacements(
            self.hub.get_config("word_substitutions", {}) or {})
        try:
            current = self._replacements.get()
        except tk.TclError:
            return
        # Same rule as the instruction box: never clobber an unsaved edit.
        if current == self._saved_replacements:
            self._replacements.set(stored)
            self._saved_replacements = stored

    def _save_replacements(self):
        try:
            raw = self._replacements.get()
        except tk.TclError as e:
            logger.warning("Could not read the replacements box: %s", e)
            self.hub.toast("Could not read that text.")
            return

        rules = self._parse_replacements(raw)
        if self._save("word_substitutions", rules):
            self._saved_replacements = raw
            self.hub.toast(f"Saved {len(rules)} replacement"
                           f"{'' if len(rules) == 1 else 's'}.")

    # ------------------------------------------------------------------
    # Section scaffolding
    # ------------------------------------------------------------------
    def _section(self, parent, heading, blurb, height):
        """A titled card. Cards are canvases, so the height is ours to size."""
        theme = self.hub.theme
        card = w.Card(parent, theme, height=height)
        card.pack(fill=tk.X, pady=(0, th.SPACE_MD))

        w.Label(card.body, theme, text=heading, size=13, weight="bold",
                bg="surface").pack(anchor="w")
        w.Label(card.body, theme, text=blurb, role="ink_soft", size=10,
                bg="surface", wraplength=WRAP, justify=tk.LEFT,
                anchor="w").pack(anchor="w", pady=(2, th.SPACE_SM), fill=tk.X)
        return card.body

    # ------------------------------------------------------------------
    # 1. Auto cleanup
    # ------------------------------------------------------------------
    def _build_cleanup(self, parent):
        theme = self.hub.theme
        body = self._section(
            parent, "Auto cleanup",
            "One dial for how much the app may change what you said. "
            "Nothing here rewords you except at High.", height=264)

        self._cleanup = w.SegmentedControl(
            body, theme,
            options=[(level, formatter.CLEANUP_LABELS[level])
                     for level in formatter.CLEANUP_LEVELS],
            value=self._cleanup_level(), command=self._on_cleanup, bg="surface")
        self._cleanup.pack(fill=tk.X, pady=(0, th.SPACE_SM))

        self._cleanup_description = w.Label(
            body, theme, size=11, bg="surface", wraplength=WRAP,
            justify=tk.LEFT, anchor="w")
        self._cleanup_description.pack(anchor="w", fill=tk.X)

        self._cleanup_example = w.Label(
            body, theme, role="ink_faint", size=10, bg="surface",
            wraplength=WRAP, justify=tk.LEFT, anchor="w")
        # Label owns its ``font`` argument, so the mono face is applied after.
        self._cleanup_example.configure(font=_mono_font(10))
        self._cleanup_example.pack(anchor="w", fill=tk.X, pady=(4, th.SPACE_SM))

        w.Divider(body, theme, bg="surface").pack(fill=tk.X, pady=(0, th.SPACE_SM))

        note = tk.Frame(body, bg=theme.surface)
        note.pack(fill=tk.X)
        self._cleanup_note = w.Label(
            note, theme, role="ink_soft", size=10, bg="surface", wraplength=460,
            justify=tk.LEFT, anchor="w")
        self._cleanup_note.pack(side=tk.LEFT, fill=tk.X, expand=True)
        w.Button(note, theme, text="AI backend", variant="secondary", size=9,
                 height=30, bg="surface",
                 command=lambda: self.hub.navigate("settings")).pack(side=tk.RIGHT)

    def _cleanup_level(self):
        """The saved level, or Medium if the config holds something unknown."""
        level = self.hub.get_config("formatting.cleanup_level",
                                    formatter.CLEANUP_MEDIUM)
        return level if level in formatter.CLEANUP_LEVELS else formatter.CLEANUP_MEDIUM

    def _on_cleanup(self, level):
        self._render_cleanup(level)
        if self._save("formatting.cleanup_level", level):
            self.hub.toast(f"Cleanup set to {formatter.CLEANUP_LABELS[level]}.")

    def _sync_cleanup(self):
        level = self._cleanup_level()
        self._cleanup.set(level, notify=False)
        self._render_cleanup(level)

    def _render_cleanup(self, level):
        self._cleanup_description.configure(
            text=formatter.CLEANUP_DESCRIPTIONS.get(level, ""))
        self._cleanup_example.configure(
            text=f"“{formatter.CLEANUP_EXAMPLES.get(level, '')}”")

        # The note has to be honest about the current backend, or High silently
        # degrades to Medium and the dial looks broken.
        if llm.is_enabled(self.hub.config_data):
            note = "High uses your AI backend, which is connected."
        elif level == formatter.CLEANUP_HIGH:
            note = ("High needs an AI backend and none is connected yet — "
                    "until one is, this behaves like Medium.")
        else:
            note = "High also needs an AI backend. The other levels are fully offline."
        self._cleanup_note.configure(text=note)

    # ------------------------------------------------------------------
    # 2. Per-app styles
    # ------------------------------------------------------------------
    def _build_per_app(self, parent):
        theme = self.hub.theme
        body = self._section(
            parent, "Per-app styles",
            "A Slack message and an email should not come out the same. "
            "Pick the voice each kind of app gets.", height=452)

        for index, category in enumerate(context.CATEGORIES):
            if index:
                w.Divider(body, theme, bg="surface").pack(fill=tk.X, pady=th.SPACE_SM)
            self._build_style_row(body, category)

    def _build_style_row(self, parent, category):
        theme = self.hub.theme

        row = tk.Frame(parent, bg=theme.surface)
        row.pack(fill=tk.X)

        top = tk.Frame(row, bg=theme.surface)
        top.pack(fill=tk.X)

        dropdown = w.Dropdown(
            top, theme,
            options=[(style, context.STYLE_LABELS[style])
                     for style in context.STYLES],
            value=self._style_for(category), width=300, bg="surface",
            command=lambda style, c=category: self._on_style(c, style))
        dropdown.pack(side=tk.RIGHT)
        self._style_dropdowns[category] = dropdown

        text = tk.Frame(top, bg=theme.surface)
        text.pack(side=tk.LEFT, fill=tk.X, expand=True)
        w.Label(text, theme, text=context.CATEGORY_LABELS[category], size=11,
                weight="bold", bg="surface", anchor="w").pack(anchor="w", fill=tk.X)
        w.Label(text, theme, text=context.CATEGORY_EXAMPLES[category],
                role="ink_faint", size=9, bg="surface", anchor="w",
                wraplength=330, justify=tk.LEFT).pack(anchor="w", fill=tk.X)

        preview = w.Label(row, theme, role="ink_soft", size=10, weight="italic",
                          bg="surface", wraplength=WRAP, justify=tk.LEFT, anchor="w")
        preview.pack(anchor="w", fill=tk.X, pady=(2, 0))
        self._style_previews[category] = preview
        self._render_preview(category, self._style_for(category))

    def _style_for(self, category):
        """The saved style for a category, falling back to the shipped default."""
        styles = self.hub.get_config("context.category_styles", {}) or {}
        style = styles.get(category, context.DEFAULT_CATEGORY_STYLES[category])
        return style if style in context.STYLES else \
            context.DEFAULT_CATEGORY_STYLES[category]

    def _on_style(self, category, style):
        self._render_preview(category, style)
        # Written back as a whole map: config_manager replaces this key rather
        # than merging it, so a partial dict would drop the other categories.
        styles = dict(self.hub.get_config("context.category_styles", {}) or {})
        styles[category] = style
        if self._save("context.category_styles", styles):
            short = context.STYLE_LABELS[style].split("—")[0].strip()
            self.hub.toast(f"{context.CATEGORY_LABELS[category]} → {short}.")

    def _render_preview(self, category, style):
        self._style_previews[category].configure(
            text=f"“{context.STYLE_PREVIEWS.get(style, '')}”")

    def _sync_styles(self):
        for category in context.CATEGORIES:
            style = self._style_for(category)
            self._style_dropdowns[category].set(style, notify=False)
            self._render_preview(category, style)

    # ------------------------------------------------------------------
    # 3. Context awareness
    # ------------------------------------------------------------------
    def _build_context(self, parent):
        theme = self.hub.theme
        body = self._section(
            parent, "Context awareness",
            "Lets the per-app styles above actually apply.", height=236)

        row = tk.Frame(body, bg=theme.surface)
        row.pack(fill=tk.X, pady=(0, th.SPACE_SM))
        self._context_toggle = w.Toggle(
            row, theme, value=bool(self.hub.get_config("context.enabled", True)),
            command=self._on_context_toggle, bg="surface")
        self._context_toggle.pack(side=tk.LEFT)
        w.Label(row, theme, text="Adapt to the app I am writing in", size=11,
                bg="surface").pack(side=tk.LEFT, padx=(th.SPACE_SM, 0))

        w.Label(body, theme, role="ink_soft", size=10, bg="surface",
                wraplength=WRAP, justify=tk.LEFT, anchor="w",
                text="This reads two things about the window in front: the name of "
                     "its program and the text in its title bar. It never reads what "
                     "is on your screen, never sees the document you are in, and "
                     "nothing about any of it leaves this machine — there is no "
                     "server to send it to."
                ).pack(anchor="w", fill=tk.X, pady=(0, th.SPACE_SM))

        self._detected = w.Label(body, theme, role="ink_faint", size=10,
                                 bg="surface", wraplength=WRAP, justify=tk.LEFT,
                                 anchor="w")
        self._detected.pack(anchor="w", fill=tk.X)

    def _on_context_toggle(self, enabled):
        if self._save("context.enabled", bool(enabled)):
            self.hub.toast("Context awareness on." if enabled
                           else "Context awareness off.")
        self._refresh_detected()

    def _sync_context(self):
        self._context_toggle.set(
            bool(self.hub.get_config("context.enabled", True)), notify=False)
        self._refresh_detected()

    def _refresh_detected(self):
        """Show what detection currently sees, so the claim above is checkable."""
        if not self.hub.get_config("context.enabled", True):
            self._detected.configure(
                text="Detection is off — everything is written in the "
                     f"{context.CATEGORY_LABELS[context.CAT_OTHER]} style.")
            return

        try:
            ctx = context.resolve(self.hub.config_data)
        except Exception as e:  # noqa: BLE001 — detection is platform-specific
            logger.debug("Context detection failed: %s", e)
            self._detected.configure(
                text="Detection is not available on this system.")
            return

        label = context.CATEGORY_LABELS.get(ctx.category, ctx.category)
        if ctx.app_name:
            self._detected.configure(
                text=f"Detected right now: {ctx.app_name} → {label}")
        else:
            self._detected.configure(
                text=f"No app detected right now — falling back to {label}.")

    # ------------------------------------------------------------------
    # 4. Custom instructions
    # ------------------------------------------------------------------
    def _build_instructions(self, parent):
        theme = self.hub.theme
        body = self._section(
            parent, "Custom instructions",
            "Standing rules for the AI polish pass, in your own words.",
            height=344)

        self._instructions = w.TextArea(body, theme, height=5, bg="surface")
        self._instructions.pack(fill=tk.X)
        self._saved_instructions = ""

        w.Label(body, theme, role="ink_faint", size=9, bg="surface",
                wraplength=WRAP, justify=tk.LEFT, anchor="w",
                text="One rule per line. For example: “Never use em dashes.” · "
                     "“Sign emails with — Jacob.” · “Write British English.”"
                ).pack(anchor="w", fill=tk.X, pady=(6, 0))

        self._instructions_note = w.Label(
            body, theme, role="ink_faint", size=9, bg="surface", wraplength=WRAP,
            justify=tk.LEFT, anchor="w")
        self._instructions_note.pack(anchor="w", fill=tk.X, pady=(2, th.SPACE_SM))

        w.Button(body, theme, text="Save instructions", size=9, height=32,
                 bg="surface", command=self._save_instructions).pack(anchor="w")

    def _sync_instructions(self):
        note = ("These apply only while an AI backend is connected — it is, so they "
                "are in use." if llm.is_enabled(self.hub.config_data) else
                "These apply only while an AI backend is connected. Without one they "
                "are stored but unused.")
        self._instructions_note.configure(text=note)

        stored = self.hub.get_config("formatting.custom_instructions", "") or ""
        try:
            current = self._instructions.get()
        except tk.TclError:
            return
        # Re-entering the page must not wipe an edit the user has not saved yet.
        if current == self._saved_instructions:
            self._instructions.set(stored)
            self._saved_instructions = stored

    def _save_instructions(self):
        try:
            value = self._instructions.get().strip()
        except tk.TclError as e:
            logger.warning("Could not read the instruction box: %s", e)
            self.hub.toast("Could not read that text.")
            return
        if self._save("formatting.custom_instructions", value):
            self._saved_instructions = value
            self.hub.toast("Custom instructions saved.")

    # ------------------------------------------------------------------
    def _save(self, path, value):
        """Write one setting; report a failed write rather than lying about it."""
        try:
            self.hub.set_config(path, value)
            return True
        except Exception as e:  # noqa: BLE001 — a bad write must not kill the page
            logger.exception("Could not save %s: %s", path, e)
            self.hub.toast("Could not save that setting.")
            return False

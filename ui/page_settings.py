"""Settings — every knob that is not about the words themselves.

The page is one long scroll of titled cards rather than a tab strip: these
settings are read far more often than they are changed, and a person looking for
"where do I pick my microphone" finds it faster by scrolling past four headings
than by guessing which tab hides it.

Every control writes straight through to config the moment it changes. The only
exceptions are the free-text fields — a hotkey capture, a URL, a number — which
commit when they lose focus, so a half-typed value is never persisted.
"""

import logging
import threading
import tkinter as tk

import llm
from . import theme as th
from . import widgets as w
from .hub import Page

logger = logging.getLogger(__name__)

# Wide enough for the content column at the Hub's minimum width, so nothing
# reflows when the window is resized down.
WRAP = 660

# (config key, label, what it actually does)
HOTKEY_ROWS = [
    ("push_to_talk", "Push to talk",
     "Hold it, speak, let go. The main way to dictate."),
    ("hands_free", "Hands free",
     "Tap once to start, tap again to stop. Optional — a double tap on push "
     "to talk does the same thing."),
    ("command_mode", "Command mode",
     "Select some text, hold this, and say what to do with it."),
    ("paste_last", "Paste last",
     "Re-insert the most recent transcript wherever the cursor is."),
    ("scratchpad", "Scratchpad",
     "Open a window to dictate into when there is nowhere else to type."),
]

# Bindings the app works fine without, so they get a Clear button.
OPTIONAL_HOTKEYS = ("hands_free", "scratchpad")

MODEL_SIZES = [
    ("tiny.en", "tiny.en — fastest, English only"),
    ("base.en", "base.en — default, English only"),
    ("small.en", "small.en — more accurate, English only"),
    ("medium.en", "medium.en — slow, English only"),
    ("tiny", "tiny — fastest, multilingual"),
    ("base", "base — multilingual"),
    ("small", "small — multilingual"),
    ("medium", "medium — slow, multilingual"),
    ("large-v2", "large-v2 — best accuracy, needs a GPU"),
    ("large-v3", "large-v3 — best accuracy, needs a GPU"),
]

DEVICES = [
    ("auto", "Automatic — GPU when one is available"),
    ("cpu", "CPU"),
    ("cuda", "GPU (CUDA)"),
]

# A deliberately short list: the pool exists to narrow detection, so offering a
# hundred codes would work against the feature.
LANGUAGES = [
    ("en", "English"), ("es", "Spanish"), ("fr", "French"), ("de", "German"),
    ("it", "Italian"), ("pt", "Portuguese"), ("nl", "Dutch"), ("pl", "Polish"),
    ("ru", "Russian"), ("ja", "Japanese"), ("zh", "Chinese"), ("ko", "Korean"),
    ("hi", "Hindi"), ("ar", "Arabic"), ("tr", "Turkish"),
]

LOG_LEVELS = [("DEBUG", "Debug — everything"), ("INFO", "Info — normal"),
              ("WARNING", "Warnings only"), ("ERROR", "Errors only")]

# Peak RMS from a one-second test recording, turned into plain language.
MIC_SILENT = 0.003
MIC_QUIET = 0.02


def _commit_on_blur(entry, handler):
    """Fire ``handler(text)`` when a field loses focus or the user hits Enter.

    :class:`~ui.widgets.Entry` wraps a real ``tk.Entry`` and exposes no bind
    hook of its own, so the events are attached to the inner widget directly.
    """
    entry._entry.bind("<FocusOut>", lambda _e: handler(entry.get()), add="+")
    entry._entry.bind("<Return>", lambda _e: handler(entry.get()), add="+")


def _pretty_hotkey(combo):
    """Render a binding for display: ctrl+windows becomes CTRL + WINDOWS."""
    if not combo:
        return "NOT SET"
    return " + ".join(part.strip().upper() for part in combo.split("+") if part.strip())


class SettingsPage(Page):
    title = "Settings"
    subtitle = "Hotkeys, audio, model, and the AI backend"

    def build(self):
        theme = self.hub.theme

        # Controls register themselves here so on_show can re-read config in one
        # pass — settings also change from onboarding and the tray menu.
        self._toggles = {}          # path -> (widget, default)
        self._dropdowns = {}        # path -> (widget, default)
        self._sync_entries = []     # (widget, path, default, formatter)
        self._hotkey_readouts = {}
        self._hotkey_buttons = {}
        self._capturing = None

        self._scroll = w.ScrollFrame(self, theme)
        self._scroll.pack(fill=tk.BOTH, expand=True, padx=th.SPACE_XL,
                          pady=(0, th.SPACE_MD))

        self._build_shortcuts(self._scroll.body)
        self._build_microphone(self._scroll.body)
        self._build_model(self._scroll.body)
        self._build_languages(self._scroll.body)
        self._build_llm(self._scroll.body)
        self._build_output(self._scroll.body)
        self._build_system(self._scroll.body)

    # ------------------------------------------------------------------
    def on_show(self):
        for path, (widget, default) in self._toggles.items():
            widget.set(bool(self.hub.get_config(path, default)))
        for path, (widget, default) in self._dropdowns.items():
            widget.set(self.hub.get_config(path, default))
        for widget, path, default, format_value in self._sync_entries:
            widget.set(format_value(self.hub.get_config(path, default)))

        for key, _label, _description in HOTKEY_ROWS:
            self._show_binding(key)

        self._refresh_devices()
        self._refresh_model_info()
        self._render_language_chips()
        self._render_llm_details()

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
        return card

    def _row(self, parent):
        """A left-hand text stack with controls pinned to the right."""
        theme = self.hub.theme
        row = tk.Frame(parent, bg=theme.surface)
        row.pack(fill=tk.X, pady=(0, th.SPACE_SM))

        controls = tk.Frame(row, bg=theme.surface)
        controls.pack(side=tk.RIGHT, padx=(th.SPACE_MD, 0))
        text = tk.Frame(row, bg=theme.surface)
        text.pack(side=tk.LEFT, fill=tk.X, expand=True)
        return text, controls

    def _row_text(self, parent, label, description):
        theme = self.hub.theme
        w.Label(parent, theme, text=label, size=11, weight="bold",
                bg="surface").pack(anchor="w")
        if description:
            # Narrow enough to clear the widest control cluster on the right
            # (a hotkey readout plus Change and Clear), or the text runs under it.
            w.Label(parent, theme, text=description, role="ink_faint", size=9,
                    bg="surface", wraplength=WRAP - 420, justify=tk.LEFT,
                    anchor="w").pack(anchor="w")

    def _toggle_row(self, parent, path, default, label, description):
        theme = self.hub.theme
        text, controls = self._row(parent)
        self._row_text(text, label, description)
        toggle = w.Toggle(controls, theme, value=bool(self.hub.get_config(path, default)),
                          command=lambda value, p=path: self._write(p, value),
                          bg="surface")
        toggle.pack()
        self._toggles[path] = (toggle, default)
        return toggle

    def _dropdown_row(self, parent, path, default, label, description, options,
                      width=260, command=None):
        theme = self.hub.theme
        text, controls = self._row(parent)
        self._row_text(text, label, description)
        dropdown = w.Dropdown(
            controls, theme, options=options,
            value=self.hub.get_config(path, default), width=width, bg="surface",
            command=command or (lambda value, p=path: self._write(p, value)))
        dropdown.pack()
        self._dropdowns[path] = (dropdown, default)
        return dropdown

    def _number_row(self, parent, path, default, label, description, parse,
                    format_value=str, width=8):
        """A numeric field that commits on blur and refuses to store nonsense."""
        theme = self.hub.theme
        text, controls = self._row(parent)
        self._row_text(text, label, description)
        entry = w.Entry(controls, theme,
                        value=format_value(self.hub.get_config(path, default)),
                        width=width, bg="surface")
        entry.pack()

        def commit(raw, p=path, d=default, e=entry):
            try:
                value = parse(raw)
            except (TypeError, ValueError):
                self.hub.toast("That needs to be a number.")
                e.set(format_value(self.hub.get_config(p, d)))
                return
            self._write(p, value)
            e.set(format_value(value))

        _commit_on_blur(entry, commit)
        self._sync_entries.append((entry, path, default, format_value))
        return entry

    def _write(self, path, value):
        """There is no Save button on this page; each control persists itself."""
        try:
            self.hub.set_config(path, value)
        except Exception as e:  # noqa: BLE001 — a read-only config dir, mostly
            logger.exception("Could not save %s: %s", path, e)
            self.hub.toast("Could not save that setting.")

    # ------------------------------------------------------------------
    # 1. Shortcuts
    # ------------------------------------------------------------------
    def _build_shortcuts(self, parent):
        theme = self.hub.theme
        body = self._section(
            parent, "Shortcuts",
            "Global keys, so dictation works in any app without switching to "
            "this one.", height=470).body

        for key, label, description in HOTKEY_ROWS:
            text, controls = self._row(body)
            self._row_text(text, label, description)

            # Badge sizes itself to its text at construction, so the readout is
            # rebuilt inside this holder every time the binding changes.
            holder = tk.Frame(controls, bg=theme.surface)
            holder.pack(side=tk.LEFT, padx=(0, th.SPACE_SM))
            self._hotkey_readouts[key] = holder

            button = w.Button(controls, theme, text="Change", variant="secondary",
                              size=9, height=28, bg="surface",
                              command=lambda k=key: self._capture(k))
            button.pack(side=tk.LEFT)
            self._hotkey_buttons[key] = button

            if key in OPTIONAL_HOTKEYS:
                w.Button(controls, theme, text="Clear", variant="secondary",
                         size=9, height=28, bg="surface",
                         command=lambda k=key: self._clear_binding(k)).pack(
                    side=tk.LEFT, padx=(6, 0))

            self._show_binding(key)

        w.Divider(body, theme, bg="surface").pack(fill=tk.X, pady=(4, th.SPACE_SM))

        self._number_row(
            body, "min_hold_seconds", 0.35, "Minimum hold",
            "Anything shorter counts as a stray tap and is thrown away, so a "
            "brushed key never records.",
            parse=lambda v: max(0.05, min(3.0, float(v))),
            format_value=lambda v: f"{float(v or 0):.2f}")
        self._number_row(
            body, "double_tap_seconds", 0.4, "Double-tap window",
            "Two taps of push to talk inside this window latch into hands-free "
            "mode.",
            parse=lambda v: max(0.1, min(2.0, float(v))),
            format_value=lambda v: f"{float(v or 0):.2f}")

    def _show_binding(self, key):
        holder = self._hotkey_readouts.get(key)
        if holder is None:
            return
        for child in holder.winfo_children():
            child.destroy()
        binding = self.hub.get_config(f"hotkeys.{key}", "")
        w.Badge(holder, self.hub.theme, text=_pretty_hotkey(binding),
                role="ink" if binding else "ink_faint", bg="surface").pack()

    def _capture(self, key):
        """Read the next chord the user presses, on a thread of its own.

        ``keyboard.read_hotkey`` blocks until a chord completes, which would
        freeze Tk's event loop outright if it ran here.
        """
        if self._capturing:
            self.hub.toast("Finish the shortcut you are already setting.")
            return

        self._capturing = key
        button = self._hotkey_buttons[key]
        button.set_text("Press keys…")
        threading.Thread(target=self._capture_worker, args=(key,), daemon=True,
                         name="hotkey-capture").start()

    def _capture_worker(self, key):
        try:
            # Imported here because the `keyboard` package is Windows-oriented
            # and may not be installed at all on this machine.
            import keyboard
        except ImportError:
            self.hub.after(0, lambda: self._capture_done(key, None, available=False))
            return

        combo = None
        try:
            # suppress=False so the chord still reaches whatever else is
            # listening — swallowing keys here would look like a freeze.
            combo = keyboard.read_hotkey(suppress=False)
        except Exception as e:  # noqa: BLE001 — no permission, no X server, …
            logger.warning("Hotkey capture failed: %s", e)
        self.hub.after(0, lambda: self._capture_done(key, combo))

    def _capture_done(self, key, combo, available=True):
        self._capturing = None
        button = self._hotkey_buttons.get(key)
        if button is not None:
            button.set_text("Change")

        if not available:
            self.hub.toast("Hotkey capture is unavailable on this system.")
            return
        if not combo:
            self.hub.toast("No keys captured.")
            return

        clash = self._binding_clash(key, combo)
        self._write(f"hotkeys.{key}", combo)
        self._show_binding(key)
        self._refresh_hub_hotkey_label()

        if clash:
            self.hub.toast(f"That is also bound to {clash}.")
        else:
            self.hub.toast(f"{_pretty_hotkey(combo)} saved.")

    def _binding_clash(self, key, combo):
        """The friendly name of another action already using ``combo``."""
        bindings = self.hub.get_config("hotkeys", {}) or {}
        labels = {row_key: label for row_key, label, _ in HOTKEY_ROWS}
        for other_key, other in bindings.items():
            if other_key != key and other and other == combo:
                return labels.get(other_key, other_key.replace("_", " "))
        return ""

    def _clear_binding(self, key):
        self._write(f"hotkeys.{key}", "")
        self._show_binding(key)
        self._refresh_hub_hotkey_label()
        self.hub.toast("Shortcut cleared.")

    def _refresh_hub_hotkey_label(self):
        try:
            self.hub.refresh_hotkey_label()
        except Exception as e:  # noqa: BLE001
            logger.debug("Could not refresh the sidebar hotkey label: %s", e)

    # ------------------------------------------------------------------
    # 2. Microphone
    # ------------------------------------------------------------------
    def _build_microphone(self, parent):
        theme = self.hub.theme
        body = self._section(
            parent, "Microphone",
            "Which input the app records from. Test it here rather than "
            "discovering the problem halfway through a paragraph.",
            height=190).body

        text, controls = self._row(body)
        self._row_text(text, "Input device", "")
        self._device_dropdown = w.Dropdown(
            controls, theme, options=[(None, "System default")],
            value=self.hub.get_config("audio.device_index", None), width=280,
            bg="surface",
            command=lambda value: self._write("audio.device_index", value))
        self._device_dropdown.pack(side=tk.LEFT)
        self._test_button = w.Button(controls, theme, text="Test mic",
                                     variant="secondary", size=9, height=30,
                                     bg="surface", command=self._test_microphone)
        self._test_button.pack(side=tk.LEFT, padx=(th.SPACE_SM, 0))

        self._device_note = w.Label(body, theme, role="ink_faint", size=9,
                                    bg="surface", wraplength=WRAP,
                                    justify=tk.LEFT, anchor="w")
        self._device_note.pack(anchor="w", fill=tk.X)

    def _refresh_devices(self):
        """Re-enumerate on every visit — mics get plugged in and unplugged."""
        options = [(None, "System default")]
        try:
            import audio
            options += [(index, name) for index, name in audio.query_input_devices()]
            note = ""
        except Exception as e:  # noqa: BLE001 — no sounddevice, no backend, …
            logger.warning("Could not list audio devices: %s", e)
            note = "No audio backend found, so only the system default is available."

        self._device_dropdown.set_options(options, keep_value=True)
        self._device_dropdown.set(self.hub.get_config("audio.device_index", None))
        self._device_note.configure(text=note)

    def _test_microphone(self):
        self._test_button.set_enabled(False)
        self._test_button.set_text("Listening…")
        index = self._device_dropdown.get()
        threading.Thread(target=self._test_worker, args=(index,), daemon=True,
                         name="mic-test").start()

    def _test_worker(self, index):
        try:
            import audio
            ok, peak = audio.test_device(index)
        except Exception as e:  # noqa: BLE001
            logger.warning("Mic test failed: %s", e)
            ok, peak = False, 0.0
        self.hub.after(0, lambda: self._test_done(ok, peak))

    def _test_done(self, ok, peak):
        self._test_button.set_text("Test mic")
        self._test_button.set_enabled(True)
        if not ok:
            self.hub.toast("Could not open that microphone.")
        elif peak < MIC_SILENT:
            self.hub.toast("No signal — check the device and that it is unmuted.")
        elif peak < MIC_QUIET:
            self.hub.toast("Quiet, but working. Try speaking closer.")
        else:
            self.hub.toast("Sounds good.")

    # ------------------------------------------------------------------
    # 3. Model
    # ------------------------------------------------------------------
    def _build_model(self, parent):
        theme = self.hub.theme
        body = self._section(
            parent, "Model",
            "Bigger models are more accurate and slower. base.en is the right "
            "default for English on a laptop CPU.", height=240).body

        self._dropdown_row(body, "model_size", "base.en", "Whisper model", "",
                           MODEL_SIZES, width=280)
        self._dropdown_row(body, "device", "auto", "Run on", "", DEVICES, width=280)

        w.Label(body, theme, role="ink_faint", size=9, bg="surface",
                wraplength=WRAP, justify=tk.LEFT, anchor="w",
                text="Both take effect the next time the app starts — the model "
                     "is loaded once and held in memory.").pack(anchor="w",
                                                                fill=tk.X)

        self._model_info = w.Label(body, theme, role="ink_soft", size=9,
                                   bg="surface", wraplength=WRAP,
                                   justify=tk.LEFT, anchor="w")
        self._model_info.pack(anchor="w", fill=tk.X, pady=(4, 0))

    def _refresh_model_info(self):
        text = ""
        try:
            import transcription
            info = transcription.get_model_info()
            if info:
                text = ("Currently loaded: {model_size} on {device} "
                        "({compute_type}).".format(
                            model_size=info.get("model_size", "?"),
                            device=str(info.get("device", "?")).upper(),
                            compute_type=info.get("compute_type", "?")))
        except Exception as e:  # noqa: BLE001 — faster-whisper may not be installed
            logger.debug("Model info unavailable: %s", e)
        self._model_info.configure(text=text)

    # ------------------------------------------------------------------
    # 4. Languages
    # ------------------------------------------------------------------
    def _build_languages(self, parent):
        theme = self.hub.theme
        self._language_card = self._section(
            parent, "Languages",
            "The languages you actually speak. Detection picks between these "
            "and nothing else.", height=300)
        body = self._language_card.body

        self._language_chips = tk.Frame(body, bg=theme.surface)
        self._language_chips.pack(fill=tk.X)

        w.Label(body, theme, role="ink_faint", size=9, bg="surface",
                wraplength=WRAP, justify=tk.LEFT, anchor="w",
                text="Keep this list short. Choosing between two languages is a "
                     "far easier problem than choosing between a hundred, and "
                     "the accuracy difference is large.").pack(
            anchor="w", fill=tk.X, pady=(th.SPACE_SM, 0))

    def _language_pool(self):
        pool = self.hub.get_config("language_pool", ["en"]) or ["en"]
        return [code for code in pool if code]

    def _render_language_chips(self):
        """Chips are rebuilt wholesale — a Button cannot change variant in place."""
        theme = self.hub.theme
        for child in self._language_chips.winfo_children():
            child.destroy()

        pool = self._language_pool()
        font = th.ui_font(self, 9, "bold")
        row = None
        used = 0
        rows = 0

        for code, name in LANGUAGES:
            width = th.measure(self, name, font) + th.SPACE_LG * 2
            if row is None or used + width > WRAP:
                row = tk.Frame(self._language_chips, bg=theme.surface)
                row.pack(fill=tk.X, pady=(0, 6))
                used = 0
                rows += 1
            used += width + 6
            w.Button(row, theme, text=name, size=9, height=30, bg="surface",
                     variant="primary" if code in pool else "secondary",
                     command=lambda c=code: self._toggle_language(c)).pack(
                side=tk.LEFT, padx=(0, 6))

        # The card is a canvas with a fixed height, so it has to grow with the
        # number of chip rows the current window width produces.
        self._language_card.configure(height=170 + rows * 36)

    def _toggle_language(self, code):
        pool = self._language_pool()
        if code in pool:
            if len(pool) == 1:
                self.hub.toast("Keep at least one language in the pool.")
                return
            pool.remove(code)
        else:
            pool.append(code)

        self._write("language_pool", pool)
        self._render_language_chips()

    # ------------------------------------------------------------------
    # 5. AI cleanup backend
    # ------------------------------------------------------------------
    def _build_llm(self, parent):
        theme = self.hub.theme
        self._llm_card = self._section(
            parent, "AI cleanup backend",
            "Optional. Rules handle punctuation and filler on their own; a "
            "model is what turns rambling into clean prose.", height=380)
        body = self._llm_card.body

        self._dropdown_row(
            body, "llm_provider", llm.PROVIDER_OFF, "Provider", "",
            [(provider, llm.PROVIDER_LABELS.get(provider, provider))
             for provider in llm.PROVIDERS], width=300,
            command=self._on_provider)

        self._llm_details = tk.Frame(body, bg=theme.surface)
        self._llm_details.pack(fill=tk.X)

        w.Label(body, theme, role="ink_faint", size=9, bg="surface",
                wraplength=WRAP, justify=tk.LEFT, anchor="w",
                text="Off and Ollama keep every word on this machine. The other "
                     "providers send your transcript to their servers.").pack(
            anchor="w", fill=tk.X, pady=(th.SPACE_SM, 0))

    def _on_provider(self, provider):
        self._write("llm_provider", provider)
        self._render_llm_details()

    def _render_llm_details(self):
        """Rebuilt on every provider change — the defaults shown as placeholder
        text and the model list both belong to one specific provider."""
        theme = self.hub.theme
        for child in self._llm_details.winfo_children():
            child.destroy()
        self._llm_model_dropdown = None
        self._llm_listing_id = 0

        provider = self.hub.get_config("llm_provider", llm.PROVIDER_OFF)
        if provider == llm.PROVIDER_OFF:
            self._llm_card.configure(height=180)
            return
        self._llm_card.configure(height=400)

        current = self.hub.get_config("llm_model", "") or ""
        options = [(current, current)] if current else [("", "Loading…")]
        text, controls = self._row(self._llm_details)
        self._row_text(text, "Model", "")
        self._llm_model_dropdown = w.Dropdown(
            controls, theme, options=options, value=current, width=280,
            bg="surface", command=lambda value: self._write("llm_model", value))
        self._llm_model_dropdown.pack()
        self._load_models(provider)

        default_url = {llm.PROVIDER_OLLAMA: llm.DEFAULT_OLLAMA_HOST,
                       llm.PROVIDER_OPENAI: llm.DEFAULT_OPENAI_BASE_URL}.get(provider, "")
        text, controls = self._row(self._llm_details)
        self._row_text(text, "Base URL", "Leave blank to use the default.")
        url_entry = w.Entry(controls, theme,
                            value=self.hub.get_config("llm_base_url", "") or "",
                            placeholder=default_url or "not needed", width=30,
                            bg="surface")
        url_entry.pack()
        _commit_on_blur(url_entry, lambda value: self._write(
            "llm_base_url", value.strip()))

        text, controls = self._row(self._llm_details)
        self._row_text(text, "API key",
                       "Stored in config.json. An environment variable is used "
                       "in preference to this when one is set.")
        key_entry = w.Entry(controls, theme,
                            value=self.hub.get_config("llm_api_key", "") or "",
                            width=30, bg="surface", show="•")
        key_entry.pack()
        _commit_on_blur(key_entry, lambda value: self._write(
            "llm_api_key", value.strip()))

        self._llm_test_button = w.Button(
            self._llm_details, theme, text="Test connection", variant="secondary",
            size=9, height=30, bg="surface", command=self._test_connection)
        self._llm_test_button.pack(anchor="w", pady=(4, 0))

    def _load_models(self, provider):
        """Ask the provider what it has. Ollama answers over HTTP, so: a thread."""
        self._llm_listing_id += 1
        listing_id = self._llm_listing_id

        def worker():
            try:
                names = llm.list_models(self.hub.config_data)
            except Exception as e:  # noqa: BLE001
                logger.warning("Model listing failed: %s", e)
                names = list(llm.SUGGESTED_MODELS.get(provider, []))
            self.hub.after(0, lambda: self._models_loaded(names, listing_id))

        threading.Thread(target=worker, daemon=True, name="llm-models").start()

    def _models_loaded(self, names, listing_id=None):
        # Switching provider rebuilds the dropdown, so "still exists" is not
        # enough — a slow listing for the old provider would land in the new
        # widget. The generation token says whether this request is still current.
        if listing_id is not None and listing_id != self._llm_listing_id:
            logger.debug("Discarding a stale model listing")
            return

        dropdown = self._llm_model_dropdown
        if dropdown is None or not dropdown.winfo_exists():
            return

        current = self.hub.get_config("llm_model", "") or ""
        # A hand-typed model that the provider does not advertise must not
        # silently disappear from the list.
        if current and current not in names:
            names = [current] + list(names)
        if not names:
            dropdown.set_options([("", "No models found")], keep_value=False)
            return
        dropdown.set_options([(name, name) for name in names], keep_value=True)
        dropdown.set(current or names[0])

    def _test_connection(self):
        self._llm_test_button.set_enabled(False)
        self._llm_test_button.set_text("Testing…")

        def worker():
            try:
                ok, message = llm.test_connection(self.hub.config_data)
            except Exception as e:  # noqa: BLE001
                logger.warning("Connection test failed: %s", e)
                ok, message = False, "The connection test could not run."
            self.hub.after(0, lambda: self._test_connection_done(ok, message))

        threading.Thread(target=worker, daemon=True, name="llm-test").start()

    def _test_connection_done(self, ok, message):
        button = self._llm_test_button
        if button is not None and button.winfo_exists():
            button.set_text("Test connection")
            button.set_enabled(True)
        self.hub.toast(message if ok else f"Failed: {message}")

    # ------------------------------------------------------------------
    # 6. Output
    # ------------------------------------------------------------------
    def _build_output(self, parent):
        theme = self.hub.theme
        body = self._section(
            parent, "Output",
            "What happens between the transcript and the text landing in your "
            "editor.", height=350).body

        self._toggle_row(body, "output.restore_clipboard", True,
                         "Restore the clipboard",
                         "Put back whatever you had copied after pasting.")
        self._toggle_row(body, "output.trailing_space", True, "Trailing space",
                         "End each insertion with a space, ready for the next one.")
        self._toggle_row(body, "output.trailing_actions", True, "Trailing actions",
                         'Honour "press enter" or "new line" at the end of a '
                         "dictation instead of typing the words.")
        # Replaces a "match the surrounding text" toggle that controlled nothing
        # — matching the seam needs to read text at the cursor, which this app
        # deliberately does not do. Auto-stop is a real control that was missing.
        self._number_row(
            body, "audio.silence_threshold_seconds", 0.0, "Stop after silence",
            "Seconds of quiet that end a hands-free session. 0 never stops on "
            "its own. Holding the key is unaffected.",
            parse=lambda v: max(0.0, min(60.0, float(v))),
            format_value=lambda v: f"{float(v or 0):.1f}")

        w.Label(body, theme, text="Insertion method", size=11, weight="bold",
                bg="surface").pack(anchor="w", pady=(th.SPACE_SM, 4))
        segment = w.SegmentedControl(
            body, theme, options=[("clipboard", "Paste"), ("type", "Type")],
            value=self.hub.get_config("output.paste_mode", "clipboard"),
            bg="surface",
            command=lambda value: self._write("output.paste_mode", value))
        segment.pack(fill=tk.X)
        # SegmentedControl shares the dropdown's get/set contract, so on_show can
        # resync it the same way.
        self._dropdowns["output.paste_mode"] = (segment, "clipboard")

    # ------------------------------------------------------------------
    # 7. System
    # ------------------------------------------------------------------
    def _build_system(self, parent):
        body = self._section(
            parent, "System",
            "Startup, storage, and what the app shows while it is working.",
            height=470).body

        self._toggle_row(body, "launch_at_startup", False, "Launch at startup",
                         "Start with Windows, minimised to the tray.")
        self._toggle_row(body, "ui.show_overlay", True, "Show the overlay",
                         "The small pill that appears while you are dictating.")
        self._toggle_row(body, "ui.sounds", True, "Sounds",
                         "A short cue when recording starts and stops.")
        self._toggle_row(body, "history_persist", True, "Keep history",
                         "Save transcripts to disk so Home and Insights have "
                         "something to show.")
        self._toggle_row(body, "dictionary.auto_learn", True, "Learn corrections",
                         "Add a term to the dictionary when you retype the same "
                         "fix twice.")
        self._toggle_row(body, "dictionary.bias_decoding", True, "Bias the model",
                         "Feed your dictionary to Whisper as hints, which is what "
                         "actually stops names being mangled.")

        self._dropdown_row(body, "log_level", "INFO", "Log level",
                           "Raise this to Debug before reporting a problem.",
                           LOG_LEVELS, width=220)
        self._number_row(body, "history_retention_days", 0, "Keep history for",
                         "Days. 0 keeps everything forever.",
                         parse=lambda v: max(0, int(float(v))),
                         format_value=lambda v: str(int(v or 0)))

import tkinter as tk
from tkinter import ttk, messagebox
import threading
import logging
import keyboard

from config_manager import load_config, save_config
from audio import query_input_devices

logger = logging.getLogger(__name__)

MODEL_SIZES = [
    "tiny.en", "base.en", "small.en", "medium.en",
    "tiny", "base", "small", "medium", "large-v2", "large-v3"
]

DEVICE_OPTIONS = ["auto", "cpu", "cuda"]

LANGUAGE_OPTIONS = [
    ("Auto-detect", None),
    ("English", "en"),
    ("Spanish", "es"),
    ("French", "fr"),
    ("German", "de"),
    ("Italian", "it"),
    ("Portuguese", "pt"),
    ("Dutch", "nl"),
    ("Polish", "pl"),
    ("Russian", "ru"),
    ("Japanese", "ja"),
    ("Chinese", "zh"),
    ("Korean", "ko"),
]


class SettingsWindow:
    def __init__(self, on_settings_saved_callback=None):
        self.on_settings_saved_callback = on_settings_saved_callback
        self.config = load_config()

        self.root = tk.Tk()
        self.root.title("LocalDictation Settings")
        self.root.geometry("520x600")
        self.root.attributes("-topmost", True)
        self.root.resizable(True, True)
        self.root.bind("<Escape>", lambda e: self.root.destroy())

        self._build_ui()

    def _build_ui(self):
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        # Tabs
        self._tab_hotkey(notebook)
        self._tab_model(notebook)
        self._tab_audio(notebook)
        self._tab_behavior(notebook)
        self._tab_substitutions(notebook)
        self._tab_advanced(notebook)

        # Bottom buttons
        btn_frame = tk.Frame(self.root)
        btn_frame.pack(fill=tk.X, padx=8, pady=(0, 8))
        tk.Button(btn_frame, text="Save & Close", command=self.save_and_close,
                  bg="#4CAF50", fg="white", font=("Helvetica", 10, "bold"),
                  width=14).pack(side=tk.RIGHT, padx=(4, 0))
        tk.Button(btn_frame, text="Cancel", command=self.root.destroy,
                  width=10).pack(side=tk.RIGHT)

    # ------------------------------------------------------------------ Tab: Hotkey
    def _tab_hotkey(self, notebook):
        frame = ttk.Frame(notebook)
        notebook.add(frame, text="Hotkey")

        tk.Label(frame, text="Global Hotkey", font=("Helvetica", 12, "bold")).pack(pady=(16, 4))
        tk.Label(frame, text="Press 'Record New Hotkey' then type your desired combination.").pack(pady=(0, 8))

        self._hotkey_var = tk.StringVar(value=self.config.get("trigger_hotkey", "ctrl+shift+f12"))
        tk.Label(frame, textvariable=self._hotkey_var, font=("Helvetica", 12, "italic"), fg="blue").pack()

        self._record_btn = tk.Button(frame, text="Record New Hotkey", command=self._start_hotkey_listen)
        self._record_btn.pack(pady=8)
        self._is_listening = False

    def _start_hotkey_listen(self):
        if self._is_listening:
            return
        self._is_listening = True
        self._record_btn.config(text="Listening... press your keys", state=tk.DISABLED, bg="orange")
        self._hotkey_var.set("...")
        self.root.after(150, self._do_hotkey_listen)

    def _do_hotkey_listen(self):
        def listener():
            try:
                new_hotkey = keyboard.read_hotkey(suppress=False)
                self.root.after(0, self._on_hotkey_heard, new_hotkey)
            except Exception as e:
                logger.warning("Hotkey read error: %s", e)
                self.root.after(0, self._on_hotkey_heard, self.config.get("trigger_hotkey", "ctrl+shift+f12"))

        threading.Thread(target=listener, daemon=True).start()

    def _on_hotkey_heard(self, new_hotkey):
        if new_hotkey:
            self._hotkey_var.set(str(new_hotkey))
        self._is_listening = False
        self._record_btn.config(text="Record New Hotkey", state=tk.NORMAL, bg="SystemButtonFace")

    # ------------------------------------------------------------------ Tab: Model
    def _tab_model(self, notebook):
        frame = ttk.Frame(notebook)
        notebook.add(frame, text="Model")

        self._model_size_var = tk.StringVar(value=self.config.get("model_size", "base.en"))
        self._device_var = tk.StringVar(value=self.config.get("device", "auto"))
        lang_code = self.config.get("language", None)
        lang_label = next((lbl for lbl, code in LANGUAGE_OPTIONS if code == lang_code), "Auto-detect")
        self._language_display_var = tk.StringVar(value=lang_label)

        row = 0
        for label, var, choices in [
            ("Model Size:", self._model_size_var, MODEL_SIZES),
            ("Device:", self._device_var, DEVICE_OPTIONS),
        ]:
            tk.Label(frame, text=label).grid(row=row, column=0, sticky=tk.W, padx=12, pady=6)
            ttk.Combobox(frame, textvariable=var, values=choices, state="readonly", width=20).grid(
                row=row, column=1, sticky=tk.W, padx=4)
            row += 1

        tk.Label(frame, text="Language:").grid(row=row, column=0, sticky=tk.W, padx=12, pady=6)
        lang_combo = ttk.Combobox(frame, textvariable=self._language_display_var,
                                  values=[lbl for lbl, _ in LANGUAGE_OPTIONS],
                                  state="readonly", width=20)
        lang_combo.grid(row=row, column=1, sticky=tk.W, padx=4)

        tk.Label(frame, text="Note: Larger models are more accurate but slower.",
                 fg="gray").grid(row=row+1, column=0, columnspan=2, padx=12, pady=(12, 0), sticky=tk.W)
        tk.Label(frame, text="'auto' device tries CUDA first, falls back to CPU.",
                 fg="gray").grid(row=row+2, column=0, columnspan=2, padx=12, sticky=tk.W)

    # ------------------------------------------------------------------ Tab: Audio
    def _tab_audio(self, notebook):
        frame = ttk.Frame(notebook)
        notebook.add(frame, text="Audio")

        # Enumerate devices
        self._audio_devices = [("System Default", None)] + query_input_devices()
        device_names = [name for _, name in self._audio_devices]
        saved_index = self.config.get("audio_device_index", None)
        saved_name = next((name for idx, name in self._audio_devices if idx == saved_index), "System Default")
        self._audio_device_var = tk.StringVar(value=saved_name)

        tk.Label(frame, text="Microphone / Input Device:").grid(row=0, column=0, sticky=tk.W, padx=12, pady=(12, 4))
        ttk.Combobox(frame, textvariable=self._audio_device_var,
                     values=device_names, state="readonly", width=36).grid(row=0, column=1, sticky=tk.W, padx=4, pady=(12, 4))

        tk.Label(frame, text="Auto-stop after silence (seconds):").grid(row=1, column=0, sticky=tk.W, padx=12, pady=6)
        self._silence_var = tk.DoubleVar(value=self.config.get("silence_threshold_seconds", 3.0))
        ttk.Spinbox(frame, from_=0.5, to=30.0, increment=0.5, textvariable=self._silence_var,
                    width=8, format="%.1f").grid(row=1, column=1, sticky=tk.W, padx=4)

        tk.Label(frame, text="(set to 0 to disable auto-stop)", fg="gray").grid(
            row=2, column=0, columnspan=2, sticky=tk.W, padx=12)

    # ------------------------------------------------------------------ Tab: Behavior
    def _tab_behavior(self, notebook):
        frame = ttk.Frame(notebook)
        notebook.add(frame, text="Behavior")

        self._paste_mode_var = tk.StringVar(value=self.config.get("paste_mode", "clipboard"))
        self._interaction_mode_var = tk.StringVar(value=self.config.get("interaction_mode", "toggle"))

        row = 0
        tk.Label(frame, text="Paste Mode:").grid(row=row, column=0, sticky=tk.W, padx=12, pady=8)
        paste_frame = tk.Frame(frame)
        paste_frame.grid(row=row, column=1, sticky=tk.W)
        tk.Radiobutton(paste_frame, text="Clipboard (recommended)", variable=self._paste_mode_var,
                       value="clipboard").pack(side=tk.LEFT)
        tk.Radiobutton(paste_frame, text="Type text", variable=self._paste_mode_var,
                       value="type").pack(side=tk.LEFT)

        row += 1
        tk.Label(frame, text="Interaction Mode:").grid(row=row, column=0, sticky=tk.W, padx=12, pady=8)
        mode_frame = tk.Frame(frame)
        mode_frame.grid(row=row, column=1, sticky=tk.W)
        tk.Radiobutton(mode_frame, text="Toggle (press once to start, once to stop)",
                       variable=self._interaction_mode_var, value="toggle").pack(anchor=tk.W)
        tk.Radiobutton(mode_frame, text="Push-to-talk (hold to record, release to stop)",
                       variable=self._interaction_mode_var, value="push_to_talk").pack(anchor=tk.W)

        row += 1
        tk.Label(frame, text="Voice Commands are always active. Examples:",
                 fg="gray").grid(row=row, column=0, columnspan=2, sticky=tk.W, padx=12, pady=(16, 2))
        examples = [
            '"new line" → inserts newline',
            '"delete that" → Ctrl+Z',
            '"period", "comma", "question mark" → punctuation',
        ]
        for i, ex in enumerate(examples):
            tk.Label(frame, text="  " + ex, fg="gray").grid(
                row=row+1+i, column=0, columnspan=2, sticky=tk.W, padx=12)

    # ------------------------------------------------------------------ Tab: Substitutions
    def _tab_substitutions(self, notebook):
        frame = ttk.Frame(notebook)
        notebook.add(frame, text="Substitutions")

        tk.Label(frame, text="Word Substitutions (Find -> Replace):",
                 font=("Helvetica", 10, "bold")).pack(anchor=tk.W, padx=8, pady=(8, 2))
        tk.Label(frame, text="Applied after each transcription. One rule per line. Format: find -> replace",
                 fg="gray", wraplength=460).pack(anchor=tk.W, padx=8)

        text_frame = tk.Frame(frame)
        text_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=6)

        scrollbar = tk.Scrollbar(text_frame)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self._sub_text = tk.Text(text_frame, height=12, yscrollcommand=scrollbar.set, wrap=tk.WORD)
        self._sub_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.config(command=self._sub_text.yview)

        # Populate from config
        subs = self.config.get("word_substitutions", {})
        for find, replace in subs.items():
            self._sub_text.insert(tk.END, f"{find} -> {replace}\n")

    def _parse_substitutions(self):
        result = {}
        text = self._sub_text.get("1.0", tk.END).strip()
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            if "->" in line:
                parts = line.split("->", 1)
                find = parts[0].strip()
                replace = parts[1].strip()
                if find:
                    result[find] = replace
        return result

    # ------------------------------------------------------------------ Tab: Advanced
    def _tab_advanced(self, notebook):
        frame = ttk.Frame(notebook)
        notebook.add(frame, text="Advanced")

        self._startup_var = tk.BooleanVar(value=self.config.get("launch_at_startup", False))
        self._history_persist_var = tk.BooleanVar(value=self.config.get("history_persist", True))

        tk.Checkbutton(frame, text="Launch at Windows startup",
                       variable=self._startup_var).grid(row=0, column=0, sticky=tk.W, padx=12, pady=8)
        tk.Checkbutton(frame, text="Persist transcription history to disk (history.json)",
                       variable=self._history_persist_var).grid(row=1, column=0, sticky=tk.W, padx=12, pady=4)

        tk.Label(frame, text="Log Level:").grid(row=2, column=0, sticky=tk.W, padx=12, pady=8)
        self._log_level_var = tk.StringVar(value=self.config.get("log_level", "INFO"))
        ttk.Combobox(frame, textvariable=self._log_level_var,
                     values=["DEBUG", "INFO", "WARNING", "ERROR"],
                     state="readonly", width=12).grid(row=2, column=1, sticky=tk.W, padx=4)

    # ------------------------------------------------------------------ Save
    def save_and_close(self):
        hotkey = self._hotkey_var.get().strip()
        if not hotkey or hotkey == "...":
            messagebox.showerror("Error", "Hotkey cannot be empty.")
            return

        # Resolve language from display label
        lang_label = self._language_display_var.get()
        lang_code = next((code for lbl, code in LANGUAGE_OPTIONS if lbl == lang_label), None)

        # Resolve audio device index
        audio_name = self._audio_device_var.get()
        audio_index = next((idx for idx, name in self._audio_devices if name == audio_name), None)

        updated = dict(self.config)
        updated["trigger_hotkey"] = hotkey
        updated["model_size"] = self._model_size_var.get()
        updated["device"] = self._device_var.get()
        updated["language"] = lang_code
        updated["paste_mode"] = self._paste_mode_var.get()
        updated["interaction_mode"] = self._interaction_mode_var.get()
        updated["silence_threshold_seconds"] = float(self._silence_var.get())
        updated["audio_device_index"] = audio_index
        updated["word_substitutions"] = self._parse_substitutions()
        updated["launch_at_startup"] = self._startup_var.get()
        updated["history_persist"] = self._history_persist_var.get()
        updated["log_level"] = self._log_level_var.get()

        save_config(updated)

        if self.on_settings_saved_callback:
            self.on_settings_saved_callback(updated)

        self.root.destroy()

    def run(self):
        self.root.mainloop()


def open_settings(callback=None):
    """Open the settings window. callback(updated_config) is called on save."""
    app = SettingsWindow(callback)
    app.run()

"""Transcription history — in-memory store with optional JSON persistence."""

import json
import os
import threading
import logging
import tkinter as tk
from tkinter import ttk
from datetime import datetime

logger = logging.getLogger(__name__)

MAX_ENTRIES = 50

_lock = threading.Lock()
_entries = []          # list of dicts: {timestamp, duration, text}
_persist_path = None   # set by init()
_persist_enabled = False


def init(app_dir, persist=True):
    """Initialize the history module.

    Args:
        app_dir: Directory where history.json is stored.
        persist: Whether to save/load history from disk.
    """
    global _persist_path, _persist_enabled
    _persist_enabled = persist
    _persist_path = os.path.join(app_dir, "history.json")
    if persist and os.path.exists(_persist_path):
        _load_from_disk()


def _load_from_disk():
    global _entries
    try:
        with open(_persist_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        with _lock:
            _entries = data[-MAX_ENTRIES:]
        logger.info("Loaded %d history entries from disk", len(_entries))
    except Exception as e:
        logger.warning("Could not load history from disk: %s", e)


def _save_to_disk():
    if not _persist_enabled or _persist_path is None:
        return
    try:
        with _lock:
            snapshot = list(_entries)
        with open(_persist_path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.warning("Could not save history to disk: %s", e)


def add_entry(text, duration_seconds):
    """Add a transcription entry to the history."""
    entry = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "duration": round(duration_seconds, 1),
        "text": text,
    }
    with _lock:
        _entries.append(entry)
        if len(_entries) > MAX_ENTRIES:
            _entries.pop(0)
    _save_to_disk()
    logger.debug("History entry added: %s", text[:60])


def get_entries():
    """Return a copy of all history entries (newest last)."""
    with _lock:
        return list(_entries)


# ---- Tkinter History Window ----

_history_window = None
_history_window_lock = threading.Lock()


class HistoryWindow:
    def __init__(self):
        self.root = tk.Toplevel()
        self.root.title("Transcription History")
        self.root.geometry("680x480")
        self.root.attributes("-topmost", False)
        self.root.resizable(True, True)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_ui()
        self._populate()

    def _build_ui(self):
        top_bar = tk.Frame(self.root)
        top_bar.pack(fill=tk.X, padx=8, pady=(8, 4))
        tk.Label(top_bar, text="Transcription History", font=("Helvetica", 12, "bold")).pack(side=tk.LEFT)
        tk.Button(top_bar, text="Refresh", command=self._populate).pack(side=tk.RIGHT)
        tk.Button(top_bar, text="Clear All", command=self._clear_all).pack(side=tk.RIGHT, padx=4)

        # Scrollable container
        container = tk.Frame(self.root)
        container.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)

        self._canvas = tk.Canvas(container)
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=self._canvas.yview)
        self._scrollable_frame = tk.Frame(self._canvas)
        self._scrollable_frame.bind(
            "<Configure>",
            lambda e: self._canvas.configure(scrollregion=self._canvas.bbox("all"))
        )
        self._canvas_window = self._canvas.create_window((0, 0), window=self._scrollable_frame, anchor="nw")
        self._canvas.configure(yscrollcommand=scrollbar.set)
        self._canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Make canvas width track window width
        self._canvas.bind("<Configure>", self._on_canvas_resize)

    def _on_canvas_resize(self, event):
        self._canvas.itemconfig(self._canvas_window, width=event.width)

    def _populate(self):
        # Clear existing widgets
        for widget in self._scrollable_frame.winfo_children():
            widget.destroy()

        entries = get_entries()
        if not entries:
            tk.Label(self._scrollable_frame, text="No history yet.", fg="gray").pack(pady=20)
            return

        # Show newest first
        for entry in reversed(entries):
            self._add_entry_widget(entry)

    def _add_entry_widget(self, entry):
        frame = tk.Frame(self._scrollable_frame, bd=1, relief=tk.GROOVE)
        frame.pack(fill=tk.X, padx=4, pady=3)

        meta = tk.Frame(frame)
        meta.pack(fill=tk.X, padx=6, pady=(4, 0))
        tk.Label(meta, text=entry.get("timestamp", ""), fg="gray", font=("Helvetica", 8)).pack(side=tk.LEFT)
        duration = entry.get("duration", 0)
        tk.Label(meta, text=f"{duration}s", fg="gray", font=("Helvetica", 8)).pack(side=tk.LEFT, padx=8)

        text_var = tk.StringVar(value=entry.get("text", ""))
        text_label = tk.Label(frame, textvariable=text_var, wraplength=540,
                               justify=tk.LEFT, anchor="w", font=("Helvetica", 10))
        text_label.pack(fill=tk.X, padx=6, pady=(2, 4))

        def copy_to_clipboard(t=entry.get("text", "")):
            self.root.clipboard_clear()
            self.root.clipboard_append(t)

        tk.Button(frame, text="Copy", command=copy_to_clipboard,
                  font=("Helvetica", 8), padx=4).pack(side=tk.RIGHT, padx=4, pady=(0, 4))

    def _clear_all(self):
        global _entries
        with _lock:
            _entries.clear()
        _save_to_disk()
        self._populate()

    def _on_close(self):
        global _history_window
        with _history_window_lock:
            _history_window = None
        self.root.destroy()


def open_history_window(parent_root=None):
    """Open or focus the history window. Safe to call from any thread via after()."""
    global _history_window

    def _open():
        global _history_window
        with _history_window_lock:
            if _history_window is not None:
                try:
                    _history_window.root.lift()
                    _history_window.root.focus_force()
                    return
                except Exception:
                    _history_window = None
            _history_window = HistoryWindow()

    if parent_root is not None:
        parent_root.after(0, _open)
    else:
        # Called from non-Tk thread — create a hidden root if needed
        try:
            _open()
        except Exception as e:
            logger.warning("Could not open history window: %s", e)

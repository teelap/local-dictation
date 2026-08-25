"""Microphone capture, level metering, and the watchdogs around a session.

Beyond recording, this module owns three things that turn silent failures into
visible ones:

* **Voice activity** — a separate signal from raw amplitude, so the overlay can
  show "I can hear you" rather than just "there is sound".
* **A mic watchdog** — sustained silence raises a warning, then escalates. The
  worst failure a dictation tool has is two minutes of speech into a muted mic.
* **A session cap** — long recordings end gracefully and get transcribed, rather
  than being dropped at the limit.
"""

import logging
import queue
import tempfile
import threading
import time

import numpy as np
import sounddevice as sd
import soundfile as sf

logger = logging.getLogger(__name__)

# RMS below this is silence; above the voice threshold is speech. The gap
# between them keeps the overlay from flickering on room tone.
SILENCE_RMS = 0.003
VOICE_RMS = 0.010

_queue = queue.Queue()
_stream = None
_recording = False
_stream_lock = threading.Lock()

_last_rms = 0.0
_peak_rms = 0.0
_started_at = 0.0
_monitor_thread = None
_callbacks = {}

# Incremented on every start. The monitor thread captures it and exits if a new
# session begins, so a watchdog from the previous recording can never fire
# against the current one.
_session_id = 0


def query_input_devices():
    """Return (index, name) for every input-capable device."""
    try:
        devices = sd.query_devices()
    except Exception as e:  # noqa: BLE001 — no audio backend at all
        logger.warning("Could not enumerate audio devices: %s", e)
        return []
    return [(i, d["name"]) for i, d in enumerate(devices) if d.get("max_input_channels", 0) > 0]


def get_device_name(index):
    if index is None:
        return "System default"
    for device_index, name in query_input_devices():
        if device_index == index:
            return name
    return f"Device {index}"


def _recording_callback(indata, frames, time_info, status):
    global _last_rms, _peak_rms
    if status:
        logger.debug("Audio stream status: %s", status)
    if not _recording:
        return
    rms = float(np.sqrt(np.mean(indata ** 2)))
    _last_rms = rms
    _peak_rms = max(_peak_rms, rms)
    _queue.put(indata.copy())


def _monitor(config, session_id):
    """Watch the live session for silence, a dead mic, and the time limit.

    Runs on its own thread and only ever fires callbacks — it never touches the
    stream, so a slow callback cannot stall capture.
    """
    tick = 0.1
    silence_run = 0.0
    warned_no_audio = False
    warned_mic_dead = False
    warned_limit = False
    auto_stop_after = config.get("silence_threshold_seconds", 0.0) or 0.0
    no_audio_after = config.get("no_audio_warn_seconds", 5)
    mic_dead_after = config.get("mic_dead_warn_seconds", 15)
    max_seconds = config.get("max_session_seconds", 0) or 0
    warn_before = config.get("warn_before_limit_seconds", 60)
    silence_rms = config.get("silence_rms", SILENCE_RMS)

    while _recording and _session_id == session_id:
        time.sleep(tick)
        if not _recording or _session_id != session_id:
            break

        elapsed = time.monotonic() - _started_at

        if _last_rms < silence_rms:
            silence_run += tick
        else:
            silence_run = 0.0
            warned_no_audio = warned_mic_dead = False

        if not warned_no_audio and no_audio_after and silence_run >= no_audio_after:
            warned_no_audio = True
            _fire("no_audio", silence_run)
        if not warned_mic_dead and mic_dead_after and silence_run >= mic_dead_after:
            warned_mic_dead = True
            _fire("mic_dead", silence_run)

        if auto_stop_after and silence_run >= auto_stop_after:
            logger.info("Auto-stop after %.1fs of silence", silence_run)
            _fire("silence")
            return

        if max_seconds:
            if not warned_limit and warn_before and elapsed >= max_seconds - warn_before:
                warned_limit = True
                _fire("limit_warning", int(max_seconds - elapsed))
            if elapsed >= max_seconds:
                logger.info("Session limit reached at %.0fs", elapsed)
                _fire("limit_reached")
                return


def _fire(event, *args):
    handler = _callbacks.get(event)
    if not handler:
        return
    try:
        handler(*args)
    except Exception as e:  # noqa: BLE001 — a bad callback must not kill capture
        logger.exception("Audio callback %r failed: %s", event, e)


def start_recording(sample_rate=16000, channels=1, device_index=None,
                    config=None, callbacks=None):
    """Open the input stream and start the watchdog.

    Args:
        sample_rate: Capture rate in Hz.
        channels: Channel count.
        device_index: sounddevice index, or None for the system default.
        config: The ``audio`` config section.
        callbacks: Handlers for "silence", "no_audio", "mic_dead",
            "limit_warning", and "limit_reached".

    Returns:
        True if the stream opened.
    """
    global _stream, _recording, _monitor_thread, _last_rms, _peak_rms
    global _started_at, _callbacks, _session_id

    config = config or {}
    _callbacks = callbacks or {}

    with _stream_lock:
        while not _queue.empty():
            try:
                _queue.get_nowait()
            except queue.Empty:
                break

        _last_rms = 0.0
        _peak_rms = 0.0
        _started_at = time.monotonic()
        _session_id += 1
        session_id = _session_id
        _recording = True

        kwargs = {"samplerate": sample_rate, "channels": channels,
                  "callback": _recording_callback, "dtype": "float32"}
        if device_index is not None:
            kwargs["device"] = device_index

        try:
            _stream = sd.InputStream(**kwargs)
            _stream.start()
        except Exception as e:  # noqa: BLE001 — device unplugged, in use, etc.
            _recording = False
            _stream = None
            logger.error("Could not open audio device %s: %s", device_index, e)
            _fire("error", str(e))
            return False

        logger.info("Recording started (device=%s, rate=%d)", device_index, sample_rate)

    _monitor_thread = threading.Thread(
        target=_monitor, args=(config, session_id), daemon=True, name="audio-monitor")
    _monitor_thread.start()
    return True


def stop_recording_and_save(output_file=None, sample_rate=16000, min_seconds=0.2):
    """Stop capture and write a WAV. Returns the path, or None if nothing usable."""
    global _stream, _recording

    with _stream_lock:
        _recording = False
        if _stream is not None:
            try:
                _stream.stop()
                _stream.close()
            except Exception as e:  # noqa: BLE001
                logger.warning("Error closing stream: %s", e)
            _stream = None

    chunks = []
    while not _queue.empty():
        try:
            chunks.append(_queue.get_nowait())
        except queue.Empty:
            break

    if not chunks:
        logger.warning("No audio data captured")
        return None

    audio = np.concatenate(chunks, axis=0)
    duration = len(audio) / sample_rate
    if duration < min_seconds:
        logger.info("Discarding %.2fs clip — too short to transcribe", duration)
        return None

    if output_file is None:
        handle = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        output_file = handle.name
        handle.close()

    try:
        sf.write(output_file, audio, sample_rate)
    except Exception as e:  # noqa: BLE001
        logger.error("Could not write audio file: %s", e)
        return None

    logger.info("Audio saved to %s (%.2fs)", output_file, duration)
    return output_file


def discard_recording():
    """Stop capture and throw the audio away — the cancel path."""
    global _stream, _recording

    # The drain stays inside the lock: releasing it first lets the next session
    # start and then have its opening frames eaten by this cleanup.
    with _stream_lock:
        _recording = False
        if _stream is not None:
            try:
                _stream.stop()
                _stream.close()
            except Exception as e:  # noqa: BLE001
                logger.warning("Error closing stream: %s", e)
            _stream = None

        while not _queue.empty():
            try:
                _queue.get_nowait()
            except queue.Empty:
                break
    logger.info("Recording discarded")


def is_recording():
    return _recording


def get_rms():
    """Most recent RMS level, 0.0–1.0. Drives the overlay waveform."""
    return _last_rms


def get_peak_rms():
    return _peak_rms


def has_voice():
    """True when the current level reads as speech rather than room tone.

    Kept separate from the raw level so the overlay can distinguish "hearing
    something" from "hearing you", which is the signal users actually want.
    """
    return _last_rms >= VOICE_RMS


def elapsed_seconds():
    return time.monotonic() - _started_at if _recording else 0.0


def test_device(device_index=None, seconds=1.0, sample_rate=16000):
    """Record briefly and report the peak level — the mic test in settings."""
    try:
        kwargs = {"samplerate": sample_rate, "channels": 1, "dtype": "float32"}
        if device_index is not None:
            kwargs["device"] = device_index
        recording = sd.rec(int(seconds * sample_rate), **kwargs)
        sd.wait()
        peak = float(np.sqrt(np.mean(recording ** 2)))
        return True, peak
    except Exception as e:  # noqa: BLE001
        logger.warning("Mic test failed: %s", e)
        return False, 0.0

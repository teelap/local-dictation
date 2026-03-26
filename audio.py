import queue
import threading
import time
import numpy as np
import sounddevice as sd
import soundfile as sf
import logging

logger = logging.getLogger(__name__)

# Global state
_q = queue.Queue()
_stream = None
_recording = False
_stream_lock = threading.Lock()

# Updated by _recording_callback; read by _silence_monitor without touching the queue
_last_rms = 0.0

# Auto-stop on silence state
_silence_callback = None
_silence_threshold_seconds = 3.0
_silence_monitor_thread = None


def query_input_devices():
    """Returns a list of (index, name) tuples for all input devices."""
    devices = sd.query_devices()
    result = []
    for i, d in enumerate(devices):
        if d['max_input_channels'] > 0:
            result.append((i, d['name']))
    return result


def _recording_callback(indata, frames, time_info, status):
    """Called by sounddevice for each audio block."""
    global _last_rms
    if status:
        logger.debug("Audio stream status: %s", status)
    if _recording:
        _last_rms = float(np.sqrt(np.mean(indata ** 2)))
        _q.put(indata.copy())


def _silence_monitor(threshold_rms, consecutive_chunks_needed, chunk_duration_s):
    """Background thread that watches for sustained silence and fires a callback.

    Polls _last_rms (set by the recording callback) on a timer instead of
    peeking at the queue — avoids counting the same chunk multiple times.
    """
    consecutive_silent = 0
    while _recording:
        time.sleep(chunk_duration_s)
        if not _recording:
            break
        rms = _last_rms
        if rms < threshold_rms:
            consecutive_silent += 1
            logger.debug("Silence chunk %d/%d (rms=%.5f)", consecutive_silent, consecutive_chunks_needed, rms)
            if consecutive_silent >= consecutive_chunks_needed and _silence_callback:
                logger.info("Auto-stop triggered by silence")
                _silence_callback()
                return
        else:
            consecutive_silent = 0


def start_recording(sample_rate=16000, channels=1, device_index=None,
                    silence_threshold_seconds=3.0, on_silence_callback=None):
    """Starts the audio recording stream.

    Args:
        sample_rate: Sample rate in Hz.
        channels: Number of channels.
        device_index: sounddevice device index, or None for system default.
        silence_threshold_seconds: Seconds of silence before auto-stop fires.
        on_silence_callback: Callable invoked when silence threshold is reached.
    """
    global _stream, _recording, _silence_callback, _silence_threshold_seconds, _silence_monitor_thread

    with _stream_lock:
        # Drain queue from any previous recording
        while not _q.empty():
            try:
                _q.get_nowait()
            except queue.Empty:
                break

        _silence_callback = on_silence_callback
        _silence_threshold_seconds = silence_threshold_seconds
        _recording = True

        kwargs = dict(samplerate=sample_rate, channels=channels, callback=_recording_callback, dtype='float32')
        if device_index is not None:
            kwargs['device'] = device_index

        _stream = sd.InputStream(**kwargs)
        _stream.start()
        logger.info("Recording started (device=%s, rate=%d)", device_index, sample_rate)

    if on_silence_callback is not None:
        # chunk_duration roughly matches the sounddevice default blocksize at 16kHz
        chunk_duration_s = 0.032  # ~512 frames at 16kHz
        # RMS threshold: values below ~0.003 are near-silence for 16-bit normalized float
        threshold_rms = 0.003
        chunks_needed = max(1, int(silence_threshold_seconds / chunk_duration_s))
        _silence_monitor_thread = threading.Thread(
            target=_silence_monitor,
            args=(threshold_rms, chunks_needed, chunk_duration_s),
            daemon=True
        )
        _silence_monitor_thread.start()


def stop_recording_and_save(output_file=None, sample_rate=16000):
    """Stops the audio recording stream and saves it to a WAV file.

    Args:
        output_file: Path to write WAV. If None, uses a tempfile.
        sample_rate: Sample rate used during recording.

    Returns:
        Path of saved WAV file, or None if no audio was captured.
    """
    global _stream, _recording

    import tempfile

    with _stream_lock:
        _recording = False
        if _stream is not None:
            try:
                _stream.stop()
                _stream.close()
            except Exception as e:
                logger.warning("Error closing stream: %s", e)
            _stream = None

    # Gather all audio blocks
    audio_chunks = []
    while not _q.empty():
        try:
            audio_chunks.append(_q.get_nowait())
        except queue.Empty:
            break

    if not audio_chunks:
        logger.warning("No audio data captured")
        return None

    audio_data = np.concatenate(audio_chunks, axis=0)

    if output_file is None:
        tmp = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
        output_file = tmp.name
        tmp.close()

    sf.write(output_file, audio_data, sample_rate)
    logger.info("Audio saved to %s (%.2fs)", output_file, len(audio_data) / sample_rate)
    return output_file


def is_recording():
    return _recording


def get_rms():
    """Return the RMS level of the most recent audio chunk (0.0–1.0)."""
    return _last_rms

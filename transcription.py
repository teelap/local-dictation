import os
import logging
from faster_whisper import WhisperModel

logger = logging.getLogger(__name__)

_model = None


def init_model(model_size="base.en", device="auto", compute_type=None):
    """Initializes the Whisper model with automatic CUDA/CPU fallback.

    Args:
        model_size: Whisper model identifier (e.g. "base.en", "small.en", "large-v3").
        device: "auto", "cuda", or "cpu".
        compute_type: Override compute type. If None, chosen automatically.
    """
    global _model

    # Auto-detect: try CUDA first, fall back to CPU
    if device == "auto":
        try:
            import torch
            if torch.cuda.is_available():
                actual_device = "cuda"
                actual_compute = compute_type or "float16"
                logger.info("CUDA detected — loading model on GPU (float16)")
            else:
                raise RuntimeError("CUDA not available")
        except Exception:
            actual_device = "cpu"
            actual_compute = compute_type or "int8"
            logger.info("No CUDA — loading model on CPU (int8)")
    elif device == "cuda":
        actual_device = "cuda"
        actual_compute = compute_type or "float16"
    else:
        actual_device = "cpu"
        actual_compute = compute_type or "int8"

    logger.info("Loading '%s' model on %s (%s)...", model_size, actual_device.upper(), actual_compute)
    print(f"Loading '{model_size}' model on {actual_device.upper()} ({actual_compute})...")

    _model = WhisperModel(model_size, device=actual_device, compute_type=actual_compute)
    logger.info("Model loaded successfully.")
    print("Model loaded successfully.")


def transcribe_audio(file_path, language=None):
    """Transcribes the given audio file.

    Args:
        file_path: Path to WAV file.
        language: ISO language code (e.g. "en", "es") or None for auto-detect.

    Returns:
        Transcribed string, or empty string on failure.
    """
    if _model is None:
        raise RuntimeError("Model not initialized. Call init_model() first.")

    logger.debug("Transcribing %s (language=%s)", file_path, language)

    try:
        kwargs = dict(beam_size=1, vad_filter=True)
        if language:
            kwargs['language'] = language

        segments, info = _model.transcribe(file_path, **kwargs)
        try:
            parts = [seg.text for seg in segments]
        except Exception as vad_err:
            if 'silero_vad' in str(vad_err).lower() or 'onnx' in str(vad_err).lower():
                logger.warning("VAD model unavailable, retrying without VAD: %s", vad_err)
                kwargs['vad_filter'] = False
                segments, info = _model.transcribe(file_path, **kwargs)
                parts = [seg.text for seg in segments]
            else:
                raise
        final_text = "".join(parts).strip()
        logger.debug("Transcription result: %s", final_text)
        return final_text
    except Exception as e:
        logger.error("Transcription error: %s", e)
        return ""
    finally:
        # Clean up temp file after transcription
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
                logger.debug("Deleted temp file: %s", file_path)
        except Exception as e:
            logger.warning("Could not delete temp file %s: %s", file_path, e)


def model_loaded():
    return _model is not None

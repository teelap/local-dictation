"""Speech-to-text via faster-whisper, biased toward the user's own vocabulary.

Two things here matter more than the model size:

* **Vocabulary biasing.** Names, jargon, and acronyms are fed to the decoder as
  hotwords and an initial prompt. This is what actually stops "Kubernetes" from
  coming out as "kubernetties" — post-hoc correction is a safety net, not a fix.
* **The language pool.** Auto-detecting across 100 languages is a much harder
  problem than choosing between the two or three a person actually speaks.
  Restricting detection to the user's pool is where multilingual accuracy comes
  from.
"""

import logging
import os

from faster_whisper import WhisperModel

logger = logging.getLogger(__name__)

_model = None
_model_info = {}


def init_model(model_size="base.en", device="auto", compute_type=None):
    """Load the Whisper model, falling back from CUDA to CPU automatically."""
    global _model, _model_info

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

    logger.info("Loading '%s' on %s (%s)...", model_size, actual_device.upper(), actual_compute)

    try:
        _model = WhisperModel(model_size, device=actual_device, compute_type=actual_compute)
    except Exception as e:
        # A CUDA build mismatch is common and entirely recoverable on CPU.
        if actual_device == "cuda":
            logger.warning("GPU load failed (%s) — retrying on CPU", e)
            actual_device, actual_compute = "cpu", compute_type or "int8"
            _model = WhisperModel(model_size, device=actual_device, compute_type=actual_compute)
        else:
            raise

    _model_info = {"model_size": model_size, "device": actual_device,
                   "compute_type": actual_compute}
    logger.info("Model loaded: %s", _model_info)
    return _model_info


def model_loaded():
    return _model is not None


def get_model_info():
    return dict(_model_info)


def detect_language(file_path, pool=None):
    """Choose the spoken language, restricted to the user's language pool.

    Returns an ISO code, or None to let Whisper decide for itself.
    """
    pool = [code for code in (pool or []) if code]
    if not pool:
        return None
    if len(pool) == 1:
        return pool[0]
    if _model is None:
        return pool[0]

    try:
        language, probability, all_probs = _model.detect_language(file_path)
    except (AttributeError, TypeError, RuntimeError) as e:
        logger.debug("Language detection unavailable (%s); using first pooled language", e)
        return pool[0]

    if all_probs:
        in_pool = [(code, prob) for code, prob in all_probs if code in pool]
        if in_pool:
            best_code, best_prob = max(in_pool, key=lambda item: item[1])
            logger.debug("Language: %s (%.2f) from pool %s", best_code, best_prob, pool)
            return best_code

    return language if language in pool else pool[0]


def transcribe_audio(file_path, language=None, language_pool=None,
                     initial_prompt=None, hotwords=None, delete_after=True):
    """Transcribe a WAV file.

    Args:
        file_path: Path to the recording.
        language: Explicit ISO code; overrides pool detection when set.
        language_pool: Candidate languages to restrict auto-detection to.
        initial_prompt: Vocabulary hint string, from the user's dictionary.
        hotwords: Terms to bias decoding toward.
        delete_after: Remove the temp file when done.

    Returns:
        (text, info) where info carries the detected language and duration.
    """
    if _model is None:
        raise RuntimeError("Model not initialized. Call init_model() first.")

    # "error" distinguishes a failure from genuine silence — without it the
    # caller reports a crashed transcription as "no speech detected".
    result_info = {"language": None, "duration": 0.0, "error": None}

    try:
        if not language:
            language = detect_language(file_path, language_pool)

        kwargs = {
            "beam_size": 1,
            "vad_filter": True,
            # Dictation is a series of independent utterances, not one long
            # document — carrying context between them causes runaway repetition.
            "condition_on_previous_text": False,
        }
        if language:
            kwargs["language"] = language
        if initial_prompt:
            kwargs["initial_prompt"] = initial_prompt

        segments, info = _transcribe(file_path, kwargs, hotwords)

        try:
            parts = [segment.text for segment in segments]
        except Exception as vad_error:
            message = str(vad_error).lower()
            if "silero" in message or "onnx" in message or "vad" in message:
                logger.warning("VAD unavailable, retrying without it: %s", vad_error)
                kwargs["vad_filter"] = False
                segments, info = _transcribe(file_path, kwargs, hotwords)
                parts = [segment.text for segment in segments]
            else:
                raise

        text = "".join(parts).strip()
        result_info["language"] = getattr(info, "language", language)
        result_info["duration"] = getattr(info, "duration", 0.0)
        logger.debug("Transcribed (%s): %s", result_info["language"], text[:120])
        return text, result_info

    except Exception as e:  # noqa: BLE001 — surfaced to the user via result_info
        logger.exception("Transcription error: %s", e)
        result_info["error"] = str(e)
        return "", result_info

    finally:
        if delete_after:
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
            except OSError as e:
                logger.warning("Could not delete %s: %s", file_path, e)


def _transcribe(file_path, kwargs, hotwords):
    """Call the model, degrading gracefully if the installed version is older.

    ``hotwords`` was added to faster-whisper after the initial releases; passing
    it to a version that predates it raises TypeError rather than being ignored.
    """
    if hotwords:
        try:
            return _model.transcribe(file_path, hotwords=hotwords, **kwargs)
        except TypeError:
            logger.debug("Installed faster-whisper has no hotwords support")
    return _model.transcribe(file_path, **kwargs)

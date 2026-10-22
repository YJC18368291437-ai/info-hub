"""QQ 语音(silk) -> 文字：pilk 解 silk 为 PCM，重采样后用 faster-whisper 本地识别。

用 numpy 数组直接喂 whisper，绕开 PyAV 解码（版本兼容更稳）。
模型首次使用自动下载（默认 small）。全部在本机跑，不外传。
"""
from __future__ import annotations

import io
import logging
import threading

import numpy as np

logger = logging.getLogger("info_hub.asr")

_model = None
_model_key: tuple[str, str] | None = None
_lock = threading.Lock()


def _get_model(model_name: str, device: str):
    global _model, _model_key
    key = (model_name, device)
    with _lock:
        if _model is None or _model_key != key:
            from faster_whisper import WhisperModel

            try:
                _model = WhisperModel(model_name, device=device, compute_type="auto")
            except Exception as exc:  # noqa: BLE001
                logger.warning("whisper %s/%s 初始化失败，回退 CPU int8: %s", model_name, device, exc)
                _model = WhisperModel(model_name, device="cpu", compute_type="int8")
            _model_key = key
    return _model


def silk_to_float(silk_bytes: bytes, src_rate: int = 24000) -> np.ndarray:
    """silk -> 16kHz float32 单声道 numpy 数组。"""
    from pilk import decode

    out = io.BytesIO()
    decode(io.BytesIO(silk_bytes), out)
    pcm = np.frombuffer(out.getvalue(), dtype="<i2").astype(np.float32) / 32768.0
    if src_rate != 16000 and pcm.size > 1:
        n = max(1, int(pcm.size * 16000 / src_rate))
        pcm = np.interp(np.linspace(0, pcm.size - 1, n), np.arange(pcm.size), pcm).astype(np.float32)
    return pcm


def transcribe_silk(silk_bytes: bytes, model_name: str = "small",
                    device: str = "auto", language: str = "zh") -> str:
    """返回识别文本；失败抛异常由调用方兜底。"""
    audio = silk_to_float(silk_bytes)
    model = _get_model(model_name, device)
    segments, _info = model.transcribe(audio, language=language, beam_size=5, vad_filter=True)
    return "".join(s.text for s in segments).strip()

"""
Kim — Voice Engine (Phase 8: Voice UI Integration)

Gives Kim a realistic human voice using local TTS inference.

Architecture
────────────
  • TTS generation runs in a background thread to avoid blocking the agent loop.
  • Audio playback uses sounddevice (cross-platform, no extra system deps).
  • Supports two backends:
      1. kokoro — lightweight local TTS (pip install kokoro soundfile)
      2. HTTP API — any OpenAI-compatible TTS endpoint on localhost
         (e.g., Maya-1, AllTalk, local OpenAI-TTS server)

Text sanitisation strips Markdown formatting (*, **, `, ```, #, [], {})
so Kim never says "asterisk" or "bracket" aloud.

Usage:
    from tray.voice import VoiceEngine

    engine = VoiceEngine(config)
    await engine.speak("Task complete. I opened Notepad.")
    engine.shutdown()
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import threading
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

logger = logging.getLogger("kim.voice")


# ---------------------------------------------------------------------------
# Text sanitisation — strip Markdown / JSON noise before speaking
# ---------------------------------------------------------------------------

def clean_for_speech(text: str) -> str:
    """
    Remove Markdown formatting, JSON brackets, and other noise that
    would sound terrible when spoken aloud.
    """
    if not text:
        return ""

    # Remove code fences
    text = re.sub(r"```[\s\S]*?```", "", text)

    # Remove inline code
    text = re.sub(r"`([^`]*)`", r"\1", text)

    # Remove markdown headings (# ## ###)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)

    # Remove bold/italic markers: ** * __ _
    text = re.sub(r"\*{1,3}([^*]*)\*{1,3}", r"\1", text)
    text = re.sub(r"_{1,3}([^_]*)_{1,3}", r"\1", text)

    # Remove markdown links [text](url) → text
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)

    # Remove image syntax ![alt](url)
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)

    # Remove JSON-like brackets and braces
    text = re.sub(r"[{}\[\]]", "", text)

    # Remove excessive quotes
    text = text.replace('"""', "").replace("'''", "")

    # Collapse multiple spaces / newlines
    text = re.sub(r"\s+", " ", text).strip()

    # Remove leftover punctuation noise
    text = text.replace(" ,", ",").replace(" .", ".")

    # Truncate very long texts (TTS shouldn't speak novels)
    if len(text) > 500:
        # Find a sentence boundary near 500 chars
        cutoff = text[:500].rfind(".")
        if cutoff > 200:
            text = text[: cutoff + 1]
        else:
            text = text[:500] + "..."

    return text


# ---------------------------------------------------------------------------
# VoiceEngine
# ---------------------------------------------------------------------------

class VoiceEngine:
    """
    Async-friendly TTS engine that speaks text in a background thread.

    Supports:
      - kokoro: Local lightweight TTS (default if installed)
      - http:   Any OpenAI-compatible TTS API on localhost

    The speak() method is async but non-blocking — audio generation
    and playback happen in a thread pool.
    """

    def __init__(self, config: dict):
        voice_cfg = config.get("voice", {})
        self._enabled = bool(voice_cfg.get("enabled", config.get("voice_enabled", False)))
        self._backend = voice_cfg.get("backend", "kokoro")  # "kokoro" | "http"
        self._voice_id = voice_cfg.get("voice_id", "af_heart")  # kokoro voice name
        self._speed = float(voice_cfg.get("speed", 1.0))

        # HTTP backend settings
        self._http_url = voice_cfg.get(
            "http_url", "http://localhost:8880/v1/audio/speech"
        )
        self._http_model = voice_cfg.get("http_model", "tts-1")
        self._http_voice = voice_cfg.get("http_voice", "nova")

        # Thread pool for non-blocking playback
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="kim-voice")
        self._lock = threading.Lock()

        # Lazy-loaded TTS model
        self._kokoro_pipeline = None
        self._kokoro_available: Optional[bool] = None

        if self._enabled:
            logger.info(
                f"VoiceEngine initialized: backend={self._backend} "
                f"voice_id={self._voice_id} speed={self._speed}"
            )
        else:
            logger.debug("VoiceEngine: disabled (voice_enabled=false)")

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, value: bool) -> None:
        self._enabled = value
        logger.info(f"Voice {'enabled' if value else 'disabled'}")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def speak(self, text: str) -> None:
        """
        Speak the given text asynchronously (non-blocking).
        Text is cleaned of Markdown/JSON before speaking.
        Does nothing if voice is disabled or text is empty.
        """
        if not self._enabled:
            return

        cleaned = clean_for_speech(text)
        if not cleaned or len(cleaned) < 3:
            return

        logger.debug(f"Speaking: {cleaned[:80]}...")

        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(self._executor, self._speak_sync, cleaned)
        except Exception as e:
            logger.warning(f"Voice playback failed: {e}")

    def speak_fire_and_forget(self, text: str) -> None:
        """
        Non-async version: submit speech to thread pool without waiting.
        Safe to call from any thread.
        """
        if not self._enabled:
            return

        cleaned = clean_for_speech(text)
        if not cleaned or len(cleaned) < 3:
            return

        self._executor.submit(self._speak_sync, cleaned)

    def shutdown(self) -> None:
        """Clean up resources."""
        self._executor.shutdown(wait=False)
        logger.debug("VoiceEngine shut down")

    # ------------------------------------------------------------------
    # Backend: kokoro (local TTS)
    # ------------------------------------------------------------------

    def _init_kokoro(self) -> bool:
        """Lazy-init the kokoro TTS pipeline. Returns True if available."""
        if self._kokoro_available is not None:
            return self._kokoro_available

        try:
            from kokoro import KPipeline
            self._kokoro_pipeline = KPipeline(lang_code="a")
            self._kokoro_available = True
            logger.info(f"Kokoro TTS loaded (voice={self._voice_id})")
            return True
        except ImportError:
            logger.warning(
                "kokoro not installed. Install with: pip install kokoro soundfile\n"
                "Falling back to HTTP TTS backend."
            )
            self._kokoro_available = False
            return False
        except Exception as e:
            logger.error(f"Kokoro init failed: {e}")
            self._kokoro_available = False
            return False

    def _speak_kokoro(self, text: str) -> bool:
        """Generate and play audio via kokoro. Returns True on success."""
        if not self._init_kokoro():
            return False

        try:
            import sounddevice as sd

            # Generate audio samples
            generator = self._kokoro_pipeline(
                text,
                voice=self._voice_id,
                speed=self._speed,
            )

            for _, _, audio in generator:
                # audio is a numpy array of float32 samples at 24kHz
                sd.play(audio, samplerate=24000, blocking=True)

            return True
        except ImportError:
            logger.warning(
                "sounddevice not installed. Install with: pip install sounddevice"
            )
            return False
        except Exception as e:
            logger.warning(f"Kokoro playback error: {e}")
            return False

    # ------------------------------------------------------------------
    # Backend: HTTP (OpenAI-compatible TTS API)
    # ------------------------------------------------------------------

    def _speak_http(self, text: str) -> bool:
        """Send text to an HTTP TTS endpoint and play returned audio."""
        try:
            import urllib.request
            import json as json_mod
            import sounddevice as sd
            import numpy as np

            payload = json_mod.dumps({
                "model": self._http_model,
                "input": text,
                "voice": self._http_voice,
                "response_format": "wav",
            }).encode("utf-8")

            req = urllib.request.Request(
                self._http_url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )

            with urllib.request.urlopen(req, timeout=30) as resp:
                audio_data = resp.read()

            # Parse WAV and play
            with wave.open(io.BytesIO(audio_data), "rb") as wf:
                channels = wf.getnchannels()
                sample_width = wf.getsampwidth()
                framerate = wf.getframerate()
                frames = wf.readframes(wf.getnframes())

            # Convert bytes to numpy array
            dtype_map = {1: np.int8, 2: np.int16, 4: np.int32}
            dtype = dtype_map.get(sample_width, np.int16)
            audio = np.frombuffer(frames, dtype=dtype)
            if channels > 1:
                audio = audio.reshape(-1, channels)

            # Normalize to float32 for sounddevice
            audio_float = audio.astype(np.float32) / np.iinfo(dtype).max
            sd.play(audio_float, samplerate=framerate, blocking=True)
            return True

        except ImportError as e:
            logger.warning(f"HTTP TTS dependency missing: {e}")
            return False
        except Exception as e:
            logger.warning(f"HTTP TTS error: {e}")
            return False

    # ------------------------------------------------------------------
    # Dispatcher
    # ------------------------------------------------------------------

    def _speak_sync(self, text: str) -> None:
        """Synchronous speech — called from thread pool. Tries backends in order."""
        with self._lock:  # One utterance at a time
            if self._backend == "kokoro":
                if self._speak_kokoro(text):
                    return
                # Fallback to HTTP if kokoro fails
                if self._speak_http(text):
                    return
            elif self._backend == "http":
                if self._speak_http(text):
                    return
                # Fallback to kokoro if HTTP fails
                if self._speak_kokoro(text):
                    return
            else:
                logger.error(f"Unknown voice backend: {self._backend}")
                return

            logger.warning(
                "All TTS backends failed. Install kokoro+sounddevice or "
                "start a local TTS server."
            )

"""
Kim — Voice Engine (Modular Provider Architecture)

Gives Kim a realistic human voice using local TTS inference.

Architecture
────────────
  • Modular provider system with a BaseVoiceProvider interface.
  • Built-in providers:
      1. KokoroVoiceProvider — lightweight local TTS (pip install kokoro soundfile)
      2. MayaVoiceProvider   — Maya-1 3B voice model via HuggingFace transformers + SNAC
      3. HttpVoiceProvider   — any OpenAI-compatible TTS endpoint on localhost
      4. HumeVoiceProvider   — Hume AI cloud TTS (ultra-low latency, no local GPU)
  • TTS generation runs in a background thread to avoid blocking the agent loop.
  • Audio playback uses sounddevice (cross-platform, no extra system deps).
  • Config toggle: voice.engine = "kokoro" | "maya1" | "http" | "hume"

Text sanitisation strips Markdown formatting (*, **, `, ```, #, [], {})
so Kim never says "asterisk" or "bracket" aloud.

Usage:
    from tray.voice import VoiceEngine, VoiceStatus

    engine = VoiceEngine(config)
    engine.set_status_callback(my_callback)
    engine.warm_up()           # pre-load model in background
    await engine.speak("Task complete. I opened Notepad.")
    engine.shutdown()
"""

from __future__ import annotations

import abc
import asyncio
import enum
import io
import logging
import re
import threading
import wave
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

logger = logging.getLogger("kim.voice")


# ---------------------------------------------------------------------------
# Voice status enum — consumed by UI for progress indication
# ---------------------------------------------------------------------------

class VoiceStatus(enum.Enum):
    DISABLED = "disabled"
    LOADING = "loading"
    READY = "ready"
    FAILED = "failed"


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
# Base Voice Provider
# ---------------------------------------------------------------------------

class BaseVoiceProvider(abc.ABC):
    """
    Abstract base class for all TTS voice providers.

    Subclasses must implement:
      - name:       a human-readable provider name (property)
      - initialize: lazy-load model / connect to service
      - speak_sync: synchronous text-to-speech (called from thread pool)
      - shutdown:   release resources
    """

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Human-readable name of this provider (e.g. 'Kokoro', 'Maya-1')."""

    @abc.abstractmethod
    def initialize(self) -> bool:
        """
        Lazy-initialize the provider (load model, check deps, etc.).
        Returns True if ready, False otherwise.
        Thread-safe: implementations must guard against concurrent calls.
        """

    @abc.abstractmethod
    def speak_sync(self, text: str) -> bool:
        """
        Speak the given (already-cleaned) text synchronously.
        Returns True on success, False on failure.
        """

    def shutdown(self) -> None:
        """Release provider resources. Override if needed."""
        pass


# ---------------------------------------------------------------------------
# Kokoro Voice Provider
# ---------------------------------------------------------------------------

class KokoroVoiceProvider(BaseVoiceProvider):
    """
    Local lightweight TTS using the Kokoro library.
    Requires: pip install kokoro soundfile sounddevice
    """

    def __init__(self, voice_id: str = "af_sky", speed: float = 1.1):
        self._voice_id = voice_id
        self._speed = speed
        self._pipeline = None
        self._available: Optional[bool] = None
        self._init_lock = threading.Lock()

    @property
    def name(self) -> str:
        return "Kokoro"

    def initialize(self) -> bool:
        with self._init_lock:
            if self._available is not None:
                return self._available

            try:
                from kokoro import KPipeline
                self._pipeline = KPipeline(lang_code="a")
                self._available = True
                logger.info(f"Kokoro TTS loaded (voice={self._voice_id})")
                return True
            except ImportError:
                logger.warning(
                    "kokoro not installed. Install with: pip install kokoro soundfile"
                )
                self._available = False
                return False
            except Exception as e:
                logger.error(f"Kokoro init failed: {e}")
                self._available = False
                return False

    def speak_sync(self, text: str) -> bool:
        if not self.initialize():
            return False

        try:
            import sounddevice as sd

            generator = self._pipeline(
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

    def shutdown(self) -> None:
        self._pipeline = None
        self._available = None
        logger.debug("Kokoro provider shut down")


# ---------------------------------------------------------------------------
# Maya-1 Voice Provider
# ---------------------------------------------------------------------------

class MayaVoiceProvider(BaseVoiceProvider):
    """
    Maya-1 (3B) voice model by Maya Research.

    Uses the HuggingFace transformers library to generate SNAC audio codec
    tokens, which are then decoded into a 24 kHz waveform via the SNAC decoder.

    Runs on Apple Silicon via the MPS backend.

    Requires: pip install torch transformers snac sounddevice

    Supported emotion tags (passed through from Gemini):
        <laugh>, <sigh>, <gasp>, <whisper>, <angry>, <cheerful>, <sad>
    """

    # Emotion tags that Maya-1 recognises — we preserve these in the text
    EMOTION_TAGS = {
        "<laugh>", "<sigh>", "<gasp>", "<whisper>",
        "<angry>", "<cheerful>", "<sad>", "<surprised>",
        "</laugh>", "</sigh>", "</gasp>", "</whisper>",
        "</angry>", "</cheerful>", "</sad>", "</surprised>",
    }

    def __init__(
        self,
        model_id: str = "maya-research/maya1",
        speaker_description: str = "A young female with a warm, clear, and natural voice.",
        max_new_tokens: int = 2048,
        use_4bit: bool = False,
    ):
        self._model_id = model_id
        self._speaker_description = speaker_description
        self._max_new_tokens = max_new_tokens
        self._use_4bit = use_4bit

        self._model = None
        self._tokenizer = None
        self._snac_decoder = None
        self._device = None
        self._available: Optional[bool] = None
        self._init_lock = threading.Lock()

    @property
    def name(self) -> str:
        return "Maya-1"

    def _select_device(self) -> str:
        """Select the best available device: MPS (Apple Silicon) > CUDA > CPU."""
        try:
            import torch

            if torch.backends.mps.is_available():
                logger.info("Maya-1: Using MPS (Apple Silicon) backend")
                return "mps"
            elif torch.cuda.is_available():
                logger.info("Maya-1: Using CUDA backend")
                return "cuda"
            else:
                logger.info("Maya-1: Using CPU backend (slow)")
                return "cpu"
        except Exception:
            return "cpu"

    def initialize(self) -> bool:
        with self._init_lock:
            if self._available is not None:
                return self._available

            try:
                import torch
                from transformers import AutoModelForCausalLM, AutoTokenizer

                self._device = self._select_device()

                logger.info(f"Maya-1: Loading tokenizer from {self._model_id}...")
                self._tokenizer = AutoTokenizer.from_pretrained(self._model_id)

                # Determine dtype and quantisation
                torch_dtype = torch.float16
                load_kwargs = {
                    "torch_dtype": torch_dtype,
                    "trust_remote_code": True,
                }

                if self._use_4bit:
                    try:
                        from transformers import BitsAndBytesConfig
                        load_kwargs["quantization_config"] = BitsAndBytesConfig(
                            load_in_4bit=True,
                            bnb_4bit_compute_dtype=torch.float16,
                        )
                        logger.info("Maya-1: Loading model with 4-bit quantisation")
                    except ImportError:
                        logger.warning(
                            "bitsandbytes not installed, loading without quantisation"
                        )
                        load_kwargs["device_map"] = "auto"
                else:
                    load_kwargs["device_map"] = "auto"

                logger.info(f"Maya-1: Loading model from {self._model_id}...")
                self._model = AutoModelForCausalLM.from_pretrained(
                    self._model_id, **load_kwargs
                )
                self._model.eval()

                # Load the SNAC decoder for audio codec token → waveform
                logger.info("Maya-1: Loading SNAC decoder...")
                try:
                    from snac import SNAC
                    self._snac_decoder = SNAC.from_pretrained("hubertsiuzdak/snac_24khz")
                    self._snac_decoder = self._snac_decoder.to(self._device)
                    self._snac_decoder.eval()
                except ImportError:
                    logger.error(
                        "snac not installed. Install with: pip install snac\n"
                        "Maya-1 requires the SNAC audio codec to decode speech."
                    )
                    self._available = False
                    return False

                self._available = True
                logger.info("Maya-1: Model and decoder loaded successfully")
                return True

            except ImportError as e:
                logger.error(
                    f"Maya-1 dependency missing: {e}\n"
                    "Install with: pip install torch transformers snac sounddevice"
                )
                self._available = False
                return False
            except Exception as e:
                logger.error(f"Maya-1 init failed: {e}", exc_info=True)
                self._available = False
                return False

    def _prepare_prompt(self, text: str) -> str:
        """
        Build the Maya-1 prompt format with speaker description.

        Preserves emotion tags like <laugh>, <whisper>, etc. that Gemini
        may include in the text — Maya-1 is trained to interpret these.
        """
        return f"<description={self._speaker_description}> {text}"

    def _clean_emotion_tags_for_fallback(self, text: str) -> str:
        """Strip emotion tags if the provider doesn't support them."""
        for tag in self.EMOTION_TAGS:
            text = text.replace(tag, "")
        return text.strip()

    def speak_sync(self, text: str) -> bool:
        if not self.initialize():
            return False

        try:
            import torch
            import sounddevice as sd
            import numpy as np

            prompt = self._prepare_prompt(text)

            inputs = self._tokenizer(prompt, return_tensors="pt")
            inputs = {k: v.to(self._model.device) for k, v in inputs.items()}

            with torch.no_grad():
                output_tokens = self._model.generate(
                    **inputs,
                    max_new_tokens=self._max_new_tokens,
                    do_sample=True,
                    temperature=0.7,
                    top_p=0.95,
                )

            # Strip the prompt tokens, keep only generated tokens
            generated = output_tokens[:, inputs["input_ids"].shape[1]:]

            # 1. Extract only valid SNAC tokens from the generation
            # Maya-1 SNAC tokens range from 128266 to 156937
            snac_tokens = [t.item() for t in generated[0] if 128266 <= t.item() <= 156937]

            # 2. Ensure complete 7-token frames
            frames = len(snac_tokens) // 7
            snac_tokens = snac_tokens[:frames * 7]

            if frames == 0:
                return False  # No valid audio generated

            # 3. Unpack 7-token frames into 3 hierarchical SNAC levels
            l1, l2, l3 = [], [], []
            CODE_OFFSET = 128266

            for i in range(frames):
                slots = snac_tokens[i*7 : (i+1)*7]

                # L1: coarse (1x rate)
                l1.append((slots[0] - CODE_OFFSET) % 4096)

                # L2: medium (2x rate)
                l2.extend([
                    (slots[1] - CODE_OFFSET) % 4096,
                    (slots[4] - CODE_OFFSET) % 4096,
                ])

                # L3: fine (4x rate)
                l3.extend([
                    (slots[2] - CODE_OFFSET) % 4096,
                    (slots[3] - CODE_OFFSET) % 4096,
                    (slots[5] - CODE_OFFSET) % 4096,
                    (slots[6] - CODE_OFFSET) % 4096,
                ])

            # 4. Convert levels to tensors expected by SNAC
            codes = [
                torch.tensor(level, dtype=torch.long, device=self._device).unsqueeze(0)
                for level in [l1, l2, l3]
            ]

            # 5. Decode into audio
            with torch.no_grad():
                z_q = self._snac_decoder.quantizer.from_codes(codes)
                audio_waveform = self._snac_decoder.decoder(z_q)
                audio_np = audio_waveform[0, 0].cpu().numpy()  # Extract to 1D numpy array

            # Normalise to [-1, 1] range for playback
            peak = np.abs(audio_np).max()
            if peak > 0:
                audio_np = audio_np / peak

            sd.play(audio_np.astype(np.float32), samplerate=24000, blocking=True)
            return True

        except ImportError as e:
            logger.warning(f"Maya-1 dependency missing for playback: {e}")
            return False
        except Exception as e:
            logger.warning(f"Maya-1 playback error: {e}", exc_info=True)
            return False

    def shutdown(self) -> None:
        """Release GPU memory held by the model and decoder."""
        try:
            import torch

            if self._model is not None:
                del self._model
                self._model = None
            if self._snac_decoder is not None:
                del self._snac_decoder
                self._snac_decoder = None
            if self._tokenizer is not None:
                del self._tokenizer
                self._tokenizer = None

            # Free MPS/CUDA memory
            if torch.backends.mps.is_available():
                torch.mps.empty_cache()
            elif torch.cuda.is_available():
                torch.cuda.empty_cache()

        except Exception as e:
            logger.warning(f"Maya-1 shutdown error: {e}")

        self._available = None
        logger.debug("Maya-1 provider shut down")


# ---------------------------------------------------------------------------
# HTTP Voice Provider (OpenAI-compatible TTS API)
# ---------------------------------------------------------------------------

class HttpVoiceProvider(BaseVoiceProvider):
    """
    Sends text to an HTTP TTS endpoint (OpenAI-compatible) and plays
    the returned WAV audio.
    """

    def __init__(
        self,
        url: str = "http://localhost:8880/v1/audio/speech",
        model: str = "tts-1",
        voice: str = "nova",
    ):
        self._url = url
        self._model = model
        self._voice = voice

    @property
    def name(self) -> str:
        return "HTTP"

    def initialize(self) -> bool:
        # HTTP provider has no lazy init — it's always "available",
        # failures happen at speak-time if the server is down.
        return True

    def speak_sync(self, text: str) -> bool:
        try:
            import urllib.request
            import json as json_mod
            import sounddevice as sd
            import numpy as np

            payload = json_mod.dumps({
                "model": self._model,
                "input": text,
                "voice": self._voice,
                "response_format": "wav",
            }).encode("utf-8")

            req = urllib.request.Request(
                self._url,
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


# ---------------------------------------------------------------------------
# Hume AI Voice Provider (cloud TTS via REST API)
# ---------------------------------------------------------------------------

class HumeVoiceProvider(BaseVoiceProvider):
    """
    Ultra-low-latency cloud TTS using Hume AI's Octave model.

    Requires: HUME_API_KEY set in .env or environment.
    Endpoint:  POST https://api.hume.ai/v0/tts
    Response:  JSON with base64-encoded audio in generations[0].audio

    This is a cloud provider — no local GPU/RAM needed.
    """

    _API_URL = "https://api.hume.ai/v0/tts"

    def __init__(
        self,
        voice_name: str = "Ava Song",
        description: str = "A warm, clear, and friendly female voice.",
        config_dict: Optional[dict] = None,
    ):
        self._voice_name = voice_name
        self._description = description
        self._config_dict = config_dict
        self._api_key: Optional[str] = None
        self._available: Optional[bool] = None
        self._init_lock = threading.Lock()

    @property
    def name(self) -> str:
        return "Hume"

    def initialize(self) -> bool:
        with self._init_lock:
            if self._available is not None:
                return self._available

            import os
            self._api_key = os.getenv("HUME_API_KEY", "")
            if not self._api_key:
                logger.error(
                    "HUME_API_KEY not set. Add it to your .env file.\n"
                    "  HUME_API_KEY=your-key-here"
                )
                self._available = False
                return False

            self._available = True
            logger.info("Hume AI TTS provider initialized")
            return True

    def speak_sync(self, text: str) -> bool:
        if not self.initialize():
            return False

        try:
            import urllib.request
            import json as json_mod
            import base64
            import sounddevice as sd
            import numpy as np

            # Read the current voice from config (allows hot-swap from UI)
            voice_name = self._voice_name
            if self._config_dict:
                voice_name = (
                    self._config_dict
                    .get("voice", {})
                    .get("hume", {})
                    .get("voice_name", self._voice_name)
                )

            payload = json_mod.dumps({
                "utterances": [
                    {
                        "text": text,
                        "voice": {
                            "name": voice_name,
                            "provider": "HUME_AI"
                        }
                    }
                ],
                "format": {
                    "type": "wav"
                }
            }).encode("utf-8")

            req = urllib.request.Request(
                self._API_URL,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "KimVoiceClient/1.0",
                    "X-Hume-Api-Key": self._api_key,
                },
                method="POST",
            )

            with urllib.request.urlopen(req, timeout=15) as resp:
                body = json_mod.loads(resp.read().decode("utf-8"))

            # Extract base64 audio from response
            generations = body.get("generations", [])
            if not generations:
                logger.warning("Hume TTS returned no generations")
                return False

            audio_b64 = generations[0].get("audio", "")
            if not audio_b64:
                logger.warning("Hume TTS generation contained no audio")
                return False

            audio_bytes = base64.b64decode(audio_b64)

            # Parse WAV and play via sounddevice
            with wave.open(io.BytesIO(audio_bytes), "rb") as wf:
                channels = wf.getnchannels()
                sample_width = wf.getsampwidth()
                framerate = wf.getframerate()
                frames = wf.readframes(wf.getnframes())

            dtype_map = {1: np.int8, 2: np.int16, 4: np.int32}
            dtype = dtype_map.get(sample_width, np.int16)
            audio = np.frombuffer(frames, dtype=dtype)
            if channels > 1:
                audio = audio.reshape(-1, channels)

            audio_float = audio.astype(np.float32) / np.iinfo(dtype).max
            sd.play(audio_float, samplerate=framerate, blocking=True)
            return True

        except ImportError as e:
            logger.warning(f"Hume TTS dependency missing: {e}")
            return False
        except Exception as e:
            logger.warning(f"Hume TTS error: {e}")
            return False

    def shutdown(self) -> None:
        self._api_key = None
        self._available = None
        logger.debug("Hume provider shut down")


# ---------------------------------------------------------------------------
# Provider Registry
# ---------------------------------------------------------------------------

# Maps config engine names → provider factory callables
PROVIDER_REGISTRY: dict[str, type[BaseVoiceProvider]] = {
    "kokoro": KokoroVoiceProvider,
    "maya1": MayaVoiceProvider,
    "http": HttpVoiceProvider,
    "hume": HumeVoiceProvider,
}


def _build_provider(voice_cfg: dict, config_dict: Optional[dict] = None) -> BaseVoiceProvider:
    """Instantiate the correct provider based on config.yaml's voice.engine field."""
    engine = voice_cfg.get("engine", voice_cfg.get("backend", "kokoro"))

    if engine == "kokoro":
        return KokoroVoiceProvider(
            voice_id=voice_cfg.get("voice_id", "af_sky"),
            speed=float(voice_cfg.get("speed", 1.1)),
        )

    elif engine == "maya1":
        maya_cfg = voice_cfg.get("maya1", {})
        return MayaVoiceProvider(
            model_id=maya_cfg.get("model_id", "maya-research/maya1"),
            speaker_description=maya_cfg.get(
                "speaker_description",
                "A young female with a warm, clear, and natural voice.",
            ),
            max_new_tokens=int(maya_cfg.get("max_new_tokens", 2048)),
            use_4bit=bool(maya_cfg.get("use_4bit", False)),
        )

    elif engine == "http":
        return HttpVoiceProvider(
            url=voice_cfg.get("http_url", "http://localhost:8880/v1/audio/speech"),
            model=voice_cfg.get("http_model", "tts-1"),
            voice=voice_cfg.get("http_voice", "nova"),
        )

    elif engine == "hume":
        hume_cfg = voice_cfg.get("hume", {})
        return HumeVoiceProvider(
            voice_name=hume_cfg.get("voice_name", "Ava Song"),
            description=hume_cfg.get(
                "description",
                "A warm, clear, and friendly female voice.",
            ),
            config_dict=config_dict,
        )

    else:
        logger.warning(
            f"Unknown voice engine '{engine}', falling back to Kokoro"
        )
        return KokoroVoiceProvider(
            voice_id=voice_cfg.get("voice_id", "af_sky"),
            speed=float(voice_cfg.get("speed", 1.1)),
        )


# ---------------------------------------------------------------------------
# Fallback chain builder
# ---------------------------------------------------------------------------

def _build_fallback_chain(primary: BaseVoiceProvider, voice_cfg: dict) -> list[BaseVoiceProvider]:
    """
    Build an ordered fallback chain: [primary, ...fallbacks].

    If the primary fails at speak-time, the engine tries each fallback
    in order until one succeeds.
    """
    chain = [primary]
    engine = voice_cfg.get("engine", voice_cfg.get("backend", "kokoro"))

    # Add sensible fallbacks (avoid duplicating the primary)
    if engine != "kokoro":
        chain.append(KokoroVoiceProvider(
            voice_id=voice_cfg.get("voice_id", "af_sky"),
            speed=float(voice_cfg.get("speed", 1.1)),
        ))
    if engine != "http":
        chain.append(HttpVoiceProvider(
            url=voice_cfg.get("http_url", "http://localhost:8880/v1/audio/speech"),
            model=voice_cfg.get("http_model", "tts-1"),
            voice=voice_cfg.get("http_voice", "nova"),
        ))

    return chain


# ---------------------------------------------------------------------------
# VoiceEngine — public facade
# ---------------------------------------------------------------------------

class VoiceEngine:
    """
    Async-friendly TTS engine that speaks text in a background thread.

    Supports multiple voice providers via a modular architecture:
      - kokoro:  Local lightweight TTS (default)
      - maya1:   Maya-1 3B voice model (HuggingFace + SNAC)
      - http:    Any OpenAI-compatible TTS API on localhost

    The speak() method is async but non-blocking — audio generation
    and playback happen in a thread pool.

    Status tracking:
      Call set_status_callback(fn) to receive (VoiceStatus, str) updates.
      Call warm_up() after construction to pre-load the model in background.
    """

    def __init__(self, config: dict):
        voice_cfg = config.get("voice", {})
        self._enabled = bool(
            voice_cfg.get("enabled", config.get("voice_enabled", False))
        )

        # Build provider + fallback chain
        self._provider = _build_provider(voice_cfg, config_dict=config)
        self._fallback_chain = _build_fallback_chain(self._provider, voice_cfg)

        # Thread pool for non-blocking playback
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="kim-voice"
        )
        self._lock = threading.Lock()

        # Status tracking for UI
        self._status = VoiceStatus.DISABLED
        self._status_callback: Optional[Callable[[VoiceStatus, str], None]] = None

        if self._enabled:
            logger.info(
                f"VoiceEngine initialized: engine={self._provider.name} "
                f"(fallback chain: "
                f"{' -> '.join(p.name for p in self._fallback_chain)})"
            )
        else:
            logger.debug("VoiceEngine: disabled (voice.enabled=false)")

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def status(self) -> VoiceStatus:
        return self._status

    @property
    def active_provider(self) -> str:
        """Name of the currently active voice provider."""
        return self._provider.name if self._provider else "none"

    # ------------------------------------------------------------------
    # Status tracking
    # ------------------------------------------------------------------

    def set_status_callback(self, callback: Callable[[VoiceStatus, str], None]) -> None:
        """Register a callback for status changes: fn(status, message).
        The callback may fire from any thread — callers must schedule
        UI updates via root.after() or equivalent."""
        self._status_callback = callback

    def _set_status(self, status: VoiceStatus, message: str = "") -> None:
        self._status = status
        if self._status_callback:
            try:
                self._status_callback(status, message)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Background warm-up
    # ------------------------------------------------------------------

    def warm_up(self) -> None:
        """Pre-initialize the active provider in the background thread pool.
        Status callback fires with LOADING → READY/FAILED."""
        if not self._enabled:
            self._set_status(VoiceStatus.DISABLED, "Voice disabled")
            return
        self._set_status(VoiceStatus.LOADING, f"Loading {self._provider.name}...")
        self._executor.submit(self._warm_up_sync)

    def _warm_up_sync(self) -> None:
        """Runs in the thread pool — initializes the primary provider."""
        with self._lock:
            try:
                ok = self._provider.initialize()
                if ok:
                    self._set_status(VoiceStatus.READY, f"{self._provider.name} ready")
                else:
                    self._set_status(VoiceStatus.FAILED, f"{self._provider.name} unavailable")
            except Exception as e:
                logger.error(f"Warm-up failed: {e}", exc_info=True)
                self._set_status(VoiceStatus.FAILED, f"{self._provider.name} failed: {e}")

    # ------------------------------------------------------------------
    # Enable / disable
    # ------------------------------------------------------------------

    def set_enabled(self, value: bool) -> None:
        self._enabled = value
        logger.info(f"Voice {'enabled' if value else 'disabled'}")
        if value and self._status in (VoiceStatus.DISABLED, VoiceStatus.FAILED):
            self.warm_up()
        elif not value:
            self._set_status(VoiceStatus.DISABLED, "Voice disabled")

    # ------------------------------------------------------------------
    # Engine hot-swap
    # ------------------------------------------------------------------

    def switch_engine(self, new_engine: str, config_dict: dict) -> None:
        """Dynamically switch voice engines while actively clearing memory."""
        self._set_status(VoiceStatus.LOADING, f"Switching to {new_engine}...")

        # Phase 1: detach old providers under lock
        with self._lock:
            old_providers = list(self._fallback_chain)
            self._fallback_chain = []
            self._provider = None

        # Phase 2: shutdown + GC outside lock (avoids deadlock with finalizers)
        for p in old_providers:
            try:
                p.shutdown()
            except Exception as e:
                logger.warning(f"Error shutting down fallback: {e}")

        import gc
        gc.collect()

        try:
            import torch
            if torch.backends.mps.is_available():
                torch.mps.empty_cache()
            elif torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:
            pass

        # Phase 3: build + initialize new provider under lock
        with self._lock:
            if "voice" not in config_dict:
                config_dict["voice"] = {}
            config_dict["voice"]["engine"] = new_engine

            self._provider = _build_provider(config_dict["voice"])
            self._fallback_chain = _build_fallback_chain(self._provider, config_dict["voice"])

            try:
                ok = self._provider.initialize()
                if ok:
                    self._set_status(VoiceStatus.READY, f"{self._provider.name} ready")
                else:
                    self._set_status(VoiceStatus.FAILED, f"{self._provider.name} unavailable")
            except Exception as e:
                logger.error(f"Engine switch init failed: {e}", exc_info=True)
                self._set_status(VoiceStatus.FAILED, f"Failed: {e}")

        logger.info(f"VoiceEngine active provider switched to: {self._provider.name}")

    # ------------------------------------------------------------------
    # Public speech API
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

        loop = asyncio.get_running_loop()
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
        """Clean up resources for all providers in the fallback chain."""
        for provider in self._fallback_chain:
            try:
                provider.shutdown()
            except Exception as e:
                logger.warning(f"Error shutting down {provider.name}: {e}")
        self._executor.shutdown(wait=False)
        logger.debug("VoiceEngine shut down")

    # ------------------------------------------------------------------
    # Dispatcher — tries providers in fallback order
    # ------------------------------------------------------------------

    def _speak_sync(self, text: str) -> None:
        """Synchronous speech — called from thread pool. Tries providers in fallback order."""
        with self._lock:  # One utterance at a time
            if not self._provider:
                return  # Provider being swapped — skip this utterance

            for provider in self._fallback_chain:
                try:
                    if provider.speak_sync(text):
                        return
                except Exception as e:
                    logger.warning(
                        f"Provider {provider.name} failed: {e}"
                    )
                    continue

            logger.warning(
                "All TTS providers failed. Install kokoro+sounddevice, "
                "configure Maya-1, or start a local TTS server."
            )

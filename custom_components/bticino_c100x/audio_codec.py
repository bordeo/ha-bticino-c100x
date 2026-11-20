"""Audio codec helpers for Classe 100X monitoring (Opus / Speex / G.711)."""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import ctypes.util
import logging
import platform
import shutil
import subprocess
from ctypes import (
    POINTER,
    Structure,
    addressof,
    byref,
    c_char,
    c_char_p,
    c_int,
    c_int16,
    c_void_p,
    create_string_buffer,
)
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)

# Speex 1.2 ctl request ids (even = set). See include/speex/speex.h.
_SPEEX_GET_FRAME_SIZE = 3
_SPEEX_SET_QUALITY = 4
_SPEEX_SET_VBR = 12
_SPEEX_SET_VBR_QUALITY = 14

# Steady-state narrowband Speex (8000 Hz, 20 ms) silence payload captured from
# libspeex via Ogg (no RTP header). Used for uplink keepalive so the door opens
# downlink audio without depending on a live ffmpeg RTP muxer.
SPEEX_8000_SILENCE_FRAME = bytes.fromhex(
    "2e9d1b9a2002017fffffffffff8100bfffffffffffc0805fffffffffffe0402ffffffffffff7"
)


def speex_silence_frame(clock_rate: int) -> bytes | None:
    """Return a canned Speex silence payload for ``clock_rate``, if known."""
    if int(clock_rate) == 8000:
        return SPEEX_8000_SILENCE_FRAME
    return None


def apply_gain_s16(pcm: bytes, gain_db: float) -> bytes:
    """Scale little-endian s16 PCM by ``gain_db`` with hard clip protection."""
    if abs(gain_db) < 1e-6 or not pcm:
        return pcm
    if len(pcm) % 2:
        pcm = pcm[:-1]
    factor = 10 ** (gain_db / 20.0)
    out = bytearray(len(pcm))
    for i in range(0, len(pcm), 2):
        sample = int.from_bytes(pcm[i : i + 2], "little", signed=True)
        scaled = int(sample * factor)
        if scaled > 32767:
            scaled = 32767
        elif scaled < -32768:
            scaled = -32768
        out[i : i + 2] = scaled.to_bytes(2, "little", signed=True)
    return bytes(out)


@dataclass(frozen=True, slots=True)
class TalkbackFormat:
    """PCM format expected by talkback / silence keepalive."""

    sample_rate: int
    channels: int = 1
    frame_ms: int = 20

    @property
    def frame_samples(self) -> int:
        return self.sample_rate * self.frame_ms // 1000

    @property
    def frame_bytes(self) -> int:
        return self.frame_samples * self.channels * 2  # s16le


def talkback_format_for(codec: Any) -> TalkbackFormat:
    """PCM rate/frame size for encoding talkback toward ``codec``."""
    name = str(codec.name).casefold()
    if name == "opus":
        return TalkbackFormat(48000)
    if name == "speex":
        return TalkbackFormat(int(getattr(codec, "clock_rate", 8000) or 8000))
    return TalkbackFormat(8000)


def ffmpeg_has_encoder(ffmpeg_path: str, name: str) -> bool:
    """Return True when ``ffmpeg -encoders`` lists ``name`` (e.g. libspeex)."""
    try:
        out = subprocess.check_output(
            [ffmpeg_path, "-hide_banner", "-encoders"],
            stderr=subprocess.STDOUT,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return name in out


def resolve_ffmpeg(path: str | None = None) -> str | None:
    if path:
        return path
    return shutil.which("ffmpeg")


class OpusEncoder:
    """Thin wrapper around opuslib for 20 ms VoIP frames."""

    def __init__(self, *, sample_rate: int = 48000, channels: int = 1) -> None:
        import opuslib

        self._encoder = opuslib.Encoder(sample_rate, channels, opuslib.APPLICATION_VOIP)
        self.sample_rate = sample_rate
        self.channels = channels
        self.frame_samples = sample_rate // 50

    def encode(self, pcm_s16le: bytes) -> bytes:
        if len(pcm_s16le) != self.frame_samples * self.channels * 2:
            raise ValueError("Opus PCM frame has unexpected length")
        return self._encoder.encode(pcm_s16le, self.frame_samples)

    def encode_silence(self) -> bytes:
        return self.encode(b"\x00" * (self.frame_samples * self.channels * 2))


class _SpeexBits(Structure):
    """libspeex bit-packer state (SpeexBits)."""

    _fields_ = [
        ("chars", c_void_p),
        ("nbBits", c_int),
        ("charPtr", c_int),
        ("bitPtr", c_int),
        ("owner", c_int),
        ("overflow", c_int),
        ("buf_size", c_int),
        ("reserved1", c_int),
        ("reserved2", c_void_p),
    ]


def _is_musl() -> bool:
    """Return True when running under musl (HA Container / Alpine)."""
    for root in (Path("/lib"), Path("/usr/lib"), Path("/lib64"), Path("/")):
        try:
            if any(root.glob("lib/ld-musl-*")) or any(root.glob("ld-musl-*")):
                return True
        except OSError:
            continue
    return False


def _vendor_libspeex_path() -> Path | None:
    """Return the vendored libspeex.so path for this OS/arch/libc, if present."""
    system = platform.system().casefold()
    machine = platform.machine().casefold()
    if system != "linux":
        return None
    if machine in {"x86_64", "amd64"}:
        arch = "x86_64"
    elif machine in {"aarch64", "arm64"}:
        arch = "aarch64"
    else:
        return None
    libc = "musl" if _is_musl() else "gnu"
    base = Path(__file__).resolve().parent / "vendor" / "libspeex" / f"linux_{arch}_{libc}"
    for name in ("libspeex.so.1", "libspeex.so.1.5.2", "libspeex.so"):
        path = base / name
        if path.is_file():
            return path
    return None


_libspeex_cache: ctypes.CDLL | None = None
_libspeex_error: BaseException | None = None


def _load_libspeex() -> ctypes.CDLL:
    """Load libspeex once: vendored path first, then a few direct sonames.

    Prefer the integration's vendored ``.so`` so Home Assistant Alpine does not
    burn seconds in ``ctypes.util.find_library`` / ldconfig probes that never
    find Speex. Cache the handle (or the failure) for the process lifetime.
    """
    global _libspeex_cache, _libspeex_error
    if _libspeex_cache is not None:
        return _libspeex_cache
    if _libspeex_error is not None:
        raise OSError(str(_libspeex_error)) from _libspeex_error

    errors: list[str] = []
    vendored = _vendor_libspeex_path()
    if vendored is not None:
        try:
            _libspeex_cache = ctypes.CDLL(str(vendored))
            return _libspeex_cache
        except OSError as err:
            errors.append(f"{vendored}: {err}")

    # Direct soname opens only — never find_library() (blocks the event loop on
    # Alpine with repeated open('/lib/speex') style probes).
    for candidate in ("libspeex.so.1", "libspeex.so"):
        try:
            _libspeex_cache = ctypes.CDLL(candidate)
            return _libspeex_cache
        except OSError as err:
            errors.append(f"{candidate}: {err}")

    _libspeex_error = OSError(
        "libspeex not available (HA ffmpeg has no Speex; tried vendored + soname): "
        + "; ".join(errors)
    )
    raise _libspeex_error


def reset_libspeex_cache_for_tests() -> None:
    """Clear the process-wide libspeex cache (unit tests only)."""
    global _libspeex_cache, _libspeex_error
    _libspeex_cache = None
    _libspeex_error = None


class SpeexNativeEncoder:
    """In-process narrowband/wideband Speex encoder via ctypes + libspeex.

    Home Assistant's Alpine ffmpeg build does not include ``libspeex``, so mic
    talkback cannot use SpeexFfmpegEncoder there. No maintained PyPI Speex
    codec wheel covers Python 3.14 + musllinux, so we ctypes-bind libspeex and
    ship musl/gnu x86_64+aarch64 shared objects under ``vendor/libspeex/``.
    """

    def __init__(
        self,
        *,
        sample_rate: int = 8000,
        quality: int = 8,
        vbr: bool = True,
    ) -> None:
        if sample_rate not in {8000, 16000}:
            raise ValueError(f"Unsupported Speex sample rate: {sample_rate}")
        self.sample_rate = sample_rate
        self.channels = 1
        self.frame_samples = sample_rate // 50  # 20 ms
        self._lib = _load_libspeex()
        self._bind()
        mode_name = "speex_nb_mode" if sample_rate == 8000 else "speex_wb_mode"
        mode_blob = (c_char * 1).in_dll(self._lib, mode_name)
        self._encoder = self._lib.speex_encoder_init(addressof(mode_blob))
        if not self._encoder:
            raise RuntimeError("speex_encoder_init failed")
        quality_c = c_int(int(quality))
        vbr_c = c_int(1 if vbr else 0)
        vbr_quality_c = c_int(int(quality))
        frame_size = c_int()
        if self._lib.speex_encoder_ctl(self._encoder, _SPEEX_SET_QUALITY, byref(quality_c)):
            raise RuntimeError("SPEEX_SET_QUALITY failed")
        if vbr and self._lib.speex_encoder_ctl(self._encoder, _SPEEX_SET_VBR, byref(vbr_c)):
            raise RuntimeError("SPEEX_SET_VBR failed")
        if vbr:
            self._lib.speex_encoder_ctl(
                self._encoder, _SPEEX_SET_VBR_QUALITY, byref(vbr_quality_c)
            )
        if self._lib.speex_encoder_ctl(
            self._encoder, _SPEEX_GET_FRAME_SIZE, byref(frame_size)
        ):
            raise RuntimeError("SPEEX_GET_FRAME_SIZE failed")
        if frame_size.value != self.frame_samples:
            self.close()
            raise RuntimeError(
                f"Unexpected Speex frame size {frame_size.value} (expected {self.frame_samples})"
            )
        self._bits = _SpeexBits()
        self._lib.speex_bits_init(byref(self._bits))
        self._out = create_string_buffer(200)
        self._closed = False

    def _bind(self) -> None:
        lib = self._lib
        lib.speex_encoder_init.argtypes = [c_void_p]
        lib.speex_encoder_init.restype = c_void_p
        lib.speex_encoder_destroy.argtypes = [c_void_p]
        lib.speex_encoder_destroy.restype = None
        lib.speex_encoder_ctl.argtypes = [c_void_p, c_int, c_void_p]
        lib.speex_encoder_ctl.restype = c_int
        lib.speex_bits_init.argtypes = [POINTER(_SpeexBits)]
        lib.speex_bits_init.restype = None
        lib.speex_bits_destroy.argtypes = [POINTER(_SpeexBits)]
        lib.speex_bits_destroy.restype = None
        lib.speex_bits_reset.argtypes = [POINTER(_SpeexBits)]
        lib.speex_bits_reset.restype = None
        lib.speex_encode_int.argtypes = [c_void_p, POINTER(c_int16), POINTER(_SpeexBits)]
        lib.speex_encode_int.restype = c_int
        lib.speex_bits_write.argtypes = [POINTER(_SpeexBits), c_char_p, c_int]
        lib.speex_bits_write.restype = c_int

    @property
    def alive(self) -> bool:
        return not self._closed and bool(self._encoder)

    def encode(self, pcm_s16le: bytes) -> bytes:
        if not self.alive:
            raise RuntimeError("Speex encoder is closed")
        expected = self.frame_samples * 2
        if len(pcm_s16le) != expected:
            raise ValueError(
                f"Speex PCM frame has unexpected length {len(pcm_s16le)} (expected {expected})"
            )
        samples = (c_int16 * self.frame_samples).from_buffer_copy(pcm_s16le)
        self._lib.speex_bits_reset(byref(self._bits))
        self._lib.speex_encode_int(self._encoder, samples, byref(self._bits))
        nbytes = self._lib.speex_bits_write(byref(self._bits), self._out, len(self._out))
        if nbytes <= 0:
            raise RuntimeError("speex_bits_write returned empty frame")
        return bytes(self._out.raw[:nbytes])

    def encode_silence(self) -> bytes:
        return self.encode(b"\x00" * (self.frame_samples * 2))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if getattr(self, "_bits", None) is not None:
            with contextlib.suppress(Exception):
                self._lib.speex_bits_destroy(byref(self._bits))
        if getattr(self, "_encoder", None):
            with contextlib.suppress(Exception):
                self._lib.speex_encoder_destroy(self._encoder)
        self._encoder = None


class SpeexFfmpegEncoder:
    """Encode 20 ms Speex frames via ffmpeg libspeex.

    ffmpeg writes RTP to a local UDP port; we strip the header and return the
    payload for SRTP. All encodes are serialized on ``_lock``. Silence is
    warmed once and reused so the keepalive loop stays near ptime.
    """

    def __init__(
        self,
        *,
        ffmpeg_path: str,
        sample_rate: int,
        payload_type: int,
    ) -> None:
        self.ffmpeg_path = ffmpeg_path
        self.sample_rate = sample_rate
        self.payload_type = payload_type
        self.frame_samples = sample_rate // 50
        self._process: asyncio.subprocess.Process | None = None
        self._transport: asyncio.DatagramTransport | None = None
        self._protocol: _RtpCollector | None = None
        self._local_port = 0
        self._lock = asyncio.Lock()
        self._silence_payload: bytes | None = None
        self._dead = False

    @property
    def alive(self) -> bool:
        """False after close, pipe failure, or ffmpeg exit."""
        if self._dead or self._process is None:
            return False
        return self._process.returncode is None

    async def start(self) -> None:
        # Probe off the event loop — ``ffmpeg -encoders`` can take hundreds of ms.
        has_libspeex = await asyncio.to_thread(
            ffmpeg_has_encoder, self.ffmpeg_path, "libspeex"
        )
        if not has_libspeex:
            raise RuntimeError("ffmpeg libspeex encoder is not available")
        loop = asyncio.get_running_loop()
        self._protocol = _RtpCollector()
        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: self._protocol,
            local_addr=("127.0.0.1", 0),
        )
        self._local_port = self._transport.get_extra_info("sockname")[1]
        command = [
            self.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-fflags",
            "nobuffer",
            "-flags",
            "low_delay",
            "-f",
            "s16le",
            "-ar",
            str(self.sample_rate),
            "-ac",
            "1",
            "-i",
            "pipe:0",
            "-c:a",
            "libspeex",
            "-ar",
            str(self.sample_rate),
            "-ac",
            "1",
            "-flush_packets",
            "1",
            "-payload_type",
            str(self.payload_type),
            "-f",
            "rtp",
            f"rtp://127.0.0.1:{self._local_port}?pkt_size=1200",
        ]
        try:
            self._process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            silence = await self._encode_unlocked(
                b"\x00" * (self.frame_samples * 2), timeout=1.0
            )
        except Exception:
            await self.close()
            raise
        if silence:
            self._silence_payload = silence
        elif not self.alive:
            await self.close()
            raise RuntimeError("Speex ffmpeg encoder exited during silence warm-up")
        else:
            _LOGGER.warning(
                "Speex ffmpeg encoder started but silence warm-up timed out; "
                "keepalive may gap until the first successful encode"
            )

    async def encode(self, pcm_s16le: bytes) -> bytes | None:
        async with self._lock:
            return await self._encode_unlocked(pcm_s16le, timeout=0.08)

    async def encode_silence(self) -> bytes | None:
        if self._silence_payload is not None:
            return self._silence_payload
        async with self._lock:
            if self._silence_payload is not None:
                return self._silence_payload
            payload = await self._encode_unlocked(
                b"\x00" * (self.frame_samples * 2), timeout=0.08
            )
            if payload:
                self._silence_payload = payload
            return payload

    def _mark_dead(self) -> None:
        """Stop treating this encoder as usable; process cleanup happens in close()."""
        self._dead = True

    async def _encode_unlocked(self, pcm_s16le: bytes, *, timeout: float) -> bytes | None:
        if not self.alive or not self._process or not self._process.stdin or not self._protocol:
            return None
        generation = self._protocol.clear()
        try:
            self._process.stdin.write(pcm_s16le)
            await self._process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError):
            self._mark_dead()
            return None
        return await self._protocol.wait_payload(timeout=timeout, generation=generation)

    async def close(self) -> None:
        # Serialize against in-flight encode/drain so teardown cannot tear stdin
        # out from under a concurrent ``_encode_unlocked``.
        async with self._lock:
            self._dead = True
            process, self._process = self._process, None
            self._silence_payload = None
            if process and process.stdin:
                with contextlib.suppress(Exception):
                    process.stdin.close()
            if process and process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()
                with contextlib.suppress(TimeoutError, ProcessLookupError):
                    await asyncio.wait_for(process.wait(), timeout=1)
                if process.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        process.kill()
                    with contextlib.suppress(TimeoutError, ProcessLookupError):
                        await asyncio.wait_for(process.wait(), timeout=1)
            if process and process.stderr:
                with contextlib.suppress(Exception):
                    await process.stderr.read()
            if self._transport:
                self._transport.close()
                self._transport = None


class _RtpCollector(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self._payload: bytes | None = None
        self._event = asyncio.Event()
        self._generation = 0

    def clear(self) -> int:
        """Start a new wait generation; ignore late packets from prior encodes."""
        self._generation += 1
        self._payload = None
        self._event = asyncio.Event()
        return self._generation

    def datagram_received(self, data: bytes, addr) -> None:  # noqa: ANN001
        if len(data) < 12:
            return
        self._payload = data[12:]
        self._event.set()

    async def wait_payload(self, *, timeout: float, generation: int) -> bytes | None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._event.wait(), timeout=timeout)
        if generation != self._generation:
            return None
        return self._payload

"""Pure-Python SIP media orchestration backed by Home Assistant FFmpeg."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import socket
import struct
import time
from collections.abc import Awaitable, Callable
from fractions import Fraction
from pathlib import Path
from typing import Any

import av
import pylibsrtp
from aiortc import MediaStreamTrack
from aiortc.rtp import RtpPacket

from .sdp import (
    Codec,
    NegotiatedSession,
    build_monitoring_offer,
    build_receive_sdp,
    parse_answer,
)
from .sip import SipClient

_LOGGER = logging.getLogger(__name__)
EventCallback = Callable[[dict[str, Any]], Awaitable[None]]
STUN_SERVER = ("stun.linphone.org", 3478)
STUN_COOKIE = 0x2112A442
STUN_BINDING_REQUEST = 0x0001
STUN_BINDING_SUCCESS = 0x0101
STUN_XOR_MAPPED_ADDRESS = 0x0020


def _stun_binding_request() -> bytes:
    return struct.pack("!HHI12s", STUN_BINDING_REQUEST, 0, STUN_COOKIE, secrets.token_bytes(12))


def _is_stun(data: bytes) -> bool:
    if len(data) < 20:
        return False
    _message_type, _length, cookie = struct.unpack("!HHI", data[:8])
    return cookie == STUN_COOKIE and (data[0] & 0xC0) == 0


def _stun_binding_success(request: bytes, addr: tuple[str, int]) -> bytes | None:
    if len(request) < 20:
        return None
    host = addr[0]
    if host.startswith("::ffff:"):
        host = host[7:]
    try:
        packed_ip = socket.inet_aton(host)
    except OSError:
        return None
    _message_type, _length, _cookie, transaction = struct.unpack("!HHI12s", request[:20])
    xor_port = addr[1] ^ (STUN_COOKIE >> 16)
    xor_address = struct.unpack("!I", packed_ip)[0] ^ STUN_COOKIE
    value = struct.pack("!BBHI", 0, 0x01, xor_port, xor_address)
    attribute = struct.pack("!HH", STUN_XOR_MAPPED_ADDRESS, len(value)) + value
    return struct.pack("!HHI12s", STUN_BINDING_SUCCESS, len(attribute), STUN_COOKIE, transaction) + attribute


def _reply_stun_binding(
    transport: asyncio.DatagramTransport | None, data: bytes, addr: tuple[str, int]
) -> bool:
    if not _is_stun(data):
        return False
    if struct.unpack("!H", data[:2])[0] == STUN_BINDING_REQUEST and transport is not None:
        response = _stun_binding_success(data, addr)
        if response is not None:
            transport.sendto(response, addr)
    return True

class MediaRuntimeError(RuntimeError):
    """The monitoring media pipeline failed safely."""


class AudioReceiveTrack(MediaStreamTrack):
    """Bounded decoded audio source shared with WebRTC viewers."""

    kind = "audio"

    def __init__(self) -> None:
        super().__init__()
        self._queue: asyncio.Queue[av.AudioFrame | None] = asyncio.Queue(maxsize=2)

    async def recv(self) -> av.AudioFrame:
        frame = await self._queue.get()
        if frame is None:
            raise asyncio.CancelledError
        return frame

    def put(self, frame: av.AudioFrame) -> None:
        while self._queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
        self._queue.put_nowait(frame)

    def close(self) -> None:
        self.stop()
        while self._queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(None)


class AudioRtpProtocol(asyncio.DatagramProtocol):
    """Receive and send one negotiated G.711 SRTP stream on the same port."""

    def __init__(self, media_received: Callable[[], None]) -> None:
        self.media_received = media_received
        self.transport: asyncio.DatagramTransport | None = None
        self.track = AudioReceiveTrack()
        self._inbound: pylibsrtp.Session | None = None
        self._outbound: pylibsrtp.Session | None = None
        self._decoder: av.AudioCodecContext | None = None
        self._encoder: av.AudioCodecContext | None = None
        self._codec: Codec | None = None
        self._remote: tuple[str, int] | None = None
        self._microphone_enabled = False
        self._pcm = bytearray()
        self._sequence = secrets.randbelow(65536)
        self._timestamp = secrets.randbelow(2**32)
        self._ssrc = secrets.randbelow(2**32)
        self._first_packet = True

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def configure(self, session: NegotiatedSession) -> None:
        media = session.audio
        if media is None or media.crypto is None:
            return
        codec = media.codec({"pcma", "pcmu"})
        if codec is None:
            raise MediaRuntimeError("No supported negotiated microphone codec")
        decoder_name = "pcm_alaw" if codec.name.casefold() == "pcma" else "pcm_mulaw"
        self._decoder = av.CodecContext.create(decoder_name, "r")
        self._decoder.sample_rate = codec.clock_rate
        self._decoder.layout = "mono"
        self._decoder.format = "s16"
        self._encoder = av.CodecContext.create(decoder_name, "w")
        self._encoder.sample_rate = codec.clock_rate
        self._encoder.layout = "mono"
        self._encoder.format = "s16"
        self._codec = codec
        self._remote = (media.connection, media.port)
        inbound_policy = pylibsrtp.Policy(
            key=media.crypto.key,
            ssrc_type=pylibsrtp.Policy.SSRC_ANY_INBOUND,
            srtp_profile=pylibsrtp.Policy.SRTP_PROFILE_AES128_CM_SHA1_80,
        )
        outbound_policy = pylibsrtp.Policy(
            key=session.offer.audio_crypto.key,
            ssrc_type=pylibsrtp.Policy.SSRC_ANY_OUTBOUND,
            srtp_profile=pylibsrtp.Policy.SRTP_PROFILE_AES128_CM_SHA1_80,
        )
        self._inbound = pylibsrtp.Session(inbound_policy)
        self._outbound = pylibsrtp.Session(outbound_policy)

    def prime_remote(self) -> None:
        """Open a symmetric-NAT path with a STUN Binding Request, not a dummy byte."""
        if self.transport is not None and self._remote is not None:
            self.transport.sendto(_stun_binding_request(), self._remote)

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if _reply_stun_binding(self.transport, data, addr):
            return
        if self._inbound is None or self._decoder is None or self._codec is None:
            return
        try:
            plain = self._inbound.unprotect(data)
            packet = RtpPacket.parse(plain)
            frames = self._decoder.decode(av.Packet(packet.payload))
        except (ValueError, pylibsrtp.Error, av.FFmpegError):
            return
        for frame in frames:
            frame.sample_rate = self._codec.clock_rate
            frame.pts = packet.timestamp
            frame.time_base = Fraction(1, self._codec.clock_rate)
            self.track.put(frame)
        if frames:
            self.media_received()

    def set_microphone(self, enabled: bool) -> None:
        self._microphone_enabled = enabled
        if not enabled:
            self._pcm.clear()

    def send_pcm(self, pcm: bytes) -> None:
        if not self._microphone_enabled:
            return
        self._transmit_pcm(pcm)

    def send_silence(self) -> None:
        """Send one 20 ms G.711 silence frame without enabling the microphone."""
        if self._microphone_enabled or self._codec is None:
            return
        try:
            self._transmit_pcm(bytes(self._codec.clock_rate // 50 * 2))
        except (ValueError, pylibsrtp.Error, av.FFmpegError):
            return

    def _transmit_pcm(self, pcm: bytes) -> None:
        if (
            self._codec is None
            or self._encoder is None
            or self._outbound is None
            or self._remote is None
            or self.transport is None
        ):
            return
        self._pcm.extend(pcm)
        packet_bytes = self._codec.clock_rate // 50 * 2  # 20 ms of mono s16
        while len(self._pcm) >= packet_bytes:
            chunk = bytes(self._pcm[:packet_bytes])
            del self._pcm[:packet_bytes]
            samples = len(chunk) // 2
            frame = av.AudioFrame(format="s16", layout="mono", samples=samples)
            frame.sample_rate = self._codec.clock_rate
            frame.planes[0].update(chunk)
            try:
                encoded = self._encoder.encode(frame)
                if not encoded:
                    continue
                payload = b"".join(bytes(item) for item in encoded)
                packet = RtpPacket(
                    payload_type=self._codec.payload_type,
                    marker=1 if self._first_packet else 0,
                    sequence_number=self._sequence,
                    timestamp=self._timestamp,
                    ssrc=self._ssrc,
                    payload=payload,
                )
                protected = self._outbound.protect(packet.serialize())
            except (ValueError, pylibsrtp.Error, av.FFmpegError):
                self.set_microphone(False)
                raise
            self.transport.sendto(protected, self._remote)
            self._first_packet = False
            self._sequence = (self._sequence + 1) & 0xFFFF
            self._timestamp = (self._timestamp + samples) & 0xFFFFFFFF

    def close(self) -> None:
        self.set_microphone(False)
        self.track.close()
        if self.transport:
            self.transport.close()
            self.transport = None


class _StunResponderProtocol(asyncio.DatagramProtocol):
    """Answer relay STUN Binding Requests on an otherwise unused RTCP port."""

    def __init__(self) -> None:
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        _reply_stun_binding(self.transport, data, addr)


class MediaRuntime:
    """Own one FFmpeg video receiver and one Python G.711 SRTP transport."""

    def __init__(
        self,
        sip: SipClient,
        ffmpeg_binary: str,
        event_callback: EventCallback,
        *,
        allocate_generation: Callable[[], int] | None = None,
    ) -> None:
        self.sip = sip
        self.ffmpeg_binary = ffmpeg_binary
        self.event_callback = event_callback
        self.process: asyncio.subprocess.Process | None = None
        self.audio: AudioRtpProtocol | None = None
        self.audio_track: AudioReceiveTrack | None = None
        self.microphone_enabled = False
        self.negotiated: NegotiatedSession | None = None
        self._rtcp_transport: asyncio.DatagramTransport | None = None
        self._monitor: asyncio.Task[None] | None = None
        self._primer: asyncio.Task[None] | None = None
        self._keepalive: asyncio.Task[None] | None = None
        self._stderr: asyncio.Task[None] | None = None
        self._ffmpeg_errors: list[str] = []
        self._first_video = asyncio.Event()
        self._last_video = 0.0
        self._last_audio = 0.0
        self._progress_out_us = -1
        self._ending = False
        self._media_path: Path | None = None
        self._sdp_path: Path | None = None
        # Generation IDs must outlive this MediaRuntime instance: the camera
        # WebRTCBridge persists across SIP reconnect while runtimes are replaced.
        # Prefer manager-owned allocate_generation; local counter is test-only fallback.
        self._allocate_generation = allocate_generation
        self._local_generation_seq = 0
        self.media_generation = 0
        self.sip.on_call_end = self.remote_ended

    def _next_media_generation(self) -> int:
        if self._allocate_generation is not None:
            return self._allocate_generation()
        self._local_generation_seq += 1
        return self._local_generation_seq

    async def start_monitoring(
        self,
        device_address: str,
        _domain: str,
        media_path: Path,
        _snapshot_path: Path,
    ) -> None:
        if self.process or self.negotiated:
            raise MediaRuntimeError("A media session is already active")
        self._ending = False
        self.microphone_enabled = False
        audio_sockets, audio_port = await asyncio.to_thread(_reserve_even_pair)
        video_sockets, video_port = await asyncio.to_thread(_reserve_even_pair)
        try:
            mappings = await asyncio.to_thread(
                _discover_media_mappings, audio_sockets + video_sockets
            )
        except Exception:
            for reserved in audio_sockets + video_sockets:
                reserved.close()
            raise
        public_addresses = {item[0] for item in mappings}
        if len(public_addresses) != 1:
            for reserved in audio_sockets + video_sockets:
                reserved.close()
            raise MediaRuntimeError("STUN returned inconsistent public media addresses")
        address = public_addresses.pop()
        _audio_transport, audio_protocol, rtcp_transport = await _bind_audio_pair(
            self._audio_received, audio_sockets
        )
        audio_sockets = ()
        self.audio = audio_protocol
        self.audio_track = self.audio.track
        self._rtcp_transport = rtcp_transport
        offer = build_monitoring_offer(
            address=address,
            audio_port=audio_port,
            video_port=video_port,
            device_address=device_address,
            advertised_audio_port=mappings[0][1],
            advertised_audio_rtcp_port=mappings[1][1],
            advertised_video_port=mappings[2][1],
            advertised_video_rtcp_port=mappings[3][1],
        )
        relay_sockets: tuple[socket.socket, ...] = ()
        try:
            answer = await self.sip.start_monitoring(offer.sdp)
            session = parse_answer(answer, offer)
            self.negotiated = session
            self.audio.configure(session)
            self.audio.prime_remote()
            if session.audio:
                rtcp_transport.sendto(
                    _stun_binding_request(), (session.audio.connection, session.audio.port + 1)
                )
            if session.video:
                video_sockets[0].sendto(
                    _stun_binding_request(), (session.video.connection, session.video.port)
                )
                video_sockets[1].sendto(
                    _stun_binding_request(), (session.video.connection, session.video.port + 1)
                )
            receive_sdp = build_receive_sdp(session, include_audio=False)
            sdp_path = media_path.with_name("receive-srtp.sdp")
            self._media_path = media_path
            self._sdp_path = sdp_path
            await asyncio.to_thread(_write_private, sdp_path, receive_sdp)
            for reserved in video_sockets:
                reserved.close()
            video_sockets = ()
            relay_sockets, relay_port = await asyncio.to_thread(_reserve_loopback_pair)
            await asyncio.to_thread(_write_private, media_path, _playback_sdp(relay_port))
            for reserved in relay_sockets:
                reserved.close()
            relay_sockets = ()
            self.media_generation = self._next_media_generation()
            command = [
                self.ffmpeg_binary,
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-protocol_whitelist",
                "file,udp,rtp,srtp",
                "-fflags",
                "nobuffer",
                "-flags",
                "low_delay",
                "-analyzeduration",
                "0",
                "-probesize",
                "32",
                "-f",
                "sdp",
                "-i",
                str(sdp_path),
                "-map",
                "0:v:0",
                "-c:v",
                "copy",
                "-payload_type",
                "96",
                "-f",
                "rtp",
                "-progress",
                "pipe:1",
                "-y",
                f"rtp://127.0.0.1:{relay_port}?rtcpport={relay_port + 1}&pkt_size=1200",
            ]
            self.process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self._ffmpeg_errors = []
            self._stderr = asyncio.create_task(self._read_ffmpeg_output())
            self._monitor = asyncio.create_task(self._watchdog())
            self._primer = asyncio.create_task(self._prime_relay())
            self._keepalive = asyncio.create_task(self._audio_keepalive())
            await self.event_callback(
                {
                    "event": "media_format",
                    "video": "H264",
                    "audio": session.audio.codec({"pcma", "pcmu"}).name
                    if session.audio and session.audio.codec({"pcma", "pcmu"})
                    else None,
                }
            )
        except Exception:
            for reserved in video_sockets:
                reserved.close()
            for reserved in relay_sockets:
                reserved.close()
            await self._teardown(send_bye=True)
            raise

    async def end_session(self) -> None:
        await self._teardown(send_bye=True)

    async def close(self) -> None:
        await self._teardown(send_bye=True)

    async def remote_ended(self) -> None:
        """Tear media down after a remote BYE without sending another BYE."""
        await self._teardown(send_bye=False)

    async def set_microphone(self, enabled: bool) -> None:
        if enabled and (
            self.negotiated is None
            or self.negotiated.audio is None
            or self.audio is None
        ):
            raise MediaRuntimeError("Microphone requires an active negotiated call")
        if (
            enabled
            and self.negotiated
            and self.negotiated.audio
            and self.negotiated.audio.direction not in {"sendrecv", "recvonly"}
        ):
            raise MediaRuntimeError("The external unit did not negotiate microphone reception")
        self.microphone_enabled = enabled
        if self.audio:
            self.audio.set_microphone(enabled)

    async def send_microphone_frame(self, pcm: bytes) -> None:
        if self.microphone_enabled and self.audio:
            self.audio.send_pcm(pcm)

    def _audio_received(self) -> None:
        """Inbound G.711 RTP — must not alone satisfy video readiness/watchdog."""
        if not self._last_audio:
            _LOGGER.warning("BTicino inbound audio RTP received")
        self._last_audio = time.monotonic()

    def _video_progress(self, out_time_us: int) -> None:
        """Accept only advancing FFmpeg out_time (zero/N/A must not mark ready)."""
        if out_time_us <= 0 or out_time_us <= self._progress_out_us:
            return
        self._progress_out_us = out_time_us
        self._last_video = time.monotonic()
        self._first_video.set()

    def ingest_ffmpeg_progress_line(self, line: str) -> None:
        """Parse one FFmpeg ``-progress`` line (testable without a live process)."""
        value_us = parse_ffmpeg_progress_out_time_us(line)
        if value_us is not None:
            self._video_progress(value_us)

    async def _read_ffmpeg_output(self) -> None:
        """Track progress and retain bounded, sanitized FFmpeg diagnostics."""
        assert self.process and self.process.stdout and self.process.stderr

        async def _progress() -> None:
            while line := await self.process.stdout.readline():
                self.ingest_ffmpeg_progress_line(line.decode(errors="replace").strip())

        async def _errors() -> None:
            while line := await self.process.stderr.readline():
                clean = _sanitize_ffmpeg_error(line.decode(errors="replace").strip())
                if clean:
                    self._ffmpeg_errors = [*self._ffmpeg_errors, clean][-4:]

        await asyncio.gather(_progress(), _errors())

    async def _watchdog(self) -> None:
        try:
            await asyncio.wait_for(self._first_video.wait(), timeout=15)
            await self.event_callback({"event": "first_frame"})
            await self.event_callback(
                {"event": "call_state", "state": "streams_running"}
            )
            while True:
                await asyncio.sleep(1)
                if self.process and self.process.returncode is not None:
                    raise MediaRuntimeError("FFmpeg media process exited")
                now = time.monotonic()
                # Video stall is fatal even when audio RTP is still flowing.
                if self._last_video and now - self._last_video > 10:
                    raise MediaRuntimeError("Video media timeout")
        except asyncio.CancelledError:
            raise
        except Exception as err:
            await self.event_callback(
                {
                    "event": "error",
                    "code": _safe_error_code(err),
                    "detail": " | ".join(self._ffmpeg_errors) or None,
                }
            )
            await self._teardown(send_bye=True, from_monitor=True)

    async def _prime_relay(self) -> None:
        """Resend STUN Binding Requests until the first video frame arrives."""
        try:
            while not self._first_video.is_set() and not self._ending:
                if self.audio:
                    self.audio.prime_remote()
                session = self.negotiated
                if session and session.audio and self._rtcp_transport:
                    self._rtcp_transport.sendto(
                        _stun_binding_request(),
                        (session.audio.connection, session.audio.port + 1),
                    )
                try:
                    await asyncio.wait_for(self._first_video.wait(), timeout=1.5)
                except TimeoutError:
                    continue
        except asyncio.CancelledError:
            raise

    async def _audio_keepalive(self) -> None:
        """Send view-only G.711 silence so the relay latches return audio, like Door Entry."""
        try:
            while not self._ending:
                session = self.negotiated
                if (
                    not self.microphone_enabled
                    and self.audio
                    and session
                    and session.audio
                    and session.audio.direction in {"sendrecv", "recvonly"}
                ):
                    self.audio.send_silence()
                await asyncio.sleep(0.02)
        except asyncio.CancelledError:
            raise

    async def _teardown(self, *, send_bye: bool, from_monitor: bool = False) -> None:
        if self._ending:
            return
        self._ending = True
        try:
            self.microphone_enabled = False
            if self.audio:
                self.audio.set_microphone(False)
            keepalive, self._keepalive = self._keepalive, None
            if keepalive:
                keepalive.cancel()
                await asyncio.gather(keepalive, return_exceptions=True)
            primer, self._primer = self._primer, None
            if primer:
                primer.cancel()
                await asyncio.gather(primer, return_exceptions=True)
            monitor, self._monitor = self._monitor, None
            if monitor and not from_monitor:
                monitor.cancel()
                await asyncio.gather(monitor, return_exceptions=True)
            stderr, self._stderr = self._stderr, None
            if stderr:
                stderr.cancel()
                await asyncio.gather(stderr, return_exceptions=True)
            process, self.process = self.process, None
            if process and process.returncode is None:
                process.terminate()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(process.wait(), timeout=1)
                if process.returncode is None:
                    process.kill()
                    await process.wait()
            if self.audio:
                self.audio.close()
                self.audio = None
                self.audio_track = None
            if self._rtcp_transport:
                self._rtcp_transport.close()
                self._rtcp_transport = None
            if send_bye:
                with contextlib.suppress(Exception):
                    await self.sip.end_monitoring()
            self.negotiated = None
            for path in (self._sdp_path, self._media_path):
                if path:
                    with contextlib.suppress(OSError):
                        path.unlink()
            self._sdp_path = None
            self._media_path = None
            self._first_video = asyncio.Event()
            self._last_video = 0.0
            self._last_audio = 0.0
            self._progress_out_us = -1
            await self.event_callback({"event": "call_state", "state": "ended"})
        finally:
            self._ending = False


def parse_ffmpeg_progress_out_time_us(line: str) -> int | None:
    """Return out_time in microseconds from one FFmpeg progress line.

    FFmpeg's ``out_time_ms`` key is a *legacy alias that is still microseconds*,
    not milliseconds. Never multiply it by 1000.
    Prefer ``out_time_us`` when both appear with the same numeric value.
    """
    if line.startswith("out_time_us="):
        try:
            return int(line.split("=", 1)[1])
        except ValueError:
            return None
    if line.startswith("out_time_ms="):
        try:
            # Legacy name; unit is microseconds (FFmpeg progress docs / observed).
            return int(line.split("=", 1)[1])
        except ValueError:
            return None
    return None


def _safe_error_code(error: Exception) -> str:
    """Map exceptions to allowlisted diagnostic codes (never raw secret-bearing text)."""
    message = str(error)
    if isinstance(error, MediaRuntimeError) and "FFmpeg" in message:
        return "ffmpeg_exit"
    if isinstance(error, MediaRuntimeError) and "Video media timeout" in message:
        return "video_timeout"
    if isinstance(error, MediaRuntimeError) and "Media timeout" in message:
        return "media_timeout"
    if isinstance(error, MediaRuntimeError) and "STUN" in message:
        return "stun_failed"
    if isinstance(error, MediaRuntimeError) and "already active" in message:
        return "session_busy"
    if isinstance(error, MediaRuntimeError) and "microphone" in message.casefold():
        return "microphone_unavailable"
    if isinstance(error, MediaRuntimeError):
        return "media_error"
    if isinstance(error, TimeoutError):
        return "setup_timeout"
    # Type name only — do not append str(error) bodies into entity state.
    return type(error).__name__


def safe_error_code_for_exception(error: Exception) -> str:
    """Public allowlisted code for session/manager entity attributes."""
    return _safe_error_code(error)


def _sanitize_ffmpeg_error(value: str) -> str:
    """Keep useful decoder errors without exposing local storage paths."""
    if not value:
        return ""
    value = value.replace("receive-srtp.sdp", "<receive-sdp>")
    if len(value) > 300:
        value = value[:297] + "..."
    return value


def _discover_media_mappings(
    sockets: tuple[socket.socket, ...],
) -> tuple[tuple[str, int], ...]:
    """Discover the public UDP endpoint for every RTP/RTCP socket using STUN."""
    return tuple(_stun_mapping(item) for item in sockets)


def _stun_mapping(media_socket: socket.socket) -> tuple[str, int]:
    candidates = socket.getaddrinfo(
        STUN_SERVER[0], STUN_SERVER[1], socket.AF_INET, socket.SOCK_DGRAM
    )
    transaction = secrets.token_bytes(12)
    request = struct.pack("!HHI12s", 0x0001, 0, STUN_COOKIE, transaction)
    previous_timeout = media_socket.gettimeout()
    try:
        media_socket.settimeout(3)
        media_socket.sendto(request, candidates[0][4])
        response, _source = media_socket.recvfrom(2048)
    except (OSError, TimeoutError) as err:
        raise MediaRuntimeError("STUN media discovery failed") from err
    finally:
        media_socket.settimeout(previous_timeout)
    if len(response) < 20:
        raise MediaRuntimeError("STUN returned a truncated response")
    message_type, length, cookie, response_transaction = struct.unpack(
        "!HHI12s", response[:20]
    )
    if (
        message_type != 0x0101
        or cookie != STUN_COOKIE
        or response_transaction != transaction
        or len(response) < 20 + length
    ):
        raise MediaRuntimeError("STUN returned an invalid response")
    offset = 20
    while offset + 4 <= 20 + length:
        attribute_type, attribute_length = struct.unpack("!HH", response[offset : offset + 4])
        value = response[offset + 4 : offset + 4 + attribute_length]
        if attribute_type == 0x0020 and len(value) >= 8 and value[1] == 0x01:
            xor_port = struct.unpack("!H", value[2:4])[0]
            xor_address = struct.unpack("!I", value[4:8])[0]
            port = xor_port ^ (STUN_COOKIE >> 16)
            address = socket.inet_ntoa(struct.pack("!I", xor_address ^ STUN_COOKIE))
            return address, port
        offset += 4 + ((attribute_length + 3) & ~3)
    raise MediaRuntimeError("STUN response has no IPv4 mapped address")


def _reserve_even_pair() -> tuple[tuple[socket.socket, socket.socket], int]:
    for _ in range(100):
        port = secrets.randbelow(10000) * 2 + 40000
        first = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        second = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            first.bind(("0.0.0.0", port))
            second.bind(("0.0.0.0", port + 1))
            return (first, second), port
        except OSError:
            first.close()
            second.close()
    raise MediaRuntimeError("No RTP port pair is available")


async def _bind_audio_pair(
    media_received: Callable[[], None],
    sockets: tuple[socket.socket, socket.socket],
) -> tuple[
    asyncio.DatagramTransport,
    AudioRtpProtocol,
    asyncio.DatagramTransport,
]:
    loop = asyncio.get_running_loop()
    rtp: asyncio.DatagramTransport | None = None
    try:
        rtp, protocol = await loop.create_datagram_endpoint(
            lambda: AudioRtpProtocol(media_received), sock=sockets[0]
        )
        rtcp, _ = await loop.create_datagram_endpoint(_StunResponderProtocol, sock=sockets[1])
        return rtp, protocol, rtcp
    except OSError as err:
        if rtp:
            rtp.close()
        for reserved in sockets:
            with contextlib.suppress(OSError):
                reserved.close()
        raise MediaRuntimeError("Audio RTP socket activation failed") from err


def _write_private(path: Path, value: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(value)
    path.chmod(0o600)


def _reserve_loopback_pair() -> tuple[tuple[socket.socket, socket.socket], int]:
    for _ in range(100):
        port = secrets.randbelow(10000) * 2 + 40000
        first = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        second = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            first.bind(("127.0.0.1", port))
            second.bind(("127.0.0.1", port + 1))
            return (first, second), port
        except OSError:
            first.close()
            second.close()
    raise MediaRuntimeError("No loopback RTP port pair is available")


def _playback_sdp(port: int) -> str:
    return "\r\n".join(
        (
            "v=0",
            "o=- 0 0 IN IP4 127.0.0.1",
            "s=BTicino Classe 100X local relay",
            "c=IN IP4 127.0.0.1",
            "t=0 0",
            f"m=video {port} RTP/AVP 96",
            "a=rtpmap:96 H264/90000",
            "a=fmtp:96 packetization-mode=1;profile-level-id=42e01f",
            "a=recvonly",
            "",
        )
    )

# -----------------------------------------------------------------------------
# Role: Bridges inbound OVH/Asterisk calls into BloxSmith events and audio.
# File Name: block.py
# Author: HackInvent
# Created Date: 2026-09-18
# -----------------------------------------------------------------------------
# Functional behavior:
# FB1 - Normalize inbound Asterisk/OVH StasisStart and StasisEnd events.
# FB2 - Publish call audio plus correlated start/stop commands for audio consumers.
# FB3 - Validate bounded settings and fixed ports before network/media effects.
# FB4 - Resolve the ARI password only as a vault reference in Active Runtime.
# FB5 - Attach and release Asterisk external media, bridges and RTP transports.
# FB6 - Skip unsupported live telephony cleanly in centralized simulation.
# FB7 - Render block-owned modal, inspector and compact node-card surfaces.
# FB8 - Support concurrent bounded calls and cooperative listener shutdown.
# FB9 - Survive ARI disconnections, per-call failures and missed end events.
# FB10 - Play graph audio to the caller on the same RTP leg, newest stream first.
# FB11 - Accept only the configured caller numbers when a list is set.
# -----------------------------------------------------------------------------

from __future__ import annotations

import asyncio
import base64
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import escape
import json
import random
import re
import shutil
import time
from urllib.parse import quote
from typing import Any

from bloxsmith_app.block_api import (
    APPLICATION_JSON,
    BlockDefinition,
    BlockRuntimeContext,
    BlockRuntimeListenerContext,
    BlockRuntimeOutput,
    BlockRuntimePreparation,
    BlockRuntimePreparationContext,
    BlockRuntimeResult,
    TEXT_PLAIN,
    render_inspector_template,
    render_node_card_template,
)


DEFAULTS = {
    "ari_base_url": "http://127.0.0.1:8088",
    "ari_username": "bloxsmith",
    "ari_password_ref": "",
    "ari_app": "bloxsmith",
    "expected_context": "",
    "expected_extension": "",
    "allowed_callers": "",
    "auto_answer": True,
    "capture_audio": True,
    "media_host": "127.0.0.1",
    "media_port": 0,
    "ffmpeg_chunk_ms": 100,
    "max_calls": 1,
}
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_CALLER = re.compile(r"^\+?\d{3,31}$")
# A caller list stays a readable setting: bound what one line can hold.
MAX_ALLOWED_CALLERS = 64
MAX_ALLOWED_CALLERS_CHARS = 2048
# Digits needed before a shorter national number may match a longer international one.
CALLER_SUFFIX_DIGITS = 9
# A gateway runs for days: losing the ARI socket is an incident to recover from,
# not a reason to stop answering the telephone line.
RECONNECT_MIN_DELAY = 1.0
RECONNECT_MAX_DELAY = 30.0
SESSION_AUDIT_INTERVAL = 15.0
# External media channels have no SDP negotiation, so Asterisk cannot learn that a
# dynamic payload type means slin16 on the way in: it drops those frames (ASTERISK-28751).
# The leg therefore uses slin, whose payload type 11 is static and accepted in both
# directions. Nothing is lost on a telephone call: the trunk itself is 8 kHz.
MEDIA_FORMAT = "slin"
MEDIA_SAMPLE_RATE_HZ = 8000
MEDIA_PAYLOAD_TYPE = 11
# 20 ms of mono 8 kHz signed 16-bit audio.
MEDIA_FRAME_BYTES = 320
PLAYBACK_FRAME_BYTES = MEDIA_FRAME_BYTES
# Start playing once a small cushion exists, so producer jitter is not audible.
PLAYBACK_PREBUFFER_FRAMES = 3
# A forgotten link, or audio arriving with no call, must not grow without bound.
PLAYBACK_MAX_BUFFERED_BYTES = 320000


class TelephonyError(RuntimeError):
    """Stable block-owned validation or transport failure."""


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    """Return one bounded integer, accepting only integral numeric text."""

    if isinstance(value, bool):
        raise TelephonyError("Numeric settings must be whole numbers.")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise TelephonyError("Numeric settings must be whole numbers.") from exc
    if not minimum <= parsed <= maximum:
        raise TelephonyError(f"Value out of range: {minimum} to {maximum}.")
    return parsed


def _boolean(value: Any, label: str) -> bool:
    """Require a real boolean so imported string settings cannot become truthy."""

    if type(value) is not bool:
        raise TelephonyError(f"{label} must be a boolean.")
    return value


def _optional_token(value: Any, label: str) -> str:
    """Normalize an optional Asterisk context or extension selector."""

    text = "" if value is None else str(value).strip()
    if not text:
        return ""
    if len(text) > 128 or not _TOKEN.fullmatch(text):
        raise TelephonyError(f"{label} contains unsupported characters.")
    return text


def caller_number(value: Any) -> str:
    """Return one caller number without its formatting, in international form when known.

    Asterisk announces the same line as ``0033612345678`` while a user writes
    ``+33 6 12 34 56 78``. Both collapse to the same comparable value here.

    Args:
        value: Raw caller number from a channel or from the settings.
    """

    text = str(value or "").strip()
    digits = re.sub(r"\D", "", text)
    if digits.startswith("00"):
        return "+" + digits[2:]
    return ("+" if text.startswith("+") else "") + digits


def caller_entries(value: Any) -> list[str]:
    """Split an allowed-caller setting written with commas, semicolons or line breaks."""

    return [entry.strip() for entry in re.split(r"[;,\n]", str(value or "")) if entry.strip()]


def _allowed_callers(value: Any) -> str:
    """Validate the allowed-caller list and return its normalized, comma-joined form.

    Args:
        value: Raw list as typed by the user.

    Raises:
        TelephonyError: When an entry is not a phone number, or the list is oversized.
    """

    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) > MAX_ALLOWED_CALLERS_CHARS:
        raise TelephonyError("The allowed caller list is too long.")
    numbers: list[str] = []
    for entry in caller_entries(text):
        number = caller_number(entry)
        if not _CALLER.fullmatch(number):
            raise TelephonyError(f"Invalid caller number: {entry}")
        if number not in numbers:
            numbers.append(number)
    if len(numbers) > MAX_ALLOWED_CALLERS:
        raise TelephonyError(f"At most {MAX_ALLOWED_CALLERS} allowed callers.")
    return ", ".join(numbers)


def caller_allowed(number: Any, allowed: Any) -> bool:
    """Return whether one caller matches an allowed entry of the configured list.

    A line spelled nationally and internationally differs only by its prefix, so a
    shorter entry matches when it ends the longer one on enough digits for the
    comparison to stay unambiguous. An unknown caller never matches a non-empty list.

    Args:
        number: Caller number announced by Asterisk.
        allowed: Configured list, already normalized by ``config``.
    """

    candidate = caller_number(number).lstrip("+").lstrip("0")
    if not candidate:
        return False
    for entry in caller_entries(allowed):
        target = caller_number(entry).lstrip("+").lstrip("0")
        if not target:
            continue
        if candidate == target:
            return True
        shorter, longer = sorted((candidate, target), key=len)
        if len(shorter) >= CALLER_SUFFIX_DIGITS and longer.endswith(shorter):
            return True
    return False


def config(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate settings before any network, media or graph side effect.

    Args:
        raw: Untrusted persisted node configuration.

    Returns:
        A normalized configuration copy safe to pass to runtime helpers.

    Raises:
        TelephonyError: When a value has an invalid type, range or format.
    """

    source = raw if isinstance(raw, Mapping) else {}
    base = str(source.get("ari_base_url") or DEFAULTS["ari_base_url"]).strip().rstrip("/")
    if not base.startswith(("http://", "https://")) or any(c.isspace() for c in base):
        raise TelephonyError("The ARI URL must be a valid HTTP or HTTPS address.")
    username = str(source.get("ari_username") or DEFAULTS["ari_username"]).strip()
    if not username or len(username) > 128 or any(c in username for c in "\r\n"):
        raise TelephonyError("Invalid ARI user name.")
    password_ref = str(source.get("ari_password_ref") or "").strip()
    if len(password_ref) > 256 or any(c in password_ref for c in "\r\n"):
        raise TelephonyError("Invalid ARI secret reference.")
    app = _optional_token(source.get("ari_app", DEFAULTS["ari_app"]), "The ARI application")
    if not app:
        raise TelephonyError("The ARI application is required.")
    media_host = str(source.get("media_host") or DEFAULTS["media_host"]).strip()
    if not media_host or len(media_host) > 253 or any(c.isspace() for c in media_host):
        raise TelephonyError("Invalid media host.")
    return {
        "ari_base_url": base,
        "ari_username": username,
        "ari_password_ref": password_ref,
        "ari_app": app,
        "expected_context": _optional_token(source.get("expected_context"), "The context"),
        "expected_extension": _optional_token(source.get("expected_extension"), "The extension"),
        "allowed_callers": _allowed_callers(source.get("allowed_callers")),
        "auto_answer": _boolean(source.get("auto_answer", DEFAULTS["auto_answer"]), "auto_answer"),
        "capture_audio": _boolean(source.get("capture_audio", DEFAULTS["capture_audio"]), "capture_audio"),
        "media_host": media_host,
        "media_port": _bounded_int(source.get("media_port", 0), 0, 0, 65535),
        "ffmpeg_chunk_ms": _bounded_int(source.get("ffmpeg_chunk_ms", 100), 100, 20, 1000),
        "max_calls": _bounded_int(source.get("max_calls", 1), 1, 1, 8),
    }


def display_config(raw: Mapping[str, Any] | None) -> tuple[dict[str, Any], str]:
    """Return configuration values for UI rendering plus an optional warning.

    Runtime paths keep the strict ``config`` validation. A surface must stay usable
    even when a stored value is invalid, otherwise the user cannot open the modal to
    correct it. The stored values are therefore returned as they are, so the offending
    one stays visible next to the reported reason.

    Args:
        raw: Untrusted persisted node configuration.

    Returns:
        A pair of displayable values and a validation message, empty when valid.
    """

    try:
        return config(raw), ""
    except TelephonyError as error:
        stored = dict(raw) if isinstance(raw, Mapping) else {}
        values = {key: stored.get(key, default) for key, default in DEFAULTS.items()}
        return values, str(error)


def _secret(context: Any, value_ref: str) -> str:
    """Resolve a wallet reference through the public injected service only."""

    resolver = context.services.get("resolve_secret")
    if not callable(resolver):
        raise TelephonyError("No secret resolver is available in this runtime.")
    try:
        value = resolver(value_ref)
    except Exception as exc:
        raise TelephonyError("The ARI secret is unreachable: unlock the vault.") from exc
    if not isinstance(value, str) or not value.strip() or any(c in value for c in "\r\n"):
        raise TelephonyError("The ARI secret is empty or invalid.")
    return value.strip()


def _event(event_name: str, channel: Mapping[str, Any], **extra: Any) -> dict[str, Any]:
    """Build one stable, provider-neutral call event without secrets."""

    timestamp = channel.get("creationtime") if isinstance(channel.get("creationtime"), str) else None
    return {
        "provider": "asterisk",
        "event": event_name,
        "channel_id": str(channel.get("id") or ""),
        "name": str(channel.get("name") or ""),
        "state": str(channel.get("state") or ""),
        "from": str(channel.get("caller", {}).get("number") or ""),
        "from_name": str(channel.get("caller", {}).get("name") or ""),
        "to": str(channel.get("dialplan", {}).get("exten") or channel.get("extension") or ""),
        "context": str(channel.get("dialplan", {}).get("context") or channel.get("context") or ""),
        "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
        **extra,
    }


def _failure(error: Exception) -> BlockRuntimeResult:
    """Return a redacted runtime failure for non block-owned exceptions."""

    message = str(error) if isinstance(error, TelephonyError) else "Asterisk transport or media stream error."
    return BlockRuntimeResult(status="failed", error=message, last_message=message, content_type=TEXT_PLAIN,
                              metadata={"telephony": {"state": "error"}})


@dataclass
class _AudioEncoder:
    """Encode Asterisk big-endian slin16 RTP payloads into Ogg/Opus chunks."""

    chunk_ms: int
    process: asyncio.subprocess.Process
    sequence: int = 0
    pending: bytearray = field(default_factory=bytearray)
    output_queue: "asyncio.Queue[bytes | None]" = field(default_factory=asyncio.Queue)
    reader_task: "asyncio.Task[None] | None" = None
    chunk_size: int = 320  # Adjusted on construction from the call sample rate.

    def __post_init__(self) -> None:
        self.chunk_size = max(64, int(MEDIA_SAMPLE_RATE_HZ * 2 * self.chunk_ms / 1000))

    @classmethod
    async def create(cls, chunk_ms: int) -> "_AudioEncoder":
        """Start an FFmpeg subprocess configured for mono 48 kHz Opus output."""

        process = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "s16be",
            "-ar", str(MEDIA_SAMPLE_RATE_HZ), "-ac", "1", "-i", "pipe:0", "-c:a", "libopus",
            "-b:a", "32000", "-page_duration", str(chunk_ms * 1000), "-f", "ogg", "pipe:1",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        encoder = cls(chunk_ms=chunk_ms, process=process)
        encoder.reader_task = asyncio.create_task(encoder._read_stdout())
        return encoder

    async def _read_stdout(self) -> None:
        """Continuously drain FFmpeg output so its pipe cannot block encoding."""

        assert self.process.stdout is not None
        while not self.process.stdout.at_eof():
            chunk = await self.process.stdout.read(4096)
            if not chunk:
                break
            await self.output_queue.put(chunk)
        await self.output_queue.put(None)

    async def feed(self, payload: bytes) -> list[bytes]:
        """Feed one RTP payload and return all completed Opus container chunks."""

        if self.process.stdin is None or self.process.stdout is None:
            return []
        self.pending.extend(payload)
        chunks: list[bytes] = []
        while len(self.pending) >= self.chunk_size:
            chunk, self.pending = self.pending[:self.chunk_size], self.pending[self.chunk_size:]
            self.process.stdin.write(chunk)
            await self.process.stdin.drain()
        while not self.output_queue.empty():
            output = self.output_queue.get_nowait()
            if output is not None:
                chunks.append(output)
        return chunks

    async def close(self) -> list[bytes]:
        """Flush and terminate the encoder; never surface FFmpeg stderr content."""

        chunks: list[bytes] = []
        try:
            if self.process.stdin is not None and not self.process.stdin.is_closing():
                self.process.stdin.close()
            while True:
                output = await self.output_queue.get()
                if output is None:
                    break
                chunks.append(output)
            if self.reader_task is not None:
                await self.reader_task
            await self.process.wait()
            if self.process.returncode not in {None, 0}:
                raise TelephonyError("FFmpeg could not encode the call audio.")
        except ProcessLookupError:
            pass
        return chunks


@dataclass
class _PlaybackDecoder:
    """Decode one graph audio stream into the slin16 frames Asterisk expects."""

    process: asyncio.subprocess.Process
    output_queue: "asyncio.Queue[bytes | None]" = field(default_factory=asyncio.Queue)
    reader_task: "asyncio.Task[None] | None" = None

    @classmethod
    async def create(cls, codec: str, sample_rate_hz: int, channels: int) -> "_PlaybackDecoder":
        """Start an FFmpeg process converting one producer format to slin16.

        Args:
            codec: Frame codec announced by the producing block.
            sample_rate_hz: Frame sample rate, used by raw PCM sources only.
            channels: Frame channel count, used by raw PCM sources only.

        Raises:
            TelephonyError: When the announced codec is not supported.
        """

        name = str(codec or "").strip().lower()
        if name == "opus":
            # Producers publish Ogg pages, which carry their own rate and channel count.
            source = ["-f", "ogg"]
        elif name in {"pcm_s16le", "pcm16", "pcm"}:
            source = ["-f", "s16le", "-ar", str(int(sample_rate_hz) or MEDIA_SAMPLE_RATE_HZ),
                      "-ac", str(int(channels) or 1)]
        else:
            raise TelephonyError(f"Unsupported playback audio format: {codec}.")
        process = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", *source, "-i", "pipe:0",
            "-f", "s16be", "-ar", str(MEDIA_SAMPLE_RATE_HZ), "-ac", "1", "pipe:1",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        decoder = cls(process=process)
        decoder.reader_task = asyncio.create_task(decoder._read_stdout())
        return decoder

    async def _read_stdout(self) -> None:
        """Drain decoded audio continuously so FFmpeg is never blocked by its pipe."""

        assert self.process.stdout is not None
        while not self.process.stdout.at_eof():
            chunk = await self.process.stdout.read(4096)
            if not chunk:
                break
            await self.output_queue.put(chunk)
        await self.output_queue.put(None)

    async def feed(self, payload: bytes) -> bytes:
        """Submit one producer frame and return whatever slin16 audio is ready."""

        if self.process.stdin is not None and not self.process.stdin.is_closing():
            self.process.stdin.write(payload)
            await self.process.stdin.drain()
        return self.drain()

    def drain(self) -> bytes:
        """Collect the audio FFmpeg has produced since the last call.

        Decoding lags behind the input, so the tail of an answer is only ready after the
        last frame was submitted. The caller drains on every cycle, not only when a new
        frame arrives, otherwise the end of each sentence stays inside the decoder.
        """

        decoded = bytearray()
        while not self.output_queue.empty():
            chunk = self.output_queue.get_nowait()
            if chunk is not None:
                decoded.extend(chunk)
        return bytes(decoded)

    async def close(self) -> None:
        """Terminate the decoder, discarding whatever has not been played."""

        try:
            if self.process.stdin is not None and not self.process.stdin.is_closing():
                self.process.stdin.close()
            if self.process.returncode is None:
                self.process.kill()
            if self.reader_task is not None:
                self.reader_task.cancel()
            await self.process.wait()
        except (ProcessLookupError, asyncio.CancelledError):
            pass


@dataclass
class _Playback:
    """Hold the audio the graph wants the caller to hear, newest stream first.

    The block exposes one playback port for the node, so a new stream identifier
    replaces the previous one: a barge-in answer must not queue behind the sentence
    it interrupts.
    """

    stream_id: str = ""
    decoder: _PlaybackDecoder | None = None
    buffer: bytearray = field(default_factory=bytearray)
    playing: bool = False
    frames_played: int = 0
    dropped_bytes: int = 0

    async def feed(self, frame: Any) -> None:
        """Decode one received frame, switching stream when the producer changes."""

        stream_id = str(getattr(frame, "stream_id", "") or "")
        if self.decoder is None or stream_id != self.stream_id:
            await self.reset()
            self.stream_id = stream_id
            self.decoder = await _PlaybackDecoder.create(
                getattr(frame, "codec", ""), getattr(frame, "sample_rate_hz", 0),
                getattr(frame, "channels", 1))
        self._store(await self.decoder.feed(bytes(getattr(frame, "payload", b""))))

    def drain(self) -> None:
        """Move whatever the decoder has finished producing into the playable buffer."""

        if self.decoder is not None:
            self._store(self.decoder.drain())

    def _store(self, decoded: bytes) -> None:
        """Append decoded audio, bounded so a forgotten link cannot grow without end."""

        if not decoded:
            return
        room = max(0, PLAYBACK_MAX_BUFFERED_BYTES - len(self.buffer))
        if len(decoded) > room:
            self.dropped_bytes += len(decoded) - room
            decoded = decoded[:room]
        self.buffer.extend(decoded)

    def take(self, size: int) -> bytes | None:
        """Return the next playable frame, or None when the caller should hear silence."""

        if not self.playing and len(self.buffer) >= size * PLAYBACK_PREBUFFER_FRAMES:
            self.playing = True
        if not self.playing:
            return None
        if len(self.buffer) < size:
            self.playing = False
            return None
        frame, self.buffer = bytes(self.buffer[:size]), self.buffer[size:]
        self.frames_played += 1
        return frame

    async def reset(self) -> None:
        """Drop the current stream and everything buffered for it."""

        decoder, self.decoder = self.decoder, None
        self.stream_id, self.playing = "", False
        self.buffer.clear()
        if decoder is not None:
            await decoder.close()


@dataclass
class _MediaSession:
    """Own one caller media bridge, RTP socket and encoder lifecycle."""

    call_id: str
    channel_id: str
    bridge_id: str | None = None
    external_channel_id: str | None = None
    pump_task: asyncio.Task[None] | None = None
    cadence_task: asyncio.Task[None] | None = None
    transport: asyncio.DatagramTransport | None = None
    encoder: _AudioEncoder | None = None
    rtp_sequence: int = 0
    frame_sequence: int = 0
    frame_count: int = 0
    byte_count: int = 0
    rtp_send_sequence: int = 0
    rtp_timestamp: int = 0
    # A dataclass default is evaluated once at import, which would share one SSRC
    # between every call of the process; RFC 3550 requires one per stream.
    rtp_ssrc: int = field(default_factory=lambda: random.getrandbits(32))
    rtp_marker_sent: bool = False
    rtp_remote_addr: tuple | None = None
    rtp_payload_type: int | None = None
    rtp_payload_size: int | None = None
    rtp_last_send: float = 0.0
    rtp_packet_count: int = 0
    rtp_received_byte_count: int = 0
    rtp_sent_packet_count: int = 0
    rtp_dropped_packet_count: int = 0
    playback_frame_count: int = 0
    command_started: bool = False
    aborted: bool = False
    stopping: bool = False

    async def stop_cadence(self) -> None:
        """Stop the return-RTP sender before its transport disappears."""

        if self.cadence_task is None:
            return
        self.cadence_task.cancel()
        try:
            await self.cadence_task
        except asyncio.CancelledError:
            pass
        self.cadence_task = None

    async def finish_media(self) -> None:
        """Stop RTP intake, drain encoded frames, and leave exact stop counters."""

        self.stopping = True
        await self.stop_cadence()
        if self.transport is not None:
            self.transport.close()
            self.transport = None
        if self.pump_task is not None:
            try:
                await self.pump_task
            except asyncio.CancelledError:
                self.aborted = True
            except Exception:
                # A broken encoder costs this call its audio, never the whole gateway.
                self.aborted = True
            self.pump_task = None

    async def close(self) -> None:
        self.stopping = True
        await self.stop_cadence()
        if self.pump_task is not None:
            self.pump_task.cancel()
            try:
                await self.pump_task
            except asyncio.CancelledError:
                pass
            self.pump_task = None
        if self.transport is not None:
            self.transport.close()
            self.transport = None
        if self.encoder is not None:
            await self.encoder.close()
            self.encoder = None


def _rtp_payload(packet: bytes) -> bytes | None:
    """Extract one RTP payload while rejecting malformed or non-media packets."""

    if len(packet) < 12:
        return None
    version = packet[0] >> 6
    padding = bool(packet[0] & 0x20)
    extension = bool(packet[0] & 0x10)
    contributing = packet[0] & 0x0F
    payload_type = packet[1] & 0x7F
    if version != 2 or payload_type in {13, 72, 73, 74, 75, 76}:
        return None
    start = 12 + contributing * 4
    if extension:
        if len(packet) < start + 4:
            return None
        extension_words = int.from_bytes(packet[start + 2:start + 4], "big")
        start += 4 + extension_words * 4
    if len(packet) <= start:
        return None
    payload = packet[start:]
    if padding and payload:
        padding_size = payload[-1]
        payload = payload[:-padding_size] if 0 < padding_size <= len(payload) else b""
    return payload or None


class TelephonyBlock(BlockDefinition):
    """Receive OVH calls through an existing Asterisk ARI application."""

    kind = "telephony"

    def render_node_card(self, *, node: dict[str, Any], payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Render the canvas card using the shared node head, title and preview chrome."""

        values, _ = display_config(node.get("config"))
        return render_node_card_template(
            block=self, node=node,
            replacements={
                "title": node.get("title") or self.default_title(),
                "ari_target": f"ARI · {values['ari_app']}",
                "capture": "Voice + events" if values["capture_audio"] else "Events only",
            },
            node_classes=["telephony-node"],
        )

    def _ui_replacements(self, values: Mapping[str, Any], warning: str) -> dict[str, str]:
        """Build the placeholder values shared by the modal and the inspector panel.

        Args:
            values: Displayable configuration returned by ``display_config``.
            warning: Validation message to surface, or an empty string.

        Returns:
            Escaped placeholder values keyed without their surrounding markers.
        """

        def text(key: str) -> str:
            return escape(str(values.get(key, "")), quote=True)

        target = f"{values.get('ari_base_url', '')} · {values.get('ari_app', '')}"
        return {
            "ari_base_url": text("ari_base_url"),
            "ari_username": text("ari_username"),
            "ari_password_ref": text("ari_password_ref"),
            "ari_app": text("ari_app"),
            "ari_target": escape(target, quote=True),
            "expected_context": text("expected_context"),
            "expected_extension": text("expected_extension"),
            "allowed_callers": text("allowed_callers"),
            "allowed_callers_count": str(len(caller_entries(values.get("allowed_callers")))),
            "media_host": text("media_host"),
            "media_port": text("media_port"),
            "ffmpeg_chunk_ms": text("ffmpeg_chunk_ms"),
            "max_calls": text("max_calls"),
            "auto_answer_checked": "checked" if values.get("auto_answer") else "",
            "capture_audio_checked": "checked" if values.get("capture_audio") else "",
            "config_warning": (
                f'<p class="field-hint is-error">{escape(warning)} Correct the value, then apply.</p>'
                if warning else ""
            ),
        }

    def render_inspector_panel(self, *, node: dict[str, Any], payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Render the control panel with every editable setting of the block.

        The panel exposes the same attributes as the modal so a call gateway can be
        adjusted without opening it. Values come from ``display_config`` so an invalid
        stored setting stays visible and correctable instead of breaking the surface.
        """

        values, warning = display_config(node.get("config"))
        template = (self.directory / "inspector_panel.html").read_text(encoding="utf-8")
        html = render_inspector_template(
            template=template,
            node={**node, "type": self.kind, "kind": self.kind},
            payload=payload,
            replacements={
                **self._ui_replacements(values, warning),
                "node_icon": "TEL",
                "node_kind": "Telephony",
                "node_tag": "Source",
            },
        )
        return {"html": html, "context": {"node_id": str(node.get("id") or ""), "node_kind": self.kind,
                                          "full_panel": True}}

    def render_modal(self, *, node: dict[str, Any], payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Render the block-owned settings modal from its own grouped template.

        Every control is declared in ``block_modal.html`` instead of being patched into
        generically generated markup: labels, bounds and hints belong to the block, and
        the surface no longer depends on the framework's internal field rendering. The
        ARI secret stays a visible vault reference, never the password itself.
        """

        values, warning = display_config(node.get("config"))
        template = (self.directory / "block_modal.html").read_text(encoding="utf-8")
        html = self._render_generic_modal_template(template=template, node=node, payload=payload or {})
        for key, value in self._ui_replacements(values, warning).items():
            html = html.replace(f"{{{{ {key} }}}}", value)
        return {"html": html, "context": {"node_id": str(node.get("id") or ""), "node_kind": self.kind}}

    def prepare_runtime(self, context: BlockRuntimePreparationContext) -> BlockRuntimePreparation:
        """Validate early and request the persistent listener in Active Runtime."""

        config(context.config)
        self._ports(context)
        return BlockRuntimePreparation(keep_alive=context.runtime_mode == "zeromq_active", listen_on_run=context.runtime_mode == "zeromq_active")

    def initialize_runtime(self, context: BlockRuntimeContext) -> BlockRuntimeResult:
        """Fail before Run when a required dependency or secret cannot be resolved."""

        try:
            config_value = config(context.config)
            if context.runtime_mode == "zeromq_active":
                _secret(context, config_value["ari_password_ref"])
            if context.runtime_mode == "zeromq_active" and config_value["capture_audio"] and shutil.which("ffmpeg") is None:
                raise TelephonyError("FFmpeg is required to publish call audio.")
            return BlockRuntimeResult(last_message="Waiting for Asterisk calls.", content_type=TEXT_PLAIN,
                                      metadata={"telephony": {"state": "waiting", "ari_app": config_value["ari_app"]}})
        except Exception as exc:
            return _failure(exc)

    def execute_runtime(self, context: BlockRuntimeContext) -> BlockRuntimeResult:
        """Report source readiness; real calls arrive on the persistent listener."""

        try:
            config_value = config(context.config)
            self._ports(context)
            metadata = {"telephony": {"state": "listening" if context.runtime_mode == "zeromq_active" else "simulation",
                                         "ari_app": config_value["ari_app"]}}
            message = "Telephony gateway active." if context.runtime_mode == "zeromq_active" else "Audio streaming is unavailable in simulation; Active Runtime listening is required."
            return BlockRuntimeResult(status="success" if context.runtime_mode == "zeromq_active" else "skipped",
                                      last_message=message, content_type=TEXT_PLAIN, metadata=metadata)
        except Exception as exc:
            return _failure(exc)

    async def _listen(self, context: BlockRuntimeListenerContext, config_value: dict[str, Any]) -> None:
        """Keep an ARI session alive until the runtime stops, reconnecting on failure.

        A dropped WebSocket is an ordinary incident for a gateway that runs for days:
        Asterisk restarts, the network blinks, a keepalive times out. Reporting a failed
        result would stop the worker for good, so a lost connection is reported as a
        degraded state and retried with a capped backoff. Only an unrecoverable setup
        error, raised before the loop, fails the node.

        Args:
            context: Listener context owning the stop signal, services and result sink.
            config_value: Validated configuration for this listener.
        """

        try:
            from websockets.asyncio.client import connect
        except ImportError as exc:
            raise TelephonyError("Missing dependency: websockets==15.0.1.") from exc
        password = _secret(context, config_value["ari_password_ref"])
        client = _AriClient(config_value, password)
        audio = context.services.get("runtime_audio_streams")
        if not config_value["capture_audio"]:
            audio = None
        if config_value["capture_audio"] and (audio is None or not getattr(audio, "available", False)):
            raise TelephonyError("Wire audio_out to a compatible audio consumer.")
        playback = _Playback() if config_value["capture_audio"] else None
        intake = (asyncio.create_task(self._playback_intake(context, audio, playback))
                  if playback is not None else None)
        try:
            await self._connection_loop(connect, context, client, config_value, audio, playback)
        finally:
            if intake is not None:
                intake.cancel()
                try:
                    await intake
                except asyncio.CancelledError:
                    pass
            if playback is not None:
                await playback.reset()

    async def _connection_loop(self, connect: Any, context: BlockRuntimeListenerContext, client: "_AriClient",
                               config_value: dict[str, Any], audio: Any,
                               playback: "_Playback | None") -> None:
        """Reconnect to ARI until the runtime stops, owning one session set per connection.

        Args:
            connect: WebSocket client factory.
            context: Listener context owning the stop signal and result sink.
            client: Authenticated ARI HTTP client.
            config_value: Validated configuration for this listener.
            audio: Runtime audio stream client, or None when capture is disabled.
            playback: Shared playback buffer, or None when capture is disabled.
        """

        delay = 0.0
        while not context.stop_requested():
            sessions: dict[str, _MediaSession] = {}
            try:
                await self._ari_session(connect, context, client, config_value, sessions, audio, playback)
                delay = 0.0
            except asyncio.CancelledError:
                raise
            except Exception as error:
                delay = min(RECONNECT_MAX_DELAY, delay * 2 or RECONNECT_MIN_DELAY)
                detail = str(error) if isinstance(error, TelephonyError) else "Asterisk ARI connection lost."
                context.emit_result(BlockRuntimeResult(
                    last_message=f"{detail} Retrying in {delay:.0f} s.", content_type=TEXT_PLAIN,
                    metadata={"telephony": {"state": "reconnecting", "ari_app": config_value["ari_app"],
                                            "retry_in_sec": round(delay, 1)}}))
            finally:
                await self._release_sessions(context, client, sessions)
            if delay:
                await self._wait(context, delay)

    async def _playback_intake(self, context: BlockRuntimeListenerContext, audio: Any,
                               playback: "_Playback") -> None:
        """Decode the graph audio wired to ``audio_in`` for as long as the listener runs.

        Frames are drained on a worker thread so the ARI loop and the return RTP cadence
        keep their own timing. A stop command marked aborted is a barge-in: the buffered
        answer is dropped immediately instead of being played to its end.

        Args:
            context: Listener context exposing the stop signal and the command port.
            audio: Runtime audio stream client bound to the input port.
            playback: Shared playback buffer feeding the return RTP senders.
        """

        if audio is None or not getattr(audio, "available", False):
            return
        loop = asyncio.get_running_loop()
        while not context.stop_requested():
            try:
                command = context.receive_command(timeout_sec=0)
            except Exception:
                command = None
            if command is not None:
                payload = getattr(command, "payload", None)
                if isinstance(payload, Mapping) and str(payload.get("action") or "") == "stop" \
                        and bool(payload.get("aborted")):
                    await playback.reset()
            try:
                frame = await loop.run_in_executor(
                    None, lambda: audio.receive_port("audio_in", timeout_sec=0.05))
            except Exception:
                await asyncio.sleep(0.05)
                continue
            if frame is None:
                playback.drain()
                continue
            try:
                await playback.feed(frame)
            except Exception:
                # A producer sending an unsupported format loses its answer, not the call.
                await playback.reset()

    async def _ari_session(self, connect: Any, context: BlockRuntimeListenerContext, client: "_AriClient",
                           config_value: dict[str, Any], sessions: dict[str, _MediaSession], audio: Any,
                           playback: "_Playback | None" = None) -> None:
        """Run one ARI WebSocket session, isolating the failures of a single event.

        Args:
            connect: WebSocket client factory injected by the caller.
            context: Listener context receiving connection and call results.
            client: Authenticated ARI HTTP client.
            config_value: Validated configuration for this listener.
            sessions: Live call sessions owned by this connection.
            audio: Runtime audio stream client, or None when capture is disabled.
        """

        async with connect(client.websocket_url(), additional_headers={"Authorization": client.authorization},
                           open_timeout=10, ping_interval=20, ping_timeout=20) as websocket:
            await self._apply_event_filter(client, config_value)
            context.emit_result(BlockRuntimeResult(last_message="Connected to Asterisk ARI.", content_type=TEXT_PLAIN,
                metadata={"telephony": {"state": "connected", "ari_app": config_value["ari_app"]}}))
            audit_deadline = time.monotonic() + SESSION_AUDIT_INTERVAL
            while not context.stop_requested():
                try:
                    raw = await asyncio.wait_for(websocket.recv(), timeout=0.5)
                except TimeoutError:
                    audit_deadline = await self._audit_sessions(context, client, sessions, audit_deadline)
                    continue
                try:
                    event = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if not isinstance(event, dict):
                    continue
                try:
                    await self._handle_event(context, client, config_value, sessions, event, audio, playback)
                except Exception as error:
                    # One malformed event or one transient ARI error concerns one call;
                    # the other calls and the connection keep running.
                    detail = str(error) if isinstance(error, TelephonyError) else "Asterisk transport error."
                    context.emit_result(BlockRuntimeResult(
                        last_message=f"Asterisk event skipped: {detail}", content_type=TEXT_PLAIN,
                        metadata={"telephony": {"state": "event_error"}}))

    @staticmethod
    async def _apply_event_filter(client: "_AriClient", config_value: Mapping[str, Any]) -> None:
        """Ask Asterisk to send only the two event types this block consumes.

        The application is subscribed to its own external media channels, so an
        unfiltered stream also carries variable, bridge and playback events that are
        parsed and discarded. The filter is declarative and best effort: a server that
        refuses it keeps sending everything, which the listener still handles correctly.

        Args:
            client: Authenticated ARI HTTP client.
            config_value: Validated configuration naming the Stasis application.
        """

        try:
            await client.request(
                "PUT", f"/applications/{quote(str(config_value['ari_app']), safe='')}/eventFilter",
                body={"allowed": [{"type": "StasisStart"}, {"type": "StasisEnd"}]},
            )
        except Exception:
            return

    async def _audit_sessions(self, context: BlockRuntimeListenerContext, client: "_AriClient",
                              sessions: dict[str, _MediaSession], deadline: float) -> float:
        """Close calls whose Asterisk channel is gone, and return the next audit deadline.

        A ``StasisEnd`` lost during a disconnection would otherwise keep a session, its
        encoder and its slot in ``max_calls`` forever. Ambiguity is never resolved by
        closing: an unreadable channel list leaves every session untouched.

        Args:
            context: Listener context receiving the recovered end events.
            client: Authenticated ARI HTTP client.
            sessions: Live call sessions owned by this connection.
            deadline: Monotonic instant from which an audit is due.

        Returns:
            The monotonic instant of the next audit.
        """

        if not sessions or time.monotonic() < deadline:
            return deadline
        try:
            channels = await client.request("GET", "/channels")
        except Exception:
            return time.monotonic() + SESSION_AUDIT_INTERVAL
        if not isinstance(channels, list):
            return time.monotonic() + SESSION_AUDIT_INTERVAL
        live = {str(item.get("id") or "") for item in channels if isinstance(item, Mapping)}
        for channel_id in [key for key in sessions if key not in live]:
            session = sessions.pop(channel_id)
            await self._close_call(context, client, session, {"id": channel_id}, recovered=True)
        return time.monotonic() + SESSION_AUDIT_INTERVAL

    async def _close_call(self, context: BlockRuntimeListenerContext, client: "_AriClient",
                          session: _MediaSession, channel: Mapping[str, Any], **extra: Any) -> None:
        """Drain media, publish the stop command and the end event, then release Asterisk.

        The graph is served before Asterisk: a cleanup error must never cost the audio
        consumer its stop command or its exact published counters.

        Args:
            context: Listener context receiving the correlated results.
            client: Authenticated ARI HTTP client.
            session: Call session being closed.
            channel: Channel object used to describe the ended call.
            extra: Additional fields merged into the end event.
        """

        await session.finish_media()
        if session.command_started:
            context.emit_result(self._command_result({
                "action": "stop", "stream_id": session.call_id,
                "frame_count": session.frame_count, "byte_count": session.byte_count,
                "aborted": session.aborted,
            }))
        context.emit_result(self._call_result(_event(
            "call.ended", channel, call_id=session.call_id,
            rtp_packets=session.rtp_packet_count,
            rtp_bytes=session.rtp_received_byte_count,
            rtp_return_packets=session.rtp_sent_packet_count,
            rtp_dropped=session.rtp_dropped_packet_count,
            playback_frames=session.playback_frame_count,
            **extra,
        )))
        await self._release_call(client, session)

    @staticmethod
    async def _release_call(client: "_AriClient", session: _MediaSession, *, hangup: bool = False) -> None:
        """Release the local and Asterisk resources of one call without ever raising.

        Args:
            client: Authenticated ARI HTTP client.
            session: Call session to release.
            hangup: Whether the caller channel must be hung up too.
        """

        await session.close()
        targets = []
        if hangup and session.channel_id:
            targets.append(f"/channels/{session.channel_id}")
        if session.bridge_id:
            targets.append(f"/bridges/{session.bridge_id}")
        if session.external_channel_id:
            targets.append(f"/channels/{session.external_channel_id}")
        for path in targets:
            try:
                await client.request("DELETE", path, quiet=True)
            except Exception:
                continue

    async def _release_sessions(self, context: BlockRuntimeListenerContext, client: "_AriClient",
                                sessions: dict[str, _MediaSession]) -> None:
        """Abort the calls of a finished ARI session, reporting an aborted stop command.

        Args:
            context: Listener context receiving the aborted stop commands.
            client: Authenticated ARI HTTP client.
            sessions: Sessions of the connection being abandoned.
        """

        for session in list(sessions.values()):
            try:
                if session.command_started:
                    session.aborted = True
                    await session.finish_media()
                    context.emit_result(self._command_result({
                        "action": "stop", "stream_id": session.call_id,
                        "frame_count": session.frame_count, "byte_count": session.byte_count,
                        "aborted": True,
                    }))
            except Exception:
                pass
            await self._release_call(client, session)
        sessions.clear()

    @staticmethod
    async def _wait(context: BlockRuntimeListenerContext, delay: float) -> None:
        """Wait for a retry delay in short steps so a stop request stays immediate.

        Args:
            context: Listener context exposing the stop signal.
            delay: Requested delay in seconds.
        """

        deadline = time.monotonic() + delay
        while not context.stop_requested():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            await asyncio.sleep(min(0.1, remaining))

    async def _handle_event(self, context: BlockRuntimeListenerContext, client: "_AriClient",
                            config_value: dict[str, Any], sessions: dict[str, _MediaSession], event: Mapping[str, Any],
                            audio: Any, playback: "_Playback | None" = None) -> None:
        """Normalize one ARI event, emit graph data and manage optional media."""

        event_type = str(event.get("type") or "")
        raw_channel = event.get("channel")
        channel = raw_channel if isinstance(raw_channel, Mapping) else {}
        channel_id = str(channel.get("id") or "")
        if self._is_media_channel(channel, sessions):
            return
        if event_type == "StasisStart" and channel_id and channel_id not in sessions:
            if not self._matches(channel, config_value) or len(sessions) >= config_value["max_calls"]:
                return
            call_id = f"ari-{channel_id}"
            session = _MediaSession(call_id=call_id, channel_id=channel_id)
            sessions[channel_id] = session
            context.emit_result(self._call_result(_event("call.incoming", channel, call_id=call_id, audio=config_value["capture_audio"])))
            reason = "answer"
            try:
                if config_value["auto_answer"]:
                    await client.request("POST", f"/channels/{channel_id}/answer")
                if config_value["capture_audio"]:
                    reason = "media_setup"
                    await self._start_media(client, config_value, session, audio, playback)
                    session.command_started = True
                    context.emit_result(self._command_result({
                        "action": "start", "stream_id": session.call_id,
                    }))
            except Exception as error:
                sessions.pop(channel_id, None)
                # An answered caller left in Stasis would hold an Asterisk channel and
                # hear silence until it gives up, so the failed call is hung up.
                await self._release_call(client, session, hangup=config_value["auto_answer"])
                context.emit_result(self._call_result(_event(
                    "call.failed", channel, call_id=call_id, audio=False, reason=reason
                )))
                detail = str(error) if isinstance(error, TelephonyError) else "Asterisk transport error."
                # A failed call is reported as a call event, not as a failed node result:
                # the framework stops the worker on the first failed listener result.
                context.emit_result(BlockRuntimeResult(
                    last_message=f"Call {call_id} dropped: {detail}", content_type=TEXT_PLAIN,
                    metadata={"telephony": {"state": "call_failed", "reason": reason}}))
            return
        if event_type == "StasisEnd" and channel_id in sessions:
            await self._close_call(context, client, sessions.pop(channel_id), channel)

    @staticmethod
    def _is_media_channel(channel: Mapping[str, Any], sessions: Mapping[str, "_MediaSession"]) -> bool:
        """Return whether an event describes one of the block's own media channels.

        External media channels join the same Stasis application, so Asterisk announces
        them exactly like an inbound call. Without this guard they are answered as new
        calls as soon as ``max_calls`` allows a second session, each one creating another
        media channel. Their identifier can still be unknown when the event arrives, so
        the channel name is checked first.

        Args:
            channel: Channel object carried by the ARI event.
            sessions: Active sessions, keyed by caller channel identifier.
        """

        if str(channel.get("name") or "").startswith("UnicastRTP/"):
            return True
        channel_id = str(channel.get("id") or "")
        return bool(channel_id) and any(
            session.external_channel_id == channel_id for session in sessions.values()
        )

    @staticmethod
    def _matches(channel: Mapping[str, Any], config_value: Mapping[str, Any]) -> bool:
        """Apply the optional context, extension and caller filters before answering."""

        dialplan = channel.get("dialplan") if isinstance(channel.get("dialplan"), Mapping) else {}
        caller = channel.get("caller") if isinstance(channel.get("caller"), Mapping) else {}
        expected_context = str(config_value.get("expected_context") or "")
        expected_extension = str(config_value.get("expected_extension") or "")
        allowed = str(config_value.get("allowed_callers") or "")
        return ((not expected_context or str(dialplan.get("context") or channel.get("context") or "") == expected_context) and
                (not expected_extension or str(dialplan.get("exten") or channel.get("extension") or "") == expected_extension) and
                (not allowed or caller_allowed(caller.get("number"), allowed)))

    async def _start_media(self, client: "_AriClient", config_value: Mapping[str, Any], session: _MediaSession,
                           audio: Any, playback: "_Playback | None" = None) -> None:
        """Attach Asterisk media to a local RTP receiver and Opus encoder."""

        loop = asyncio.get_running_loop()
        encoder = await _AudioEncoder.create(int(config_value["ffmpeg_chunk_ms"]))
        queue: asyncio.Queue[tuple[bytes, bytes] | None] = asyncio.Queue(maxsize=256)

        class _Protocol(asyncio.DatagramProtocol):
            def datagram_received(self, data: bytes, addr: tuple) -> None:
                payload = _rtp_payload(data)
                payload_type = data[1] & 0x7f
                if payload is not None:
                    try:
                        queue.put_nowait((addr, payload_type, payload))
                    except asyncio.QueueFull:
                        # Silent audio loss is the worst case for a recording or a
                        # transcription: count it and report it on call.ended.
                        session.rtp_dropped_packet_count += 1

        transport, _ = await loop.create_datagram_endpoint(_Protocol, local_addr=(config_value["media_host"], int(config_value["media_port"])))
        sock = transport.get_extra_info("socket")
        session.transport = transport
        session.encoder = encoder
        external = await client.request("POST", "/channels/externalMedia", data={
            "app": config_value["ari_app"], "external_host": f"{config_value['media_host']}:{sock.getsockname()[1]}",
            "encapsulation": "rtp", "transport": "udp", "connection_type": "client", "format": MEDIA_FORMAT,
        })
        session.external_channel_id = str(external.get("id") or "")
        external_vars = external.get("channelvars") if isinstance(external.get("channelvars"), dict) else {}
        asterisk_host = str(external_vars.get("UNICASTRTP_LOCAL_ADDRESS") or "")
        asterisk_port = int(external_vars.get("UNICASTRTP_LOCAL_PORT") or 0)
        if asterisk_host and asterisk_port > 0:
            # Start return media immediately instead of waiting for the first inbound RTP packet.
            # Inbound RTP refines both values if this endpoint negotiated something else.
            session.rtp_remote_addr = (asterisk_host, asterisk_port)
            session.rtp_payload_type = MEDIA_PAYLOAD_TYPE
            session.rtp_payload_size = MEDIA_FRAME_BYTES
        # One dedicated sender owns the 20 ms return cadence. Deriving it from inbound
        # packets starves it exactly while the caller speaks, which is when Asterisk and
        # the SIP provider watch for return media before dropping the call.
        session.cadence_task = asyncio.create_task(self._return_rtp_cadence(session, playback))
        bridge = await client.request("POST", "/bridges", data={"type": "mixing"})
        session.bridge_id = str(bridge.get("id") or "")
        await client.request("POST", f"/bridges/{session.bridge_id}/addChannel", data={"channel": f"{session.channel_id},{session.external_channel_id}"})
        # A short real-media playback opens the PJSIP/RTP path through NAT.  RTP silence
        # returned to externalMedia keeps that path alive after the playback finishes.
        await client.request("POST", f"/bridges/{session.bridge_id}/play", data={"media": "sound:silence/1"})
        session.pump_task = asyncio.create_task(self._pump_media(session, encoder, queue, audio))

    @staticmethod
    def _rtp_packet(session: "_MediaSession", payload_type: int, payload: bytes) -> bytes:
        """Build one return RTP packet carrying playback audio or silence."""

        marker = 0x00 if session.rtp_marker_sent else 0x80
        header = bytes((
            0x80,
            marker | (payload_type & 0x7f),
            (session.rtp_send_sequence >> 8) & 0xff,
            session.rtp_send_sequence & 0xff,
            (session.rtp_timestamp >> 24) & 0xff,
            (session.rtp_timestamp >> 16) & 0xff,
            (session.rtp_timestamp >> 8) & 0xff,
            session.rtp_timestamp & 0xff,
            (session.rtp_ssrc >> 24) & 0xff,
            (session.rtp_ssrc >> 16) & 0xff,
            (session.rtp_ssrc >> 8) & 0xff,
            session.rtp_ssrc & 0xff,
        ))
        session.rtp_marker_sent = True
        session.rtp_send_sequence = (session.rtp_send_sequence + 1) & 0xffff
        session.rtp_timestamp += max(1, len(payload) // 2)
        return header + payload

    @staticmethod
    def _send_rtp(session: "_MediaSession", remote_addr: tuple, payload_type: int, payload: bytes) -> None:
        """Send one return packet and record the send cadence.

        Args:
            session: Media session owning the RTP transport and counters.
            remote_addr: Asterisk address observed or announced for this call.
            payload_type: Negotiated RTP payload type.
            payload: Frame to send, playback audio or silence of the same size.
        """

        if session.transport is None or not payload:
            return
        session.transport.sendto(TelephonyBlock._rtp_packet(session, payload_type, payload), remote_addr)
        session.rtp_sent_packet_count += 1
        session.rtp_last_send = time.monotonic()

    @staticmethod
    async def _return_rtp_cadence(session: "_MediaSession", playback: "_Playback | None" = None,
                                  interval: float = 0.02) -> None:
        """Send one return RTP packet every ``interval`` seconds for the whole call.

        The cadence is independent of the inbound flow: it starts as soon as the
        external-media address is known and keeps running while the caller speaks. It
        carries playback audio when the graph provides some, and silence otherwise, so
        the media path stays open between two answers.

        Args:
            session: Media session owning the RTP transport and its negotiation state.
            playback: Decoded graph audio to play, or None when nothing is wired.
            interval: Packet period, one 20 ms slin16 frame by default.
        """

        deadline = time.monotonic()
        while not session.stopping:
            if (session.transport is not None and session.rtp_remote_addr is not None
                    and session.rtp_payload_type is not None and session.rtp_payload_size):
                size = session.rtp_payload_size
                frame = playback.take(size) if playback is not None else None
                if frame is not None:
                    session.playback_frame_count += 1
                TelephonyBlock._send_rtp(
                    session, session.rtp_remote_addr, session.rtp_payload_type,
                    frame if frame is not None else bytes(size),
                )
            deadline += interval
            delay = deadline - time.monotonic()
            if delay < -interval:
                # After a long stall, resume from now instead of catching up in a burst.
                deadline, delay = time.monotonic(), 0.0
            await asyncio.sleep(max(0.0, delay))

    @staticmethod
    async def _pump_media(session: "_MediaSession", encoder: "_AudioEncoder",
                          queue: "asyncio.Queue[tuple[tuple, int, bytes]]", audio: Any) -> None:
        """Convert inbound RTP into Opus frames and publish them on the audio port."""

        encoder_failed = False
        while not session.stopping:
            try:
                remote_addr, payload_type, payload = await asyncio.wait_for(queue.get(), timeout=0.02)
            except asyncio.TimeoutError:
                # Return RTP belongs to the cadence task; this timeout only lets the loop
                # observe that the session is stopping.
                continue
            session.rtp_packet_count += 1
            session.rtp_received_byte_count += len(payload)
            session.rtp_remote_addr = remote_addr
            session.rtp_payload_type = payload_type
            session.rtp_payload_size = len(payload)
            # Inbound packets only refresh the negotiated address, payload type and frame
            # size; the cadence task picks them up for its next packet.
            try:
                chunks = await encoder.feed(payload)
            except Exception:
                # FFmpeg died mid-call: stop this capture and let the call go on.
                session.aborted, encoder_failed = True, True
                break
            if audio is not None:
                for chunk in chunks:
                    session.frame_sequence += 1
                    session.frame_count += 1
                    session.byte_count += len(chunk)
                    audio.publish_port("audio_out", chunk, codec="opus", sample_rate_hz=48000, channels=1,
                                       stream_id=session.call_id, sequence=session.frame_sequence)
        final_chunks: list[bytes] = []
        try:
            final_chunks = [] if encoder_failed else await encoder.close()
        except Exception:
            session.aborted = True
        if audio is not None:
            for chunk in final_chunks:
                session.frame_sequence += 1
                session.frame_count += 1
                session.byte_count += len(chunk)
                audio.publish_port("audio_out", chunk, codec="opus", sample_rate_hz=48000, channels=1,
                                   stream_id=session.call_id, sequence=session.frame_sequence)
        session.encoder = None

    def listen_runtime(self, context: BlockRuntimeListenerContext) -> None:
        """Run the Asterisk listener on the framework-owned supervised thread."""

        try:
            config_value = config(context.config)
            asyncio.run(self._listen(context, config_value))
        except Exception as exc:
            context.emit_result(_failure(exc))

    def _ports(self, context: Any) -> None:
        """Protect the fixed event/audio port identities and transports."""

        inputs, outputs = tuple(context.input_ports), tuple(context.output_ports)
        expected_inputs = {(1, "audio_in", "audio_stream"), (2, "command_in", "message")}
        expected_outputs = {
            (1, "event_out", "message"), (2, "audio_out", "audio_stream"),
            (3, "command_out", "message"),
        }
        identity = lambda ports: {(p.id, p.name, getattr(p, "transport", "message")) for p in ports}
        if identity(outputs) != expected_outputs:
            raise TelephonyError("event_out, audio_out and command_out ports must remain unchanged; recreate an altered node.")
        # Playback inputs stay optional: a node that only listens leaves them unconnected.
        if identity(inputs) != expected_inputs:
            raise TelephonyError("audio_in and command_in ports must remain unchanged; recreate an altered node.")

    @staticmethod
    def _command_result(command: Mapping[str, Any]) -> BlockRuntimeResult:
        """Build one listener result containing one correlated capture command."""

        return BlockRuntimeResult(outputs=[BlockRuntimeOutput(port_id=3, port_name="command_out",
            value=json.dumps(command, ensure_ascii=False, separators=(",", ":")), content_type=APPLICATION_JSON)],
            content_type=APPLICATION_JSON, metadata={"telephony": {"command": command.get("action")}})

    @staticmethod
    def _call_result(payload: Mapping[str, Any]) -> BlockRuntimeResult:
        """Build one listener result containing one JSON call event."""

        return BlockRuntimeResult(outputs=[BlockRuntimeOutput(port_id=1, port_name="event_out",
            value=json.dumps(payload, ensure_ascii=False, separators=(",", ":")), content_type=APPLICATION_JSON)],
            content_type=APPLICATION_JSON, metadata={"telephony": {"event": payload.get("event")}})


class _AriClient:
    """Small async ARI HTTP client using only the standard library."""

    def __init__(self, config_value: Mapping[str, Any], password: str) -> None:
        self.config_value = config_value
        self._password = password
        self.authorization = "Basic " + base64.b64encode(f"{config_value['ari_username']}:{password}".encode()).decode()

    def websocket_url(self) -> str:
        """Return the authenticated ARI events WebSocket URL."""

        return self.config_value["ari_base_url"].replace("http://", "ws://", 1).replace("https://", "wss://", 1) + \
            f"/ari/events?app={self.config_value['ari_app']}"

    async def request(self, method: str, path: str, data: Mapping[str, Any] | None = None, *,
                      body: Mapping[str, Any] | None = None, quiet: bool = False) -> dict[str, Any]:
        """Perform one bounded ARI request and return its JSON object.

        Args:
            method: HTTP method.
            path: ARI path below ``/ari``.
            data: Query parameters, which is how ARI takes most of its arguments.
            body: JSON body, required by the few resources that read one.
            quiet: Whether a missing or conflicting resource is an acceptable answer.
        """

        def call() -> dict[str, Any]:
            from urllib.error import HTTPError
            from urllib.parse import urlencode
            from urllib.request import Request, urlopen
            suffix = f"?{urlencode(data)}" if data else ""
            headers = {"Authorization": self.authorization, "Accept": "application/json"}
            payload = None
            if body is not None:
                payload = json.dumps(body).encode("utf-8")
                headers["Content-Type"] = "application/json"
            request = Request(self.config_value["ari_base_url"] + "/ari" + path + suffix, method=method,
                              data=payload, headers=headers)
            try:
                with urlopen(request, timeout=5) as response:
                    # Never rebind `body`: assigning it here would make the JSON body
                    # parameter local to this function and unreadable above.
                    content = response.read()
                    return json.loads(content) if content else {}
            except HTTPError as exc:
                if quiet and exc.code in {404, 409, 410}:
                    return {}
                raise TelephonyError(f"Asterisk ARI refused the request ({exc.code}).") from exc
            except Exception as exc:
                raise TelephonyError("Asterisk ARI is unreachable.") from exc

        return await asyncio.get_running_loop().run_in_executor(None, call)

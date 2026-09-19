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
    "auto_answer": True,
    "capture_audio": True,
    "media_host": "127.0.0.1",
    "media_port": 0,
    "ffmpeg_chunk_ms": 100,
    "max_calls": 1,
}
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


class TelephonyInError(RuntimeError):
    """Stable block-owned validation or transport failure."""


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    """Return one bounded integer, accepting only integral numeric text."""

    if isinstance(value, bool):
        raise TelephonyInError("Les valeurs numériques doivent être des entiers.")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise TelephonyInError("Les valeurs numériques doivent être des entiers.") from exc
    if not minimum <= parsed <= maximum:
        raise TelephonyInError(f"Valeur hors limites : {minimum} à {maximum}.")
    return parsed


def _boolean(value: Any, label: str) -> bool:
    """Require a real boolean so imported string settings cannot become truthy."""

    if type(value) is not bool:
        raise TelephonyInError(f"{label} doit être un booléen.")
    return value


def _optional_token(value: Any, label: str) -> str:
    """Normalize an optional Asterisk context or extension selector."""

    text = "" if value is None else str(value).strip()
    if not text:
        return ""
    if len(text) > 128 or not _TOKEN.fullmatch(text):
        raise TelephonyInError(f"{label} contient des caractères non autorisés.")
    return text


def config(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate settings before any network, media or graph side effect.

    Args:
        raw: Untrusted persisted node configuration.

    Returns:
        A normalized configuration copy safe to pass to runtime helpers.

    Raises:
        TelephonyInError: When a value has an invalid type, range or format.
    """

    source = raw if isinstance(raw, Mapping) else {}
    base = str(source.get("ari_base_url") or DEFAULTS["ari_base_url"]).strip().rstrip("/")
    if not base.startswith(("http://", "https://")) or any(c.isspace() for c in base):
        raise TelephonyInError("L'URL ARI doit être une adresse HTTP ou HTTPS valide.")
    username = str(source.get("ari_username") or DEFAULTS["ari_username"]).strip()
    if not username or len(username) > 128 or any(c in username for c in "\r\n"):
        raise TelephonyInError("Nom d'utilisateur ARI invalide.")
    password_ref = str(source.get("ari_password_ref") or "").strip()
    if len(password_ref) > 256 or any(c in password_ref for c in "\r\n"):
        raise TelephonyInError("Référence du secret ARI invalide.")
    app = _optional_token(source.get("ari_app", DEFAULTS["ari_app"]), "L'application ARI")
    if not app:
        raise TelephonyInError("L'application ARI est obligatoire.")
    media_host = str(source.get("media_host") or DEFAULTS["media_host"]).strip()
    if not media_host or len(media_host) > 253 or any(c.isspace() for c in media_host):
        raise TelephonyInError("Hôte média invalide.")
    return {
        "ari_base_url": base,
        "ari_username": username,
        "ari_password_ref": password_ref,
        "ari_app": app,
        "expected_context": _optional_token(source.get("expected_context"), "Le contexte"),
        "expected_extension": _optional_token(source.get("expected_extension"), "L'extension"),
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
    except TelephonyInError as error:
        stored = dict(raw) if isinstance(raw, Mapping) else {}
        values = {key: stored.get(key, default) for key, default in DEFAULTS.items()}
        return values, str(error)


def _secret(context: Any, value_ref: str) -> str:
    """Resolve a wallet reference through the public injected service only."""

    resolver = context.services.get("resolve_secret")
    if not callable(resolver):
        raise TelephonyInError("Résolveur de secrets indisponible dans ce runtime.")
    try:
        value = resolver(value_ref)
    except Exception as exc:
        raise TelephonyInError("Secret ARI inaccessible : déverrouillez le coffre.") from exc
    if not isinstance(value, str) or not value.strip() or any(c in value for c in "\r\n"):
        raise TelephonyInError("Le secret ARI est vide ou invalide.")
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

    message = str(error) if isinstance(error, TelephonyInError) else "Erreur de transport Asterisk ou de flux média."
    return BlockRuntimeResult(status="failed", error=message, last_message=message, content_type=TEXT_PLAIN,
                              metadata={"telephony_in": {"state": "error"}})


@dataclass
class _AudioEncoder:
    """Encode little-endian PCM RTP payloads into compatible Ogg/Opus chunks."""

    chunk_ms: int
    process: asyncio.subprocess.Process
    sequence: int = 0
    pending: bytearray = field(default_factory=bytearray)
    output_queue: "asyncio.Queue[bytes | None]" = field(default_factory=asyncio.Queue)
    reader_task: "asyncio.Task[None] | None" = None
    chunk_size: int = 320  # 10 ms of mono 16 kHz s16le; adjusted on construction.

    def __post_init__(self) -> None:
        self.chunk_size = max(64, int(32000 * self.chunk_ms / 1000))

    @classmethod
    async def create(cls, chunk_ms: int) -> "_AudioEncoder":
        """Start an FFmpeg subprocess configured for mono 48 kHz Opus output."""

        process = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "s16be",
            "-ar", "16000", "-ac", "1", "-i", "pipe:0", "-c:a", "libopus",
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
                raise TelephonyInError("FFmpeg n'a pas pu encoder l'audio d'appel.")
        except ProcessLookupError:
            pass
        return chunks


@dataclass
class _MediaSession:
    """Own one caller media bridge, RTP socket and encoder lifecycle."""

    call_id: str
    channel_id: str
    bridge_id: str | None = None
    external_channel_id: str | None = None
    pump_task: asyncio.Task[None] | None = None
    transport: asyncio.DatagramTransport | None = None
    encoder: _AudioEncoder | None = None
    rtp_sequence: int = 0
    frame_sequence: int = 0
    frame_count: int = 0
    byte_count: int = 0
    rtp_send_sequence: int = 0
    rtp_timestamp: int = 0
    rtp_ssrc: int = random.getrandbits(32)
    rtp_marker_sent: bool = False
    rtp_remote_addr: tuple | None = None
    rtp_payload_type: int | None = None
    rtp_payload_size: int | None = None
    rtp_last_send: float = 0.0
    rtp_packet_count: int = 0
    rtp_received_byte_count: int = 0
    rtp_sent_packet_count: int = 0
    command_started: bool = False
    aborted: bool = False
    stopping: bool = False

    async def finish_media(self) -> None:
        """Stop RTP intake, drain encoded frames, and leave exact stop counters."""

        self.stopping = True
        if self.transport is not None:
            self.transport.close()
            self.transport = None
        if self.pump_task is not None:
            try:
                await self.pump_task
            except asyncio.CancelledError:
                self.aborted = True
            self.pump_task = None

    async def close(self) -> None:
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


class TelephonyInBlock(BlockDefinition):
    """Receive OVH calls through an existing Asterisk ARI application."""

    kind = "telephony_in"

    def render_node_card(self, *, node: dict[str, Any], payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Render the canvas card using the shared node head, title and preview chrome."""

        values, _ = display_config(node.get("config"))
        return render_node_card_template(
            block=self, node=node,
            replacements={
                "title": node.get("title") or self.default_title(),
                "ari_target": f"ARI · {values['ari_app']}",
                "capture": "Audio + événements" if values["capture_audio"] else "Événements seuls",
            },
            node_classes=["telephony-in-node"],
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
            "media_host": text("media_host"),
            "media_port": text("media_port"),
            "ffmpeg_chunk_ms": text("ffmpeg_chunk_ms"),
            "max_calls": text("max_calls"),
            "auto_answer_checked": "checked" if values.get("auto_answer") else "",
            "capture_audio_checked": "checked" if values.get("capture_audio") else "",
            "config_warning": (
                f'<p class="field-hint is-error">{escape(warning)} Corrigez la valeur puis appliquez.</p>'
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
                "node_kind": "Telephony In",
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
                raise TelephonyInError("FFmpeg est requis pour publier l'audio d'appel.")
            return BlockRuntimeResult(last_message="Attente d'appels Asterisk.", content_type=TEXT_PLAIN,
                                      metadata={"telephony_in": {"state": "waiting", "ari_app": config_value["ari_app"]}})
        except Exception as exc:
            return _failure(exc)

    def execute_runtime(self, context: BlockRuntimeContext) -> BlockRuntimeResult:
        """Report source readiness; real calls arrive on the persistent listener."""

        try:
            config_value = config(context.config)
            self._ports(context)
            metadata = {"telephony_in": {"state": "listening" if context.runtime_mode == "zeromq_active" else "simulation",
                                         "ari_app": config_value["ari_app"]}}
            message = "Passerelle téléphonie active." if context.runtime_mode == "zeromq_active" else "Flux audio indisponible en simulation ; écoute Active Runtime requise."
            return BlockRuntimeResult(status="success" if context.runtime_mode == "zeromq_active" else "skipped",
                                      last_message=message, content_type=TEXT_PLAIN, metadata=metadata)
        except Exception as exc:
            return _failure(exc)

    async def _listen(self, context: BlockRuntimeListenerContext, config_value: dict[str, Any]) -> None:
        """Own cancellable ARI WebSocket and RTP media IO on the listener thread."""

        try:
            from websockets.asyncio.client import connect
        except ImportError as exc:
            raise TelephonyInError("Dépendance manquante : websockets==15.0.1.") from exc
        password = _secret(context, config_value["ari_password_ref"])
        client = _AriClient(config_value, password)
        sessions: dict[str, _MediaSession] = {}
        audio = context.services.get("runtime_audio_streams")
        if not config_value["capture_audio"]:
            audio = None
        if config_value["capture_audio"] and (audio is None or not getattr(audio, "available", False)):
            raise TelephonyInError("Reliez audio_out à un consommateur audio compatible.")
        websocket_url = client.websocket_url()
        try:
            async with connect(websocket_url, additional_headers={"Authorization": client.authorization}, open_timeout=10, ping_interval=20, ping_timeout=20) as websocket:
                context.emit_result(BlockRuntimeResult(last_message="Connecté à Asterisk ARI.", content_type=TEXT_PLAIN,
                    metadata={"telephony_in": {"state": "connected", "ari_app": config_value["ari_app"]}}))
                while not context.stop_requested():
                    try:
                        raw = await asyncio.wait_for(websocket.recv(), timeout=0.5)
                    except TimeoutError:
                        continue
                    try:
                        event = json.loads(raw)
                    except (TypeError, ValueError):
                        continue
                    if not isinstance(event, dict):
                        continue
                    await self._handle_event(context, client, config_value, sessions, event, audio)
        finally:
            for session in list(sessions.values()):
                if session.command_started:
                    session.aborted = True
                    await session.finish_media()
                    context.emit_result(self._command_result({
                        "action": "stop", "stream_id": session.call_id,
                        "frame_count": session.frame_count, "byte_count": session.byte_count,
                        "aborted": True,
                    }))
                await session.close()
                if session.bridge_id:
                    await client.request("DELETE", f"/bridges/{session.bridge_id}", quiet=True)
                if session.external_channel_id:
                    await client.request("DELETE", f"/channels/{session.external_channel_id}", quiet=True)
            sessions.clear()

    async def _handle_event(self, context: BlockRuntimeListenerContext, client: "_AriClient",
                            config_value: dict[str, Any], sessions: dict[str, _MediaSession], event: Mapping[str, Any], audio: Any) -> None:
        """Normalize one ARI event, emit graph data and manage optional media."""

        event_type = str(event.get("type") or "")
        raw_channel = event.get("channel")
        channel = raw_channel if isinstance(raw_channel, Mapping) else {}
        channel_id = str(channel.get("id") or "")
        if event_type == "StasisStart" and channel_id and channel_id not in sessions:
            if not self._matches(channel, config_value) or len(sessions) >= config_value["max_calls"]:
                return
            call_id = f"ari-{channel_id}"
            session = _MediaSession(call_id=call_id, channel_id=channel_id)
            sessions[channel_id] = session
            context.emit_result(self._call_result(_event("call.incoming", channel, call_id=call_id, audio=config_value["capture_audio"])))
            if config_value["auto_answer"]:
                await client.request("POST", f"/channels/{channel_id}/answer")
            if config_value["capture_audio"]:
                try:
                    await self._start_media(client, config_value, session, audio)
                    session.command_started = True
                    context.emit_result(self._command_result({
                        "action": "start", "stream_id": session.call_id,
                    }))
                except Exception:
                    await session.close()
                    if session.bridge_id:
                        await client.request("DELETE", f"/bridges/{session.bridge_id}", quiet=True)
                    if session.external_channel_id:
                        await client.request("DELETE", f"/channels/{session.external_channel_id}", quiet=True)
                    sessions.pop(channel_id, None)
                    context.emit_result(self._call_result(_event(
                        "call.failed", channel, call_id=call_id, audio=False, reason="media_setup"
                    )))
                    context.emit_result(_failure(TelephonyInError("Initialisation média Asterisk impossible.")))
            return
        if event_type == "StasisEnd" and channel_id in sessions:
            session = sessions.pop(channel_id)
            await session.finish_media()
            if session.bridge_id:
                await client.request("DELETE", f"/bridges/{session.bridge_id}", quiet=True)
            if session.external_channel_id:
                await client.request("DELETE", f"/channels/{session.external_channel_id}", quiet=True)
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
            )))

    @staticmethod
    def _matches(channel: Mapping[str, Any], config_value: Mapping[str, Any]) -> bool:
        """Apply optional context/extension filters before emitting or answering."""

        dialplan = channel.get("dialplan") if isinstance(channel.get("dialplan"), Mapping) else {}
        expected_context = str(config_value.get("expected_context") or "")
        expected_extension = str(config_value.get("expected_extension") or "")
        return ((not expected_context or str(dialplan.get("context") or channel.get("context") or "") == expected_context) and
                (not expected_extension or str(dialplan.get("exten") or channel.get("extension") or "") == expected_extension))

    async def _start_media(self, client: "_AriClient", config_value: Mapping[str, Any], session: _MediaSession, audio: Any) -> None:
        """Attach Asterisk media to a local RTP receiver and Opus encoder."""

        loop = asyncio.get_running_loop()
        encoder = await _AudioEncoder.create(int(config_value["ffmpeg_chunk_ms"]))
        queue: asyncio.Queue[tuple[bytes, bytes] | None] = asyncio.Queue(maxsize=256)

        class _Protocol(asyncio.DatagramProtocol):
            def datagram_received(self, data: bytes, addr: tuple) -> None:
                payload = _rtp_payload(data)
                payload_type = data[1] & 0x7f
                if payload is not None:
                    try: queue.put_nowait((addr, payload_type, payload))
                    except asyncio.QueueFull: pass

        transport, _ = await loop.create_datagram_endpoint(_Protocol, local_addr=(config_value["media_host"], int(config_value["media_port"])))
        sock = transport.get_extra_info("socket")
        session.transport = transport
        session.encoder = encoder
        external = await client.request("POST", "/channels/externalMedia", data={
            "app": config_value["ari_app"], "external_host": f"{config_value['media_host']}:{sock.getsockname()[1]}",
            "encapsulation": "rtp", "transport": "udp", "connection_type": "client", "format": "slin16",
        })
        session.external_channel_id = str(external.get("id") or "")
        external_vars = external.get("channelvars") if isinstance(external.get("channelvars"), dict) else {}
        asterisk_host = str(external_vars.get("UNICASTRTP_LOCAL_ADDRESS") or "")
        asterisk_port = int(external_vars.get("UNICASTRTP_LOCAL_PORT") or 0)
        if asterisk_host and asterisk_port > 0:
            # Start return media immediately instead of waiting for the first inbound RTP packet.
            # Asterisk 20 uses dynamic payload type 118 for slin16 (640 bytes / 20 ms).
            session.rtp_remote_addr = (asterisk_host, asterisk_port)
            session.rtp_payload_type = 118
            session.rtp_payload_size = 640
        bridge = await client.request("POST", "/bridges", data={"type": "mixing"})
        session.bridge_id = str(bridge.get("id") or "")
        await client.request("POST", f"/bridges/{session.bridge_id}/addChannel", data={"channel": f"{session.channel_id},{session.external_channel_id}"})
        # A short real-media playback opens the PJSIP/RTP path through NAT.  RTP silence
        # returned to externalMedia keeps that path alive after the playback finishes.
        await client.request("POST", f"/bridges/{session.bridge_id}/play", data={"media": "sound:silence/1"})
        session.pump_task = asyncio.create_task(self._pump_media(session, encoder, queue, audio))

    @staticmethod
    def _rtp_silence_packet(session: "_MediaSession", payload_type: int, size: int) -> bytes:
        """Build one return RTP silence packet using the external-media payload type."""

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
        session.rtp_timestamp += max(1, size // 2)
        return header + bytes(size)

    @staticmethod
    def _send_rtp_silence(session: "_MediaSession", remote_addr: tuple, payload_type: int, size: int) -> None:
        """Send one valid silence packet and record the send cadence."""

        if session.transport is None or size <= 0:
            return
        session.transport.sendto(TelephonyInBlock._rtp_silence_packet(session, payload_type, size), remote_addr)
        session.rtp_sent_packet_count += 1
        session.rtp_last_send = time.monotonic()

    @staticmethod
    async def _pump_media(session: "_MediaSession", encoder: "_AudioEncoder",
                          queue: "asyncio.Queue[tuple[tuple, int, bytes]]", audio: Any) -> None:
        """Convert RTP input, return silence to Asterisk, and publish encoded frames."""

        while not session.stopping:
            try:
                remote_addr, payload_type, payload = await asyncio.wait_for(queue.get(), timeout=0.02)
            except asyncio.TimeoutError:
                if (session.rtp_remote_addr is not None and session.rtp_payload_type is not None and
                        session.rtp_payload_size is not None and
                        time.monotonic() - session.rtp_last_send >= 0.02):
                    TelephonyInBlock._send_rtp_silence(
                        session, session.rtp_remote_addr, session.rtp_payload_type, session.rtp_payload_size
                    )
                continue
            session.rtp_packet_count += 1
            session.rtp_received_byte_count += len(payload)
            session.rtp_remote_addr = remote_addr
            session.rtp_payload_type = payload_type
            session.rtp_payload_size = len(payload)
            # Inbound packets refresh negotiation; one monotonic 20 ms sender owns the return cadence.
            chunks = await encoder.feed(payload)
            if audio is not None:
                for chunk in chunks:
                    session.frame_sequence += 1
                    session.frame_count += 1
                    session.byte_count += len(chunk)
                    audio.publish_port("audio_out", chunk, codec="opus", sample_rate_hz=48000, channels=1,
                                       stream_id=session.call_id, sequence=session.frame_sequence)
        final_chunks: list[bytes] = []
        try:
            final_chunks = await encoder.close()
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
        if inputs or len(outputs) != 3:
            raise TelephonyInError("Telephony In requires no inputs and its three fixed outputs.")
        expected = {
            (1, "event_out", "message"), (2, "audio_out", "audio_stream"),
            (3, "command_out", "message"),
        }
        actual = {(p.id, p.name, getattr(p, "transport", "message")) for p in outputs}
        if actual != expected:
            raise TelephonyInError("event_out, audio_out and command_out ports must remain unchanged; recreate an altered node.")

    @staticmethod
    def _command_result(command: Mapping[str, Any]) -> BlockRuntimeResult:
        """Build one listener result containing one correlated capture command."""

        return BlockRuntimeResult(outputs=[BlockRuntimeOutput(port_id=3, port_name="command_out",
            value=json.dumps(command, ensure_ascii=False, separators=(",", ":")), content_type=APPLICATION_JSON)],
            content_type=APPLICATION_JSON, metadata={"telephony_in": {"command": command.get("action")}})

    @staticmethod
    def _call_result(payload: Mapping[str, Any]) -> BlockRuntimeResult:
        """Build one listener result containing one JSON call event."""

        return BlockRuntimeResult(outputs=[BlockRuntimeOutput(port_id=1, port_name="event_out",
            value=json.dumps(payload, ensure_ascii=False, separators=(",", ":")), content_type=APPLICATION_JSON)],
            content_type=APPLICATION_JSON, metadata={"telephony_in": {"event": payload.get("event")}})


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

    async def request(self, method: str, path: str, data: Mapping[str, Any] | None = None, *, quiet: bool = False) -> dict[str, Any]:
        """Perform one bounded ARI request and return its JSON object."""

        def call() -> dict[str, Any]:
            from urllib.error import HTTPError
            from urllib.parse import urlencode
            from urllib.request import Request, urlopen
            suffix = f"?{urlencode(data)}" if data else ""
            request = Request(self.config_value["ari_base_url"] + "/ari" + path + suffix, method=method,
                              headers={"Authorization": self.authorization, "Accept": "application/json"})
            try:
                with urlopen(request, timeout=5) as response:
                    body = response.read()
                    return json.loads(body) if body else {}
            except HTTPError as exc:
                if quiet and exc.code in {404, 409, 410}:
                    return {}
                raise TelephonyInError(f"Asterisk ARI a refusé la requête ({exc.code}).") from exc
            except Exception as exc:
                raise TelephonyInError("Asterisk ARI est injoignable.") from exc

        return await asyncio.get_running_loop().run_in_executor(None, call)

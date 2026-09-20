#!/usr/bin/env python3
"""F9.10: contract, Asterisk event handling, and both runtime modes."""

from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import shutil
import struct
import subprocess

ROOT = Path(__file__).resolve().parents[3]
sys_path = [str(ROOT), str(ROOT / "tests")]
for item in reversed(sys_path):
    if item not in __import__("sys").path:
        __import__("sys").path.insert(0, item)
import sys

from blocs.telephony.block import DEFAULTS, TelephonyBlock, config, _event, _rtp_payload
from blocs.display.block import DisplayBlock
from bloxsmith_app.block_api import BlockRuntimeContext
from bloxsmith_app.block_runtime import BlockRuntimePreparationContext
from bloxsmith_app.graph import WorkflowGraph
from bloxsmith_app.orchestrator import WorkflowOrchestrator
from bloxsmith_app.secrets import SecretManager
from block_test_fixtures import instance_scope
from tests.ui_smoke_common import (
    create_run_api, expect, graph_payload, isolated_server, wait_for_run_terminal,
)

REF = "secret://workspace/telephony-ari"


def until(predicate, message, timeout=8):
    """Wait for an asynchronous graph/state condition with an explicit deadline."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(message)


def ports(block=TelephonyBlock()):
    inputs = tuple(SimpleNamespace(**item) for item in block.model["ports"]["inputs"])
    outputs = tuple(SimpleNamespace(**item) for item in block.model["ports"]["outputs"])
    return inputs, outputs


def runtime_context(mode="centralized", overrides=None, services=None):
    block = TelephonyBlock()
    inputs, outputs = ports(block)
    return BlockRuntimeContext(
        run_id="run-telephony", node_id="tele", kind=block.kind,
        title=block.default_title(), config={**DEFAULTS, **(overrides or {})},
        input_ports=inputs, output_ports=outputs, runtime_mode=mode,
        services=services or {}, root_dir=ROOT,
    )


def preparation_context(mode="zeromq_active", overrides=None):
    block = TelephonyBlock()
    inputs, outputs = ports(block)
    return BlockRuntimePreparationContext(
        run_id="run-telephony", node_id="tele", kind=block.kind,
        title=block.default_title(), config={**DEFAULTS, **(overrides or {})},
        input_ports=inputs, output_ports=outputs, project_settings={},
        runtime_mode=mode,
    )


def test_contract_config_ports_ui():
    """FB3/FB4/FB7: validate settings, fixed ports, rendering and secret handling."""
    assert config({"ari_password_ref": "secret://x"})["ari_password_ref"] == "secret://x"
    assert config({"expected_context": " from-ovh "})["expected_context"] == "from-ovh"
    for values in ({"ari_base_url": "ftp://bad"}, {"ari_app": ""}, {"max_calls": 0},
                   {"ffmpeg_chunk_ms": 19}, {"media_port": 65536}, {"ari_password_ref": "a\nb"},
                   {"auto_answer": "false"}, {"capture_audio": 1}):
        try:
            config({**DEFAULTS, "ari_password_ref": "secret://x", **values})
        except Exception:
            pass
        else:
            raise AssertionError(f"Invalid setting accepted: {values}")

    block = TelephonyBlock()
    assert block.model["version"] == "0.0.1"
    command_port = next(port for port in block.model["ports"]["outputs"] if port["name"] == "command_out")
    assert command_port["id"] == 3 and command_port["transport"] == "message"
    assert block.prepare_runtime(preparation_context("centralized")).listen_on_run is False
    assert block.prepare_runtime(preparation_context("zeromq_active")).listen_on_run is True
    assert "node_card" not in block.model, "Telephony must use the renderer's standard node geometry"
    # Release assets are scoped to the declared version; a stale scope silently applies to nothing.
    scope = f'[data-block-release="telephony@{block.model["version"]}"]'
    block_css = (Path(__file__).parents[1] / "assets/css/block_ui.css").read_text(encoding="utf-8")
    assert scope in block_css, "Release CSS must be scoped to the declared block version"
    for declared in block.model["ui_assets"].values():
        for asset in declared:
            assert (Path(__file__).parents[1] / asset["path"]).is_file(), asset["path"]
    bad = preparation_context("centralized", {"ari_app": ""})
    try:
        block.prepare_runtime(bad)
    except Exception as exc:
        assert "application" in str(exc).lower()
    else:
        raise AssertionError("Preparation must reject an invalid ARI app before Run.")

    # Altered port identities reject before any ARI or media side effect.
    altered = runtime_context()
    altered.output_ports = (SimpleNamespace(id=1, name="wrong", transport="message"),)
    result = block.execute_runtime(altered)
    assert result.status == "failed" and "event_out" in result.error
    altered = runtime_context()
    altered.input_ports = (SimpleNamespace(id=1, name="wrong", transport="audio_stream"),)
    result = block.execute_runtime(altered)
    assert result.status == "failed" and "audio_in" in result.error
    from blocs.telephony.block import MEDIA_FORMAT, MEDIA_PAYLOAD_TYPE, MEDIA_FRAME_BYTES
    # Asterisk drops a dynamic payload type it never negotiated, so the leg stays on slin.
    assert (MEDIA_FORMAT, MEDIA_PAYLOAD_TYPE, MEDIA_FRAME_BYTES) == ("slin", 11, 320)
    playback_port = next(port for port in block.model["ports"]["inputs"] if port["name"] == "audio_in")
    assert playback_port["transport"] == "audio_stream" and playback_port["required"] is False
    # The playback port must accept what the audio producers of the workspace emit.
    assert set(playback_port["audio_stream"]["codecs"]) >= {"opus", "pcm_s16le"}

    node = block.build_node_payload(node_id="tele")
    modal = block.render_modal(node=node)["html"]
    inspector = block.render_inspector_panel(node=node)["html"]
    card = block.render_node_card(node=node)["html"]
    assert 'data-block-title-field' in modal, "Modal title must use the generic editable binding."
    assert modal.count("data-block-apply") == 1, "Exactly one Apply control can own the dirty state."
    assert 'data-block-modal-apply' not in modal, "Legacy apply binding must not return."
    assert 'placeholder="secret://workspace/asterisk_secret"' in modal
    assert 'type="password"' not in modal, "The vault reference is not itself a secret and stays visible."
    assert 'data-block-skip-empty' not in modal, "Users must be able to clear the secret reference."
    secret_ref = 'secret://workspace/telephony-demo'
    node["config"]["ari_password_ref"] = secret_ref
    modal = block.render_modal(node=node)["html"]
    assert f'value="{secret_ref}"' in modal, "Existing vault reference must render in clear text."
    assert "Ports" in inspector and "bloxsmith" in card
    assert "raw password" not in modal
    # The card reuses the shared canvas chrome so it stays homogeneous with standard blocks.
    for markup in ("node-head", "node-type-pill", "<h3>", "node-owned-card-preview"):
        assert markup in card, f"Canvas card must use the shared {markup} chrome"


def test_ui_surfaces_expose_every_setting():
    """FB7: modal and control panel expose every editable attribute and survive bad values."""
    block = TelephonyBlock()
    node = block.build_node_payload(node_id="tele-ui")
    surfaces = {
        "modal": block.render_modal(node=node)["html"],
        "inspector": block.render_inspector_panel(node=node)["html"],
    }
    bounds = {"max_calls": ('min="1"', 'max="8"'), "media_port": ('min="0"', 'max="65535"'),
              "ffmpeg_chunk_ms": ('min="20"', 'max="1000"')}
    for name, html in surfaces.items():
        assert "{{" not in html, f"Unreplaced placeholder in the {name} surface"
        for key in DEFAULTS:
            assert f'data-block-config-field="{key}"' in html, f"{key} is not editable in the {name}"
        for key, limits in bounds.items():
            assert all(limit in html for limit in limits), f"{key} bounds are missing in the {name}"
        for checkbox in ("auto_answer", "capture_audio"):
            assert f'data-block-config-field="{checkbox}" data-block-value-type="boolean" type="checkbox"' in html
        assert html.count("<label") >= len(DEFAULTS), f"Each control needs its own label in the {name}"
    assert "data-node-title-input" in surfaces["inspector"], "The control panel must rename its node"
    assert "data-block-apply" in surfaces["inspector"], "The control panel must apply its own edits"

    # An invalid stored value must keep every surface open, otherwise it cannot be corrected.
    broken = block.build_node_payload(node_id="tele-broken")
    broken["config"]["ffmpeg_chunk_ms"] = 5
    for html in (block.render_modal(node=broken)["html"], block.render_inspector_panel(node=broken)["html"]):
        assert 'class="field-hint is-error"' in html, "An invalid setting must be reported in place"
        assert 'value="5"' in html, "The rejected value stays visible so it can be corrected"
    assert block.render_node_card(node=broken)["html"], "The canvas card must survive an invalid setting"


def test_centralized_simulation():
    """FB6: centralized graph validates and skips without ARI or fabricated calls."""
    with isolated_server() as server:
        block = TelephonyBlock()
        node = block.build_node_payload(node_id="tele")
        document = graph_payload("Telephony centralized", [node], [])
        created = create_run_api(server, document, runtime_mode="centralized")
        state = wait_for_run_terminal(server, str(created.get("run_id") or ""))
        expect(state.get("status") == "success", f"Simulation failed: {state}")
        expect(not state.get("output_values"), "Simulation must not fabricate a live call event.")


def test_event_normalization_and_listener_emission():
    """FB1/FB3: normalize provider events and emit fixed JSON event output."""
    channel = {"id": "ch-1", "name": "PJSIP/ovh-1", "state": "Ring",
               "caller": {"number": "+33612345678", "name": "Test"},
               "dialplan": {"context": "from-ovh", "exten": "s"},
               "creationtime": "2026-09-18T10:00:00Z"}
    event = _event("call.incoming", channel, call_id="ari-ch-1", audio=False)
    assert event["provider"] == "asterisk" and event["from"] == "+33612345678"
    assert event["context"] == "from-ovh" and event["to"] == "s"

    block = TelephonyBlock()
    emitted = []
    context = SimpleNamespace(emit_result=emitted.append)
    client = AsyncRequestRecorder()
    config_value = config({**DEFAULTS, "capture_audio": False, "auto_answer": False,
                           "ari_password_ref": REF})
    asyncio.run(block._handle_event(context, client, config_value, {}, {
        "type": "StasisStart", "channel": channel,
    }, None))
    assert len(emitted) == 1 and emitted[0].outputs[0].port_id == 1
    payload = json.loads(emitted[0].outputs[0].value)
    assert payload["event"] == "call.incoming" and payload["channel_id"] == "ch-1"
    assert emitted[0].outputs[0].content_type == "application/json"
    assert not client.requests


class AsyncRequestRecorder:
    """Record ARI writes for event tests that disable media and answer."""

    def __init__(self):
        self.requests = []

    async def request(self, *args, **kwargs):
        self.requests.append((args, kwargs))
        if hasattr(self, "responses"):
            index = len(self.requests) - 1
            return self.responses[index] if index < len(self.responses) else {}
        return {}


def test_media_setup_failure_releases_transport():
    """FB5/FB9: a failed media setup closes local IO, reports call.failed and keeps running."""
    from blocs.telephony.block import _MediaSession, TelephonyError

    class FailingClient(AsyncRequestRecorder):
        async def request(self, *args, **kwargs):
            self.requests.append((args, kwargs))
            if args and args[1] == "/channels/externalMedia":
                raise TelephonyError("ARI external media refused")
            return {}

    async def scenario():
        block = TelephonyBlock()
        settings = config({**DEFAULTS, "ari_password_ref": REF, "capture_audio": True, "auto_answer": False})
        emitted = []
        context = SimpleNamespace(emit_result=emitted.append)
        sessions = {}
        module = __import__("blocs.telephony.block", fromlist=["_AudioEncoder"])
        original = module._AudioEncoder
        module._AudioEncoder = FakeEncoder
        try:
            await block._handle_event(
                context, FailingClient(), settings, sessions,
                {"type": "StasisStart", "channel": {"id": "fail-call", "caller": {}, "dialplan": {}}},
                FakeAudioClient(),
            )
        finally:
            module._AudioEncoder = original
        assert not sessions, "Failed call must not remain active"
        events = [json.loads(result.outputs[0].value) for result in emitted if result.outputs]
        assert [event["event"] for event in events] == ["call.incoming", "call.failed"]
        assert events[-1]["reason"] == "media_setup" and events[-1]["audio"] is False
        # The framework stops the worker on the first failed listener result, so one
        # broken call must be reported as a call event and a degraded state instead.
        assert all(result.status != "failed" for result in emitted), "One call must not fail the node"
        assert emitted[-1].metadata["telephony"]["state"] == "call_failed"

    asyncio.run(scenario())


def test_real_opus_encoder():
    """FB2: RTP's big-endian linear PCM becomes decodable Ogg/Opus audio."""
    if shutil.which("ffmpeg") is None:
        raise AssertionError("FFmpeg is required by the telephony audio contract.")
    from blocs.telephony.block import _AudioEncoder

    expected = [int(10000 * math.sin(2 * math.pi * 440 * index / 8000)) for index in range(1600)]
    pcm = b"".join(sample.to_bytes(2, "big", signed=True) for sample in expected)

    async def scenario():
        encoder = await _AudioEncoder.create(20)
        chunks = []
        for offset in range(0, len(pcm), 320):
            chunks.extend(await encoder.feed(pcm[offset:offset + 320]))
        chunks.extend(await encoder.close())
        return b"".join(chunks)

    encoded = asyncio.run(scenario())
    assert encoded.startswith(b"OggS"), "Encoder must emit an Ogg container"
    decoded = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "ogg", "-i", "pipe:0",
         "-f", "s16le", "-ar", "8000", "-ac", "1", "pipe:1"],
        input=encoded, capture_output=True, timeout=10, check=True,
    ).stdout
    actual = struct.unpack("<%dh" % (len(decoded) // 2), decoded)
    assert len(actual) >= len(expected), "Encoded call audio must cover the source duration"
    assert max(abs(left - right) for left, right in zip(expected, actual)) < 1000, \
        "Asterisk slin16 RTP payload must be decoded as big-endian PCM"


def test_active_graph_with_fake_ari():
    """FB1/FB4/FB8: Active Runtime emits a real ARI WebSocket event to Display."""
    from websockets.sync.server import serve

    channel = {"id": "ari-graph-call", "name": "PJSIP/ovh", "state": "Ring",
               "caller": {"number": "+33698765432"}, "dialplan": {"context": "from-ovh", "exten": "s"}}
    opened = False
    release_event = __import__("threading").Event()

    def ari_handler(websocket):
        nonlocal opened
        opened = True
        release_event.wait(timeout=5)
        websocket.send(json.dumps({"type": "StasisStart", "channel": channel}))
        try:
            for _ in websocket:
                pass
        except Exception:
            pass

    server = serve(ari_handler, "127.0.0.1", 0)
    server_thread = __import__("threading").Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    port = server.socket.getsockname()[1]
    try:
        with tempfile.TemporaryDirectory(prefix="telephony-active-") as temporary:
            root = Path(temporary)
            wallet = SecretManager(root / "secrets")
            wallet.initialize("test-wallet-password")
            wallet.set_secret(ref=REF, value="ari-test-password")
            engine = WorkflowOrchestrator(root_dir=root, runs_dir=root / "runs", secret_manager=wallet)
            tele = TelephonyBlock().build_node_payload(node_id="tele", config_overrides={
                "ari_base_url": f"http://127.0.0.1:{port}", "ari_password_ref": REF,
                "ari_app": "bloxsmith", "capture_audio": False, "auto_answer": False,
            })
            display = DisplayBlock().build_node_payload(node_id="display")
            payload = graph_payload("Telephony active", [tele, display], [
                {"id": "event", "kind": "data", "from": {"node": "tele", "port": 1},
                 "to": {"node": "display", "port": 1}}
            ])
            graph = WorkflowGraph.from_payload({**payload, "edges": [
                {"id": edge["id"], "kind": edge["kind"], "fromNodeId": edge["from"]["node"],
                 "fromPortId": edge["from"]["port"], "toNodeId": edge["to"]["node"],
                 "toPortId": edge["to"]["port"]} for edge in payload["edges"]
            ]})
            run = engine.prepare_active_run(graph, run_data_scope=instance_scope(root, payload))
            assert run.status == "prepared", run.logs
            played = engine.play_active_run(run.run_id)
            assert played.status == "running"
            time.sleep(0.2)
            release_event.set()
            until(lambda: run.output_values.get("tele:1", {}).get("value"), "ARI event did not reach the graph")
            event = json.loads(run.output_values["tele:1"]["value"])
            assert event["event"] == "call.incoming" and event["channel_id"] == "ari-graph-call"
            assert event["from"] == "+33698765432" and event["audio"] is False
            until(lambda: run.results.get("display", {}).get("display_received_count") == 1,
                  "ARI event did not execute Display")
            stopped = engine.stop_active_run(run.run_id, wait_timeout_sec=8)
            assert stopped.status in {"cancelled", "success"}
            assert opened
    finally:
        release_event.set()
        server.shutdown()
        server_thread.join(timeout=2)


class FakeAudioClient:
    """Capture Runtime Audio Streams port publications."""

    def __init__(self):
        self.available = True
        self.publications = []

    def publish_port(self, port, payload, **metadata):
        self.publications.append((port, payload, metadata))
        return "frame-id"


class FakeEncoder:
    """Deterministic encoder stand-in for external-media pump tests."""

    def __init__(self):
        self.payloads = []
        self.closed = False

    @classmethod
    async def create(cls, chunk_ms):
        assert chunk_ms == 100
        return cls()

    async def feed(self, payload):
        self.payloads.append(payload)
        return [b"ogg-frame"]

    async def close(self):
        self.closed = True
        return []


def test_rtp_payload_skips_extension_header():
    """FB5: RTP extension headers must not be interpreted as call audio."""
    packet = (
        bytes([0x90, 118, 0, 3, 0, 0, 0, 1, 0, 0, 0, 2]) +
        bytes([0xbe, 0xde, 0, 1, 0, 0, 0, 0]) +
        b"audio-payload"
    )
    assert _rtp_payload(packet) == b"audio-payload"


def test_correlated_audio_commands():
    """FB2/FB8: media start/stop emits exact, correlated STT command counters."""
    from blocs.telephony.block import _MediaSession

    async def scenario():
        block = TelephonyBlock()
        client = AsyncRequestRecorder()
        client.responses = [{"id": "external-1"}, {"id": "bridge-1"}]
        audio = FakeAudioClient()
        settings = config({**DEFAULTS, "ari_password_ref": REF, "capture_audio": True, "auto_answer": False})
        emitted = []
        context = SimpleNamespace(emit_result=emitted.append)
        sessions = {}
        module = __import__("blocs.telephony.block", fromlist=["_AudioEncoder"])
        original = module._AudioEncoder
        module._AudioEncoder = FakeEncoder
        channel = {"id": "command-call", "caller": {}, "dialplan": {}}
        try:
            await block._handle_event(context, client, settings, sessions, {
                "type": "StasisStart", "channel": channel,
            }, audio)
            session = sessions["command-call"]
            assert session.command_started and session.transport is not None
            port = session.transport.get_extra_info("socket").getsockname()[1]
            packet = bytes([0x80, 0x00, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1]) + b"pcm-sample"
            with __import__("socket").socket(__import__("socket").AF_INET, __import__("socket").SOCK_DGRAM) as sender:
                sender.sendto(packet, ("127.0.0.1", port))
            end = time.monotonic() + 2
            while not audio.publications and time.monotonic() < end:
                await asyncio.sleep(0.01)
            assert audio.publications, "Audio was not published for the command session"
            end = time.monotonic() + 2
            while session.rtp_sent_packet_count < 1 and time.monotonic() < end:
                await asyncio.sleep(0.01)
            assert session.rtp_sent_packet_count >= 1, "Return RTP cadence did not start"
            await block._handle_event(context, client, settings, sessions, {
                "type": "StasisEnd", "channel": channel,
            }, audio)
            assert not sessions
            commands = [result for result in emitted
                        if result.outputs and result.outputs[0].port_id == 3]
            assert len(commands) == 2
            start = json.loads(commands[0].outputs[0].value)
            stop = json.loads(commands[1].outputs[0].value)
            assert start == {"action": "start", "stream_id": "ari-command-call"}
            assert stop["action"] == "stop" and stop["stream_id"] == "ari-command-call"
            assert stop["frame_count"] == len(audio.publications) >= 1
            assert stop["byte_count"] == sum(len(item[1]) for item in audio.publications)
            assert stop["aborted"] is False
            events = [json.loads(result.outputs[0].value) for result in emitted
                      if result.outputs and result.outputs[0].port_id == 1]
            ended = events[-1]
            assert ended["rtp_packets"] == 1
            assert ended["rtp_bytes"] == len(b"pcm-sample")
            assert ended["rtp_return_packets"] >= 1
            for command in commands:
                assert command.outputs[0].port_name == "command_out"
                assert command.outputs[0].content_type == "application/json"
        finally:
            module._AudioEncoder = original
            await session.close()

    asyncio.run(scenario())


def test_audio_media_attach_publish_and_release():
    """FB2/FB5: external media, RTP pumping, Opus publication and cleanup work."""
    from blocs.telephony.block import _MediaSession

    async def scenario():
        client = AsyncRequestRecorder()
        client.responses = [{"id": "external-1"}, {"id": "bridge-1"}]
        audio = FakeAudioClient()
        block = TelephonyBlock()
        settings = config({**DEFAULTS, "ari_password_ref": REF})
        session = _MediaSession(call_id="call-1", channel_id="caller-1")
        module = __import__("blocs.telephony.block", fromlist=["_AudioEncoder"])
        original = module._AudioEncoder
        module._AudioEncoder = FakeEncoder
        try:
            await block._start_media(client, settings, session, audio)
            assert session.transport is not None
            port = session.transport.get_extra_info("socket").getsockname()[1]
            packet = bytes([0x80, 118, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1]) + bytes(640)
            socket = __import__("socket")
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
                sender.settimeout(2)
                sender.sendto(packet, ("127.0.0.1", port))
                loop = asyncio.get_running_loop()
                first, _ = await loop.run_in_executor(None, sender.recvfrom, 65536)
                assert first[0] & 0xc0 == 0x80, "Return RTP must use version 2"
                assert first[1] == 0x80 | 118, "The first return RTP packet must set the marker bit"
                assert int.from_bytes(first[2:4], "big") == 0
                assert int.from_bytes(first[4:8], "big") == 0
                assert len(first) == len(packet), "Return RTP must preserve the negotiated payload size"

                sender.sendto(bytes([0x80, 118, 0, 2, 0, 0, 1, 64, 0, 0, 0, 1]) + bytes(640), ("127.0.0.1", port))
                second, _ = await loop.run_in_executor(None, sender.recvfrom, 65536)
                assert second[1] == 118, "Only the first return RTP packet may set the marker"
                assert int.from_bytes(second[2:4], "big") == 1
                assert int.from_bytes(second[4:8], "big") == 320, "Timestamps advance by one sample per byte pair"

                keepalive, _ = await loop.run_in_executor(None, sender.recvfrom, 65536)
                assert keepalive[1] == 118 and int.from_bytes(keepalive[2:4], "big") == 2, \
                    "RTP silence must continue when Asterisk intake pauses"
            assert session.rtp_packet_count == 2
            end = time.monotonic() + 2
            while len(audio.publications) < 2 and time.monotonic() < end:
                await asyncio.sleep(0.01)
            assert audio.publications, "RTP payload was not published as audio"
            await session.close()
            await asyncio.sleep(0)
            assert audio.publications[0][0] == "audio_out"
            assert audio.publications[0][1] == b"ogg-frame"
            assert audio.publications[0][2]["codec"] == "opus"
            assert audio.publications[0][2]["sample_rate_hz"] == 48000
            assert audio.publications[0][2]["channels"] == 1
            assert audio.publications[0][2]["sequence"] == 1
            await session.finish_media()
            assert session.frame_count == len(audio.publications)
            assert session.byte_count == sum(len(item[1]) for item in audio.publications)
            paths = [args[0][1] for args in client.requests]
            assert paths == ["/channels/externalMedia", "/bridges", "/bridges/bridge-1/addChannel",
                             "/bridges/bridge-1/play"]
            assert client.requests[-1][1]["data"]["media"] == "sound:silence/1"
        finally:
            module._AudioEncoder = original
            await session.close()
    asyncio.run(scenario())


def test_proactive_rtp_before_inbound_media():
    """FB5: external-media return RTP starts before Asterisk sends inbound audio."""
    from blocs.telephony.block import _MediaSession

    async def scenario():
        socket = __import__("socket")
        client = AsyncRequestRecorder()
        audio = FakeAudioClient()
        block = TelephonyBlock()
        settings = config({**DEFAULTS, "ari_password_ref": REF})
        session = _MediaSession(call_id="call-proactive", channel_id="caller-proactive")
        module = __import__("blocs.telephony.block", fromlist=["_AudioEncoder"])
        original = module._AudioEncoder
        module._AudioEncoder = FakeEncoder
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as asterisk_media:
            asterisk_media.bind(("127.0.0.1", 0))
            asterisk_media.settimeout(2)
            host, port = asterisk_media.getsockname()
            client.responses = [{
                "id": "external-proactive",
                "channelvars": {
                    "UNICASTRTP_LOCAL_ADDRESS": host,
                    "UNICASTRTP_LOCAL_PORT": str(port),
                },
            }, {"id": "bridge-proactive"}]
            try:
                await block._start_media(client, settings, session, audio)
                loop = asyncio.get_running_loop()
                first, _ = await loop.run_in_executor(None, asterisk_media.recvfrom, 65536)
                assert first[0] & 0xc0 == 0x80
                assert first[1] == 0x80 | 11, "Proactive slin RTP uses the static payload type 11"
                assert len(first) == 332, "Proactive RTP sends one 20 ms slin frame"
                second, _ = await loop.run_in_executor(None, asterisk_media.recvfrom, 65536)
                assert second[1] == 11, "Only the first proactive RTP packet sets the marker"
                assert int.from_bytes(second[4:8], "big") == 160
            finally:
                module._AudioEncoder = original
                await session.close()

    asyncio.run(scenario())


def test_return_rtp_cadence_survives_inbound_audio():
    """FB5: the return cadence keeps its own pace while Asterisk sends call audio."""
    from blocs.telephony.block import _MediaSession

    async def scenario():
        socket = __import__("socket")
        client = AsyncRequestRecorder()
        audio = FakeAudioClient()
        block = TelephonyBlock()
        settings = config({**DEFAULTS, "ari_password_ref": REF})
        session = _MediaSession(call_id="call-cadence", channel_id="caller-cadence")
        module = __import__("blocs.telephony.block", fromlist=["_AudioEncoder"])
        original = module._AudioEncoder
        module._AudioEncoder = FakeEncoder
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as asterisk_media:
            asterisk_media.bind(("127.0.0.1", 0))
            asterisk_media.setblocking(False)
            host, port = asterisk_media.getsockname()
            client.responses = [{
                "id": "external-cadence",
                "channelvars": {"UNICASTRTP_LOCAL_ADDRESS": host, "UNICASTRTP_LOCAL_PORT": str(port)},
            }, {"id": "bridge-cadence"}]
            try:
                await block._start_media(client, settings, session, audio)
                inbound_addr = session.transport.get_extra_info("socket").getsockname()
                # One 20 ms slin16 frame, delivered far faster than the 20 ms return period so
                # the media pump never observes an idle queue.
                frame = bytes((0x80, 11, 0, 1, 0, 0, 0, 0, 0, 0, 0, 1)) + bytes(320)
                returned, deadline = 0, time.monotonic() + 0.3
                while time.monotonic() < deadline:
                    # Asterisk sends and receives on one socket; the block answers the
                    # address it observes, so the test must use symmetric RTP too.
                    asterisk_media.sendto(frame, inbound_addr)
                    await asyncio.sleep(0.001)
                    while True:
                        try:
                            asterisk_media.recv(65536)
                        except BlockingIOError:
                            break
                        returned += 1
                assert session.rtp_packet_count > 0, "Inbound call audio must still reach the pump"
                assert audio.publications, "Inbound call audio must still be published"
                assert returned >= 8, f"Return RTP stalled under inbound audio: {returned} packets"
            finally:
                module._AudioEncoder = original
                await session.close()

    asyncio.run(scenario())


def test_external_media_channel_is_not_taken_for_a_call():
    """FB1/FB8: the block never answers its own external media channels as new calls."""
    from blocs.telephony.block import _MediaSession

    block = TelephonyBlock()
    client = AsyncRequestRecorder()
    emitted = []
    context = SimpleNamespace(emit_result=emitted.append)
    # A second allowed call is what exposes the loop: the media channel of the first one
    # joins the same Stasis application and is announced exactly like an inbound call.
    settings = config({**DEFAULTS, "ari_password_ref": REF, "max_calls": 2, "capture_audio": False})
    sessions = {"caller-1": _MediaSession(call_id="ari-caller-1", channel_id="caller-1",
                                          external_channel_id="external-1")}
    for channel in ({"id": "external-1", "name": "Announcer/ARI"},
                    {"id": "not-yet-known", "name": "UnicastRTP/127.0.0.1:41000-00000002"}):
        asyncio.run(block._handle_event(context, client, settings, sessions, {
            "type": "StasisStart", "channel": channel,
        }, None))
    assert set(sessions) == {"caller-1"}, "A media channel must not open a call session"
    assert not emitted, "A media channel must not emit a call event"
    assert not client.requests, "A media channel must never be answered"

    # A real caller is still accepted while that first session is active.
    asyncio.run(block._handle_event(context, client, settings, sessions, {
        "type": "StasisStart", "channel": {"id": "caller-2", "name": "PJSIP/ovh-2"},
    }, None))
    assert set(sessions) == {"caller-1", "caller-2"}, "A second real call must still be accepted"
    assert json.loads(emitted[0].outputs[0].value)["channel_id"] == "caller-2"


def test_sessions_use_distinct_ssrc():
    """FB5: every call owns its RTP synchronization source, as RFC 3550 requires."""
    from blocs.telephony.block import _MediaSession

    sources = {_MediaSession(call_id=f"call-{index}", channel_id=f"chan-{index}").rtp_ssrc
               for index in range(5)}
    assert len(sources) == 5, "Concurrent calls must not share one SSRC"


class FakeWebSocket:
    """Deliver scripted ARI frames, then raise or idle like a real connection."""

    def __init__(self, frames, error=None):
        self.frames = list(frames)
        self.error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def recv(self):
        if self.frames:
            return self.frames.pop(0)
        if self.error is not None:
            raise self.error
        await asyncio.sleep(0.05)
        raise TimeoutError


def listener_context(stop_after=2, services=None):
    """Build a listener context that stops after a bounded number of stop checks."""
    state = {"checks": 0}
    emitted = []

    def stop_requested():
        state["checks"] += 1
        return state["checks"] > stop_after

    return SimpleNamespace(emit_result=emitted.append, stop_requested=stop_requested,
                           services=services or {"resolve_secret": lambda ref: "ari-secret"}), emitted


def test_listener_reconnects_after_a_dropped_connection():
    """FB9: a lost ARI connection is retried instead of stopping the worker."""
    import websockets.asyncio.client as ws_client

    block = TelephonyBlock()
    settings = config({**DEFAULTS, "ari_password_ref": REF, "capture_audio": False})
    context, emitted = listener_context(stop_after=400)
    attempts = []

    def fake_connect(url, **kwargs):
        attempts.append(url)
        if len(attempts) == 1:
            raise OSError("connection reset by peer")
        if len(attempts) == 2:
            return FakeWebSocket([], error=OSError("connection closed"))
        context.stop_requested = lambda: True
        return FakeWebSocket([])

    original, module = ws_client.connect, __import__("blocs.telephony.block", fromlist=["block"])
    ws_client.connect = fake_connect
    module.RECONNECT_MIN_DELAY = 0.01
    module.RECONNECT_MAX_DELAY = 0.02
    try:
        asyncio.run(block._listen(context, settings))
    finally:
        ws_client.connect = original
        module.RECONNECT_MIN_DELAY, module.RECONNECT_MAX_DELAY = 1.0, 30.0

    assert len(attempts) >= 3, "The listener must keep retrying after a dropped connection"
    states = [result.metadata["telephony"]["state"] for result in emitted]
    assert "reconnecting" in states and states.count("connected") >= 1
    assert all(result.status != "failed" for result in emitted), \
        "A reconnection must not report a failed result, which would stop the worker"


def test_transient_answer_failure_releases_the_caller():
    """FB9: an ARI error while answering fails one call and hangs up its channel."""
    from blocs.telephony.block import TelephonyError

    class AnswerFailure(AsyncRequestRecorder):
        async def request(self, *args, **kwargs):
            self.requests.append((args, kwargs))
            if args and args[1].endswith("/answer"):
                raise TelephonyError("Asterisk ARI is unreachable.")
            return {}

    block = TelephonyBlock()
    client = AnswerFailure()
    emitted = []
    context = SimpleNamespace(emit_result=emitted.append)
    settings = config({**DEFAULTS, "ari_password_ref": REF, "capture_audio": False, "auto_answer": True})
    sessions = {}
    asyncio.run(block._handle_event(context, client, settings, sessions, {
        "type": "StasisStart", "channel": {"id": "answer-fail", "caller": {}, "dialplan": {}},
    }, None))

    assert not sessions, "A call that cannot be answered must not stay active"
    events = [json.loads(result.outputs[0].value) for result in emitted if result.outputs]
    assert [event["event"] for event in events] == ["call.incoming", "call.failed"]
    assert events[-1]["reason"] == "answer"
    assert ("DELETE", "/channels/answer-fail") in [args[:2] for args, _ in client.requests], \
        "The answered caller must be hung up instead of being left in silence"
    assert all(result.status != "failed" for result in emitted)


def test_stop_command_survives_a_cleanup_failure():
    """FB9: the audio consumer gets its stop command even if Asterisk cleanup fails."""
    from blocs.telephony.block import _MediaSession, TelephonyError

    class CleanupFailure(AsyncRequestRecorder):
        async def request(self, *args, **kwargs):
            self.requests.append((args, kwargs))
            if args and args[0] == "DELETE":
                raise TelephonyError("Asterisk ARI is unreachable.")
            return {}

    block = TelephonyBlock()
    emitted = []
    context = SimpleNamespace(emit_result=emitted.append)
    session = _MediaSession(call_id="ari-cleanup", channel_id="cleanup-call",
                            bridge_id="bridge-cleanup", external_channel_id="external-cleanup")
    session.command_started, session.frame_count, session.byte_count = True, 3, 120
    sessions = {"cleanup-call": session}
    settings = config({**DEFAULTS, "ari_password_ref": REF})
    asyncio.run(block._handle_event(context, CleanupFailure(), settings, sessions, {
        "type": "StasisEnd", "channel": {"id": "cleanup-call", "caller": {}, "dialplan": {}},
    }, None))

    payloads = [json.loads(result.outputs[0].value) for result in emitted if result.outputs]
    assert payloads[0] == {"action": "stop", "stream_id": "ari-cleanup", "frame_count": 3,
                           "byte_count": 120, "aborted": False}
    assert payloads[1]["event"] == "call.ended", "The end event must follow the stop command"
    assert not sessions


def test_missed_end_event_is_recovered_by_the_audit():
    """FB9: a call whose Asterisk channel disappeared is closed instead of leaking."""
    from blocs.telephony.block import _MediaSession

    class ChannelList(AsyncRequestRecorder):
        def __init__(self, channels):
            super().__init__()
            self.channels = channels

        async def request(self, *args, **kwargs):
            self.requests.append((args, kwargs))
            return self.channels if args and args[0] == "GET" else {}

    block = TelephonyBlock()
    emitted = []
    context = SimpleNamespace(emit_result=emitted.append)
    session = _MediaSession(call_id="ari-ghost", channel_id="ghost-call")
    session.rtp_dropped_packet_count = 4

    # An unreadable channel list must never be read as "every call ended".
    sessions = {"ghost-call": session}
    asyncio.run(block._audit_sessions(context, ChannelList({}), sessions, 0.0))
    assert sessions, "An ambiguous audit must leave live calls untouched"
    assert not emitted

    asyncio.run(block._audit_sessions(context, ChannelList([{"id": "other-call"}]), sessions, 0.0))
    assert not sessions, "A call whose channel is gone must be closed"
    ended = json.loads(emitted[-1].outputs[0].value)
    assert ended["event"] == "call.ended" and ended["recovered"] is True
    assert ended["rtp_dropped"] == 4, "Dropped inbound packets must be reported, not silent"


def test_encoder_failure_keeps_the_call_alive():
    """FB9: a dead encoder aborts one capture without tearing down the session."""
    from blocs.telephony.block import _MediaSession

    class BrokenEncoder(FakeEncoder):
        async def feed(self, payload):
            raise BrokenPipeError("ffmpeg died")

        async def close(self):
            raise BrokenPipeError("ffmpeg died")

    async def scenario():
        audio = FakeAudioClient()
        session = _MediaSession(call_id="ari-broken", channel_id="broken-call")
        queue = asyncio.Queue(maxsize=4)
        await queue.put((("127.0.0.1", 4000), 118, b"pcm"))
        pump = asyncio.create_task(TelephonyBlock._pump_media(session, BrokenEncoder(), queue, audio))
        await until_async(lambda: session.aborted, "The capture must report itself aborted")
        session.stopping = True
        await asyncio.wait_for(pump, timeout=2)
        assert session.aborted and not audio.publications

    asyncio.run(scenario())


async def until_async(predicate, message, timeout=2):
    """Await an in-process condition with an explicit deadline."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(message)


def test_event_filter_is_declared_and_optional():
    """FB9: the app subscribes to the two event types it uses, and tolerates a refusal."""
    import websockets.asyncio.client as ws_client
    from blocs.telephony.block import TelephonyError

    class Refusing(AsyncRequestRecorder):
        async def request(self, *args, **kwargs):
            self.requests.append((args, kwargs))
            raise TelephonyError("Asterisk ARI refused the request (404).")

    block = TelephonyBlock()
    settings = config({**DEFAULTS, "ari_password_ref": REF, "capture_audio": False, "ari_app": "bloxsmith"})

    def as_ari(recorder):
        recorder.websocket_url = lambda: "ws://127.0.0.1:8088/ari/events?app=bloxsmith"
        recorder.authorization = "Basic test"
        return recorder

    for client in (as_ari(AsyncRequestRecorder()), as_ari(Refusing())):
        context, emitted = listener_context(stop_after=400)
        context.stop_requested = lambda: False
        websocket = FakeWebSocket([], error=OSError("closed"))

        def fake_connect(url, **kwargs):
            return websocket

        original = ws_client.connect
        ws_client.connect = fake_connect
        try:
            asyncio.run(block._ari_session(fake_connect, context, client, settings, {}, None))
        except OSError:
            pass
        finally:
            ws_client.connect = original

        args, kwargs = client.requests[0]
        assert args == ("PUT", "/applications/bloxsmith/eventFilter"), args
        assert kwargs["body"] == {"allowed": [{"type": "StasisStart"}, {"type": "StasisEnd"}]}
        # A server that refuses the filter keeps sending everything; the session goes on.
        assert emitted and emitted[0].metadata["telephony"]["state"] == "connected"


def test_graph_audio_is_played_to_the_caller():
    """FB10: audio buffered for playback leaves on the call's return RTP leg."""
    from blocs.telephony.block import _MediaSession, _Playback

    async def scenario():
        socket = __import__("socket")
        client = AsyncRequestRecorder()
        audio = FakeAudioClient()
        block = TelephonyBlock()
        settings = config({**DEFAULTS, "ari_password_ref": REF})
        session = _MediaSession(call_id="call-play", channel_id="caller-play")
        playback = _Playback()
        answer = bytes(range(256)) * 10  # 2560 bytes: more than the prebuffer of three frames
        playback.buffer.extend(answer)
        module = __import__("blocs.telephony.block", fromlist=["_AudioEncoder"])
        original = module._AudioEncoder
        module._AudioEncoder = FakeEncoder
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as asterisk_media:
            asterisk_media.bind(("127.0.0.1", 0))
            asterisk_media.settimeout(2)
            host, port = asterisk_media.getsockname()
            client.responses = [{
                "id": "external-play",
                "channelvars": {"UNICASTRTP_LOCAL_ADDRESS": host, "UNICASTRTP_LOCAL_PORT": str(port)},
            }, {"id": "bridge-play"}]
            try:
                await block._start_media(client, settings, session, audio, playback)
                loop = asyncio.get_running_loop()
                packet, _ = await loop.run_in_executor(None, asterisk_media.recvfrom, 65536)
                assert packet[12:] == answer[:320], "The return leg must carry the graph audio"
                assert packet[1] & 0x7f == 11, "Playback keeps the negotiated payload type"
                await until_async(lambda: session.playback_frame_count >= 1, "Playback was not counted")
            finally:
                module._AudioEncoder = original
                await session.close()

        # Barge-in: an aborted answer is dropped instead of being played to its end.
        await playback.reset()
        assert not playback.buffer and not playback.playing and not playback.stream_id

    asyncio.run(scenario())


def test_playback_decodes_a_producer_stream():
    """FB10: an Ogg/Opus answer from a producer block becomes Asterisk slin16 frames."""
    if shutil.which("ffmpeg") is None:
        raise AssertionError("FFmpeg is required by the telephony audio contract.")
    from blocs.telephony.block import _Playback

    samples = [int(9000 * math.sin(2 * math.pi * 440 * index / 48000)) for index in range(48000)]
    source = b"".join(sample.to_bytes(2, "little", signed=True) for sample in samples)
    encoded = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "s16le", "-ar", "48000", "-ac", "1",
         "-i", "pipe:0", "-c:a", "libopus", "-b:a", "32000", "-f", "ogg", "pipe:1"],
        input=source, capture_output=True, timeout=20, check=True,
    ).stdout
    assert encoded.startswith(b"OggS")

    async def scenario():
        playback = _Playback()
        # One Ogg stream delivered like a producer publishes it, in successive frames.
        for offset in range(0, len(encoded), 4096):
            await playback.feed(SimpleNamespace(
                stream_id="tts-1", codec="opus", sample_rate_hz=48000, channels=1,
                payload=encoded[offset:offset + 4096]))
        async def decoded_enough():
            playback.drain()
            return len(playback.buffer) >= 320 * 3

        end = time.monotonic() + 5
        while time.monotonic() < end and not await decoded_enough():
            await asyncio.sleep(0.02)
        assert len(playback.buffer) >= 320 * 3, "Nothing was decoded for playback"
        frame = playback.take(320)
        assert frame is not None and len(frame) == 320, "Decoded audio must be served as 20 ms frames"
        assert frame != bytes(320), "Decoded playback must not be silence"
        await playback.reset()

    asyncio.run(scenario())


def test_ari_client_talks_to_a_real_server():
    """FB3/FB9: the ARI client performs real requests, with and without a JSON body.

    Every other test fakes this client, so a defect here reaches production unseen:
    one regression made every ARI call fail before any HTTP traffic happened.
    """
    from blocs.telephony.block import _AriClient, TelephonyError
    import http.server
    import threading

    received = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _serve(self):
            length = int(self.headers.get("Content-Length") or 0)
            received.append({
                "method": self.command, "path": self.path,
                "content_type": self.headers.get("Content-Type"),
                "body": self.rfile.read(length) if length else b"",
                "authorization": self.headers.get("Authorization"),
            })
            if "missing" in self.path:
                self.send_error(404)
                return
            if "broken" in self.path:
                self.send_error(500)
                return
            payload = json.dumps({"id": "channel-1"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_PUT = do_POST = do_DELETE = _serve

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    client = _AriClient(config({**DEFAULTS, "ari_password_ref": REF,
                                "ari_base_url": f"http://{host}:{port}"}), "secret")

    async def scenario():
        assert (await client.request("POST", "/channels/abc/answer"))["id"] == "channel-1"
        await client.request("PUT", "/applications/bloxsmith/eventFilter",
                             body={"allowed": [{"type": "StasisStart"}]})
        assert await client.request("DELETE", "/channels/missing", quiet=True) == {}
        for path, expected in (("/channels/missing", "404"), ("/channels/broken", "500")):
            try:
                await client.request("POST", path)
            except TelephonyError as error:
                assert expected in str(error)
            else:
                raise AssertionError(f"{path} must report the ARI status")

    try:
        asyncio.run(scenario())
    finally:
        server.shutdown()
        server.server_close()

    answer, event_filter = received[0], received[1]
    assert answer["method"] == "POST" and answer["path"] == "/ari/channels/abc/answer"
    assert answer["authorization"].startswith("Basic ") and not answer["body"]
    assert event_filter["content_type"] == "application/json"
    assert json.loads(event_filter["body"]) == {"allowed": [{"type": "StasisStart"}]}

    unreachable = _AriClient(config({**DEFAULTS, "ari_password_ref": REF,
                                     "ari_base_url": "http://127.0.0.1:1"}), "secret")
    try:
        asyncio.run(unreachable.request("GET", "/channels"))
    except TelephonyError as error:
        assert "unreachable" in str(error)
    else:
        raise AssertionError("An unreachable ARI service must be reported as such")


def test_caller_filter_accepts_only_the_listed_numbers():
    """FB11: an allowed-caller list is normalized, and it gates the calls that are answered."""
    from blocs.telephony.block import caller_allowed, caller_number

    # One line is written in several ways; the comparison reduces them to a single form.
    assert caller_number("+33 6 12.34-56 78") == "+33612345678"
    assert caller_number("0033612345678") == "+33612345678"
    assert caller_number("") == ""

    # The list accepts commas, semicolons and spaces, and drops duplicates.
    normalized = config({**DEFAULTS, "ari_password_ref": REF,
                         "allowed_callers": " +33 612 345 678 ; 0033698765432, +33612345678 "})
    assert normalized["allowed_callers"] == "+33612345678, +33698765432"
    assert config({**DEFAULTS, "ari_password_ref": REF})["allowed_callers"] == ""

    too_many = ", ".join(f"+3360000{index:04d}" for index in range(65))
    for invalid in ("+33612345678, agent", "12", too_many):
        try:
            config({**DEFAULTS, "ari_password_ref": REF, "allowed_callers": invalid})
        except Exception:
            pass
        else:
            raise AssertionError(f"Invalid list accepted: {invalid[:40]}")

    allowed = normalized["allowed_callers"]
    assert caller_allowed("0033612345678", allowed), "The 00 form must match the + form"
    assert caller_allowed("0612345678", allowed), "A national spelling must match the international one"
    assert not caller_allowed("+33611111111", allowed), "A number that is not listed is refused"
    assert not caller_allowed("", allowed), "A withheld caller is refused when a list is set"
    assert not caller_allowed("345678", allowed), "A fragment that is too short must never match"

    block = TelephonyBlock()
    settings = config({**DEFAULTS, "ari_password_ref": REF, "allowed_callers": "+33612345678"})
    channel = {"id": "ch-filter", "caller": {"number": "0033612345678"}, "dialplan": {}}
    assert block._matches(channel, settings)
    assert not block._matches({**channel, "caller": {"number": "+33600000000"}}, settings)
    assert not block._matches({**channel, "caller": {}}, settings)
    # With no list, the filter lets every caller through.
    assert block._matches({**channel, "caller": {}}, config({**DEFAULTS, "ari_password_ref": REF}))


def test_rejected_caller_never_reaches_the_graph():
    """FB11: a filtered call emits nothing and is never answered."""
    block = TelephonyBlock()
    client = AsyncRequestRecorder()
    emitted = []
    context = SimpleNamespace(emit_result=emitted.append)
    settings = config({**DEFAULTS, "ari_password_ref": REF, "capture_audio": False,
                       "allowed_callers": "+33612345678"})
    sessions = {}
    asyncio.run(block._handle_event(context, client, settings, sessions, {
        "type": "StasisStart",
        "channel": {"id": "unwanted", "name": "PJSIP/ovh-9", "caller": {"number": "0033699999999"},
                    "dialplan": {}},
    }, None))
    assert not sessions and not emitted, "A caller that is not allowed must produce nothing"
    assert not client.requests, "A caller that is not allowed must never be answered"


def main():
    test_contract_config_ports_ui()
    test_caller_filter_accepts_only_the_listed_numbers()
    test_rejected_caller_never_reaches_the_graph()
    test_ui_surfaces_expose_every_setting()
    test_centralized_simulation()
    test_event_normalization_and_listener_emission()
    test_correlated_audio_commands()
    test_audio_media_attach_publish_and_release()
    test_proactive_rtp_before_inbound_media()
    test_return_rtp_cadence_survives_inbound_audio()
    test_external_media_channel_is_not_taken_for_a_call()
    test_sessions_use_distinct_ssrc()
    test_listener_reconnects_after_a_dropped_connection()
    test_transient_answer_failure_releases_the_caller()
    test_stop_command_survives_a_cleanup_failure()
    test_missed_end_event_is_recovered_by_the_audit()
    test_encoder_failure_keeps_the_call_alive()
    test_event_filter_is_declared_and_optional()
    test_ari_client_talks_to_a_real_server()
    test_graph_audio_is_played_to_the_caller()
    test_playback_decodes_a_producer_stream()
    test_rtp_payload_skips_extension_header()
    test_media_setup_failure_releases_transport()
    test_real_opus_encoder()
    test_active_graph_with_fake_ari()
    print("[ok] F9.10_telephony_block")


if __name__ == "__main__":
    main()

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

from blocs.telephony_in.block import DEFAULTS, TelephonyInBlock, config, _event, _rtp_payload
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


def ports(block=TelephonyInBlock()):
    inputs = tuple(SimpleNamespace(**item) for item in block.model["ports"]["inputs"])
    outputs = tuple(SimpleNamespace(**item) for item in block.model["ports"]["outputs"])
    return inputs, outputs


def runtime_context(mode="centralized", overrides=None, services=None):
    block = TelephonyInBlock()
    inputs, outputs = ports(block)
    return BlockRuntimeContext(
        run_id="run-telephony", node_id="tele", kind=block.kind,
        title=block.default_title(), config={**DEFAULTS, **(overrides or {})},
        input_ports=inputs, output_ports=outputs, runtime_mode=mode,
        services=services or {}, root_dir=ROOT,
    )


def preparation_context(mode="zeromq_active", overrides=None):
    block = TelephonyInBlock()
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

    block = TelephonyInBlock()
    assert block.model["version"] == "0.0.1"
    command_port = next(port for port in block.model["ports"]["outputs"] if port["name"] == "command_out")
    assert command_port["id"] == 3 and command_port["transport"] == "message"
    assert block.prepare_runtime(preparation_context("centralized")).listen_on_run is False
    assert block.prepare_runtime(preparation_context("zeromq_active")).listen_on_run is True
    assert "node_card" not in block.model, "Telephony must use the renderer's standard node geometry"
    # Release assets are scoped to the declared version; a stale scope silently applies to nothing.
    scope = f'[data-block-release="telephony_in@{block.model["version"]}"]'
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

    # Altered output identities reject before any ARI or media side effect.
    altered = runtime_context()
    altered.output_ports = (SimpleNamespace(id=1, name="wrong", transport="message"),)
    result = block.execute_runtime(altered)
    assert result.status == "failed" and "fixed outputs" in result.error

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
    block = TelephonyInBlock()
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
        block = TelephonyInBlock()
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

    block = TelephonyInBlock()
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
    """FB5/FB8: failed Asterisk media setup closes local IO and emits call.failed."""
    from blocs.telephony_in.block import _MediaSession, TelephonyInError

    class FailingClient(AsyncRequestRecorder):
        async def request(self, *args, **kwargs):
            self.requests.append((args, kwargs))
            if args and args[1] == "/channels/externalMedia":
                raise TelephonyInError("ARI external media refused")
            return {}

    async def scenario():
        block = TelephonyInBlock()
        settings = config({**DEFAULTS, "ari_password_ref": REF, "capture_audio": True, "auto_answer": False})
        emitted = []
        context = SimpleNamespace(emit_result=emitted.append)
        sessions = {}
        module = __import__("blocs.telephony_in.block", fromlist=["_AudioEncoder"])
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
        assert emitted[-1].status == "failed" and "média" in emitted[-1].error

    asyncio.run(scenario())


def test_real_opus_encoder():
    """FB2: RTP's big-endian linear PCM becomes decodable Ogg/Opus audio."""
    if shutil.which("ffmpeg") is None:
        raise AssertionError("FFmpeg is required by the telephony audio contract.")
    from blocs.telephony_in.block import _AudioEncoder

    expected = [int(10000 * math.sin(2 * math.pi * 440 * index / 16000)) for index in range(3200)]
    pcm = b"".join(sample.to_bytes(2, "big", signed=True) for sample in expected)

    async def scenario():
        encoder = await _AudioEncoder.create(20)
        chunks = []
        for offset in range(0, len(pcm), 640):
            chunks.extend(await encoder.feed(pcm[offset:offset + 640]))
        chunks.extend(await encoder.close())
        return b"".join(chunks)

    encoded = asyncio.run(scenario())
    assert encoded.startswith(b"OggS"), "Encoder must emit an Ogg container"
    decoded = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "ogg", "-i", "pipe:0",
         "-f", "s16le", "-ar", "16000", "-ac", "1", "pipe:1"],
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
            tele = TelephonyInBlock().build_node_payload(node_id="tele", config_overrides={
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
    from blocs.telephony_in.block import _MediaSession

    async def scenario():
        block = TelephonyInBlock()
        client = AsyncRequestRecorder()
        client.responses = [{"id": "external-1"}, {"id": "bridge-1"}]
        audio = FakeAudioClient()
        settings = config({**DEFAULTS, "ari_password_ref": REF, "capture_audio": True, "auto_answer": False})
        emitted = []
        context = SimpleNamespace(emit_result=emitted.append)
        sessions = {}
        module = __import__("blocs.telephony_in.block", fromlist=["_AudioEncoder"])
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
    from blocs.telephony_in.block import _MediaSession

    async def scenario():
        client = AsyncRequestRecorder()
        client.responses = [{"id": "external-1"}, {"id": "bridge-1"}]
        audio = FakeAudioClient()
        block = TelephonyInBlock()
        settings = config({**DEFAULTS, "ari_password_ref": REF})
        session = _MediaSession(call_id="call-1", channel_id="caller-1")
        module = __import__("blocs.telephony_in.block", fromlist=["_AudioEncoder"])
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
                assert int.from_bytes(second[4:8], "big") == 320, "slin16 timestamps advance by one sample per byte pair"

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
    from blocs.telephony_in.block import _MediaSession

    async def scenario():
        socket = __import__("socket")
        client = AsyncRequestRecorder()
        audio = FakeAudioClient()
        block = TelephonyInBlock()
        settings = config({**DEFAULTS, "ari_password_ref": REF})
        session = _MediaSession(call_id="call-proactive", channel_id="caller-proactive")
        module = __import__("blocs.telephony_in.block", fromlist=["_AudioEncoder"])
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
                assert first[1] == 0x80 | 118, "Proactive slin16 RTP uses payload type 118"
                assert len(first) == 652, "Proactive RTP sends one 20 ms slin16 frame"
                second, _ = await loop.run_in_executor(None, asterisk_media.recvfrom, 65536)
                assert second[1] == 118, "Only the first proactive RTP packet sets the marker"
                assert int.from_bytes(second[4:8], "big") == 320
            finally:
                module._AudioEncoder = original
                await session.close()

    asyncio.run(scenario())


def main():
    test_contract_config_ports_ui()
    test_ui_surfaces_expose_every_setting()
    test_centralized_simulation()
    test_event_normalization_and_listener_emission()
    test_correlated_audio_commands()
    test_audio_media_attach_publish_and_release()
    test_proactive_rtp_before_inbound_media()
    test_rtp_payload_skips_extension_header()
    test_media_setup_failure_releases_transport()
    test_real_opus_encoder()
    test_active_graph_with_fake_ari()
    print("[ok] F9.10_telephony_in_block")


if __name__ == "__main__":
    main()

# Telephony

<!-- block-metadata:start -->
[![Block version: 0.0.1](https://img.shields.io/badge/block-0.0.1-blue)](model.json)
[![BloxSmith compatibility: 1.0.9](https://img.shields.io/badge/BloxSmith-1.0.9-brightgreen)](compatibility.json)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

Verified BloxSmith versions: **1.0.9** (bundled-block tests; see [test evidence](compatibility.json)).
<!-- block-metadata:end -->

[![Asterisk: tested 20.6.0](https://img.shields.io/badge/Asterisk-tested%2020.6.0-orange)](#asterisk-compatibility)
[![Asterisk: requires 16.6+](https://img.shields.io/badge/Asterisk-requires%2016.6%2B-lightgrey)](#asterisk-compatibility)

Answer inbound telephone calls from an OVHcloud SIP line through an existing local Asterisk server, expose them to a BloxSmith blueprint as normalized call events and runtime audio, and play the blueprint's audio back to the caller on the same call.

## Role

`telephony` owns one inbound call leg, in both directions. Asterisk remains the SIP endpoint for OVH.
The block connects to the local Asterisk ARI application, receives `StasisStart` and `StasisEnd` events, and
publishes:

- `event_out`: JSON call lifecycle events.
- `audio_out`: Opus/Ogg runtime audio, when call media is enabled.
- `command_out`: correlated JSON `start`/`stop` commands for audio consumers.

It also accepts, on the same call leg:

- `audio_in`: runtime audio to play to the caller, in Opus or raw `pcm_s16le`.
- `command_in`: the correlated `start`/`stop` commands of that producer.

Both inputs are optional. A blueprint that only listens leaves them unconnected.

The block does not register the OVH SIP trunk itself. Reuse the Asterisk/PJSIP registration from your existing OVH configuration and route the selected inbound context to `Stasis(<ari_app>)`.

## Ports

### `event_out` (ID 1)

Message output with `application/json`. Events use a provider-neutral shape:

```json
{
  "provider": "asterisk",
  "event": "call.incoming",
  "call_id": "ari-channel-id",
  "channel_id": "channel-id",
  "name": "PJSIP/...",
  "state": "Up",
  "from": "+33612345678",
  "from_name": "",
  "to": "s",
  "context": "phone-test-from-ovh",
  "timestamp": "2026-09-18T12:00:00.000Z",
  "audio": true
}
```

`event` is `call.incoming`, `call.ended`, or `call.failed`. A failed call carries `reason=answer` when Asterisk refuses to answer it and `reason=media_setup` when media attachment fails; in both cases the call is released and the other calls keep running. With capture enabled, `call.ended` reports diagnostic RTP counters: `rtp_packets`, `rtp_bytes`, `rtp_return_packets`, and `rtp_dropped` for inbound packets discarded under load. A call closed by the periodic audit rather than by `StasisEnd` also carries `recovered=true`.

```json
{
  "provider": "asterisk",
  "event": "call.ended",
  "call_id": "ari-channel-id",
  "rtp_packets": 500,
  "rtp_bytes": 320000,
  "rtp_return_packets": 500,
  "rtp_dropped": 0
}
```

### `audio_out` (ID 2)

Runtime audio stream output:

- codec: Opus in Ogg;
- 48 kHz decode clock;
- mono;
- drop-oldest overflow policy.

Connect it to a compatible audio consumer such as `Save Audio` or a supported transcription block. Capture requires a graph-wired `audio_out`; an unconnected port is treated as an audio transport error before Asterisk media is attached.

## Settings

| Field | Default | Meaning |
| --- | --- | --- |
| `ari_base_url` | `http://127.0.0.1:8088` | Local HTTP ARI endpoint; converted internally to a WebSocket events URL. |
| `ari_username` | `bloxsmith` | ARI user. |
| `ari_password_ref` | empty | Secret vault reference containing the ARI password. Required for Active Runtime; shown in the modal as **ARI secret**, in clear text, with a `secret://workspace/asterisk_secret` placeholder. |
| `ari_app` | `bloxsmith` | Asterisk Stasis application name. |
| `expected_context` | empty | Optional inbound context filter. Empty accepts all contexts routed to the app. |
| `expected_extension` | empty | Optional inbound extension filter. Empty accepts all extensions. |
| `auto_answer` | `true` | Answer matching incoming calls; this is independent of `capture_audio`. |
| `capture_audio` | `true` | Attach the two-way external media leg: publish call audio and allow playback. |
| `media_host` | `127.0.0.1` | Address advertised to Asterisk for RTP. |
| `media_port` | `0` | Fixed UDP port, or `0` for an ephemeral local port. |
| `ffmpeg_chunk_ms` | `100` | Encoder read target, from 20 to 1000 ms. |
| `max_calls` | `1` | Concurrent captured calls, from 1 to 8. |

Changing the app name, capture mode, audio route, or listener declaration requires Stop, then Load/Run again in Active Runtime.

## Playback input

`audio_in` (ID 1) accepts `audio/*` on an `audio_stream` transport, in `opus` or `pcm_s16le`, at any sample
rate, mono or stereo. It matches what the workspace audio producers emit, so the output of a text-to-speech
block connects to it directly. `command_in` (ID 2) takes that producer's JSON commands.

Incoming audio is decoded to the 8 kHz mono signed linear format Asterisk expects and played on the call's
return RTP leg. Playback starts once a short cushion is buffered, so producer jitter is not audible, and the
leg falls back to silence between two answers, which keeps the media path open.

One stream is played at a time: a frame carrying a new `stream_id` replaces the current answer instead of
queuing behind it, and a `stop` command marked `aborted` drops what is still buffered. That is what a
barge-in needs — the caller interrupting must not wait for the previous sentence to finish.

Playback requires call media to be enabled, since it travels on the external media channel created for
capture. It is meant for one active call: with several simultaneous calls, the same audio is sent to each of
them, so keep `max_calls` at `1` when the blueprint answers.

## Command output

`command_out` (ID 3) emits JSON lifecycle commands with the same `stream_id` as the audio frames. Connect it separately to `OpenAI Realtime STT.command_in`, `Save Audio.command_in`, or another compatible consumer.

Normal capture commands are:

```json
{"action":"start","stream_id":"ari-channel-id"}
```

```json
{"action":"stop","stream_id":"ari-channel-id","frame_count":132,"byte_count":184217,"aborted":false}
```

The stop command is emitted only after RTP intake has stopped and encoded frames have drained. `frame_count` and `byte_count` are exact published totals. `aborted` is `true` if Active Runtime stops while a call is still active. A node created before the fixed three-port contract must be recreated.

## Block surfaces

The three surfaces are block-owned and reuse the shared editor chrome, so they stay consistent with the standard blocks and follow the active theme.

- **Canvas card**: standard node geometry derived from the three outputs, with the shared head, title and preview chrome. It shows the ARI application and whether audio capture is enabled.
- **Control panel** (inspector): every editable attribute, grouped into ARI connection, inbound calls, and audio and media, plus the node name, the port list and the Apply, Duplicate and Delete actions. The gateway can be adjusted without opening the modal.
- **Settings modal**: the same attributes in four sections, each control carrying its own label, its validation bounds and a hint. Apply and Close stay reachable below the scrolling body.

Numeric controls declare the same bounds as the runtime validator. A stored value that fails validation does not break a surface: the offending setting falls back to its default, the reason is reported next to the fields, and the value can be corrected in place.

Release assets declared in `model.json.ui_assets` are scoped to `telephony@<version>`, which the editor applies to installed releases. Every surface must therefore remain readable through the shared classes alone, since a bundled node carries no release scope.

## Runtime behavior

### Active Runtime (`zeromq_active`)

After Run, the persistent listener connects to Asterisk ARI and declares an event filter so the application only receives `StasisStart` and `StasisEnd`. The filter is best effort: an Asterisk that refuses it keeps sending every event, which the listener still handles. Matching calls emit `call.incoming`. With capture enabled, the block answers the call, creates an external media channel and mixing bridge, and plays Asterisk's built-in one-second silence prompt to open the PJSIP/RTP path. The playback is inaudible; it forces real media through NAT until the block's return RTP takes over.

A dedicated sender then returns standards-compliant 20 ms RTP for the whole call, at its own pace and independently of the inbound flow, so the return path never stalls while the caller speaks. It starts from Asterisk's `UNICASTRTP_LOCAL_ADDRESS` and `UNICASTRTP_LOCAL_PORT`, then follows the source address, payload type and frame size observed on inbound RTP, as symmetric RTP requires. Each call owns its RTP synchronization source.

Inbound RTP is transcoded from Asterisk's big-endian `slin` PCM to Opus/Ogg and published through `audio_out`, while audio received on `audio_in` is decoded to `slin16` and carried by that same return sender, which falls back to silence when there is nothing to say. External media channels join the same Stasis application, so Asterisk announces them like inbound calls; the block recognizes its own and ignores them, and `max_calls` therefore counts real callers only. `StasisEnd` drains the capture, publishes the correlated stop command and `call.ended`, and only then releases the Asterisk resources: a cleanup error never costs the audio consumer its stop command or its exact counters.

### Failure handling

The gateway is built to stay up. A lost ARI connection is not a node failure: the listener reports a
`reconnecting` state and retries with a capped backoff, from one second up to thirty, until the runtime is
stopped. Only an unrecoverable setup error, such as an unreachable vault or a missing dependency, fails the
block. A single call that cannot be answered or captured is released, reported as `call.failed`, and hung up
so it does not hold an Asterisk channel while the caller hears silence. A broken encoder aborts that call's
capture and leaves the session and its events intact.

Every fifteen seconds, calls whose Asterisk channel has disappeared are closed and reported with
`recovered=true`. Without this audit, an end event lost during a disconnection would keep a session, its
encoder and its slot in `max_calls` forever. An unreadable channel list is never read as "every call ended":
ambiguity leaves live calls untouched.

### One Shot Simulation (`centralized`)

Runtime audio and live ARI listeners are unavailable. The block validates its contract and reports a skipped source result without fabricating calls.

## OVH SIP credentials

OVH credentials are **not block settings**. They belong to the Asterisk PJSIP registration.

In a `phone_test`-style Asterisk setup, keep them in its protected `.env` file:

```env
OVH_SIP_USERNAME=your-ovh-number
OVH_SIP_PASSWORD=your-ovh-sip-password
OVH_SIP_DOMAIN=your-ovh-sip-domain
OVH_SIP_PROXY=your-ovh-sip-proxy
```

Then render and apply the Asterisk configuration using the tooling that owns that setup. Do not copy these values into the BloxSmith blueprint or a plain block config. The block only needs local ARI access and the secret reference for the ARI password.

## Call audio format

The external media leg uses `slin`: mono 8 kHz signed linear PCM, RTP payload type `11`, 320 bytes per
20 ms frame. Wideband `slin16` cannot be used in both directions: Asterisk chooses a dynamic payload type
for it and, having no SDP negotiation on an external media channel, drops the frames sent back with that
same type. Nothing is lost on a telephone call, where the trunk itself carries 8 kHz audio.

`audio_out` still publishes Opus at 48 kHz, which is what the workspace audio consumers expect, and
`audio_in` accepts any rate and resamples.

## Asterisk compatibility

| | Version | Scope |
| --- | --- | --- |
| Tested | **20.6.0** (Asterisk 20 LTS) | The only version this block has been exercised against |
| Required | **16.6 or later** | Version that introduced the `externalMedia` ARI resource |

Asterisk 16.6 is a declared requirement derived from the features the block uses, not tested evidence.
Only 20.6.0 has been run. Other versions are unverified, not known to be incompatible.

The block depends on three Asterisk behaviors:

- the `externalMedia` ARI resource, added in Asterisk 16.6, which creates a `UnicastRTP` channel;
- the `UNICASTRTP_LOCAL_ADDRESS` and `UNICASTRTP_LOCAL_PORT` channel variables set by `chan_rtp`, which
  give the address the block must send return RTP to;
- the `eventFilter` application resource, used to subscribe to two event types only; it is optional, so an older or restricted server still works;
- the `slin` format and its static RTP payload type `11`. External media channels have no SDP
  negotiation, so Asterisk cannot learn that a dynamic payload type means `slin16` on the way in and
  silently drops those frames ([ASTERISK-28751](https://issues-archive.asterisk.org/ASTERISK-28751)).
  A static payload type is accepted in both directions. The block starts from that value and then
  follows what inbound RTP actually carries.

Asterisk 20 is an LTS release: its bug-fix support ends in October 2026 and its security support in
October 2027. Asterisk 22 is the current LTS. Moving to another Asterisk version requires a new test run,
exactly like a new BloxSmith version.

## Asterisk requirement

Asterisk must already register the OVH line and route matching inbound calls to the configured Stasis application, for example:

```asterisk
[phone-test-from-ovh]
exten => s,1,Stasis(bloxsmith)
 same => n,Hangup()
```

The block needs ARI access and, for audio, permission to create bridges, play media, and create external media channels. It also declares its event filter, which requires no extra permission. FFmpeg must be installed with `libopus`, and Asterisk's core `sound:silence/1` prompt must be installed.

When Asterisk is behind NAT, configure the PJSIP transport with `local_net`, `external_media_address`, and `external_signaling_address`. Route the SIP UDP port and Asterisk RTP UDP range to the Asterisk host. Otherwise OVH can answer the SIP dialog but drop the call after its media timeout even though ARI setup succeeds.

## Example

1. Configure the local ARI endpoint and `ari_app=bloxsmith`.
2. Put the ARI password in the secret vault and reference it with `ari_password_ref`.
3. Route the OVH inbound context to `Stasis(bloxsmith)`.
4. Start Active Runtime.
5. Connect all three ports separately:
   - `event_out → Display` (optional);
   - `audio_out → Save Audio.audio_in` or `OpenAI Realtime STT.audio_in`;
   - `command_out → Save Audio.command_in` or `OpenAI Realtime STT.command_in`.
6. Call the OVH number; the blueprint receives the call event, audio stream and correlated start/stop commands.

## Limits and warnings

- Live calls require Active Runtime and a reachable Asterisk service.
- The block captures only calls Asterisk routes into its Stasis application.
- The current release does not dial outbound calls, play prompts, collect DTMF, record files itself, or transcribe audio.
- RTP delivery is transient; there is no persisted event journal or replay.
- Asterisk, RTP and FFmpeg failures stop the affected capture and are reported as call events and a degraded state, without stopping the listener.
- Calls active when the ARI connection drops are released locally and reported with `aborted=true`; their caller channels are left to Asterisk.
- Never expose the ARI HTTP/WebSocket port, SIP port, or RTP range directly to untrusted networks.
- Recording telephone calls can require caller consent and may be regulated; configure and retain audio only where lawful.

## Compatibility policy

`compatibility.json` records HackInvent's verified BloxSmith versions and test evidence. Only versions listed above are verified. Other framework versions are unverified, not necessarily incompatible. The block-version badge follows `model.json`, not a published Git tag.

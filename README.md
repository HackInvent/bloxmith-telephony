# Telephony In

<!-- block-metadata:start -->
[![Block version: 0.0.1](https://img.shields.io/badge/block-0.0.1-blue)](model.json)
[![BloxSmith compatibility: 1.0.9](https://img.shields.io/badge/BloxSmith-1.0.9-brightgreen)](compatibility.json)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

Verified BloxSmith versions: **1.0.9** (bundled-block tests; see [test evidence](compatibility.json)).
<!-- block-metadata:end -->

Receive inbound telephone calls from an OVHcloud SIP line through an existing local Asterisk server and expose them to a BloxSmith blueprint as normalized call events and runtime audio.

## Role

`telephony_in` is a source block. Asterisk remains the SIP endpoint for OVH. The block connects to the local Asterisk ARI application, receives `StasisStart` and `StasisEnd` events, and publishes:

- `event_out`: JSON call lifecycle events.
- `audio_out`: Opus/Ogg runtime audio, when capture is enabled.
- `command_out`: correlated JSON `start`/`stop` commands for audio consumers.

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

`event` is `call.incoming`, `call.ended`, or `call.failed` (with `reason=media_setup` when Asterisk media attachment fails). With capture enabled, `call.ended` also reports diagnostic RTP counters: `rtp_packets`, `rtp_bytes`, and `rtp_return_packets`.

```json
{
  "provider": "asterisk",
  "event": "call.ended",
  "call_id": "ari-channel-id",
  "rtp_packets": 500,
  "rtp_bytes": 320000,
  "rtp_return_packets": 500
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
| `capture_audio` | `true` | Create an Asterisk external-media bridge and publish call audio. |
| `media_host` | `127.0.0.1` | Address advertised to Asterisk for RTP. |
| `media_port` | `0` | Fixed UDP port, or `0` for an ephemeral local port. |
| `ffmpeg_chunk_ms` | `100` | Encoder read target, from 20 to 1000 ms. |
| `max_calls` | `1` | Concurrent captured calls, from 1 to 8. |

Changing the app name, capture mode, audio route, or listener declaration requires Stop, then Load/Run again in Active Runtime.

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

Release assets declared in `model.json.ui_assets` are scoped to `telephony_in@<version>`, which the editor applies to installed releases. Every surface must therefore remain readable through the shared classes alone, since a bundled node carries no release scope.

## Runtime behavior

### Active Runtime (`zeromq_active`)

After Run, the persistent listener connects to Asterisk ARI. Matching calls emit `call.incoming`. With capture enabled, the block answers the call, creates an external media channel and mixing bridge, plays Asterisk's built-in one-second silence prompt to open the PJSIP/RTP path, uses Asterisk's `UNICASTRTP_LOCAL_ADDRESS` and `UNICASTRTP_LOCAL_PORT` to start standards-compliant 20 ms RTP silence immediately, receives RTP, transcodes Asterisk's big-endian `slin16` PCM to Opus/Ogg, and publishes frames through `audio_out`. The playback is inaudible; it forces real media through NAT until the block's return RTP takes over. The payload type and frame size are refined from inbound RTP when it arrives. `StasisEnd` releases media and emits `call.ended`. A media setup failure releases the local transport and emits `call.failed` before a redacted runtime error.

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

## Asterisk requirement

Asterisk must already register the OVH line and route matching inbound calls to the configured Stasis application, for example:

```asterisk
[phone-test-from-ovh]
exten => s,1,Stasis(bloxsmith)
 same => n,Hangup()
```

The block needs ARI access and, for audio, permission to create bridges, play media, and create external media channels. FFmpeg must be installed with `libopus`, and Asterisk's core `sound:silence/1` prompt must be installed.

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
- Asterisk, RTP and FFmpeg failures stop the affected capture and surface a redacted runtime error.
- Never expose the ARI HTTP/WebSocket port, SIP port, or RTP range directly to untrusted networks.
- Recording telephone calls can require caller consent and may be regulated; configure and retain audio only where lawful.

## Compatibility policy

`compatibility.json` records HackInvent's verified BloxSmith versions and test evidence. Only versions listed above are verified. Other framework versions are unverified, not necessarily incompatible. The block-version badge follows `model.json`, not a published Git tag.

# Telephony In

<!-- block-metadata:start -->
[![Block version: 0.1.0](https://img.shields.io/badge/block-0.1.0-blue)](model.json)
[![BloxSmith compatibility: 1.0.9](https://img.shields.io/badge/BloxSmith-1.0.9-brightgreen)](compatibility.json)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

Verified BloxSmith versions: **1.0.9** (bundled-block tests; see [test evidence](compatibility.json)).
<!-- block-metadata:end -->

Receive inbound telephone calls from an OVHcloud SIP line through an existing local Asterisk server and expose them to a BloxSmith blueprint as normalized call events and runtime audio.

## Role

`telephony_in` is a source block. Asterisk remains the SIP endpoint for OVH. The block connects to the local Asterisk ARI application, receives `StasisStart` and `StasisEnd` events, and publishes:

- `event_out`: JSON call lifecycle events.
- `audio_out`: Opus/Ogg runtime audio, when capture is enabled.

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

`event` is `call.incoming`, `call.ended`, or `call.failed` (with `reason=media_setup` when Asterisk media attachment fails).

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
| `ari_password_ref` | empty | Secret vault reference containing the ARI password. Required for Active Runtime; it is never stored as a raw password. |
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

## Runtime behavior

### Active Runtime (`zeromq_active`)

After Run, the persistent listener connects to Asterisk ARI. Matching calls emit `call.incoming`. With capture enabled, the block answers the call, creates an external media channel and mixing bridge, receives RTP, transcodes linear PCM to Opus/Ogg, and publishes frames through `audio_out`. `StasisEnd` releases media and emits `call.ended`. A media setup failure releases the local transport and emits `call.failed` before a redacted runtime error.

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

The block needs ARI access and, for audio, permission to create bridges and external media channels. FFmpeg must be installed with `libopus`.

## Example

1. Configure the local ARI endpoint and `ari_app=bloxsmith`.
2. Put the ARI password in the secret vault and reference it with `ari_password_ref`.
3. Route the OVH inbound context to `Stasis(bloxsmith)`.
4. Start Active Runtime.
5. Connect `event_out` to `Display` and `audio_out` to `Save Audio`.
6. Call the OVH number; the blueprint receives the call event and audio stream.

## Limits and warnings

- Live calls require Active Runtime and a reachable Asterisk service.
- The block captures only calls Asterisk routes into its Stasis application.
- The current release does not dial outbound calls, play prompts, collect DTMF, record files itself, or transcribe audio.
- RTP delivery is transient; there is no persisted event journal or replay in version 0.1.0.
- Asterisk, RTP and FFmpeg failures stop the affected capture and surface a redacted runtime error.
- Never expose the ARI HTTP/WebSocket port, SIP port, or RTP range directly to untrusted networks.
- Recording telephone calls can require caller consent and may be regulated; configure and retain audio only where lawful.

## Compatibility policy

`compatibility.json` records HackInvent's verified BloxSmith versions and test evidence. Only versions listed above are verified. Other framework versions are unverified, not necessarily incompatible. The block-version badge follows `model.json`, not a published Git tag.

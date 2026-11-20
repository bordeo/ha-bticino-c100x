# Changelog

## 0.3.9 — 2026-11-20

- **Second-call video/mic stuck on “changing”:** Open MediaPlayer as soon as SIP media starts (`prime`) so it can catch the first H.264 IDR from the UDP loopback relay. If no frame arrives before the WebRTC answer, reopen the player once to resync. Avoids the card’s 10 s `watchStream` timeout after a missed keyframe with no PLI/FIR.
- **Faster ICE:** When TURN is configured, put TURN in `iceServers` and skip STUN so gather is not stalled ~5 s by blocked STUN. Still **host + TURN** (`iceTransportPolicy=all`), never relay-only — LAN keeps working with host candidates. With no TURN key, STUN remains (TURN stays optional). Card ICE gather timeout reduced to 2.5 s.
- **Speex load:** Prefer the vendored `libspeex` path first, never call `ctypes.util.find_library` (Alpine probes blocked the event loop). Cache the handle; construct the encoder via `asyncio.to_thread`.
- **WebRTC peer close identity:** `close(session_id, peer=…)` only tears down that peer instance, so a reused HA `session_id` cannot close a newer connection.
- Echo-cancellation (speexdsp AEC) not added in this release (optional, off-by-default) — deferred until a cheap/safe path is clear.

## 0.3.8 — 2026-11-20

- **Mic talkback without ffmpeg libspeex:** Home Assistant’s Alpine ffmpeg build does not include Speex, so v0.3.7 silence keepalive worked but unmuted mic frames were dropped. Add an in-process Speex encoder (ctypes + vendored `libspeex` for musl/gnu × x86_64/aarch64; system `libspeex` preferred when present). Prefer native encoder; keep ffmpeg Speex as fallback. Log which encoder is used; disable talkback only if both fail. Canned Speex silence remains for muted keepalive.

## 0.3.7 — 2026-11-20

- **Door listen/talk audio:** SIP offer matches Door Entry (Opus → Speex → G.711). The C100X rejects PCMA/PCMU-only offers (audio port 0); with the full offer it answers Speex/8000. Keep the negotiated codec/PT for both directions.
- Continuous Speex silence uplink every 20 ms from call start (`sendrecv`, like Door Entry with a locally muted mic) so the door opens downlink; real mic frames replace silence when unmuted. Speex decode (PyAV) → WebRTC PCM; mic PCM → Speex (ffmpeg libspeex) / G.711 / optional Opus.
- Downlink **audio gain** option (default **+12 dB**, hard limiter/clip). Configurable in integration options (`audio_gain_db`, −12…+24).
- Card JS content-hash digest computed via `hass.async_add_executor_job` (no blocking `read_bytes` on the event loop). Existing TURN support unchanged.

## 0.3.6 — 2026-11-20

- Microphone uplink now uses the same ICE servers as the video peer (HA `camera/webrtc/get_client_config`, including Cloudflare TURN). The aiortc mic peer also gathers TURN relay candidates. Fixes mic/ICE failure from cellular CGNAT when video already worked via TURN.
- Mic button also unmutes door playback (iOS/WKWebView autoplay often leaves the video muted); clearer status text for negotiate/ICE failures (`Microfono: connessione non riuscita`).
- Card JS content-hash digest is computed at import time (no blocking `read_bytes` on the event loop).
- Cloudflare TURN via **integration options** (and optional config-flow step): key ID + API token (password selector). Precedence: options → YAML block → `bticino_turn.yaml`. Options reload refreshes ICE. Token redacted in diagnostics. README remote-access guide + en/it strings.

## 0.3.5 — 2026-11-20

- TURN credentials fallback: if `configuration.yaml` has no `bticino_c100x:` TURN keys, load `bticino_turn.yaml` from the HA config directory (`cloudflare_turn_key_id` / `cloudflare_turn_api_token`). Missing file → same as no TURN. Malformed file → warning (no values logged) and continue without TURN. YAML block still takes precedence. Debug log when credentials are loaded from the file.

## 0.3.4 — 2026-11-20

- Optional TURN relay for viewing from outside the home network (CGNAT home + cellular phone). Configure `bticino_c100x: cloudflare_turn_key_id` / `cloudflare_turn_api_token` (via `!secret`). Short-lived Cloudflare Realtime TURN credentials are minted and refreshed, registered with Home Assistant's WebRTC ICE servers for browsers, and used by the aiortc side as a relay candidate. Without the YAML keys nothing changes. The YAML block is validated (both keys or neither); the TURN key is only ever sent to `rtc.live.cloudflare.com` and is never logged. Alternatively place the same two keys in `bticino_turn.yaml` in the HA config directory (see 0.3.5).

## 0.3.3 — 2026-11-10

### SIP keepalive vs REGISTER health check

v0.3.2 treated a missing RFC 5626 CRLF pong as a dead flow and reconnected.
Legrand/Azure (`vdesip`, behind an LB that drops idle TCP after ~4 min) does
**not** answer `CRLFCRLF` with a `CRLF` pong within 10 s, so the socket bounced
every ~41 s while REGISTER still succeeded.

- **CRLFCRLF every 30 s** still sent to keep the LB flow alive; a missing pong
  is logged at **debug** only and does **not** reconnect. Framer still strips
  interleaved CRLF and replies to server pings.
- **REGISTER refresh every 180 s** on the same connection (Expires **600**,
  same Call-ID, incrementing CSeq) — below the ~4 min LB idle timeout. Final
  response timeout **15 s**; non-2xx or timeout triggers reconnect.
- **Reconnect** only on real failures: socket/EOF/TLS/write error, or REGISTER
  refresh failure. Backoff remains 1 / 2 / 5 / max 30 s. SO_KEEPALIVE kept.
- Door release and server-initiated INVITE/doorbell unchanged.

## 0.3.2 — 2026-11-10

### SIP transport keepalive

- **RFC 5626 CRLF keepalive** every 30 s (`\r\n\r\n` ping, expect `\r\n` pong
  within 10 s). Matches the official Door Entry app and keeps the Azure LB flow
  alive (idle timeout ~4 min). Without this, the 480 s REGISTER refresh hit a
  dead socket → 20 s timeout → ~2 min outage.
- **SipFramer** strips leading/interleaved CRLF pongs and server CRLFCRLF pings
  so they cannot corrupt the next SIP response; replies to server pings with a
  single CRLF.
- **Reconnect** after refresh timeout, keepalive timeout, or socket error: fresh
  DNS + new TLS + full REGISTER with short backoff (1 s, 2 s, 5 s, max 30 s).
  Only one SipClient at a time (no parallel registrations).
- Optional TCP SO_KEEPALIVE on the SIP socket.
- Door-release semantics unchanged (still at most one retry on link drop).

## 0.3.1 — 2026-11-10

### SIP registration refresh

- **Reuse Call-ID and From-tag on REGISTER refresh** (RFC 3261 §10.2.4), only
  incrementing CSeq. Previously every 480 s refresh minted a new Call-ID/tag, which
  matched production drops about every 500 s when the cloud ignored the refresh.
- **Refresh timeout reconnects immediately** at warning level (no 10 s backoff).
- **Door release no longer waits behind a pending refresh** — the send lock covers
  the write only, not the response wait.
- **If the SIP link drops during a user-requested release**, retry that same release
  exactly once on the new connection. Log the cloud response code or `no response`.

## 0.3.0 — 2026-11-10

Community continuation of `alpichlabs/ha-bticino-c100x` `0.2.0-beta.28` (original
GitHub repository no longer available). Maintainer: [@bordeo](https://github.com/bordeo).

### Media (`media_runtime.py`)

- **STUN Binding Request primers.** NAT/relay priming sends RFC 5389 STUN Binding
  Requests instead of a single `0x00` byte on the audio RTP/RTCP and video RTP/RTCP
  ports. A primer task resends them every 1.5 s until the first video frame arrives.
- **STUN responder.** Incoming STUN Binding Requests on the audio RTP and RTCP
  sockets get a Binding Success with XOR-MAPPED-ADDRESS; STUN packets are not passed
  to the SRTP decoder.
- **View-only G.711 silence keepalive.** While the microphone is off and the
  negotiated audio direction is `sendrecv` or `recvonly`, the runtime sends one
  20 ms SRTP G.711 silence frame every 20 ms so the relay latches return audio.
- Logs the first inbound audio RTP packet once per session.
- Teardown cancels keepalive and primer tasks before existing monitor/BYE logic.

### Microphone uplink (`uplink.py`)

- Mic peer `RTCPeerConnection` uses `SERVER_STUN_URL` so the uplink can gather a
  srflx candidate.
- Idempotent `close()` when nothing is open; connection-state handler only closes
  the current peer under the lock.

### Doorbell (`event.py`, `manager.py`)

- `event.*_doorbell` uses `device_class: doorbell` and event type `ring`, for the
  Home Assistant `doorbell.rang` trigger and HomeKit linked doorbell.
- **5 s ring debounce:** a new INVITE (new Call-ID) inside the active ring window
  is ignored so one press yields one event.

### Lovelace card (`frontend/bticino-c100x-card.js`)

- Browser mic peer uses the same STUN set as `webrtc_config.STUN_URLS`.
- UUID fallback when `crypto.randomUUID` is unavailable (e.g. iOS app over HTTP).
- Single round session button (start / connecting / stop), icon-only mic and door
  release, HTTPS note for microphone, short mic error messages.
- Card-owned `<video>` autoplay for unmuted playback on WebKit/iOS; falls back to
  HA's player for HLS.

### Packaging

- Manifest version `0.3.0`, codeowners `@bordeo`, documentation and issue tracker
  pointed at this repository.
- HACS layout (`hacs.json`), pytest + Playwright card harness, GitHub Actions CI.

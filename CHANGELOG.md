# Changelog

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

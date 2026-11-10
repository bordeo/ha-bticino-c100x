function newOwnerId() {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  const bytes = new Uint8Array(16);
  if (typeof crypto !== "undefined" && typeof crypto.getRandomValues === "function") {
    crypto.getRandomValues(bytes);
  } else {
    for (let i = 0; i < 16; i += 1) {
      bytes[i] = Math.floor(Math.random() * 256);
    }
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

const START_TIMEOUT_MS = 30000;
const ICE_GATHER_TIMEOUT_MS = 8000;
const LIVE_STATES = new Set(["streaming"]);
const ENDED_STATES = new Set(["idle", "error", "unavailable"]);

function microphoneSupported() {
  return typeof navigator !== "undefined" &&
    typeof navigator.mediaDevices?.getUserMedia === "function" &&
    typeof RTCPeerConnection === "function";
}

function microphoneErrorText(error) {
  const name = error?.name || "";
  if (name === "NotAllowedError" || name === "SecurityError") return "Permesso microfono negato";
  if (name === "NotFoundError" || name === "OverconstrainedError") return "Nessun microfono trovato";
  if (name === "NotReadableError" || name === "AbortError") return "Microfono occupato";
  if (error?.code === "microphone_failed") return "Microfono non disponibile";
  return "Errore microfono";
}

class BticinoC100XCard extends HTMLElement {
  setConfig(config) {
    for (const key of ["camera_entity", "start_entity", "end_entity", "release_entity"]) {
      if (!config[key]) throw new Error(`Missing ${key}`);
    }
    this.config = config;
    this.owner = this.owner || newOwnerId();
    this.actionGeneration = this.actionGeneration || 0;
    this.streamGeneration = this.streamGeneration || 0;
    this.phase = "idle"; // idle | starting | live | ending
    this.micBusy = false;
    this.videoReady = false;
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    this.render();
  }

  set hass(value) {
    this._hass = value;
    if (this.stream) {
      this.stream.hass = value;
      this.stream.stateObj = value.states[this.config.camera_entity];
    }
    this.syncSession();
  }

  render() {
    this.shadowRoot.innerHTML = `
      <style>
        ha-card { overflow:hidden; }
        .media { position:relative; background:#111; min-height:240px; display:flex;
          align-items:center; justify-content:center; }
        .media ha-camera-stream { position:absolute; left:0; top:0; width:1px; height:1px;
          opacity:0; overflow:hidden; pointer-events:none; }
        :host([data-native]) .media ha-camera-stream { position:static; width:100%; height:auto;
          opacity:1; overflow:visible; pointer-events:auto; }
        video.live { display:none; width:100%; aspect-ratio:auto 16 / 9; background:#000;
          object-fit:contain; }
        :host([data-phase="starting"]) video.live, :host([data-phase="live"]) video.live { display:block; }
        :host([data-phase="starting"]:not([data-native])) .media,
        :host([data-phase="live"]:not([data-native])) .media { min-height:0; }
        :host([data-native]) video.live { display:none; }
        .unmute { position:absolute; right:12px; bottom:12px; width:40px; height:40px;
          background:rgba(0,0,0,.55); color:#fff; --mdc-icon-size:22px; }
        .unmute[hidden] { display:none; }
        .placeholder { color:rgba(255,255,255,.28); --mdc-icon-size:56px; }
        .overlay { position:absolute; inset:0; display:none; align-items:center;
          justify-content:center; background:rgba(0,0,0,.35); pointer-events:none; }
        :host([data-phase="starting"]) .overlay, :host([data-waiting]) .overlay { display:flex; }
        :host([data-phase="live"]) .placeholder, :host([data-phase="starting"]) .placeholder { display:none; }
        .spinner { width:22px; height:22px; border-radius:50%; box-sizing:border-box;
          border:3px solid currentColor; border-right-color:transparent;
          animation:spin .8s linear infinite; }
        .overlay .spinner { width:40px; height:40px; color:#fff; }
        @keyframes spin { to { transform:rotate(360deg); } }
        .controls { display:flex; align-items:center; gap:12px; padding:12px 16px; }
        button { width:48px; height:48px; border-radius:50%; border:0; padding:0; margin:0;
          display:inline-flex; align-items:center; justify-content:center; cursor:pointer;
          --mdc-icon-size:24px; transition:background-color .15s, color .15s, opacity .15s;
          -webkit-tap-highlight-color:transparent; }
        button:focus-visible { outline:2px solid var(--primary-color); outline-offset:2px; }
        button:disabled { cursor:default; opacity:.38; }
        .session { background:var(--primary-color); color:var(--text-primary-color, #fff); }
        .session.live { background:var(--error-color, #db4437); color:#fff; }
        .session.starting { background:var(--secondary-background-color, rgba(127,127,127,.15));
          color:var(--primary-color); }
        .mic { background:var(--secondary-background-color, rgba(127,127,127,.15));
          color:var(--primary-text-color); }
        .mic.on { background:var(--success-color, #43a047); color:#fff; }
        .release { margin-left:auto; background:transparent; color:var(--error-color, #db4437);
          box-shadow:inset 0 0 0 1.5px var(--error-color, #db4437); }
        .release:not(:disabled):hover { background:rgba(219,68,55,.1); }
        .status { padding:0 16px 12px; min-height:0; font-size:13px; line-height:18px;
          color:var(--error-color, #db4437); }
        .status:empty { display:none; }
        .status.info { color:var(--secondary-text-color); }
      </style>
      <ha-card header="${this.escape(this.config.title || "Front door")}">
        <div class="media"><ha-icon class="placeholder" icon="mdi:doorbell-video"></ha-icon>
          <video class="live" playsinline webkit-playsinline autoplay></video>
          <div class="overlay"><div class="spinner"></div></div>
          <button class="unmute" data-action="unmute" title="Attiva audio" aria-label="Attiva audio" hidden>
            <ha-icon icon="mdi:volume-off"></ha-icon></button></div>
        <div class="controls">
          <button class="session" data-action="session"></button>
          <button class="mic" data-action="microphone"></button>
          <button class="release" data-action="release" title="Apri porta" aria-label="Apri porta">
            <ha-icon icon="mdi:door-open"></ha-icon></button>
        </div><div class="status" role="status" aria-live="polite"></div></ha-card>`;
    this.shadowRoot.querySelectorAll("button").forEach((button) =>
      button.addEventListener("click", () => this.action(button.dataset.action))
    );
    this.video = this.shadowRoot.querySelector("video.live");
    // Own controls replace native ones; double tap/click opens fullscreen.
    this.video.addEventListener("dblclick", () => this.fullscreen());
    this.updateControls();
  }

  fullscreen() {
    const video = this.video;
    if (!video?.srcObject) return;
    if (video.requestFullscreen) video.requestFullscreen().catch(() => {});
    else if (video.webkitEnterFullscreen) video.webkitEnterFullscreen();
  }

  primeVideo() {
    // Runs synchronously inside the Start tap: WebKit (iOS) lifts the user-gesture
    // playback restriction for this element, so the stream can later play with audio.
    const video = this.video;
    video.muted = false;
    this.setAudioBlocked(false);
    try { video.play()?.catch(() => {}); } catch (error) { /* no source yet */ }
  }

  playLive(video = this.video) {
    video.muted = false;
    const attempt = video.play();
    if (!attempt?.catch) return;
    attempt.catch((error) => {
      if (error?.name !== "NotAllowedError") return;
      // Audio autoplay refused: play muted and offer a one-tap unmute.
      video.muted = true;
      video.play()?.catch(() => {});
      this.setAudioBlocked(true, video);
    });
  }

  setAudioBlocked(blocked, video = this.video) {
    this.audioBlockedVideo = blocked ? video : null;
    const button = this.shadowRoot?.querySelector(".unmute");
    if (button) button.hidden = !blocked;
  }

  unmute() {
    const video = this.audioBlockedVideo || this.video;
    this.setAudioBlocked(false);
    video.muted = false;
    video.play()?.catch(() => this.setAudioBlocked(true, video));
  }

  adoptVideo(stream) {
    // HA's player (ha-camera-stream > ha-web-rtc-player > <video>) gets its
    // MediaStream seconds after the tap; mirror it into the primed card video.
    const inner = this.findVideo(stream);
    if (!inner) return;
    const source = inner.srcObject;
    if (source && typeof MediaStream !== "undefined" && source instanceof MediaStream) {
      inner.autoplay = false;
      if (!inner.muted) inner.muted = true;
      if (!inner.paused) inner.pause();
      if (this.video.srcObject !== source) {
        this.video.srcObject = source;
        this.playLive();
      } else if (this.video.paused && !this.audioBlockedVideo) {
        this.playLive();
      }
      this.mediaMode = "mirror";
    } else if (inner.currentSrc || inner.getAttribute("src")) {
      // HLS fallback: show HA's own player and start it with audio.
      if (this.mediaMode !== "native") {
        this.mediaMode = "native";
        this.setAttribute("data-native", "");
        this.playLive(inner);
      }
    }
  }

  frameReady(stream) {
    const video = this.mediaMode === "native" ? this.findVideo(stream) : this.video;
    return Boolean(video && video.readyState >= 2 && video.videoWidth > 0);
  }

  escape(text) {
    return String(text).replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
  }

  get sessionEntityId() {
    if (this.config.session_entity) return this.config.session_entity;
    if (this._sessionId && this._hass?.states?.[this._sessionId]) return this._sessionId;
    this._sessionId = this.findSessionEntity();
    return this._sessionId;
  }

  findSessionEntity() {
    // Monitoring-session binary_sensor of the same device (exposes session_state).
    const states = this._hass?.states || {};
    const device = this._hass?.entities?.[this.config.camera_entity]?.device_id;
    let fallback = null;
    for (const [entityId, stateObj] of Object.entries(states)) {
      if (!entityId.startsWith("binary_sensor.") || !("session_state" in (stateObj.attributes || {}))) continue;
      if (device && this._hass.entities?.[entityId]?.device_id === device) return entityId;
      fallback = fallback || entityId;
    }
    return fallback;
  }

  get sessionState() {
    const id = this.sessionEntityId;
    const stateObj = id && this._hass?.states?.[id];
    if (stateObj) return stateObj.attributes.session_state || "unavailable";
    const camera = this._hass?.states?.[this.config.camera_entity];
    if (camera?.state === "streaming") return "streaming";
    return null; // unknown: rely on video frames only
  }

  syncSession() {
    if (!this.config || !this._hass) return;
    const state = this.sessionState;
    if (this.phase === "starting" && state && LIVE_STATES.has(state)) {
      this.goLive();
    } else if (this.phase === "starting" && state === "error" && this.sawConnecting) {
      this.failStart("Connessione al citofono non riuscita");
    } else if (this.phase === "starting" && state === "connecting") {
      this.sawConnecting = true;
    } else if (this.phase === "live" && state && ENDED_STATES.has(state)) {
      this.externalEnd(state === "error" ? "Sessione interrotta" : "");
    }
  }

  setPhase(phase) {
    this.phase = phase;
    this.setAttribute("data-phase", phase);
    this.updateControls();
  }

  updateControls() {
    const root = this.shadowRoot;
    if (!root) return;
    this.setAttribute("data-phase", this.phase);
    const session = root.querySelector(".session");
    session.classList.toggle("live", this.phase === "live");
    session.classList.toggle("starting", this.phase === "starting" || this.phase === "ending");
    const sessionView = {
      idle: ["mdi:video", "Avvia video"],
      starting: [null, "Connessione…"],
      live: ["mdi:stop", "Termina"],
      ending: [null, "Chiusura…"],
    }[this.phase];
    session.innerHTML = sessionView[0]
      ? `<ha-icon icon="${sessionView[0]}"></ha-icon>` : `<div class="spinner"></div>`;
    session.title = sessionView[1];
    session.setAttribute("aria-label", sessionView[1]);
    session.disabled = this.phase === "ending";
    session.setAttribute("aria-busy", String(this.phase === "starting" || this.phase === "ending"));

    const mic = root.querySelector(".mic");
    const supported = microphoneSupported();
    const on = Boolean(this.peer);
    mic.classList.toggle("on", on);
    let micLabel;
    if (!supported) micLabel = "Microfono disponibile solo via HTTPS";
    else if (on) micLabel = "Disattiva microfono";
    else if (this.phase !== "live") micLabel = "Microfono (avvia prima il video)";
    else micLabel = "Attiva microfono";
    mic.disabled = !supported || this.micBusy || (!on && this.phase !== "live");
    mic.title = micLabel;
    mic.setAttribute("aria-label", micLabel);
    mic.setAttribute("aria-pressed", String(on));
    mic.innerHTML = this.micBusy ? `<div class="spinner"></div>`
      : `<ha-icon icon="${on ? "mdi:microphone" : "mdi:microphone-off"}"></ha-icon>`;
    if (!supported && !this.statusText) {
      this.setStatus("Microfono disponibile solo via HTTPS", false, true);
    }
  }

  async action(action) {
    if (action === "session") {
      if (this.phase === "idle") return this.start();
      return this.end();
    }
    if (action === "release") {
      try {
        await this._hass.callService("button", "press", { entity_id: this.config.release_entity });
      } catch (error) {
        this.setStatus("Apertura porta non riuscita");
      }
      return;
    }
    if (action === "microphone") return this.toggleMicrophone();
    if (action === "unmute") return this.unmute();
  }

  async start() {
    this.primeVideo(); // must stay before any await (user gesture)
    const token = ++this.actionGeneration;
    this.clearStatus();
    this.sawConnecting = false;
    this.videoReady = false;
    this.setPhase("starting");
    clearTimeout(this.startTimer);
    this.startTimer = setTimeout(() => {
      if (token === this.actionGeneration && this.phase === "starting") {
        this.failStart("Nessun video dal citofono");
      }
    }, START_TIMEOUT_MS);
    try {
      await this._hass.callService("button", "press", { entity_id: this.config.start_entity });
      if (token !== this.actionGeneration) return;
      this.showStream();
      this.syncSession();
    } catch (error) {
      if (token !== this.actionGeneration) return;
      clearTimeout(this.startTimer);
      this.hideStream();
      this.setPhase("idle");
      this.setStatus("Avvio non riuscito");
    }
  }

  goLive() {
    clearTimeout(this.startTimer);
    if (this.phase !== "live") this.setPhase("live");
    this.toggleAttribute("data-waiting", !this.videoReady);
  }

  async failStart(message) {
    // Timeout or error before streaming: drop the player and release the call.
    const token = ++this.actionGeneration;
    clearTimeout(this.startTimer);
    this.hideStream();
    this.setPhase("idle");
    this.setStatus(message);
    try {
      await this._hass.callService("button", "press", { entity_id: this.config.end_entity });
    } catch (error) {
      if (token === this.actionGeneration) this.setStatus(message);
    }
  }

  async externalEnd(message) {
    // Session ended elsewhere (other device, gateway, viewer cleanup).
    this.actionGeneration += 1;
    clearTimeout(this.startTimer);
    this.hideStream();
    this.setPhase("idle");
    if (message) this.setStatus(message); else this.clearStatus();
    try { await this.disableMicrophone(); } catch (error) { this.dropMicrophone(); }
  }

  async end() {
    // Invalidate in-flight Start tokens so a late callService resolve cannot
    // recreate the stream after End (13:33 stale Connecting repro class).
    const endToken = ++this.actionGeneration;
    clearTimeout(this.startTimer);
    this.clearStatus();
    this.setPhase("ending");
    try {
      this.hideStream();
      try { await this.disableMicrophone(); } catch (error) { this.dropMicrophone(); }
      await this._hass.callService("button", "press", { entity_id: this.config.end_entity });
      if (endToken === this.actionGeneration) this.setPhase("idle");
    } catch (error) {
      if (endToken === this.actionGeneration) {
        this.setPhase("idle");
        this.setStatus("Chiusura non riuscita");
      }
    }
  }

  async toggleMicrophone() {
    if (!microphoneSupported()) {
      this.setStatus("Microfono disponibile solo via HTTPS", false, true);
      return;
    }
    this.micBusy = true;
    this.clearStatus();
    this.updateControls();
    try {
      if (this.peer) await this.disableMicrophone();
      else if (this.phase === "live") await this.enableMicrophone();
    } catch (error) {
      this.setStatus(microphoneErrorText(error));
    } finally {
      this.micBusy = false;
      this.updateControls();
    }
  }

  showStream() {
    // Start must replace any leftover stream/status from a prior attempt.
    this.hideStream();
    this.streamGeneration = (this.streamGeneration || 0) + 1;
    this.streamAttempt = 0;
    this.startStreamAttempt(this.streamGeneration);
  }

  startStreamAttempt(generation) {
    if (generation !== this.streamGeneration) return;
    this.streamAttempt += 1;
    this.stream = document.createElement("ha-camera-stream");
    this.stream.controls = false;
    this.stream.muted = false;
    this.stream.hass = this._hass;
    this.stream.stateObj = this._hass.states[this.config.camera_entity];
    this.shadowRoot.querySelector(".media").append(this.stream);
    this.watchStream(this.stream, generation);
  }

  async watchStream(stream, generation) {
    for (let index = 0; index < 20; index += 1) {
      await new Promise((resolve) => setTimeout(resolve, 500));
      if (!this.isActiveStream(stream, generation)) return;
      this.adoptVideo(stream);
      if (this.frameReady(stream)) {
        this.videoReady = true;
        this.goLive();
        this.keepAdopting(stream, generation);
        return;
      }
    }
    if (!this.isActiveStream(stream, generation)) return;
    stream.remove();
    if (this.stream === stream) this.stream = null;
    if (this.streamAttempt < 3) {
      await new Promise((resolve) => setTimeout(resolve, 300));
      if (generation !== this.streamGeneration) return;
      this.startStreamAttempt(generation);
    } else if (generation === this.streamGeneration && this.phase === "live") {
      this.removeAttribute("data-waiting");
      this.setStatus("Video non disponibile");
    }
  }

  async keepAdopting(stream, generation) {
    // Follow player reconnects (new MediaStream) and resume after iOS pauses.
    while (this.isActiveStream(stream, generation)) {
      await new Promise((resolve) => setTimeout(resolve, 1000));
      if (this.isActiveStream(stream, generation)) this.adoptVideo(stream);
    }
  }

  isActiveStream(stream, generation) {
    return generation === this.streamGeneration && stream === this.stream;
  }

  findVideo(root) {
    const direct = root.shadowRoot?.querySelector("video");
    if (direct) return direct;
    for (const element of root.shadowRoot?.querySelectorAll("*") || []) {
      const nested = element.shadowRoot && this.findVideo(element);
      if (nested) return nested;
    }
    return null;
  }

  hideStream() {
    // Bump generation first so in-flight watchStream/startStreamAttempt exit
    // without rewriting status after End or disconnect.
    this.streamGeneration = (this.streamGeneration || 0) + 1;
    this.videoReady = false;
    this.removeAttribute("data-waiting");
    this.removeAttribute("data-native");
    this.mediaMode = null;
    this.setAudioBlocked(false);
    if (this.video) {
      this.video.pause();
      this.video.srcObject = null;
      this.video.muted = false;
    }
    if (!this.stream) return;
    this.stream.remove();
    this.stream = null;
  }

  disconnectedCallback() {
    this.actionGeneration += 1;
    clearTimeout(this.startTimer);
    this.disableMicrophone().catch(() => this.dropMicrophone());
    this.hideStream();
    this.phase = "idle";
    this.clearStatus();
    this.updateControls();
  }

  async enableMicrophone() {
    // Permission is intentionally requested only from this explicit click.
    const media = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
    // Same STUN set as webrtc_config.STUN_URLS so the uplink can gather srflx.
    const peer = new RTCPeerConnection({
      iceServers: [
        { urls: "stun:stun.cloudflare.com:3478" },
        { urls: "stun:stun.cloudflare.com:53" },
        { urls: "stun:stun.linphone.org:3478" },
      ],
    });
    try {
      media.getAudioTracks().forEach((track) => peer.addTrack(track, media));
      await peer.setLocalDescription(await peer.createOffer());
      await new Promise((resolve) => {
        if (peer.iceGatheringState === "complete") return resolve();
        // Cap the wait: an unreachable STUN server must not leave the button spinning.
        setTimeout(resolve, ICE_GATHER_TIMEOUT_MS);
        peer.addEventListener("icegatheringstatechange", () =>
          peer.iceGatheringState === "complete" && resolve());
      });
      const result = await this._hass.connection.sendMessagePromise({
        type: "bticino_c100x/microphone/negotiate", entry_id: this.entryId,
        owner: this.owner, offer: peer.localDescription.sdp
      });
      await peer.setRemoteDescription({ type: "answer", sdp: result.answer });
      if (this.phase !== "live") {
        // Session ended while negotiating: close the uplink again.
        this.peer = peer; this.microphoneMedia = media;
        await this.disableMicrophone();
        return;
      }
      this.peer = peer; this.microphoneMedia = media;
    } catch (error) {
      media.getTracks().forEach((track) => track.stop()); peer.close(); throw error;
    }
  }

  async disableMicrophone() {
    if (!this.peer) return;
    // Server disables SRTP transmission first; capture stops only after acknowledgement.
    await this._hass.connection.sendMessagePromise({ type: "bticino_c100x/microphone/set",
      entry_id: this.entryId, owner: this.owner, enabled: false });
    this.dropMicrophone();
  }

  dropMicrophone() {
    this.microphoneMedia?.getTracks().forEach((track) => track.stop());
    this.peer?.close(); this.peer = null; this.microphoneMedia = null;
    this.updateControls();
  }

  setStatus(text, error = true, info = false) {
    const status = this.shadowRoot?.querySelector(".status");
    if (!status) return;
    this.statusText = text;
    status.textContent = text;
    status.classList.toggle("info", info || !error);
  }

  clearStatus() {
    this.statusText = "";
    const status = this.shadowRoot?.querySelector(".status");
    if (status) status.textContent = "";
    if (!microphoneSupported()) this.setStatus("Microfono disponibile solo via HTTPS", false, true);
  }

  get entryId() {
    const entryId = this.config.entry_id || this._hass?.entities?.[this.config.camera_entity]?.config_entry_id;
    if (!entryId) throw new Error("Unable to resolve the BTicino config entry");
    return entryId;
  }

  getCardSize() { return 5; }
  static getStubConfig() { return {}; }
}

customElements.define("bticino-c100x-card", BticinoC100XCard);
window.customCards = window.customCards || [];
window.customCards.push({ type: "bticino-c100x-card", name: "BTicino C100X Intercom",
  description: "User-initiated Classe 100X video, audio and controls" });

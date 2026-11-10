const { chromium, webkit } = require("playwright");
const BROWSER = process.env.CARD_BROWSER || "chromium";
const OUT = process.env.SCREENSHOT_DIR || require("os").tmpdir();
const ONLY = process.env.ONLY ? process.env.ONLY.split(",") : null;
const fs = require("fs");
const CARD = fs.readFileSync(__dirname + "/../../custom_components/bticino_c100x/frontend/bticino-c100x-card.js", "utf8");
const URL = "file://" + __dirname + "/harness.html";
const results = [];
const ok = (name, cond, extra = "") => { results.push([cond ? "PASS" : "FAIL", name, extra]); };

// Mock hass installed inside the page.
const SETUP = ({ scenario }) => {
  const S = "binary_sensor.demo_monitoring_session";
  window.calls = [];
  window.wsCalls = [];
  const base = {
    "camera.demo_front_door": { state: "idle", attributes: {} },
    [S]: { state: "off", attributes: { session_state: "idle", last_error: null, session_error: null } },
    "button.demo_start_monitoring": { state: "unknown", attributes: {} },
    "button.demo_end_session": { state: "unknown", attributes: {} },
    "button.demo_release_door": { state: "unknown", attributes: {} },
  };
  window.states = base;
  const entities = {};
  for (const id of Object.keys(base)) entities[id] = { entity_id: id, device_id: "dev1", platform: "bticino_c100x" };
  const card = document.createElement("bticino-c100x-card");
  window.card = card;
  window.setSession = (s) => {
    window.states = { ...window.states, [S]: { state: ["connecting","streaming"].includes(s) ? "on" : "off",
      attributes: { session_state: s } },
      "camera.demo_front_door": { state: s === "streaming" ? "streaming" : "idle", attributes: {} } };
    card.hass = window.mkHass();
  };
  window.mkHass = () => ({
    states: window.states, entities,
    callService: async (domain, service, data) => {
      window.calls.push(`${domain}.${service}:${data.entity_id}`);
      await new Promise((r) => setTimeout(r, 150));
      if (data.entity_id === "button.demo_start_monitoring") {
        setTimeout(() => window.setSession("connecting"), 100);
        if (scenario !== "timeout") setTimeout(() => window.setSession("streaming"), window.STREAM_DELAY || 1500);
      }
      if (data.entity_id === "button.demo_end_session") setTimeout(() => window.setSession("idle"), 100);
    },
    connection: { sendMessagePromise: async (msg) => {
      window.wsCalls.push(msg.type + (msg.enabled === false ? ":false" : ""));
      if (window.WS_FAIL) throw { code: "microphone_failed", message: "RuntimeError" };
      if (msg.type === "bticino_c100x/microphone/negotiate") {
        const pc = new RTCPeerConnection();
        await pc.setRemoteDescription({ type: "offer", sdp: msg.offer });
        await pc.setLocalDescription(await pc.createAnswer());
        window.remotePc = pc;
        return { answer: pc.localDescription.sdp };
      }
      return null;
    } },
  });
  card.setConfig({ type: "custom:bticino-c100x-card", camera_entity: "camera.demo_front_door",
    start_entity: "button.demo_start_monitoring", end_entity: "button.demo_end_session",
    release_entity: "button.demo_release_door", entry_id: "01TESTENTRYID00000000000000" });
  card.hass = window.mkHass();
  document.getElementById("host").append(card);
};

const info = (page) => page.evaluate(() => {
  const r = card.shadowRoot; const b = (s) => r.querySelector(s);
  const d = (el) => ({ disabled: el.disabled, title: el.title, aria: el.getAttribute("aria-label"),
    icon: el.querySelector("ha-icon")?.getAttribute("icon") || (el.querySelector(".spinner") ? "spinner" : null),
    cls: el.className, rect: (({ x, y, width, height }) => ({ x, y, width, height }))(el.getBoundingClientRect()) });
  return { phase: card.phase, session: d(b(".session")), mic: d(b(".mic")), release: d(b(".release")),
    status: b(".status").textContent, calls: [...window.calls], ws: [...window.wsCalls],
    overlay: getComputedStyle(b(".overlay")).display, streams: b(".media").querySelectorAll("ha-camera-stream").length,
    video: { paused: card.video.paused, muted: card.video.muted, hasSrc: !!card.video.srcObject, w: card.video.videoWidth,
      display: getComputedStyle(card.video).display, controls: card.video.controls, unlocked: window.isUnlocked ? window.isUnlocked(card.video) : null },
    inner: window.innerVideo ? { paused: window.innerVideo.paused, muted: window.innerVideo.muted, controls: window.innerVideo.controls } : null,
    unmuteHidden: b(".unmute").hidden, blockedPlays: window.blockedPlays };
});

async function run(name, opts, body) {
  if (ONLY && !ONLY.includes(name)) return;
  name = BROWSER + ":" + name;
  const browser = BROWSER === "webkit" ? await webkit.launch() : await chromium.launch({
    executablePath: process.env.CHROMIUM_PATH || undefined,
    args: ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream", "--autoplay-policy=no-user-gesture-required"],
  });
  const ctx = await browser.newContext({ viewport: { width: 470, height: 620 }, deviceScaleFactor: 2,
    permissions: BROWSER === "webkit" ? [] : ["microphone"] });
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  await page.goto(URL + (opts.ios ? "?ios=1" : ""));
  await page.evaluate(({ theme, video, noMic, gumFail }) => {
    document.body.className = theme; window.VIDEO_OK = video;
    if (noMic) { Object.defineProperty(navigator, "mediaDevices", { value: undefined, configurable: true }); }
    if (gumFail) navigator.mediaDevices.getUserMedia = async () => { throw new DOMException("denied", "NotAllowedError"); };
  }, { theme: opts.theme || "light", video: opts.video !== false, noMic: !!opts.noMic, gumFail: !!opts.gumFail });
  await page.addScriptTag({ content: CARD });
  await page.evaluate(SETUP, { scenario: opts.scenario || "normal" });
  await page.waitForTimeout(200);
  const shot = async (file) => page.locator("#host").screenshot({ path: OUT + "/" + (BROWSER === "webkit" ? "wk-" : "") + file });
  try { await body(page, shot); } catch (e) { ok(name + " threw", false, String(e)); }
  ok(name + " no page errors", errors.length === 0, errors.join(" | "));
  await browser.close();
}

(async () => {
  await run("idle-light", {}, async (page, shot) => {
    const i = await info(page);
    ok("idle: session icon video", i.session.icon === "mdi:video" && !i.session.disabled, JSON.stringify(i.session));
    ok("idle: mic disabled off-icon", i.mic.disabled && i.mic.icon === "mdi:microphone-off");
    ok("idle: release enabled door icon", !i.release.disabled && i.release.icon === "mdi:door-open");
    ok("idle: no status text", i.status === "", i.status);
    ok("idle: no service calls", i.calls.length === 0);
    await shot("01-idle-light.png");
  });
  await run("idle-dark", { theme: "dark" }, async (page, shot) => { await shot("02-idle-dark.png"); });

  await run("start-flow-light", { }, async (page, shot) => {
    await page.evaluate(() => { window.STREAM_DELAY = 2500; window.VIDEO_DELAY = 2200; });
    await page.evaluate(() => card.shadowRoot.querySelector(".session").click());
    await page.waitForTimeout(700);
    let i = await info(page);
    ok("loading: spinner in session", i.phase === "starting" && i.session.icon === "spinner", JSON.stringify(i.session));
    ok("loading: overlay visible", i.overlay === "flex");
    ok("loading: mic disabled", i.mic.disabled);
    ok("loading: start called once", JSON.stringify(i.calls) === JSON.stringify(["button.press:button.demo_start_monitoring"]), JSON.stringify(i.calls));
    await shot("03-loading-light.png");
    await page.waitForFunction(() => card.phase === "live", null, { timeout: 10000 });
    await page.waitForTimeout(3500);
    i = await info(page);
    ok("live: stop icon", i.session.icon === "mdi:stop" && i.session.cls.includes("live"), JSON.stringify(i.session));
    ok("live: mic enabled", !i.mic.disabled && i.mic.icon === "mdi:microphone-off");
    ok("live: overlay hidden after video", i.overlay === "none", i.overlay);
    ok("live: one stream", i.streams === 1, String(i.streams));
    await shot("04-streaming-light.png");
    // Mic on with real fake-device getUserMedia + real SDP answer.
    await page.evaluate(() => card.shadowRoot.querySelector(".mic").click());
    await page.waitForFunction(() => !!card.peer, null, { timeout: 20000 });
    await page.waitForTimeout(200);
    i = await info(page);
    ok("mic on: icon + class", i.mic.icon === "mdi:microphone" && i.mic.cls.includes("on") && !i.mic.disabled, JSON.stringify(i.mic));
    ok("mic on: negotiate sent", i.ws.includes("bticino_c100x/microphone/negotiate"), JSON.stringify(i.ws));
    ok("mic on: no status", i.status === "", i.status);
    await shot("05-mic-on-light.png");
    const iceServers = await page.evaluate(() => card.peer.getConfiguration().iceServers.map((s) => s.urls));
    ok("mic: STUN servers intact", JSON.stringify(iceServers).includes("stun.cloudflare.com:3478") && JSON.stringify(iceServers).includes("stun.linphone.org"), JSON.stringify(iceServers));
    // End
    await page.evaluate(() => card.shadowRoot.querySelector(".session").click());
    await page.waitForFunction(() => card.phase === "idle", null, { timeout: 5000 });
    await page.waitForTimeout(400);
    i = await info(page);
    ok("end: mic set false before end", i.ws.includes("bticino_c100x/microphone/set:false") && i.calls.at(-1) === "button.press:button.demo_end_session", JSON.stringify([i.ws, i.calls]));
    ok("end: back to start, no stream", i.session.icon === "mdi:video" && i.streams === 0 && !card_peer(i), JSON.stringify(i));
    ok("never pressed release", !i.calls.some((c) => c.includes("release")));
  });

  await run("streaming-dark", { theme: "dark" }, async (page, shot) => {
    await page.evaluate(() => card.shadowRoot.querySelector(".session").click());
    await page.waitForFunction(() => card.phase === "live" && card.videoReady, null, { timeout: 10000 });
    await page.evaluate(() => card.shadowRoot.querySelector(".mic").click());
    await page.waitForFunction(() => !!card.peer, null, { timeout: 20000 });
    await page.waitForTimeout(300);
    await shot("06-mic-on-dark.png");
    // External end (another device / viewer cleanup)
    await page.evaluate(() => window.setSession("idle"));
    await page.waitForTimeout(400);
    const i = await info(page);
    ok("external end: back to idle, mic dropped", i.phase === "idle" && i.session.icon === "mdi:video" && i.streams === 0 && i.mic.icon === "mdi:microphone-off" && i.mic.disabled, JSON.stringify(i));
    ok("external end: mic set false sent", i.ws.includes("bticino_c100x/microphone/set:false"), JSON.stringify(i.ws));
  });

  await run("timeout", { scenario: "timeout", video: false }, async (page, shot) => {
    await page.evaluate(() => card.shadowRoot.querySelector(".session").click());
    await page.waitForTimeout(29000);
    let i = await info(page);
    ok("timeout: still loading at 29s", i.phase === "starting", i.phase);
    await page.waitForTimeout(2500);
    i = await info(page);
    ok("timeout: back to start + error", i.phase === "idle" && i.session.icon === "mdi:video" && i.status === "Nessun video dal citofono", JSON.stringify(i));
    ok("timeout: end pressed to release call", i.calls.at(-1) === "button.press:button.demo_end_session", JSON.stringify(i.calls));
    ok("timeout: player removed", i.streams === 0);
    await shot("07-timeout-light.png");
  });

  await run("no-mic-https", { noMic: true }, async (page, shot) => {
    let i = await info(page);
    ok("no mic: disabled + https title", i.mic.disabled && i.mic.title === "Microfono disponibile solo via HTTPS" && i.status === "Microfono disponibile solo via HTTPS", JSON.stringify(i));
    await page.evaluate(() => card.shadowRoot.querySelector(".session").click());
    await page.waitForFunction(() => card.phase === "live" && card.videoReady, null, { timeout: 10000 });
    await page.waitForTimeout(300);
    i = await info(page);
    ok("no mic live: still disabled, no crash", i.mic.disabled && i.phase === "live", JSON.stringify(i.mic));
    await shot("08-no-mic-streaming-light.png");
  });
  await run("no-mic-dark", { noMic: true, theme: "dark" }, async (page, shot) => { await shot("09-no-mic-dark.png"); });

  await run("mic-denied", { gumFail: true }, async (page, shot) => {
    await page.evaluate(() => card.shadowRoot.querySelector(".session").click());
    await page.waitForFunction(() => card.phase === "live" && card.videoReady, null, { timeout: 10000 });
    await page.evaluate(() => card.shadowRoot.querySelector(".mic").click());
    await page.waitForTimeout(500);
    const i = await info(page);
    ok("mic denied: readable error", i.status === "Permesso microfono negato" && !i.mic.disabled && i.mic.icon === "mdi:microphone-off", JSON.stringify(i));
    await shot("10-mic-denied-light.png");
  });
  await run("mic-ws-fail", {}, async (page) => {
    await page.evaluate(() => { window.WS_FAIL = true; card.shadowRoot.querySelector(".session").click(); });
    await page.waitForFunction(() => card.phase === "live" && card.videoReady, null, { timeout: 10000 });
    await page.evaluate(() => card.shadowRoot.querySelector(".mic").click());
    await page.waitForFunction(() => card.shadowRoot.querySelector(".status").textContent, null, { timeout: 20000 });
    const i = await info(page);
    ok("mic ws fail: readable error", i.status === "Microfono non disponibile", i.status);
  });

  await run("autoplay-ios", { ios: true }, async (page, shot) => {
    // Real trusted tap on Start only; no further click anywhere.
    await page.locator("bticino-c100x-card button.session").click();
    await page.waitForFunction(() => card.phase === "live" && card.videoReady, null, { timeout: 15000 });
    await page.waitForTimeout(1500);
    const i = await info(page);
    ok("autoplay: card video playing with audio, no extra tap", i.video.paused === false && i.video.muted === false && i.video.hasSrc && i.video.w > 0, JSON.stringify(i.video));
    ok("autoplay: element unlocked by Start gesture", i.video.unlocked === true, JSON.stringify(i.video));
    ok("autoplay: HA inner video muted+paused (no double audio)", i.inner && i.inner.muted && i.inner.paused, JSON.stringify(i.inner));
    ok("autoplay: no native controls", i.video.controls === false && i.inner.controls === false, JSON.stringify([i.video, i.inner]));
    ok("autoplay: unmute icon hidden", i.unmuteHidden === true);
    ok("autoplay: emulation active (HA inner autoplay was blocked)", i.blockedPlays > 0, String(i.blockedPlays));
    const audio = await page.evaluate(() => card.video.srcObject.getAudioTracks().length);
    ok("autoplay: mirrored stream carries audio track", audio === 1, String(audio));
    await shot("11-autoplay-ios.png");
    await page.locator("bticino-c100x-card button.session").click();
    await page.waitForFunction(() => card.phase === "idle", null, { timeout: 5000 });
    const j = await info(page);
    ok("autoplay: End clears video", j.video.hasSrc === false && j.video.display === "none" && j.streams === 0, JSON.stringify(j.video));
  });

  await run("autoplay-fallback", { ios: true }, async (page, shot) => {
    // Start without user activation (simulates the gesture not reaching the element).
    await page.evaluate(() => { window.__noGesture = true; card.shadowRoot.querySelector(".session").click(); });
    await page.waitForFunction(() => card.phase === "live" && card.videoReady, null, { timeout: 15000 });
    await page.waitForTimeout(1200);
    await page.evaluate(() => { window.__noGesture = false; });
    let i = await info(page);
    ok("fallback: plays muted with unmute icon", i.video.paused === false && i.video.muted === true && i.unmuteHidden === false, JSON.stringify([i.video, i.unmuteHidden]));
    await shot("12-autoplay-fallback-unmute.png");
    await page.locator("bticino-c100x-card button.unmute").click();
    await page.waitForTimeout(500);
    i = await info(page);
    ok("fallback: one tap unmutes and keeps playing", i.video.paused === false && i.video.muted === false && i.unmuteHidden === true, JSON.stringify([i.video, i.unmuteHidden]));
    await page.waitForTimeout(2200);
    i = await info(page);
    ok("fallback: stays unmuted/playing after keepAdopting", i.video.paused === false && i.video.muted === false, JSON.stringify(i.video));
  });
  await run("release-mock", {}, async (page) => {
    await page.evaluate(() => card.shadowRoot.querySelector(".release").click());
    await page.waitForTimeout(300);
    const i = await info(page);
    ok("release (mock only): presses release entity once", JSON.stringify(i.calls) === JSON.stringify(["button.press:button.demo_release_door"]), JSON.stringify(i.calls));
  });
  await run("uuid-fallback", {}, async (page) => {
    const id = await page.evaluate(() => { const o = crypto.randomUUID; crypto.randomUUID = undefined; const v = newOwnerId(); crypto.randomUUID = o; return v; });
    ok("newOwnerId fallback v4", /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(id), id);
  });

  for (const r of results) console.log(r.join("  "));
  console.log(results.filter((r) => r[0] === "FAIL").length ? "SOME FAILED" : "ALL PASS");
})();
function card_peer() { return false; }

// Capture what a player page ACTUALLY fetches, including response bodies.
//
// Static reading of these bundles is a dead end (obfuscated, turbopack chunks,
// encrypted payloads). This runs the real page in Chromium with the network
// layer hooked from the very first script, so every fetch/XHR and every
// JSON/media answer is recorded. What we get back is the player's real API
// contract, which is what a resolver has to replay.
//
// usage: node hook.mjs <embedUrl> [waitMs] [outFile]
//   CDP_PORT=<n>   run several in parallel
//   CLICK=0        don't poke play buttons
//   BODY=6000      max response body chars kept per call
import { spawn } from "node:child_process";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const CHROME = process.env.CHROME_BIN
  || "/root/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome";
const PORT = Number(process.env.CDP_PORT || 9350);
const url = process.argv[2];
const waitMs = Number(process.argv[3] || 15000);
const outFile = process.argv[4] || "/tmp/hook.json";
const BODY = Number(process.env.BODY || 6000);
const doClick = process.env.CLICK !== "0";
const REFERER = process.env.REFERER || "";
const ua = process.env.UA
  || "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36";

if (!url) { console.error("usage: node hook.mjs <url> [waitMs] [outFile]"); process.exit(2); }

const profile = mkdtempSync(join(tmpdir(), "hook-"));
const chrome = spawn(CHROME, [
  "--headless=new", `--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`,
  "--no-sandbox", "--disable-dev-shm-usage", "--mute-audio", "--no-first-run",
  "--disable-gpu", "--disable-software-rasterizer",
  "--autoplay-policy=no-user-gesture-required",
  "--disable-features=IsolateOrigins,site-per-process",
  `--user-agent=${ua}`, "--window-size=1280,720", "about:blank",
], { stdio: ["ignore", "ignore", "ignore"] });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function wsUrl() {
  for (let i = 0; i < 75; i++) {
    try {
      const j = await (await fetch(`http://127.0.0.1:${PORT}/json/version`)).json();
      if (j.webSocketDebuggerUrl) return j.webSocketDebuggerUrl;
    } catch { /* not up */ }
    await sleep(200);
  }
  throw new Error("chrome did not come up");
}

function connect(ws) {
  let id = 0;
  const pending = new Map();
  const handlers = [];
  const ready = new Promise((res, rej) => {
    ws.addEventListener("open", () => res());
    ws.addEventListener("error", (e) => rej(new Error("ws " + String(e?.message || e))));
  });
  ws.addEventListener("message", (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) {
      const { res, rej } = pending.get(m.id);
      pending.delete(m.id);
      m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result);
    } else if (m.method) for (const h of handlers) h(m);
  });
  const send = (method, params = {}, sessionId) => new Promise((res, rej) => {
    const msg = { id: ++id, method, params };
    if (sessionId) msg.sessionId = sessionId;
    pending.set(msg.id, { res, rej });
    ws.send(JSON.stringify(msg));
  });
  return { send, on: (h) => handlers.push(h), ready };
}

// Injected before any page script: wraps fetch/XHR so we see the requests the
// player builds in JavaScript (the ones the CDP network log already shows) and,
// more importantly, the bodies they answer with.
const HOOK_SRC = `(() => {
  if (window.__hooked) return 'already';
  window.__hooked = true;
  window.__calls = [];
  const keep = ${BODY};
  const rec = (o) => { try { window.__calls.push(o); } catch (e) {} };
  const clip = (s) => (typeof s === 'string' ? s.slice(0, keep) : s);
  const origFetch = window.fetch;
  window.fetch = async function (...args) {
    const req = args[0];
    const url = typeof req === 'string' ? req : (req && req.url) || '';
    const opt = args[1] || {};
    const entry = { kind: 'fetch', url: String(url), method: (opt.method || (req && req.method) || 'GET'),
                    body: clip(typeof opt.body === 'string' ? opt.body : null) };
    try {
      const r = await origFetch.apply(this, args);
      entry.status = r.status;
      entry.contentType = r.headers.get('content-type') || '';
      if (/json|text|mpegurl|x-mpegurl|octet-stream/i.test(entry.contentType)) {
        try { entry.response = clip(await r.clone().text()); } catch (e) {}
      }
      rec(entry);
      return r;
    } catch (e) { entry.error = String(e); rec(entry); throw e; }
  };
  const origOpen = XMLHttpRequest.prototype.open;
  const origSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (m, u, ...rest) {
    this.__m = m; this.__u = u; return origOpen.call(this, m, u, ...rest);
  };
  XMLHttpRequest.prototype.send = function (body) {
    const x = this;
    const entry = { kind: 'xhr', url: String(x.__u), method: x.__m,
                    body: clip(typeof body === 'string' ? body : null) };
    x.addEventListener('load', () => {
      entry.status = x.status;
      entry.contentType = x.getResponseHeader('content-type') || '';
      try {
        const t = x.responseType === '' || x.responseType === 'text' ? x.responseText : null;
        if (t) entry.response = clip(t);
      } catch (e) {}
      rec(entry);
    });
    x.addEventListener('error', () => { entry.error = 'xhr error'; rec(entry); });
    return origSend.call(this, body);
  };
  // players usually stash the real payload on the window too
  window.__dumpState = () => {
    const out = {};
    for (const k of Object.keys(window)) {
      try {
        const v = window[k];
        if (!v || typeof v === 'function') continue;
        if (typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean') out[k] = String(v).slice(0, 200);
        else if (Array.isArray(v) && v.length && v.length < 40) out[k] = v.slice(0, 6);
        else if (typeof v === 'object' && ['playlist','sources','sourcesBackup','backups','media','config','stream','data','player'].some(s => k.toLowerCase().includes(s))) out[k] = v;
      } catch (e) {}
    }
    return out;
  };
  return 'ok';
})()`;

const MEDIA = /\.m3u8|\.mpd|\/master\.|\/playlist\.|\/manifest|\.mp4(\?|$)|\.webm(\?|$)/i;
const NOISE = /googletagmanager|google-analytics|doubleclick|jwpcdn|prebid|adsbygoogle|fundingchoices|cloudflareinsights|umami\.|histats|/i;

async function verify(c) {
  const headers = { "User-Agent": ua, Accept: "*/*" };
  const hdrs = { ...headers };
  if (c.referer) hdrs.Referer = c.referer;
  try {
    const r = await fetch(c.url, { headers: hdrs, redirect: "follow", signal: AbortSignal.timeout(15000) });
    const ct = (r.headers.get("content-type") || "").toLowerCase();
    let head = "";
    if (ct.includes("mpegurl") || ct.includes("octet") || ct.includes("text/plain") || ct.includes("json")) {
      head = (await r.text()).slice(0, 200);
    }
    const ok = r.status === 200
      && (ct.includes("mpegurl") || ct.includes("dash") || ct.includes("video/")
          || head.includes("#EXTM3U") || head.includes("<MPD"));
    return { status: r.status, contentType: ct, ok, extm3u: head.includes("#EXTM3U") };
  } catch (e) {
    return { status: 0, contentType: "", ok: false, error: String(e).slice(0, 100) };
  }
}

async function main() {
  const conn = connect(new WebSocket(await wsUrl()));
  await conn.ready;
  const { send, on } = conn;
  const targets = await send("Target.getTargets");
  const page = targets.targetInfos.find((t) => t.type === "page");
  const { sessionId } = await send("Target.attachToTarget", { targetId: page.targetId, flatten: true });
  const t = (m, p) => send(m, p, sessionId);

  const media = new Map();
  const allReqs = [];
  const sessions = new Set([sessionId]);
  const onChain = [];

  async function attachChildren(kind) {
    try {
      const r = await send("Target.getTargets");
      for (const ti of r.targetInfos) {
        if (ti.type !== "iframe" && ti.type !== "page") continue;
        if (sessions.has(ti.targetId)) continue;
        try {
          const a = await send("Target.attachToTarget", { targetId: ti.targetId, flatten: true });
          sessions.add(ti.targetId);
          await send("Network.enable", {}, a.sessionId);
          await send("Page.enable", {}, a.sessionId);
          await send("Runtime.enable", {}, a.sessionId);
          await applyHeaders(a.sessionId);
          await send("Page.addScriptToEvaluateOnNewDocument", { source: HOOK_SRC }, a.sessionId);
          if (kind === "start") {
            try { await send("Page.navigate", { url: ti.url }, a.sessionId); } catch (e) {}
          }
        } catch (e) { /* target may be gone */ }
      }
    } catch (e) { /* ignore */ }
  }

  // These embeds are meant to be loaded from a page on the streaming site, and
  // some gate on that: without the referer they sit showing only a poster.
  const EXTRA = {};
  if (REFERER) { EXTRA.Referer = REFERER; EXTRA.Origin = new URL(REFERER).origin; }
  if (Object.keys(EXTRA).length) EXTRA["User-Agent"] = ua;
  const applyHeaders = (sid) => Object.keys(EXTRA).length
    ? send("Network.setExtraHTTPHeaders", { headers: EXTRA }, sid).catch(() => {})
    : Promise.resolve();
  onChain.push(setInterval(() => { attachChildren("live"); }, 1500));

  on((m) => {
    if (m.method === "Target.attachedToTarget") {
      const sid = m.params.sessionId;
      sessions.add(sid);
      send("Network.enable", {}, sid).catch(() => {});
      send("Runtime.enable", {}, sid).catch(() => {});
      send("Page.addScriptToEvaluateOnNewDocument", { source: HOOK_SRC }, sid).catch(() => {});
    }
    if (m.method === "Network.requestWillBeSent") {
      const u = m.params.request.url;
      if (process.env.DEBUG_ALL) allReqs.push({ url: u.slice(0, 170), type: m.params.type, frame: m.sessionId === sessionId ? "top" : "child" });
      if (MEDIA.test(u) && !NOISE.test(u) && !media.has(u)) {
        media.set(u, { url: u, referer: m.params.request.headers?.Referer || url,
                       via: m.params.type, frame: m.sessionId === sessionId ? "top" : "child" });
      }
    }
    if (m.method === "Network.responseReceived") {
      const u = m.params.response.url;
      if (media.has(u)) {
        const c = media.get(u);
        c.status = m.params.response.status;
        c.contentType = m.params.response.mimeType;
      }
    }
  });

  await t("Network.enable");
  await t("Page.enable");
  await t("Runtime.enable");
  await applyHeaders(sessionId);
  await t("Target.setAutoAttach", { autoAttach: true, waitForDebuggerOnStart: false, flatten: true });
  // register the hook for every frame the page is about to create
  await t("Page.addScriptToEvaluateOnNewDocument", { source: HOOK_SRC });
  await t("Page.navigate", { url });
  await sleep(waitMs);
  await attachChildren("live");

  const press = `(() => {
    const v = document.querySelector('video');
    try { v && v.play && v.play(); } catch (e) {}
    for (const [x,y] of [[640,360],[640,300],[420,360],[860,360]]) {
      const el = document.elementFromPoint(x,y);
      try { el && el.click(); } catch (e) {}
    }
    for (const e of document.querySelectorAll('[class*=play],[id*=play],button')) {
      try { e.click(); } catch (err) {}
    }
    return true;
  })()`;
  if (doClick) {
    await t("Runtime.evaluate", { expression: press, returnByValue: true });
    await sleep(waitMs);
  }

  // pull the in-page hook log from every attached frame, then the loose
  // window state as a fallback
  let calls = [];
  for (const sid of sessions) {
    try {
      const r = await send("Runtime.evaluate",
        { expression: "JSON.stringify(window.__calls || [])", returnByValue: true }, sid);
      const got = JSON.parse(r.result?.value || "[]");
      if (got.length) calls = calls.concat(got);
    } catch (e) { /* frame gone */ }
  }
  let state = {};
  try {
    const r = await t("Runtime.evaluate", { expression: "JSON.stringify(window.__dumpState ? window.__dumpState() : {})", returnByValue: true });
    state = JSON.parse(r.result?.value || "{}");
  } catch (e) { state = { error: String(e).slice(0, 120) }; }
  for (const iv of onChain) clearInterval(iv);

  // coalesce: hook records give bodies, network gives media hits without a body
  const byUrl = new Map();
  for (const c of calls) {
    if (!c || !c.url) continue;
    if (NOISE.test(c.url)) continue;
    if (!byUrl.has(c.url)) byUrl.set(c.url, { url: c.url, kind: c.kind, method: c.method, status: c.status, contentType: c.contentType, requestBody: c.body, response: c.response });
  }
  const interesting = [...byUrl.values()].filter((c) =>
    c.response || MEDIA.test(c.url) || /json|api|token|sources?|playlist|data/i.test(c.url));

  const found = [];
  for (const c of media.values()) {
    const v = await verify(c);
    found.push({ ...c, ...v });
  }
  const live = found.filter((f) => f.ok);

  const out = {
    embed: url,
    hooked: calls.length > 0,
    mediaCandidates: found.map((f) => ({ url: f.url, status: f.status, ok: f.ok, contentType: f.contentType, referer: f.referer })),
    live: live.map((l) => ({ url: l.url, referer: l.referer, contentType: l.contentType, status: l.status })),
    calls: interesting.slice(0, 40),
    state,
  };
  if (process.env.DEBUG_ALL) out.allRequests = allReqs;
  writeFileSync(outFile, JSON.stringify(out, null, 2));
  console.log(JSON.stringify({ embed: url, hooked: out.hooked, media: found.length, live: live.length, calls: interesting.length }, null, 2));
  for (const l of live.slice(0, 4)) console.log("  LIVE", l.url.slice(0, 110));
  for (const c of interesting.slice(0, 8)) {
    const bodyBits = c.response ? String(c.response).slice(0, 130).replace(/\s+/g, " ") : "";
    console.log("  call", c.method, String(c.url).slice(0, 90), "->", c.status, "|", bodyBits);
  }
}

main().then(() => { try { chrome.kill(); } catch {} process.exit(0); })
  .catch((e) => {
    console.error("ERR", e.message);
    try { chrome.kill(); } catch {}
    writeFileSync(outFile, JSON.stringify({ embed: url, error: e.message }, null, 2));
    process.exit(1);
  });


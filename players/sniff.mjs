// Generic embed-player stream sniffer.
//
// Technique that works on these players: don't try to read their (often
// obfuscated) JavaScript statically. Load the real embed page in Chromium and
// watch the network: the player itself asks the CDN for its playlist, and that
// request carries the token/signature the CDN wants. Whatever we can replay
// afterwards we verify from Node so nothing unproven is reported.
//
// usage: node sniff.mjs <embedUrl> [waitMs] [outFile]
//   CDP_PORT=<port> to run several in parallel
//   CLICK=0 to skip the play-button poking
import { spawn } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const CHROME = process.env.CHROME_BIN
  || "/root/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome";
const PORT = Number(process.env.CDP_PORT || 9340);
const url = process.argv[2];
const waitMs = Number(process.argv[3] || 12000);
const outFile = process.argv[4] || "/tmp/sniff.json";
const doClick = process.env.CLICK !== "0";
const ua = process.env.UA
  || "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36";

if (!url) { console.error("usage: node sniff.mjs <url> [waitMs] [outFile]"); process.exit(2); }

const profile = mkdtempSync(join(tmpdir(), "sniff-"));
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
      const r = await fetch(`http://127.0.0.1:${PORT}/json/version`);
      const j = await r.json();
      if (j.webSocketDebuggerUrl) return j.webSocketDebuggerUrl;
    } catch { /* not up yet */ }
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
    ws.addEventListener("error", (e) => rej(new Error("ws error " + String(e?.message || e))));
  });
  ws.addEventListener("message", (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) {
      const { res, rej } = pending.get(m.id);
      pending.delete(m.id);
      m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result);
    } else if (m.method) {
      for (const h of handlers) h(m);
    }
  });
  const send = (method, params = {}, sessionId) => new Promise((res, rej) => {
    const msg = { id: ++id, method, params };
    if (sessionId) msg.sessionId = sessionId;
    pending.set(msg.id, { res, rej });
    ws.send(JSON.stringify(msg));
  });
  return { send, on: (h) => handlers.push(h), ready };
}

// Anything that smells like a media playlist or a progressive video file.
const MEDIA = /\.m3u8|\.mpd|\/master\.|\/playlist\.|\/manifest|\.mp4(\?|$)|\.webm(\?|$)/i;
// ...but not the page's own UI/analytics noise.
const NOISE = /googletagmanager|google-analytics|doubleclick|jwplayer\.js|jwpcdn|prebid|adsbygoogle|fundingchoices/i;

async function verify(cand) {
  const headers = { "User-Agent": ua, Accept: "*/*" };
  if (cand.referer) headers.Referer = cand.referer;
  try {
    const ctl = AbortSignal.timeout(15000);
    const r = await fetch(cand.url, { headers, redirect: "follow", signal: ctl });
    const ct = (r.headers.get("content-type") || "").toLowerCase();
    let head = "";
    if (ct.includes("mpegurl") || ct.includes("text") || ct.includes("octet")) {
      head = (await r.text()).slice(0, 200);
    }
    const ok = r.status === 200
      && (ct.includes("mpegurl") || ct.includes("dash") || ct.includes("video/")
          || head.includes("#EXTM3U") || head.includes("<MPD"));
    return { status: r.status, contentType: ct, ok, finalUrl: r.url, extm3u: head.includes("#EXTM3U") };
  } catch (e) {
    return { status: 0, contentType: "", ok: false, error: String(e).slice(0, 120) };
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

  const cands = new Map();
  const allReqs = [];
  on((m) => {
    if (m.sessionId !== sessionId) return;
    if (m.method === "Network.requestWillBeSent") {
      const u = m.params.request.url;
      if (process.env.DEBUG_ALL) allReqs.push({ url: u.slice(0, 150), type: m.params.type });
      if (!MEDIA.test(u) || NOISE.test(u)) return;
      if (!cands.has(u)) cands.set(u, { url: u, referer: m.params.request.headers?.Referer || url, via: m.params.type });
    }
    if (m.method === "Network.responseReceived") {
      const u = m.params.response.url;
      if (!MEDIA.test(u) || NOISE.test(u)) return;
      const c = cands.get(u) || { url: u, referer: url, via: m.params.type };
      c.status = m.params.response.status;
      c.contentType = m.params.response.mimeType;
      c.fromNetwork = true;
      cands.set(u, c);
    }
  });

  await t("Network.enable");
  await t("Page.enable");
  await t("Runtime.enable");
  await t("Page.navigate", { url });
  await sleep(Math.min(waitMs, 15000));

  // Some players only start loading after the overlay/play button is hit.
  if (doClick) {
    await t("Runtime.evaluate", {
      expression: `(() => {
        const v = document.querySelector('video');
        try { v && v.play(); } catch (e) {}
        const spots = [[640,360],[640,300],[400,360],[840,360]];
        for (const [x,y] of spots) {
          const el = document.elementFromPoint(x,y);
          if (el) { el.click(); }
        }
        document.querySelectorAll('[class*=play],[id*=play],button').forEach(e=>{
          try { e.click(); } catch (err) {}
        });
        return !!v;
      })()`, returnByValue: true,
    });
    await sleep(waitMs);
  }

  const found = [];
  for (const c of cands.values()) {
    const v = await verify(c);
    found.push({ ...c, ...v });
  }
  const live = found.filter((f) => f.ok);
  const title = await t("Runtime.evaluate", { expression: "document.title", returnByValue: true })
    .then((r) => r.result?.value).catch(() => null);

  const out = {
    embed: url,
    pageTitle: title,
    candidates: found.length,
    live: live.map((l) => ({ url: l.url, referer: l.referer, contentType: l.contentType, status: l.status })),
    dead: found.filter((f) => !f.ok).map((f) => ({ url: f.url, status: f.status, contentType: f.contentType, error: f.error })),
  };
  if (process.env.DEBUG_ALL) out.allRequests = allReqs;
  writeFileSync(outFile, JSON.stringify(out, null, 2));
  console.log(JSON.stringify(out, null, 2));
}

main().then(() => { try { chrome.kill(); } catch {} process.exit(0); })
  .catch((e) => {
    console.error("ERR", e.message);
    try { chrome.kill(); } catch {}
    writeFileSync(outFile, JSON.stringify({ embed: url, error: e.message, live: [], dead: [] }, null, 2));
    process.exit(1);
  });

// Read what the xpass player computes at runtime: window.backups (the source
// list) and the JW Player playlist. The page encrypts its /data payload and
// routes through obfuscated JS, so the only faithful way to learn the sources
// is to let its own code run and then read the result.
// usage: CDP_PORT=9340 node xpass_backups.mjs <embed-url> [waitMs]
// prints JSON: { title, playlist:[{file,label}], backups:[{id,url}] }
import { spawn } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const CHROME = "/root/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome";
const PORT = Number(process.env.CDP_PORT || 9340);
const url = process.argv[2];
const waitMs = Number(process.argv[3] || 14000);
if (!url) { console.error("usage: node xpass_backups.mjs <embed-url> [waitMs]"); process.exit(2); }

const profile = mkdtempSync(join(tmpdir(), "xp-"));
const chrome = spawn(CHROME, ["--headless=new","--no-sandbox","--disable-setuid-sandbox","--disable-gpu","--disable-dev-shm-usage","--no-first-run","--mute-audio","--autoplay-policy=no-user-gesture-required","--window-size=1280,800",`--remote-debugging-port=${PORT}`,`--user-data-dir=${profile}`,"about:blank"], { stdio: ["ignore","pipe","pipe"] });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const jget = async (p) => (await fetch(`http://127.0.0.1:${PORT}${p}`)).json();
let ws, nextId = 1; const pending = new Map(); const events = [];
function send(method, params = {}, sessionId) {
  const id = nextId++; const msg = { id, method, params }; if (sessionId) msg.sessionId = sessionId;
  ws.send(JSON.stringify(msg));
  return new Promise((res, rej) => { pending.set(id, { res, rej }); setTimeout(() => { if (pending.has(id)) { pending.delete(id); rej(new Error("timeout " + method)); } }, 30000); });
}
const open = () => new Promise((resolve, reject) => { ws.onmessage = (ev) => { const m = JSON.parse(ev.data); if (m.id && pending.has(m.id)) { const p = pending.get(m.id); pending.delete(m.id); m.error ? p.rej(new Error(m.error.message)) : p.res(m.result); } else if (m.method) events.push(m); }; ws.onerror = reject; ws.onopen = resolve; });

try {
  for (let i = 0; i < 60; i++) { try { await jget("/json/version"); break; } catch { await sleep(250); } }
  const ver = await jget("/json/version");
  ws = new WebSocket(ver.webSocketDebuggerUrl); await open();
  const { targetId } = await send("Target.createTarget", { url: "about:blank" });
  const { sessionId } = await send("Target.attachToTarget", { targetId, flatten: true });
  await send("Page.enable", {}, sessionId);
  await send("Runtime.enable", {}, sessionId);
  await send("Page.navigate", { url }, sessionId);
  await sleep(waitMs);

  const ev = async (expr) => { try { const r = await send("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true }, sessionId); return r.result.value; } catch (e) { return "ERR " + e.message; } };
  const backups = await ev(`JSON.stringify({
    title: document.title,
    backups: (window.backups || []).map(b => ({ id: b && b.id, url: b && b.url, label: b && b.label })),
    playlist: (() => { try { const p = jwplayer().getPlaylist() || []; return p.map(i => ({ file: i.file, label: i.label, tracks: (i.tracks||[]).map(t=>({file:t.file,label:t.label,language:t.language})) })); } catch (e) { return "ERR " + e.message; } })()
  })`);
  console.log(backups);
} catch (e) {
  console.log(JSON.stringify({ error: String((e && e.message) || e) }));
} finally {
  try { chrome.kill("SIGKILL"); } catch {}
  try { rmSync(profile, { recursive: true, force: true }); } catch {}
  process.exit(0);
}

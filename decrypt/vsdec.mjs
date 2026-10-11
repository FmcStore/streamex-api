// Decrypt data.vidsrc.sh stream_urls (base64 ChaCha20) with the player own wasm.
// usage: node vsdec.mjs <base64-encrypted> <wasm-url>
import { readFileSync } from 'fs';

async function main() {
  const encB64 = process.argv[2];
  const wasmArg = process.argv[3];
  let bytes;
  if (wasmArg.startsWith('http')) {
    const r = await fetch(wasmArg);
    bytes = new Uint8Array(await r.arrayBuffer());
  } else {
    bytes = new Uint8Array(readFileSync(wasmArg));
  }
  const mod = await WebAssembly.compile(bytes);
  const inst = await WebAssembly.instantiate(mod, {});
  const ex = inst.exports;
  const bin = Buffer.from(encB64, 'base64');
  const ptr = ex.alloc(bin.length);
  new Uint8Array(ex.memory.buffer, ptr, bin.length).set(bin);
  const outLen = ex.decrypt(ptr, bin.length);
  const out = new Uint8Array(ex.memory.buffer, ptr + 12, outLen);
  const txt = new TextDecoder().decode(out);
  console.log(txt);
}

main().catch((e) => { console.error('ERR', e); process.exit(1); });

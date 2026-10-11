#!/usr/bin/env python3
"""
Stream resolution for streamex.pw player #1.

Chain (all GETs, signed and single use):
  1. https://vsembed.ru/vs_src.php?type=movie&id=<tmdb>
       -> {"src": "https://stellarconductornexus.com/embed/movie/<id>?vs=<token>"}
  2. that page carries window.CFG with the real playerUrl (fresh ?vs token)
  3. the player page carries window.CONFIG with the stream api + a single use
     apiToken
  4. https://data.vidsrc.sh/api.php?...&stream_urls&api_token=<token>
       -> data.stream_urls is a base64 ChaCha20 blob, plus vs.wasm_url
  5. the per window wasm decryptor turns that blob into the HLS url list

Node runs the WebAssembly (no python wasm runtime is installed here).
"""
import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
HERE = os.path.dirname(os.path.abspath(__file__))
DECODER = os.path.join(HERE, "decrypt", "vsdec.mjs")
NODE = shutil.which("node") or "/usr/bin/node"


class StreamError(Exception):
    def __init__(self, status, message, chain=None):
        super().__init__(message)
        self.status = status
        self.chain = chain or []


def _get(url, referer, origin=None, timeout=45):
    h = {"User-Agent": UA, "Referer": referer, "Accept": "*/*"}
    if origin:
        h["Origin"] = origin
    req = urllib.request.Request(url, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001
            pass
        raise StreamError(502, "upstream menolak (HTTP %d) %s" % (e.code, body))
    except Exception as e:  # noqa: BLE001
        raise StreamError(502, "%s: %s" % (type(e).__name__, e))


def _cfg(html, var):
    m = re.search(r"window\.%s = (\{.*?\});" % var, html, re.S)
    if not m:
        raise StreamError(502, "halaman player tidak memuat window.%s" % var)
    try:
        return json.loads(m.group(1))
    except ValueError as e:
        raise StreamError(502, "window.%s bukan JSON: %s" % (var, e))


def decrypt(encrypted_b64, wasm_url):
    """Run the site's own wasm decryptor through node."""
    if not shutil.which("node") and not os.path.exists(NODE):
        raise StreamError(500, "node tidak tersedia untuk dekripsi wasm")
    p = subprocess.run([NODE, DECODER, encrypted_b64, wasm_url],
                       capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        raise StreamError(502, "dekripsi stream gagal: %s"
                          % (p.stderr.strip()[:300] or "unknown"))
    return [u for u in p.stdout.strip().split("\n") if u]


def origin_of(url):
    m = re.match(r"^(https://[^/]+)", url or "")
    return m.group(1) if m else None


def mint_token(stream_url):
    """Each media host needs a short lived, IP bound ?token= on its playlist.

    The player asks the host itself: GET <origin>/generate.php -> plain token.
    Without it the host answers 401 'no token'.
    """
    origin = origin_of(stream_url)
    if not origin:
        return None
    status, body = _get(origin + "/generate.php", origin + "/")
    token = body.strip()
    if status != 200 or not token or token.startswith("<"):
        return None
    return token


def playable_url(url, token=None):
    if not token:
        return url
    if "__TOKEN__" in url:
        return url.replace("__TOKEN__", token)
    return "%s%stoken=%s" % (url, "&" if "?" in url else "?", urllib.parse.quote(token, safe=""))


def resolve(tmdb_id, media_type="movie", season=None, episode=None):
    tid = str(tmdb_id)
    if not tid.isdigit():
        raise StreamError(400, "tmdbId harus angka")
    chain = []
    if media_type == "movie":
        embed = "https://vsembed.ru/embed/movie?tmdb=%s" % tid
        srcapi = "https://vsembed.ru/vs_src.php?type=movie&id=%s" % tid
    elif media_type == "tv":
        s = str(season or 1)
        e = str(episode or 1)
        embed = "https://vsembed.ru/embed/tv?tmdb=%s&season=%s&episode=%s" % (tid, s, e)
        srcapi = ("https://vsembed.ru/vs_src.php?type=tv&id=%s&season=%s&episode=%s"
                  % (tid, s, e))
    else:
        raise StreamError(400, "media type harus movie atau tv")
    chain.append(srcapi)
    _, body = _get(srcapi, embed, "https://vsembed.ru")
    try:
        inner = json.loads(body)["src"]
    except (ValueError, KeyError):
        raise StreamError(502, "vs_src.php tidak mengembalikan src",
                          chain)
    chain.append(inner)
    m = re.match(r"^(https://[^/]+)", inner)
    if not m:
        raise StreamError(502, "src bukan URL absolut", chain)
    host = m.group(1)
    _, stage = _get(inner, embed, "https://vsembed.ru")
    player_url = host + _cfg(stage, "CFG")["playerUrl"]
    chain.append(player_url)
    _, page = _get(player_url, inner, host)
    cfg = _cfg(page, "CONFIG")
    if cfg.get("apiTokenRefresh"):
        _, page = _get(player_url, inner, host)
        cfg = _cfg(page, "CONFIG")
    api = cfg.get("api")
    if not api:
        base = cfg.get("streamBase")
        if not base:
            raise StreamError(502, "player tidak punya endpoint stream", chain)
        api = "%s&season=%s&episode=%s&stream_urls" % (
            base, cfg.get("season"), cfg.get("episode"))
    if cfg.get("apiToken"):
        api = "%s&api_token=%s" % (api, urllib.parse.quote(cfg["apiToken"]))
    chain.append(re.sub(r"api_token=.*$", "api_token=<minted>", api))
    _, body = _get(api, player_url, host)
    try:
        payload = json.loads(body)
    except ValueError:
        raise StreamError(502, "endpoint stream bukan JSON: %s" % body[:200], chain)
    d = payload.get("data") or {}
    urls = d.get("stream_urls")
    if isinstance(urls, str):
        wasm_url = (payload.get("vs") or {}).get("wasm_url")
        if not wasm_url:
            raise StreamError(502, "stream terenkripsi tapi wasm_url tidak ada", chain)
        chain.append(wasm_url.split("?")[0] + "?w=<window>")
        urls = decrypt(urls, wasm_url)
    elif not isinstance(urls, list):
        urls = []
    d["stream_urls"] = urls
    d["chain"] = chain
    if not urls:
        raise StreamError(404, "tidak ada sumber stream untuk %s %s%s"
                          % (media_type, tid,
                             "" if media_type == "movie" else " S%sE%s" % (season, episode)))

    # Each stream host needs its own token before the playlist will answer.
    resolved = []
    for u in urls:
        entry = {"raw": u, "token": None, "playable": u, "tokenError": None}
        try:
            tok = mint_token(u)
            if tok:
                entry["token"] = tok
                entry["playable"] = playable_url(u, tok)
            else:
                entry["tokenError"] = "generate.php tidak mengembalikan token"
        except StreamError as e:
            entry["tokenError"] = str(e)
        resolved.append(entry)
    d["sources"] = resolved
    d["streams"] = [e["playable"] for e in resolved]
    d["streams_needing_token"] = sum(1 for e in resolved if e["token"])
    return d


if __name__ == "__main__":
    import sys
    if sys.argv[1] == "tv":
        out = resolve(sys.argv[2], "tv", sys.argv[3], sys.argv[4])
    else:
        out = resolve(sys.argv[1])
    print(json.dumps(out, indent=1)[:2000])

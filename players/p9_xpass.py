#!/usr/bin/env python3
"""
Player #9 - play.xpass.top - resolver.

What the player actually does (traced with a real browser, see below):

    GET /e/movie/<tmdb>?autostart=true
        the HTML carries
          var dataUrl  = "/data/movie/<tmdb>?autostart=true&token=<opaque>"
          var suburl   = "https://sub.1x2.space/api/movie/<tmdb>"
          var backups  = []                       <- filled in by the player
    GET /data/...          -> an AES encrypted blob (not decompressable, not JSON)
    GET /mdata/<ID>/1/playlist.json  -> {"playlist":[{"sources":[{"file": HLS}]}]}
    GET /vip/<ID>/1/playlist.json    -> same shape, other CDN
    GET /mvid/<ID>/1/playlist.json   -> same shape, third CDN
    GET https://sub.1x2.space/api/<type>/<tmdb>[/s/e]  -> subtitle list (vtt)

The <ID> is not derivable from the /data body: the body is encrypted and the
routing lives in a javascript-obfuscator bundle whose property names are also
rotated, so a static read gives no path from one to the other. The player
itself keeps the answer in window.backups, which the page populates only when
its own script runs. This resolver therefore runs that page in headless
Chromium over CDP (the same request the real player performs, no DOM scraping)
and reads window.backups back out.

The per-source endpoints are self contained once the id is known, so the
expensive part is one browser run; the id is stable per title and is cached.
"""
import json
import os
import re
import shutil
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request

NAME = "xpass"
HOST = "play.xpass.top"
# the CDN mirrors reject any request that does not carry the player's own
# origin, so both the probe and the caller need this
EMBED_REFERER = "https://%s/" % HOST

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
HERE = os.path.dirname(os.path.abspath(__file__))
READER = os.path.join(HERE, "p9_backups.mjs")
NODE = shutil.which("node") or "/usr/bin/node"

_ID_CACHE = {}
_PORT_LOCK = threading.Lock()
_CDP_BASE = 9300


class PlayerError(Exception):
    pass


def _get(url, referer, timeout=30):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Referer": referer, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:300]
    except Exception:  # noqa: BLE001  a dead CDN must not abort the resolve
        return 0, ""


def embed_url(tmdb_id, media_type="movie", season=None, episode=None):
    tid = str(tmdb_id)
    if media_type == "tv":
        return ("https://%s/e/tv/%s/%s/%s?autostart=true"
                % (HOST, tid, season or 1, episode or 1))
    return "https://%s/e/movie/%s?autostart=true" % (HOST, tid)


def _read_backups(embed, timeout=180):
    """Run the player page and read window.backups the way it computes it."""
    if not shutil.which("node") and not os.path.exists(NODE):
        raise PlayerError("node tidak tersedia untuk membaca window.backups")
    env = dict(os.environ)
    # A Chromium debug port is single-owner: two runs that pick the same one
    # collide and the loser comes back with no sources at all (which reads as
    # "this title has no streams"). Derive the port from the title, not the pid,
    # and hold a lock for it so the same title can never overlap itself.
    port = _CDP_BASE + (abs(hash(embed)) % 90)
    env["CDP_PORT"] = str(port)
    with _PORT_LOCK:
        p = subprocess.run([NODE, READER, embed, "14000"],
                           capture_output=True, text=True, timeout=timeout, env=env)
    if p.returncode != 0:
        raise PlayerError("reader gagal: %s" % (p.stderr.strip()[:200] or "unknown"))
    try:
        return json.loads(p.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise PlayerError("reader tidak mengembalikan JSON: %s" % p.stdout[:200])


def _subtitles(tmdb_id, media_type, season, episode, timeout=30):
    tid = str(tmdb_id)
    if media_type == "tv":
        api = ("https://sub.1x2.space/api/tv/%s/%s/%s"
               % (tid, season or 1, episode or 1))
    else:
        api = "https://sub.1x2.space/api/movie/%s" % tid
    st, body = _get(api, "https://%s/" % HOST, timeout)
    if st != 200:
        return []
    try:
        rows = json.loads(body)
    except ValueError:
        return []
    out = []
    for r in rows if isinstance(rows, list) else []:
        if r.get("status") != "cached":
            continue
        u = r.get("url") or ""
        out.append({
            "label": r.get("label"),
            "language": r.get("language"),
            "url": u if u.startswith("http") else "https://sub.1x2.space" + u,
            "size": r.get("size"),
        })
    return out


def _sources_from_pages(backups, referer, source_id, timeout=12):
    """Query the per-source playlist endpoints the player itself lists, plus
    the same id on each of the sibling CDN prefixes, with a short timeout so
    a dead CDN cannot stall resolution."""
    found = []
    seen = set()
    eps = []
    for b in backups or []:
        u = (b or {}).get("url") or ""
        if u.startswith("/") and u not in seen:
            seen.add(u)
            eps.append(u)
    # also try the player's known prefixes on its disclosed source id
    for tpl in ("/mdata/%s/1/playlist.json", "/mvid/%s/1/playlist.json",
                "/vip/%s/1/playlist.json"):
        u = tpl % source_id
        if u not in seen:
            seen.add(u)
            eps.append(u)
    for u in eps:
        url = "https://%s%s" % (HOST, u)
        st, body = _get(url, referer, timeout)
        if st != 200:
            continue
        try:
            payload = json.loads(body)
        except ValueError:
            continue
        for item in (payload.get("playlist") or []):
            for src in (item.get("sources") or []):
                f = src.get("file")
                # the player's own list carries placeholder rows ("/video/error");
                # only absolute media URLs are real sources
                if not f or not str(f).startswith("http"):
                    continue
                found.append({
                    "raw": f,
                    "playable": f,
                    "type": src.get("type") or "hls",
                    "label": src.get("label"),
                    "sourceId": src.get("id") or source_id,
                    "endpoint": url,
                    "token": None,
                })
    return found


def _sources_parallel(backups, referer, source_id, timeout=12, workers=None):
    """Same as _sources_from_pages but concurrent.

    The player lists ~20 sibling mirrors and each endpoint answers in seconds;
    queried one after another that is minutes of latency for a single title.
    Order is preserved so `sources[0]` stays the player's own first choice.
    """
    import concurrent.futures as _cf

    seen, eps = set(), []
    for b in backups or []:
        u = (b or {}).get("url") or ""
        if u.startswith("/") and u not in seen:
            seen.add(u)
            eps.append(u)
    for tpl in ("/mdata/%s/1/playlist.json", "/mvid/%s/1/playlist.json",
                "/vip/%s/1/playlist.json"):
        u = tpl % source_id
        if u not in seen:
            seen.add(u)
            eps.append(u)

    def one(u):
        return _sources_from_pages([{"url": u}], referer, source_id, timeout)

    workers = workers or min(16, max(4, len(eps)))
    out = []
    with _cf.ThreadPoolExecutor(workers) as ex:
        for part in ex.map(one, eps):
            out.extend(part)
    return out


def _dedup(sources):
    """Each mirror endpoint answers with the same playlist, so collapse by URL."""
    seen, out = set(), []
    for s in sources:
        u = s.get("raw")
        if not u or u in seen:
            continue
        seen.add(u)
        out.append(s)
    return out


def _gather(info, embed, source_id, tid, media_type, season, episode):
    """Everything the resolver needs from the player's own state, gathered
    concurrently: the mirror playlist endpoints, the subtitle list, and the
    track list the live playlist carries. Serial these cost minutes."""
    import concurrent.futures as _cf

    tracks = []
    for item in (info.get("playlist") or []):
        for t in (item.get("tracks") or []):
            if t.get("file"):
                tracks.append({"label": t.get("label"), "language": t.get("language"),
                               "url": t["file"], "source": "playlist.tracks"})

    with _cf.ThreadPoolExecutor(3) as ex:
        f_src = ex.submit(_sources_parallel, info.get("backups") or [], embed, source_id)
        f_sub = ex.submit(_subtitles, tid, media_type, season, episode)
        sources = f_src.result()
        subs = f_sub.result()
    sources = _dedup(sources or [])

    # the player's live playlist is the authoritative, currently-signed source
    for item in (info.get("playlist") or []):
        f = item.get("file")
        if not f or any(s["raw"] == f for s in sources):
            continue
        sources.insert(0, {
            "raw": f, "playable": f, "type": "hls",
            "label": item.get("label"), "sourceId": source_id,
            "endpoint": "jwplayer.getPlaylist()", "token": None,
        })
    return sources, tracks, (subs or tracks)


def _probe(url, referer, timeout=20):
    """Follow redirects and report whether the URL really serves HLS."""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Referer": referer, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            head = r.read(200).decode("utf-8", "replace")
            ct = r.headers.get("Content-Type") or ""
            return r.status, ct, head
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type") or "", ""
    except Exception:  # noqa: BLE001
        return 0, "", ""


def resolve(tmdb_id, media_type="movie", season=None, episode=None, verify=True):
    tid = str(tmdb_id)
    if not tid.isdigit():
        raise PlayerError("tmdbId harus angka")
    if media_type not in ("movie", "tv"):
        raise PlayerError("media type harus movie atau tv")
    embed = embed_url(tid, media_type, season, episode)
    chain = [embed]

    key = "%s|%s|%s|%s" % (media_type, tid, season or "", episode or "")
    info = _ID_CACHE.get(key)
    if not info:
        info = _read_backups(embed)
        _ID_CACHE[key] = info
    # the reader already ran /mdata, /vip and /mvid through the page; take the
    # playlist it produced, then re-query the endpoints ourselves for the rest.
    source_id = None
    for b in (info.get("backups") or []):
        if b.get("id"):
            source_id = b["id"]
            break
    if not source_id:
        m = re.search(r"/mdata/([^/]+)/", json.dumps(info))
        if not m:
            m = re.search(r"/mvid/([^/]+)/", json.dumps(info))
        source_id = m.group(1) if m else None
    if not source_id:
        raise PlayerError("player tidak mengungkap sumber untuk %s %s" % (media_type, tid))

    sources, tracks, subs = _gather(info, embed, source_id, tid, media_type, season, episode)
    if not sources:
        raise PlayerError("tidak ada sumber dari player %s untuk %s %s"
                          % (NAME, media_type, tid))

    # Only advertise sources that really serve a playlist: this player lists
    # ~28 mirrors and most are dead or redirect to an ad page. Probing keeps
    # `streams` honest; everything found stays in `allSources`.
    #
    # Some of those mirrors (tik/vip.1x2.space) 403 unless the request carries
    # the player's own origin, so each source is probed with it and the header
    # is kept on the source for the caller. Without this a live stream reads as
    # dead, which is exactly the kind of lie this API must not tell.
    live, dead = [], []
    if verify:
        import concurrent.futures as _cf
        with _cf.ThreadPoolExecutor(12) as ex:
            checks = list(ex.map(lambda s: (s, _probe(s["raw"], embed)), sources))
        for s, (code, ct, head) in checks:
            s = dict(s)
            s["status"] = code
            s["contentType"] = ct
            if code == 200 and ("mpegurl" in ct.lower() or "#EXTM3U" in head
                                or "video/" in ct.lower()):
                s["playable"] = True
                # the mirrors that gate on Referer answer 403 without it, so
                # hand the caller the header that makes the URL actually work
                if re.search(r"(?:tik|vip|mol|fil)\.1x2\.space", s.get("raw") or ""):
                    s["headers"] = {"Referer": EMBED_REFERER}
                live.append(s)
            else:
                s["playable"] = False
                s["reason"] = ("HTTP %s" % code) if code else "tidak merespons"
                dead.append(s)
    else:
        live = sources

    if not live:
        raise PlayerError("player %s: %d sumber ditemukan tapi tidak ada yang "
                          "merespons dengan playlist" % (NAME, len(sources)))
    return {
        "title": info.get("title"),
        "sources": live,
        "streams": [s["raw"] for s in live],
        "allSources": sources,
        "deadSources": dead,
        "subtitles": subs or tracks,
        "sourceId": source_id,
        "chain": chain + ["/mdata/%s/1/playlist.json" % source_id,
                          "/mvid/%s/1/playlist.json" % source_id],
        "note": ("sumber diperoleh dengan menjalankan halaman player di "
                 "Chromium headless dan membaca window.backups (bukan scraping "
                 "DOM): body /data terenkripsi dan routing-nya ada di bundle "
                 "javascript-obfuscator. Endpoint /mdata|/mvid|/vip/<id>/1/"
                 "playlist.json lalu dipanggil langsung tanpa browser. "
                 "streams[] hanya yang terbukti membalas playlist; sisanya ada "
                 "di allSources/deadSources."),
    }


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "tv":
        out = resolve(sys.argv[2], "tv", sys.argv[3], sys.argv[4])
    else:
        out = resolve(sys.argv[1])
    print(json.dumps(out, indent=1, ensure_ascii=False)[:3000])

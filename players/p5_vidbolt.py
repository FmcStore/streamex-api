#!/usr/bin/env python3
"""
Player #5 - vidbolt.xyz - resolver.

vidbolt is not a stream host: it is a fan-out over upstream scrapers. Its embed
page is a Vite SPA and the real contract is written in its own chunks, which is
what this resolver reads instead of hardcoding anything:

    /assets/index-*.js
        -> /assets/EmbeddedOnlyEmbedPage-*.js      provider table (15 entries)
            -> /assets/scraperUrl-*.js             the shared backend base
            -> /assets/<provider>-*.js             one upstream call each

Most providers (Callisto, FastVa, Ninetta, Quasar, Saffron, Naija, Nova) call

    https://scraper.vidbolt.xyz/scrape/<Name>/<movie|tv>/<id>?tmdbId=&title=&year=

and answer {"sources":[{url,quality,type,headers}], "subtitles":[...]}.
Two have their own upstream: Orion (api.movy.lol / img.animecurx.tech, needs an
api_key and a vidbolt.xyz referer) and Nebula (/showbox.filmu.in/scrape/VidRock,
needs an x-api-key header).

Nothing is reported as playable until it has been fetched and answered with a
playlist, so a caller never gets handed a dead URL.
"""
import concurrent.futures as _cf
import html as _html
import json
import re
import urllib.error
import urllib.parse
import urllib.request

NAME = "vidbolt"
HOST = "vidbolt.xyz"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
BASE = "https://%s" % HOST
# the base every provider chunk imports from /assets/scraperUrl-*.js
DEFAULT_SCRAPER = "https://scraper.vidbolt.xyz"
# Nebula's own upstream; the sibling sslip.io host in the chunk times out
NEBULA_HOST = "https://showbox.filmu.in"
NEBULA_KEY = "09eb429913afb6b1cc90f23746f41fb3279aed77726c625c40672b81444c0bac"


class PlayerError(Exception):
    pass


def _get(url, referer=BASE + "/", accept="application/json, */*", timeout=25,
         extra=None):
    headers = {"User-Agent": UA, "Referer": referer, "Origin": BASE,
               "Accept": accept}
    if extra:
        headers.update(extra)
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:300]
    except Exception as e:  # noqa: BLE001
        return 0, "%s: %s" % (type(e).__name__, str(e)[:90])


def _head(url, referer=BASE + "/", extra=None, timeout=20):
    headers = {"User-Agent": UA, "Referer": referer, "Accept": "*/*"}
    if extra:
        headers.update(extra)
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            head = r.read(220).decode("utf-8", "replace")
            return r.status, r.headers.get("Content-Type") or "", head
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type") or "", ""
    except Exception:  # noqa: BLE001
        return 0, "", ""


def _chunk(name):
    st, body = _get("%s/assets/%s" % (BASE, name), accept="*/*")
    return body if st == 200 else ""


def catalog_providers():
    """Read the provider table out of the player's own chunks.

    Discovering it (rather than pinning a list) means a provider added or
    removed on their side shows up here without a code change.
    """
    st, page = _get("%s/movie/550?autoplay=true&theme=addc35" % BASE,
                    accept="text/html,*/*")
    if st != 200:
        return []
    m = re.search(r'/assets/(index-[A-Za-z0-9_\-]+\.js)', page)
    if not m:
        return []
    idx = _chunk(m.group(1))
    m = re.search(r'assets/(EmbeddedOnlyEmbedPage-[A-Za-z0-9_\-]+\.js)', idx)
    if not m:
        return []
    emb = _chunk(m.group(1))
    if not emb:
        return []
    out = []
    for pm in re.finditer(r'\{(?:id|"id"):"([a-z0-9\-]+)",name:"([^"]*)",desc:"([^"]*)"', emb):
        prov = {"id": pm.group(1), "name": pm.group(2), "desc": pm.group(3),
                "chunk": None}
        cm = re.search(r'id:"%s"(.*?)import\("\./([A-Za-z0-9_\-\.]+\.js)"\)'
                       % re.escape(prov["id"]), emb, re.S)
        if cm:
            prov["chunk"] = cm.group(2)
        out.append(prov)
    return out


def _query(tmdb_id, media_type, season, episode, title=None, year=None):
    return urllib.parse.urlencode(
        {k: v for k, v in (("tmdbId", tmdb_id), ("title", title), ("year", year),
                           ("season", season), ("episode", episode))
         if v not in (None, "") and (k in ("tmdbId", "title", "year")
                                     or media_type == "tv")})


def _norm(payload):
    """Normalise an upstream answer, keeping the per-source headers."""
    out, subs = [], []
    for s in (payload.get("sources") or []):
        u = s.get("url") or s.get("file")
        if not u:
            continue
        low = str(u).lower()
        is_hls = ("m3u8" in str(s.get("type") or "").lower() or ".m3u8" in low
                  or "/m3u8" in low)
        out.append({
            "raw": u, "url": u,
            "quality": s.get("quality") or s.get("name"),
            "label": s.get("name") or s.get("quality") or "Auto",
            "type": "application/x-mpegurl" if is_hls else (s.get("type") or "video/mp4"),
            "kind": "hls" if is_hls else "mp4",
            "headers": s.get("headers") or {},
        })
    for t in (payload.get("subtitles") or []):
        if isinstance(t, dict) and t.get("url"):
            subs.append({"label": t.get("label") or t.get("language"),
                         "language": t.get("language") or t.get("lang"),
                         "url": _html.unescape(t["url"])})
        elif isinstance(t, str) and t.startswith("http"):
            subs.append({"label": None, "language": None, "url": t})
    return out, subs


def _via_scraper(base, cname, tmdb_id, media_type, season, episode,
                 title=None, year=None):
    """Call the shared /scrape/<Name>/... backend the provider chunks use."""
    tid = str(tmdb_id)
    seg = "movie" if media_type == "movie" else "tv"
    ident = "tmdb%s" % tid if media_type == "tv" else tid
    url = "%s/scrape/%s/%s/%s?%s" % (
        base.rstrip("/"), cname, seg, ident,
        _query(tid, media_type, season, episode, title, year))
    st, body = _get(url)
    if st != 200:
        return None, {"endpoint": url, "status": st, "error": (body or "")[:140]}
    try:
        payload = json.loads(body)
    except ValueError:
        return None, {"endpoint": url, "error": "balasan bukan JSON"}
    srcs, subs = _norm(payload)
    if not srcs:
        return None, {"endpoint": url, "error": "tidak ada sources"}
    return {"sources": srcs, "subtitles": subs, "endpoint": url}, None


def _via_orion(tmdb_id, media_type, season, episode):
    """Orion / darkmatter: own upstreams, api_key in the query, and the anime
    host only answers when the referer is vidbolt.xyz."""
    tid = str(tmdb_id)
    key = "0f461eaa465bb2a7acd037425217f2f209ef540a3171e1ac"
    if media_type == "movie":
        candidates = [("https://api.movy.lol", "/api/source/%s" % tid, key,
                       "https://img.rousav.tech")]
    else:
        candidates = [("https://img.animecurx.tech",
                       "/api/tv/%s/%s/%s" % (tid, season or 1, episode or 1),
                       "streamrip_secret_2026", "https://img.rousav.tech"),
                      ("https://api.movy.lol", "/api/source/%s" % tid, key,
                       "https://img.rousav.tech")]
    last = None
    for base, path, k, media_host in candidates:
        url = "%s%s?api_key=%s&apikey=%s" % (base.rstrip("/"), path, k, k)
        st, body = _get(url)
        if st != 200:
            last = {"endpoint": url, "status": st, "error": (body or "")[:120]}
            continue
        try:
            payload = json.loads(body)
        except ValueError:
            last = {"endpoint": url, "error": "balasan bukan JSON"}
            continue
        srcs, subs = _norm(payload)
        if not srcs and payload.get("m3u8_path"):
            u = "%s/%s?api_key=%s" % (media_host, payload["m3u8_path"].lstrip("/"), k)
            srcs = [{"raw": u, "url": u, "quality": "1080p", "label": "1080p",
                     "type": "application/x-mpegurl", "kind": "hls", "headers": {}}]
        if srcs:
            return {"sources": srcs, "subtitles": subs, "endpoint": url}, None
        last = {"endpoint": url, "error": "tidak ada sources"}
    return None, (last or {"error": "Orion tidak mengembalikan sumber"})


def _via_nebula(tmdb_id, media_type, season, episode, title=None, year=None):
    """Nebula: /showbox.filmu.in/scrape/VidRock with an x-api-key header."""
    tid = str(tmdb_id)
    seg = "movie" if media_type == "movie" else "tv"
    url = "%s/scrape/VidRock/%s/tmdb%s?%s" % (
        NEBULA_HOST, seg, tid, _query(tid, media_type, season, episode, title, year))
    st, body = _get(url, extra={"x-api-key": NEBULA_KEY})
    if st != 200:
        return None, {"endpoint": url, "status": st, "error": (body or "")[:140]}
    try:
        payload = json.loads(body)
    except ValueError:
        return None, {"endpoint": url, "error": "balasan bukan JSON"}
    srcs, subs = _norm(payload)
    keep = [s for s in srcs if "nova" in (s.get("name") or s.get("label") or "").lower()]
    keep = keep or srcs
    if not keep:
        return None, {"endpoint": url, "error": "tidak ada sources"}
    return {"sources": keep, "subtitles": subs, "endpoint": url}, None


def resolve(tmdb_id, media_type="movie", season=None, episode=None,
            title=None, year=None):
    tid = str(tmdb_id)
    if not tid.isdigit():
        raise PlayerError("tmdbId harus angka")
    # these backends match by title on some paths, so fetch it when not given
    if not title:
        try:
            import server as _srv
            meta = _srv.tmdb(("movie/%s" if media_type == "movie" else "tv/%s") % tid)
            title = meta.get("title") or meta.get("name")
            year = (meta.get("release_date") or meta.get("first_air_date") or "")[:4]
        except Exception:  # noqa: BLE001  title is optional for most providers
            title, year = None, None

    provs = catalog_providers()
    if not provs:
        raise PlayerError("tabel provider %s tidak terbaca" % HOST)

    # the shared scraper base, read from a provider chunk (scraperUrl-*.js)
    scraper_base = None
    for prov in provs:
        if not prov.get("chunk"):
            continue
        m = re.search(r'"(https?://scraper\.[a-z0-9\.\-]+)"', _chunk(prov["chunk"]))
        if m:
            scraper_base = m.group(1)
            break
    if not scraper_base:
        scraper_base = DEFAULT_SCRAPER

    def call(prov):
        pid = prov["id"]
        try:
            src = _chunk(prov["chunk"]) if prov.get("chunk") else ""
            if "api.movy.lol" in src or "animecurx" in src:
                return pid, _via_orion(tid, media_type, season, episode)
            if "VidRock" in src or "showbox.filmu.in" in src:
                return pid, _via_nebula(tid, media_type, season, episode, title, year)
            m = re.search(r'/scrape/([A-Za-z][A-Za-z0-9_]*)/', src)
            if not m:
                return pid, (None, {"error": "provider tidak punya endpoint /scrape"})
            base = scraper_base
            mb = re.search(r'"(https?://[a-z0-9\.\-]+\.[a-z]{2,})/"', src)
            if mb and "vidbolt" not in mb.group(1):
                base = mb.group(1)
            return pid, _via_scraper(base, m.group(1), tid, media_type, season,
                                     episode, title, year)
        except Exception as e:  # noqa: BLE001  one provider must not sink the rest
            return pid, (None, {"error": "%s: %s" % (type(e).__name__, str(e)[:90])})

    with _cf.ThreadPoolExecutor(8) as ex:
        got = list(ex.map(call, provs))

    by_id = {p["id"]: p for p in provs}
    providers, tried, all_sources = [], [], []
    for pid, (ok, err) in got:
        name = by_id.get(pid, {}).get("name")
        if ok:
            providers.append({"provider": pid, "name": name,
                              "endpoint": ok.get("endpoint"),
                              "sources": ok["sources"],
                              "subtitles": ok.get("subtitles") or []})
            for s in ok["sources"]:
                s = dict(s)
                s["provider"] = pid
                s["providerName"] = name
                all_sources.append(s)
            tried.append({"provider": pid, "name": name,
                          "endpoint": ok.get("endpoint"),
                          "sources": len(ok["sources"])})
        else:
            tried.append({"provider": pid, "name": name,
                          "endpoint": (err or {}).get("endpoint"),
                          "status": (err or {}).get("status"),
                          "error": (err or {}).get("error")})

    if not all_sources:
        raise PlayerError("player %s: %d provider dicoba, tidak ada yang "
                          "mengembalikan sumber" % (NAME, len(tried)))

    def probe(s):
        code, ct, head = _head(s["url"], extra=s.get("headers") or None)
        s = dict(s)
        s["status"] = code
        s["contentType"] = ct
        s["playable"] = bool(code == 200 and ("mpegurl" in ct.lower()
                                              or "#EXTM3U" in head
                                              or "video/" in ct.lower()))
        if not s["playable"]:
            s["reason"] = ("HTTP %s" % code) if code else "tidak merespons"
        return s

    with _cf.ThreadPoolExecutor(10) as ex:
        checked = list(ex.map(probe, all_sources))
    live = [s for s in checked if s["playable"]]
    dead = [s for s in checked if not s["playable"]]
    if not live:
        raise PlayerError("player %s: %d sumber ditemukan, tidak ada yang "
                          "merespons dengan playlist" % (NAME, len(checked)))

    subs = []
    for p in providers:
        for t in p.get("subtitles") or []:
            if t.get("url"):
                subs.append({"label": t.get("label"), "language": t.get("language"),
                             "url": t["url"], "source": "provider.%s" % p["provider"]})

    return {
        "title": title,
        "sources": live,
        "streams": [s["url"] for s in live],
        "allSources": all_sources,
        "deadSources": dead,
        "providers": providers,
        "providersTried": tried,
        "subtitles": subs,
        "sourceId": None,
        "chain": ["%s/assets/index-*.js -> EmbeddedOnlyEmbedPage-*.js (tabel "
                  "provider) -> scraperUrl-*.js (%s) -> /scrape/<Provider>/..."
                  % (BASE, scraper_base)],
        "note": ("vidbolt adalah fan-out ke banyak scraper upstream. Tabel "
                 "provider dan endpoint per provider dibaca dari chunk-nya "
                 "sendiri (%s/assets/*.js), lalu /scrape/<Nama>/<movie|tv>/<id> "
                 "di %s dipanggil langsung tanpa browser. Orion (api.movy.lol) "
                 "dan Nebula (/showbox.filmu.in + x-api-key) punya jalur sendiri. "
                 "streams[] hanya yang terbukti membalas playlist."
                 % (BASE, scraper_base)),
    }

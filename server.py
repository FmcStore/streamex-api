#!/usr/bin/env python3
"""
StreameX (streamex.pw) -> JSON REST API.

The site is a Next.js front end with no API of its own:
  * all media metadata comes from TMDB with a hardcoded key
  * comments / reactions / watchlist live on https://dd.flixbaba.me
  * playback is 12 third party embed players, player #1 (vsembed.ru) can be
    resolved down to a direct HLS url through a signed, single use chain
This service wraps all of that as JSON endpoints. Live passthrough, nothing
is written to disk (stream resolution keeps a short in memory cache).
"""
import argparse
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import streams
import players as player_registry

# streamex.pw ships this TMDB key inside its public JS bundle and calls the API
# straight from the browser, so the value is public by construction. It stays
# overridable in case the site rotates it: export TMDB_KEY=... to take over.
TMDB_KEY = os.environ.get("TMDB_KEY", "7045bc4055c6293e84534dd8f6dbb024")
TMDB = "https://api.themoviedb.org/3"
FLIX = "https://dd.flixbaba.me"
SITE = "https://streamex.pw"
IMG = "https://image.tmdb.org/t/p"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

TTL_LIST = 300
TTL_DETAIL = 900
TTL_STREAM = 90

_lock = threading.Lock()
_cache = {}
_local = threading.local()


class ApiError(Exception):
    def __init__(self, status, message, detail=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.detail = detail or {}


# ---------------------------------------------------------------- upstream

def record(url):
    lst = getattr(_local, "src", None)
    if lst is None:
        lst = _local.src = []
    lst.append(url)


def sources():
    return list(getattr(_local, "src", []) or [])


def fetch(url, referer=None, origin=None, timeout=30, accept=None):
    h = {"User-Agent": UA,
         "Accept": accept or "application/json, text/html;q=0.9, */*;q=0.8"}
    if referer:
        h["Referer"] = referer
    if origin:
        h["Origin"] = origin
    record(url)
    req = urllib.request.Request(url, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001 - surfaced as 502
        return 599, "%s: %s" % (type(e).__name__, e)


def tmdb(path, timeout=25, **params):
    params["api_key"] = TMDB_KEY
    q = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    status, body = fetch("%s/%s?%s" % (TMDB, path.lstrip("/"), q), timeout=timeout)
    if status == 599:
        raise ApiError(502, "TMDB tidak bisa dihubungi", {"upstream": body[:300]})
    if status == 404:
        raise ApiError(404, "data tidak ditemukan di TMDB")
    if status >= 500:
        raise ApiError(502, "TMDB error %d" % status, {"upstream": body[:300]})
    if status != 200:
        raise ApiError(status, "TMDB menolak permintaan (HTTP %d)" % status,
                       {"upstream": body[:300]})
    try:
        return json.loads(body)
    except ValueError:
        raise ApiError(502, "respons TMDB bukan JSON", {"upstream": body[:300]})


def flix(path, query=None, timeout=20):
    q = urllib.parse.urlencode({k: v for k, v in (query or {}).items()
                                if v not in (None, "")})
    url = "%s%s%s" % (FLIX, path, ("?" + q) if q else "")
    status, body = fetch(url, referer=SITE + "/", origin=SITE, timeout=timeout)
    if status == 599:
        raise ApiError(502, "dd.flixbaba.me tidak bisa dihubungi",
                       {"upstream": body[:300]})
    if status == 404:
        raise ApiError(404, "tidak ditemukan di backend flixbaba", {"url": url})
    if status >= 500:
        raise ApiError(502, "backend flixbaba error %d" % status,
                       {"upstream": body[:300], "url": url})
    if status != 200:
        raise ApiError(status, "backend flixbaba menolak (HTTP %d)" % status,
                       {"upstream": body[:300]})
    try:
        return json.loads(body)
    except ValueError:
        raise ApiError(502, "respons flixbaba bukan JSON",
                       {"upstream": body[:300]})


# ------------------------------------------------------------------ cache

def cache_get(key):
    with _lock:
        hit = _cache.get(key)
        if not hit:
            return None
        exp, payload = hit
        if exp < time.time():
            _cache.pop(key, None)
            return None
        return payload


def cache_put(key, payload, ttl):
    with _lock:
        _cache[key] = (time.time() + ttl, payload)
        if len(_cache) > 800:
            now = time.time()
            for k in [k for k, (e, _) in _cache.items() if e < now]:
                _cache.pop(k, None)


# ------------------------------------------------------------------ helpers

def img(path, size="w500"):
    if not path:
        return None
    if path.startswith("http"):
        return path
    return "%s/%s%s" % (IMG, size, path)


def slug(name):
    if not name:
        return ""
    s = re.sub(r"[^a-zA-Z0-9]+", "-", name).strip("-").lower()
    return re.sub(r"-+", "-", s)


def media_links(item):
    """Build the site urls that a client can hand to a browser."""
    mid = item.get("tmdbId") or item.get("id")
    mt = item.get("mediaType") or item.get("media_type") or (
        "tv" if item.get("name") and not item.get("title") else "movie")
    if mt == "person":
        return {"detail": "%s/person/%s/%s" % (SITE, mid, slug(item.get("name")))}
    name = item.get("title") or item.get("name") or ""
    base = "%s/%s/%s/%s" % (SITE, mt, mid, slug(name))
    out = {"detail": base}
    if mt == "movie":
        out["watch"] = base + "/watch"
        out["watchQuery"] = base + "/watch?p={player}"
    else:
        out["season"] = base + "/season/{season}"
        out["watch"] = base + "/season/{season}" + "?e={episode}&p={player}"
    return out


def person_links(item):
    return {"detail": "%s/person/%s/%s" % (SITE, item.get("id"), slug(item.get("name")))}


def shape_item(r, media_type=None):
    """A list/card item, mirroring what the site renders in its cards."""
    mt = r.get("media_type") or media_type
    out = {
        "tmdbId": r.get("id"),
        "mediaType": mt,
        "title": r.get("title") or r.get("name"),
        "originalTitle": r.get("original_title") or r.get("original_name"),
        "overview": r.get("overview"),
        "poster": img(r.get("poster_path")),
        "posterPath": r.get("poster_path"),
        "backdrop": img(r.get("backdrop_path"), "w1280"),
        "releaseDate": r.get("release_date") or r.get("first_air_date"),
        "year": (r.get("release_date") or r.get("first_air_date") or "")[:4] or None,
        "rating": round(r["vote_average"], 1) if r.get("vote_average") else None,
        "ratingPercent": int(round((r.get("vote_average") or 0) * 10)),
        "voteCount": r.get("vote_count"),
        "popularity": r.get("popularity"),
        "originalLanguage": r.get("original_language"),
        "adult": r.get("adult", False),
    }
    out["links"] = media_links(out)
    return out


def shape_person(r):
    out = {
        "tmdbId": r.get("id"),
        "mediaType": "person",
        "name": r.get("name"),
        "knownForDepartment": r.get("known_for_department"),
        "gender": r.get("gender"),
        "popularity": r.get("popularity"),
        "profile": img(r.get("profile_path"), "w185"),
        "profilePath": r.get("profile_path"),
        "knownFor": [shape_item(k) for k in (r.get("known_for") or [])][:6],
    }
    out["links"] = person_links(out)
    return out


def paged(resp, page, per_page_note=None):
    data = {
        "page": resp.get("page", page),
        "totalPages": min(resp.get("total_pages", 0) or 0, 500),
        "totalResults": resp.get("total_results", 0),
        "hasNext": bool(resp.get("page", 1) < min(resp.get("total_pages", 0) or 0, 500)),
    }
    if per_page_note:
        data.update(per_page_note)
    return data


# ------------------------------------------------------------------ players

PLAYER_TAGS = {
    1: [{"label": "OG", "color": "info"}],
    2: [{"label": "4K", "color": "success"}, {"label": "BEST", "color": "success"}],
    3: [{"label": "GOOD", "color": "info"}],
    4: [{"label": "4K", "color": "success"}],
    5: [], 6: [], 7: [], 8: [],
    9: [{"label": "NEW", "color": "error"}, {"label": "THUMBS_UP", "color": "info"}],
    10: [], 11: [],
    12: [{"label": "4K", "color": "success"}],
}


def players(tid, media_type="movie", season=None, episode=None):
    s = season or 1
    e = episode or 1
    if media_type == "movie":
        urls = {
            1: "https://vsembed.ru/embed/movie?tmdb=%s" % tid,
            2: "https://vidcore.io/movie/%s?autoPlay=true&theme=addc35" % tid,
            3: "https://player.videasy.to/movie/%s?color=addc35&overlay=true" % tid,
            4: "https://mapple.rip/watch/movie/%s?autoPlay=true&theme=addc35" % tid,
            5: "https://vidbolt.xyz/movie/%s?autoplay=true&theme=addc35&title=true&poster=true" % tid,
            6: "https://cinemaos.tech/player/%s?autoPlay=true&theme=addc35" % tid,
            7: "https://zxcstream.xyz/player/movie/%s?autoplay=true&color=addc35&back=false&domainAd=Flixmomo.tv" % tid,
            8: "https://peachify.top/embed/movie/%s?accent=ADDC35&pip=hide&autoPlay=true" % tid,
            9: "https://play.xpass.top/e/movie/%s?autostart=true" % tid,
            10: "https://1embed.cc/embed/movie/%s?autoPlay=1&color=ADDC35" % tid,
            11: "https://multiembed.mov/?tmdb=1&video_id=%s" % tid,
            12: "https://vidfast.vc/movie/%s?autoPlay=true&title=true&poster=true&theme=ADDC35" % tid,
        }
    else:
        urls = {
            1: "https://vsembed.ru/embed/tv?tmdb=%s&season=%s&episode=%s" % (tid, s, e),
            2: "https://vidcore.io/tv/%s/%s/%s?theme=addc35&nextButton=true&autoPlay=true&autoNext=true" % (tid, s, e),
            3: "https://player.videasy.to/tv/%s/%s/%s?nextEpisode=true&episodeSelector=true&overlay=true&color=addc35&autoplayNextEpisode=true" % (tid, s, e),
            4: "https://mapple.rip/watch/tv/%s-%s-%s?theme=addc35&autoPlay=true&autoNext=true" % (tid, s, e),
            5: "https://vidbolt.xyz/tv/%s/%s/%s?theme=addc35&title=true&poster=true&autonext=true&autoplay=true" % (tid, s, e),
            6: "https://cinemaos.tech/player/%s/%s/%s?theme=addc35&autoPlay=true&autoNext=true" % (tid, s, e),
            7: "https://zxcstream.xyz/player/tv/%s/%s/%s?color=addc35&back=false&domainAd=Flixmomo.tv&autoplay=true" % (tid, s, e),
            8: "https://peachify.top/embed/tv/%s/%s/%s?accent=ADDC35&pip=hide&autoPlay=true" % (tid, s, e),
            9: "https://play.xpass.top/e/tv/%s/%s/%s?autostart=true" % (tid, s, e),
            10: "https://1embed.cc/embed/tv/%s/%s/%s?color=ADDC35&autoPlay=1" % (tid, s, e),
            11: "https://multiembed.mov/?tmdb=1&video_id=%s&s=%s&e=%s" % (tid, s, e),
            12: "https://vidfast.vc/tv/%s/%s/%s?title=true&poster=true&theme=ADDC35&autoPlay=true&nextButton=true&autoNext=true" % (tid, s, e),
        }
    return [{"id": i, "tags": PLAYER_TAGS.get(i, []), "embed": urls[i],
             "resolvable": player_registry.reasons(i) is None,
             "unresolvedReason": player_registry.reasons(i)} for i in sorted(urls)]


# ------------------------------------------------------------------ filters

def discover_filters(media_type, q):
    """Mirror the filter dropdowns the site exposes on its listing pages."""
    lang = q.get("lang", "en")
    region = q.get("region")
    country = q.get("country")
    year = q.get("year")
    genre = q.get("genre")
    min_rating = q.get("minRating")
    p = {}
    if lang and lang != "all":
        p["with_original_language"] = lang
    # region is the TMDB watch-provider region (tied to whichever language is
    # being browsed) and does not filter results on its own. Browsing by where a
    # title was made needs with_origin_country, so a client asking for a country
    # gets the filter it actually means.
    if region and lang and lang != "all":
        p["region"] = region
    if country and str(country).upper() != "ALL":
        p["with_origin_country"] = str(country).upper()
    date_gte = "primary_release_date.gte" if media_type == "movie" else "first_air_date.gte"
    date_lte = "primary_release_date.lte" if media_type == "movie" else "first_air_date.lte"
    if year:
        if not re.fullmatch(r"\d{4}", str(year)):
            raise ApiError(400, "year harus 4 digit (contoh 2024)")
        p[date_gte] = "%s-01-01" % year
        p[date_lte] = "%s-12-31" % year
    if genre:
        p["with_genres"] = genre_id(media_type, genre)
    if min_rating:
        p["vote_average.gte"] = min_rating
    return p


def genre_id(media_type, value):
    """Accept a numeric genre id or a genre name."""
    if value in (None, ""):
        return None
    if str(value).isdigit():
        return value
    chosen = str(value).strip().lower()
    for g in tmdb("genre/%s/list" % media_type)["genres"]:
        if g["name"].lower() == chosen:
            return g["id"]
    raise ApiError(400, "genre '%s' tidak dikenal untuk %s" % (value, media_type))


# ------------------------------------------------------------------ handlers

ENDPOINTS = {
    "catalogue": "GET /",
    "health": "GET /api/health",
    "home": "GET /api/home",
    "movies_trending": "GET /api/movies/trending?page=",
    "movies_popular": "GET /api/movies/popular?page=&lang=&region=&year=&genre=&minRating=",
    "movies_recent": "GET /api/movies/recent?page=&lang=&region=&genre=&minRating=",
    "movies_top_rated": "GET /api/movies/top-rated?page=&lang=&region=&year=&genre=&minRating=",
    "movies_by_provider": "GET /api/movies/by-provider?provider=<tmdb watch provider id>",
    "movie_detail": "GET /api/movies/{tmdbId}",
    "movie_watch": "GET /api/movies/{tmdbId}/watch",
    "tv_trending": "GET /api/tv/trending?page=",
    "tv_popular": "GET /api/tv/popular?page=&lang=&region=&year=&genre=&minRating=",
    "tv_recent": "GET /api/tv/recent?page=&lang=&region=&genre=&minRating=",
    "tv_top_rated": "GET /api/tv/top-rated?page=&lang=&region=&year=&genre=&minRating=",
    "tv_by_provider": "GET /api/tv/by-provider?provider=<tmdb watch provider id>",
    "tv_detail": "GET /api/tv/{tmdbId}",
    "tv_season": "GET /api/tv/{tmdbId}/season/{season}",
    "tv_episode": "GET /api/tv/{tmdbId}/season/{season}/episode/{episode}",
    "tv_watch": "GET /api/tv/{tmdbId}/watch?season=&episode=",
    "search": "GET /api/search?q=&page=&type=multi|movie|tv|person",
    "genres": "GET /api/genres?type=movie|tv",
    "countries": "GET /api/countries",
    "people_trending": "GET /api/people/trending",
    "person_detail": "GET /api/person/{tmdbId}",
    "comments": "GET /api/comments/{movie|tv}/{tmdbId}?page=&limit=&season=&episode=",
    "reactions": "GET /api/reactions/{movie|tv}/{tmdbId}?season=&episode=",
    "streams": "GET /api/streams/{movie|tv}/{tmdbId}?season=&episode=",
    "players": "GET /api/players/{movie|tv}/{tmdbId}?season=&episode=",
}

NOTE = ("Read only wrapper of streamex.pw. Data sources: api.themoviedb.org "
        "(metadata), dd.flixbaba.me (comments + reactions), vsembed.ru -> "
        "stellarconductornexus.com -> data.vidsrc.sh (resolved HLS streams). "
        "Auth-only features of the site (login, profile, watchlist, posting "
        "comments) are not mirrored.")


def h_health(q):
    return {"service": "streamex-api", "status": "ok", "mode": "live",
            "storage": "none", "site": SITE}


def h_home(q):
    out = {}
    popular = tmdb("movie/popular", page=1)
    out["hero"] = [shape_item(r, "movie") for r in popular["results"]]
    out["trendingMovies"] = [shape_item(r, "movie")
                             for r in tmdb("trending/movie/day")["results"]]
    out["trendingShows"] = [shape_item(r, "tv")
                            for r in tmdb("trending/tv/day")["results"]]
    out["trendingArtists"] = [shape_person(r)
                              for r in tmdb("trending/person/day")["results"]]
    out["sections"] = {
        "movies": {
            "trending": ENDPOINTS["movies_trending"],
            "popular": ENDPOINTS["movies_popular"],
            "recent": ENDPOINTS["movies_recent"],
            "topRated": ENDPOINTS["movies_top_rated"],
        },
        "tv": {
            "trending": ENDPOINTS["tv_trending"],
            "popular": ENDPOINTS["tv_popular"],
            "recent": ENDPOINTS["tv_recent"],
            "topRated": ENDPOINTS["tv_top_rated"],
        },
    }
    return out


def listing(media_type, kind, q):
    page = int_arg(q, "page", 1)
    if page < 1:
        raise ApiError(400, "page harus >= 1")
    base = {}
    if kind == "trending":
        resp = tmdb("trending/%s/day" % media_type, page=page)
    else:
        gq = dict(q)
        gq["genre"] = genre_id(media_type, gq.get("genre"))
        base = discover_filters(media_type, gq)
        if kind == "popular":
            resp = tmdb("discover/%s" % media_type, page=page,
                        sort_by="popularity.desc", **base)
        elif kind == "recent":
            if media_type == "movie":
                since = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 14 * 86400))
                resp = tmdb("discover/movie", page=page,
                            sort_by="primary_release_date.desc",
                            **dict(base, **{"primary_release_date.lte": since,
                                            "with_release_type": "3|2",
                                            "vote_count.gte": 20}))
            else:
                now = time.gmtime()
                y0 = now.tm_year - 2
                resp = tmdb("discover/tv", page=page, sort_by="popularity.desc",
                            **dict(base, **{"first_air_date.gte": "%d-01-01" % y0,
                                            "first_air_date.lte": "%d-12-31" % now.tm_year,
                                            "vote_count.gte": 20}))
        elif kind == "top-rated":
            if media_type == "movie":
                resp = tmdb("discover/movie", page=page, sort_by="vote_average.desc",
                            **dict(base, **{"vote_count.gte": 1000}))
            else:
                resp = tmdb("discover/tv", page=page, sort_by="vote_average.desc",
                            **dict(base, **{"vote_count.gte": 300}))
        else:
            raise ApiError(404, "listing '%s' tidak ada" % kind)
    items = [shape_item(r, media_type) for r in resp["results"]]
    data = {"mediaType": media_type, "list": kind, "count": len(items),
            "items": items}
    data.update(paged(resp, page))
    data["filters"] = base
    return data


def by_provider(media_type, q):
    provider = req_arg(q, "provider")
    r = tmdb("discover/%s" % media_type, with_watch_providers=provider,
             watch_region=q.get("watchRegion", "US"),
             sort_by="popularity.desc")
    items = [shape_item(x, media_type) for x in r["results"]]
    data = {"mediaType": media_type, "provider": provider, "count": len(items),
            "items": items, "page": 1, "totalPages": 1, "hasNext": False,
            "note": "TMDB mengembalikan satu halaman untuk query provider ini, "
                    "sama seperti yang dipakai situs."}
    return data


def movie_detail(mid):
    r = tmdb("movie/%s" % mid,
             append_to_response="images,videos,credits,recommendations,similar")
    item = shape_item(r, "movie")
    item.update({
        "runtime": r.get("runtime"),
        "tagline": r.get("tagline"),
        "status": r.get("status"),
        "budget": r.get("budget"),
        "revenue": r.get("revenue"),
        "imdbId": r.get("imdb_id"),
        "genres": r.get("genres"),
        "productionCompanies": [c.get("name") for c in
                                (r.get("production_companies") or [])],
        "productionCountries": [c.get("iso_3166_1") for c in
                                (r.get("production_countries") or [])],
        "spokenLanguages": [c.get("english_name") or c.get("name") for c in
                            (r.get("spoken_languages") or [])],
        "cast": [{"tmdbId": c.get("id"), "name": c.get("name"),
                  "character": c.get("character"), "order": c.get("order"),
                  "profile": img(c.get("profile_path"), "w185")}
                 for c in (r.get("credits", {}).get("cast") or [])][:30],
        "crew": [{"tmdbId": c.get("id"), "name": c.get("name"),
                  "job": c.get("job"), "department": c.get("department")}
                 for c in (r.get("credits", {}).get("crew") or [])][:30],
        "videos": [{"key": v.get("key"), "name": v.get("name"),
                    "site": v.get("site"), "type": v.get("type"),
                    "url": ("https://www.youtube.com/watch?v=%s" % v.get("key"))}
                   for v in (r.get("videos", {}).get("results") or [])],
        "backdrops": [img(i.get("file_path"), "w1280")
                      for i in (r.get("images", {}).get("backdrops") or [])][:12],
        "recommendations": [shape_item(x, "movie")
                            for x in (r.get("recommendations", {}).get("results") or [])],
        "similar": [shape_item(x, "movie")
                    for x in (r.get("similar", {}).get("results") or [])],
        "playersCount": 12,
        "playersEndpoint": "/api/movies/%s/watch" % mid,
    })
    return item


def tv_detail(tid):
    r = tmdb("tv/%s" % tid,
             append_to_response="images,videos,credits,recommendations,similar")
    item = shape_item(r, "tv")
    item.update({
        "firstAirDate": r.get("first_air_date"),
        "lastAirDate": r.get("last_air_date"),
        "status": r.get("status"),
        "tagline": r.get("tagline"),
        "numberOfSeasons": r.get("number_of_seasons"),
        "numberOfEpisodes": r.get("number_of_episodes"),
        "episodeRunTime": r.get("episode_run_time"),
        "genres": r.get("genres"),
        "networks": [n.get("name") for n in (r.get("networks") or [])],
        "createdBy": [c.get("name") for c in (r.get("created_by") or [])],
        "spokenLanguages": [c.get("english_name") or c.get("name") for c in
                            (r.get("spoken_languages") or [])],
        "cast": [{"tmdbId": c.get("id"), "name": c.get("name"),
                  "character": c.get("character"), "order": c.get("order"),
                  "profile": img(c.get("profile_path"), "w185")}
                 for c in (r.get("credits", {}).get("cast") or [])][:30],
        "crew": [{"tmdbId": c.get("id"), "name": c.get("name"),
                  "job": c.get("job"), "department": c.get("department")}
                 for c in (r.get("credits", {}).get("crew") or [])][:30],
        "seasons": [{"seasonNumber": s.get("season_number"),
                     "name": s.get("name"), "episodeCount": s.get("episode_count"),
                     "airDate": s.get("air_date"),
                     "poster": img(s.get("poster_path")),
                     "overview": s.get("overview"),
                     "endpoint": "/api/tv/%s/season/%s" % (tid, s.get("season_number"))}
                    for s in (r.get("seasons") or [])],
        "videos": [{"key": v.get("key"), "name": v.get("name"),
                    "site": v.get("site"), "type": v.get("type"),
                    "url": ("https://www.youtube.com/watch?v=%s" % v.get("key"))}
                   for v in (r.get("videos", {}).get("results") or [])],
        "backdrops": [img(i.get("file_path"), "w1280")
                      for i in (r.get("images", {}).get("backdrops") or [])][:12],
        "recommendations": [shape_item(x, "tv")
                            for x in (r.get("recommendations", {}).get("results") or [])],
        "similar": [shape_item(x, "tv")
                    for x in (r.get("similar", {}).get("results") or [])],
        "playersCount": 12,
        "playersEndpoint": "/api/tv/%s/watch" % tid,
    })
    return item


def season_payload(tid, sn):
    r = tmdb("tv/%s/season/%s" % (tid, sn),
             append_to_response="images,videos,credits,recommendations,similar")
    eps = []
    for e in r.get("episodes") or []:
        eps.append({
            "episodeNumber": e.get("episode_number"),
            "seasonNumber": e.get("season_number"),
            "name": e.get("name"),
            "overview": e.get("overview"),
            "airDate": e.get("air_date"),
            "runtime": e.get("runtime"),
            "rating": round(e["vote_average"], 1) if e.get("vote_average") else None,
            "voteCount": e.get("vote_count"),
            "still": img(e.get("still_path"), "w300"),
            "stillPath": e.get("still_path"),
            "crewCount": len(e.get("crew") or []),
            "guestStars": [g.get("name") for g in (e.get("guest_stars") or [])][:10],
            "watch": "%s/tv/%s/%s/season/%s?e=%s" % (SITE, tid,
                                                     slug(r.get("name")), sn,
                                                     e.get("episode_number")),
        })
    out = {
        "tmdbId": int(tid) if str(tid).isdigit() else tid,
        "mediaType": "tv",
        "seasonNumber": r.get("season_number"),
        "name": r.get("name"),
        "overview": r.get("overview"),
        "airDate": r.get("air_date"),
        "rating": round(r["vote_average"], 1) if r.get("vote_average") else None,
        "poster": img(r.get("poster_path")),
        "episodeCount": len(eps),
        "episodes": eps,
        "cast": [{"tmdbId": c.get("id"), "name": c.get("name"),
                  "character": c.get("character")}
                 for c in (r.get("credits", {}).get("cast") or [])][:30],
        "networks": [n.get("name") for n in (r.get("networks") or [])],
        "watchEndpoint": "/api/tv/%s/watch?season=%s&episode={episode}" % (tid, sn),
    }
    return out


def episode_payload(tid, sn, en):
    season = season_payload(tid, sn)
    for e in season["episodes"]:
        if str(e["episodeNumber"]) == str(en):
            out = dict(e)
            out["mediaType"] = "tv"
            out["tmdbId"] = season["tmdbId"]
            out["season"] = season["seasonNumber"]
            out["seasonName"] = season["name"]
            out["seasonEpisodeCount"] = season["episodeCount"]
            out["watchEndpoint"] = "/api/tv/%s/watch?season=%s&episode=%s" % (tid, sn, en)
            return out
    raise ApiError(404, "episode %s tidak ada di season %s" % (en, sn))


def search(q):
    query = req_arg(q, "q")
    page = int_arg(q, "page", 1)
    kind = q.get("type", "multi")
    if kind not in ("multi", "movie", "tv", "person"):
        raise ApiError(400, "type harus multi|movie|tv|person")
    path = "search/%s" % kind
    resp = tmdb(path, query=query, page=page, include_adult="false")
    items = []
    for r in resp.get("results") or []:
        if kind != "multi":
            mt = kind
        else:
            mt = r.get("media_type")
        if mt == "person":
            items.append(shape_person(r))
        else:
            items.append(shape_item(r, mt))
    data = {"query": query, "type": kind, "count": len(items), "items": items}
    data.update(paged(resp, page))
    return data


def genres(media_type):
    r = tmdb("genre/%s/list" % media_type)
    return {"mediaType": media_type, "count": len(r["genres"]),
            "genres": r["genres"]}


def countries():
    """Country browse needs ISO 3166-1 codes, so serve the list TMDB accepts."""
    rows = tmdb("configuration/countries")
    items = sorted(({"code": c["iso_3166_1"],
                     "name": c.get("english_name") or c.get("native_name"),
                     "nativeName": c.get("native_name")} for c in rows),
                   key=lambda c: c["name"] or "")
    return {"count": len(items), "items": items}


def people_trending():
    r = tmdb("trending/person/day")
    return {"count": len(r["results"]),
            "items": [shape_person(p) for p in r["results"]]}


def person_detail(pid):
    r = tmdb("person/%s" % pid,
             append_to_response="images,videos,movie_credits,tv_credits")
    known = sorted((r.get("combined_credits") or {}).get("cast") or [],
                   key=lambda c: c.get("popularity") or 0, reverse=True)
    credits = [{"tmdbId": c.get("id"), "mediaType": c.get("media_type"),
                "title": c.get("title") or c.get("name"),
                "character": c.get("character"),
                "releaseDate": c.get("release_date") or c.get("first_air_date"),
                "poster": img(c.get("poster_path")),
                "rating": round(c["vote_average"], 1) if c.get("vote_average") else None,
                "links": media_links({"id": c.get("id"), "media_type": c.get("media_type"),
                                      "title": c.get("title"), "name": c.get("name")})}
               for c in known][:60]
    item = shape_person(r)
    item.update({
        "biography": r.get("biography"),
        "birthday": r.get("birthday"),
        "deathday": r.get("deathday"),
        "placeOfBirth": r.get("place_of_birth"),
        "alsoKnownAs": r.get("also_known_as"),
        "homepage": r.get("homepage"),
        "imdbId": r.get("imdb_id"),
        "movieCredits": [{"tmdbId": c.get("id"), "title": c.get("title"),
                          "character": c.get("character"),
                          "releaseDate": c.get("release_date"),
                          "poster": img(c.get("poster_path")),
                          "links": media_links({"id": c.get("id"), "media_type": "movie",
                                                "title": c.get("title")})}
                         for c in (r.get("movie_credits", {}).get("cast") or [])][:40],
        "tvCredits": [{"tmdbId": c.get("id"), "title": c.get("name"),
                       "character": c.get("character"),
                       "releaseDate": c.get("first_air_date"),
                       "poster": img(c.get("poster_path")),
                       "links": media_links({"id": c.get("id"), "media_type": "tv",
                                             "name": c.get("name")})}
                      for c in (r.get("tv_credits", {}).get("cast") or [])][:40],
        "images": [img(i.get("file_path"), "w185")
                   for i in (r.get("images", {}).get("profiles") or [])][:20],
    })
    return item


def comments(media_type, mid, q):
    if media_type not in ("movie", "tv"):
        raise ApiError(400, "media type harus movie atau tv")
    page = int_arg(q, "page", 1)
    limit = int_arg(q, "limit", 10)
    if page < 1 or limit < 1:
        raise ApiError(400, "page dan limit harus >= 1")
    if limit > 50:
        limit = 50
    query = {"page": page, "limit": limit}
    extra = {}
    if media_type == "tv":
        if q.get("season"):
            query["season"] = q["season"]
            extra["season"] = q["season"]
        if q.get("episode"):
            query["episode"] = q["episode"]
            extra["episode"] = q["episode"]
    r = flix("/comments/%s/%s" % (media_type, mid), query)
    r["mediaType"] = media_type
    r["tmdbId"] = int(mid) if str(mid).isdigit() else mid
    r["extra"] = extra
    return r


def reactions(media_type, mid, q):
    if media_type not in ("movie", "tv"):
        raise ApiError(400, "media type harus movie atau tv")
    query = {"anonymousId": q.get("anonymousId", ""),
             "mediaType": media_type, "mediaId": mid}
    if media_type == "tv":
        query["season"] = q.get("season", "1")
        query["episode"] = q.get("episode", "1")
    r = flix("/mediareaction", query)
    counts = r.get("counts") or {}
    r["totalVotes"] = sum(counts.values())
    r["tmdbId"] = int(mid) if str(mid).isdigit() else mid
    return r


def streams_endpoint(media_type, mid, q):
    if media_type not in ("movie", "tv"):
        raise ApiError(400, "media type harus movie atau tv")
    season = q.get("season")
    episode = q.get("episode")
    if media_type == "tv":
        season = season or "1"
        episode = episode or "1"
        if not str(season).isdigit() or not str(episode).isdigit():
            raise ApiError(400, "season dan episode harus angka")
    # which player? default 1 (the one that has always worked); "all" resolves
    # every player that has a resolver.
    want = str(q.get("player") or "1").strip().lower()
    if want == "all":
        pids = [p for p in player_registry.available()]
    elif want.isdigit():
        pids = [int(want)]
    else:
        raise ApiError(400, "player harus angka (1..12) atau 'all'")

    key = "stream:%s:%s:%s:%s:%s" % (media_type, mid, season or "",
                                     episode or "", ",".join(map(str, pids)))
    hit = cache_get(key)
    if hit is not None:
        hit = dict(hit)
        hit["cached"] = True
        return hit

    per_player = []
    for pid in pids:
        entry = {"playerId": pid,
                 "playerName": player_registry.PLAYER_META.get(pid, ("?",))[0],
                 "embed": player_registry.embed_url(pid, mid, media_type, season, episode)}
        try:
            if pid == 1:
                data = streams.resolve(mid, media_type, season, episode)
                entry.update({
                    "fileName": data.get("file_name"),
                    "title": data.get("title"),
                    "imdbId": data.get("imdb_id"),
                    "backdrop": data.get("backdrop"),
                    "defaultSubs": data.get("default_subs") or [],
                    "thumbnails": data.get("thumbnails_url"),
                    "streams": data.get("streams") or [],
                    "sources": data.get("sources") or [],
                    "tokensMinted": data.get("streams_needing_token", 0),
                    "chain": data.get("chain") or [],
                    "resolved": True,
                })
            else:
                data = player_registry.resolve(pid, mid, media_type, season, episode)
                entry.update({
                    "title": data.get("title"),
                    "streams": data.get("streams") or [],
                    "sources": data.get("sources") or [],
                    "subtitles": data.get("subtitles") or [],
                    "sourceId": data.get("sourceId"),
                    "chain": data.get("chain") or [],
                    "allSources": data.get("allSources") or [],
                    "deadSources": data.get("deadSources") or [],
                    "note": data.get("note"),
                    "resolved": bool(data.get("streams")),
                })
        except streams.StreamError as e:
            entry.update({"resolved": False, "status": e.status,
                          "error": str(e), "chain": e.chain,
                          "deadSources": getattr(e, "dead", None) or []})
        except Exception as e:  # noqa: BLE001  one player must not sink the rest
            entry.update({"resolved": False, "status": 502,
                          "error": "%s: %s" % (type(e).__name__, e)})
        per_player.append(entry)

    # flatten for convenience: every playable URL from every resolved player
    flat = []
    for e in per_player:
        if not e.get("resolved"):
            continue
        for u in (e.get("streams") or []):
            flat.append({"playerId": e["playerId"], "playerName": e["playerName"],
                         "url": u})
    payload = {
        "mediaType": media_type,
        "tmdbId": int(mid) if str(mid).isdigit() else mid,
        "season": int(season) if season else None,
        "episode": int(episode) if episode else None,
        "players": per_player,
        "streams": [f["url"] for f in flat],
        "flatStreams": flat,
        "resolvedPlayers": [e["playerId"] for e in per_player if e.get("resolved")],
        "failedPlayers": [{"playerId": e["playerId"], "error": e.get("error"),
                           "status": e.get("status")}
                          for e in per_player if not e.get("resolved")],
        "cached": False,
    }
    if len(pids) == 1:
        # single player: keep the old flat shape so existing callers still work
        e = per_player[0]
        payload.update({k: e.get(k) for k in
                        ("title", "imdbId", "fileName", "backdrop", "defaultSubs",
                         "thumbnails", "sources", "tokensMinted", "subtitles",
                         "sourceId", "allSources", "deadSources", "note")})
        payload["playerId"] = e["playerId"]
        payload["playerName"] = e["playerName"]
        payload["chain"] = e.get("chain") or []
        if not e.get("resolved"):
            payload["error"] = e.get("error")
            payload["status"] = e.get("status")
            # one player, and it failed: keep the old contract and answer with
            # the real status instead of a 200 carrying an error field. Only
            # `player=all` degrades gracefully, because there the partial
            # result is the point.
            raise ApiError(e.get("status") or 502, e.get("error") or "player gagal",
                           {"playerId": e["playerId"],
                            "chain": e.get("chain") or []})
    cache_put(key, payload, TTL_STREAM)
    return payload


def watch_endpoint(media_type, mid, q):
    if media_type not in ("movie", "tv"):
        raise ApiError(400, "media type harus movie atau tv")
    season = q.get("season") or ("1" if media_type == "tv" else None)
    episode = q.get("episode") or ("1" if media_type == "tv" else None)
    if media_type == "tv" and (not str(season).isdigit() or not str(episode).isdigit()):
        raise ApiError(400, "season dan episode harus angka")
    if media_type == "movie":
        meta = tmdb("movie/%s" % mid)
        title = meta.get("title")
    else:
        meta = tmdb("tv/%s" % mid)
        title = meta.get("name")
    page_kind = media_type
    url = "%s/%s/%s/%s" % (SITE, page_kind, mid, slug(title))
    if media_type == "movie":
        page = url + "/watch"
    else:
        page = "%s/season/%s?e=%s" % (url, season, episode)
    want = q.get("player") or "1"
    out = {
        "mediaType": media_type,
        "tmdbId": int(mid) if str(mid).isdigit() else mid,
        "title": title,
        "season": int(season) if season else None,
        "episode": int(episode) if episode else None,
        "sitePage": page,
        "players": players(mid, media_type, season, episode),
        "streamsEndpoint": "/api/streams/%s/%s%s" % (
            media_type, mid,
            "?season=%s&episode=%s" % (season, episode) if media_type == "tv" else ""),
        "allStreamsEndpoint": "/api/streams/%s/%s?player=all%s" % (
            media_type, mid,
            "&season=%s&episode=%s" % (season, episode) if media_type == "tv" else ""),
    }
    try:
        s = streams_endpoint(media_type, mid, dict(q, player=want))
        out["resolvedStreams"] = {
            "playerId": want,
            "fileName": s.get("fileName"),
            "defaultSubs": s.get("defaultSubs") or [],
            "thumbnails": s.get("thumbnails"),
            "streams": s.get("streams") or [],
            "sources": s.get("sources") or [],
            "subtitles": s.get("subtitles") or [],
            "sourceId": s.get("sourceId"),
            "chain": s.get("chain") or [],
        }
        if not s.get("streams"):
            out["resolvedStreams"]["error"] = s.get("error") or "tidak ada stream"
    except ApiError as e:
        out["resolvedStreams"] = {"playerId": want, "error": e.message,
                                  "status": e.status}
    out["unresolvedPlayers"] = [
        {"id": p["id"], "embed": p["embed"],
         "reason": p["unresolvedReason"] or "embed belum punya resolver"}
        for p in out["players"] if not p["resolvable"]
    ]
    return out


# ------------------------------------------------------------------ args

def req_arg(q, name):
    v = q.get(name)
    if v in (None, ""):
        raise ApiError(400, "parameter '%s' wajib diisi" % name)
    return v


def int_arg(q, name, default):
    v = q.get(name)
    if v in (None, ""):
        return default
    if not str(v).isdigit():
        raise ApiError(400, "parameter '%s' harus angka" % name)
    return int(v)


ROUTES = [
    (r"^/$", lambda m, q: {"service": "streamex-api", "version": "1.0.0",
                           "source": SITE, "mode": "live", "storage": "none",
                           "note": NOTE, "endpoints": ENDPOINTS}),
    (r"^/api/?$", lambda m, q: {"service": "streamex-api", "version": "1.0.0",
                                "source": SITE, "mode": "live", "storage": "none",
                                "note": NOTE, "endpoints": ENDPOINTS}),
    (r"^/api/health/?$", lambda m, q: h_health(q)),
    (r"^/api/home/?$", lambda m, q: h_home(q)),
    (r"^/api/movies/trending/?$", lambda m, q: listing("movie", "trending", q)),
    (r"^/api/movies/popular/?$", lambda m, q: listing("movie", "popular", q)),
    (r"^/api/movies/recent/?$", lambda m, q: listing("movie", "recent", q)),
    (r"^/api/movies/top-rated/?$", lambda m, q: listing("movie", "top-rated", q)),
    (r"^/api/movies/by-provider/?$", lambda m, q: by_provider("movie", q)),
    (r"^/api/movies/(\d+)/watch/?$", lambda m, q: watch_endpoint("movie", m.group(1), q)),
    (r"^/api/movies/(\d+)/?$", lambda m, q: movie_detail(m.group(1))),
    (r"^/api/tv/trending/?$", lambda m, q: listing("tv", "trending", q)),
    (r"^/api/tv/popular/?$", lambda m, q: listing("tv", "popular", q)),
    (r"^/api/tv/recent/?$", lambda m, q: listing("tv", "recent", q)),
    (r"^/api/tv/top-rated/?$", lambda m, q: listing("tv", "top-rated", q)),
    (r"^/api/tv/by-provider/?$", lambda m, q: by_provider("tv", q)),
    (r"^/api/tv/(\d+)/season/(\d+)/episode/(\d+)/?$",
     lambda m, q: episode_payload(m.group(1), m.group(2), m.group(3))),
    (r"^/api/tv/(\d+)/season/(\d+)/?$",
     lambda m, q: season_payload(m.group(1), m.group(2))),
    (r"^/api/tv/(\d+)/watch/?$", lambda m, q: watch_endpoint("tv", m.group(1), q)),
    (r"^/api/tv/(\d+)/?$", lambda m, q: tv_detail(m.group(1))),
    (r"^/api/search/?$", lambda m, q: search(q)),
    (r"^/api/genres/?$", lambda m, q: genres(valid_media(q.get("type", "movie")))),
    (r"^/api/countries/?$", lambda m, q: countries()),
    (r"^/api/people/trending/?$", lambda m, q: people_trending()),
    (r"^/api/people/(\d+)/?$", lambda m, q: person_detail(m.group(1))),
    (r"^/api/person/(\d+)/?$", lambda m, q: person_detail(m.group(1))),
    (r"^/api/comments/([a-z]+)/(\d+)/?$", lambda m, q: comments(m.group(1), m.group(2), q)),
    (r"^/api/reactions/([a-z]+)/(\d+)/?$", lambda m, q: reactions(m.group(1), m.group(2), q)),
    (r"^/api/streams/([a-z]+)/(\d+)/?$", lambda m, q: streams_endpoint(m.group(1), m.group(2), q)),
    (r"^/api/players/([a-z]+)/(\d+)/?$",
     lambda m, q: {"mediaType": m.group(1), "tmdbId": int(m.group(2)),
                   "season": q.get("season"), "episode": q.get("episode"),
                   "count": 12,
                   "players": players(m.group(2), m.group(1),
                                      q.get("season"), q.get("episode"))}),
]

ROUTE_TTL = {
    "/api/movies/trending": TTL_LIST,
    "/api/movies/popular": TTL_LIST,
    "/api/movies/recent": TTL_LIST,
    "/api/movies/top-rated": TTL_LIST,
    "/api/tv/trending": TTL_LIST,
    "/api/tv/popular": TTL_LIST,
    "/api/tv/recent": TTL_LIST,
    "/api/tv/top-rated": TTL_LIST,
    "/api/home": TTL_LIST,
    "/api/search": TTL_LIST,
    "/api/people/trending": TTL_DETAIL,
    "/api/genres": 86400,
    "/api/countries": 86400,
    "/api/comments": 60,
    "/api/reactions": 60,
}


def valid_media(v):
    if v not in ("movie", "tv"):
        raise ApiError(400, "type harus movie atau tv")
    return v


def resolve_route(path):
    for pattern, fn in ROUTES:
        m = re.match(pattern, path)
        if m:
            return m, fn
    return None, None


def ttl_for(path):
    for prefix, ttl in ROUTE_TTL.items():
        if path.startswith(prefix):
            return ttl
    return 0


# ------------------------------------------------------------------ server

class Handler(BaseHTTPRequestHandler):
    server_version = "streamex-api/1.0.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # keep the journal readable
        print("%s - %s" % (self.address_string(), fmt % args), flush=True)

    def _send(self, status, payload):
        body = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _dispatch(self):
        started = time.time()
        _local.src = []
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if len(path) > 1 and path.endswith("/"):
            path = path.rstrip("/")
        q = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        m, fn = resolve_route(path)
        if fn is None:
            self._send(404, {"ok": False, "error": {
                "status": 404, "message": "route tidak ada: %s" % path,
                "detail": {"routes": ENDPOINTS}}})
            return
        cache_key = None
        ttl = ttl_for(path)
        if ttl and self.command == "GET":
            cache_key = "route:%s?%s" % (path, parsed.query)
            cached = cache_get(cache_key)
            if cached is not None:
                payload = dict(cached)
                payload["cached"] = True
                payload["elapsedMs"] = int((time.time() - started) * 1000)
                self._send(200, payload)
                return
        try:
            data = fn(m, q)
            payload = {"ok": True, "mode": "live", "cached": False,
                       "elapsedMs": int((time.time() - started) * 1000),
                       "source": sources(),
                       "data": data}
            status = 200
        except ApiError as e:
            status = e.status
            payload = {"ok": False, "mode": "live",
                       "elapsedMs": int((time.time() - started) * 1000),
                       "source": sources(),
                       "error": {"status": e.status, "message": e.message,
                                 "detail": e.detail}}
        except Exception as e:  # noqa: BLE001
            status = 500
            payload = {"ok": False, "mode": "live",
                       "elapsedMs": int((time.time() - started) * 1000),
                       "source": sources(),
                       "error": {"status": 500,
                                 "message": "%s: %s" % (type(e).__name__, e),
                                 "detail": {}}}
        if status == 200 and cache_key:
            cache_put(cache_key, payload, ttl)
        self._send(status, payload)

    def do_GET(self):
        self._dispatch()

    def do_HEAD(self):
        self._dispatch()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        self._send(405, {"ok": False, "error": {
            "status": 405, "message": "API ini read only",
            "detail": {"note": "POST comment / watchlist butuh akun, tidak "
                               "dicerminkan di sini."}}})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8798)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    print("streamex-api listening on http://%s:%d (live, no storage)"
          % (args.host, args.port), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()

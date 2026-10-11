#!/usr/bin/env python3
"""
Player registry for streamex.pw.

The site offers 12 third-party embed players per title. Player #1 is resolved
by ../streams.py; the rest live in this package as p<id>_<name>.py, each
exposing:

    NAME = "vsembed"
    HOST = "vsembed.ru"
    def resolve(tmdb_id, media_type="movie", season=None, episode=None) -> dict

resolve() returns a dict with at least:
    {"streams": [playable url, ...], "sources": [{"raw","playable",...}]}
and may add subtitles / evidence. Raising StreamError (from ../streams.py) is
the expected way to report a failure.

A player whose module is absent, or which raises, is reported as unresolved
with the reason: the API never invents a stream URL for a player it could not
actually resolve.
"""
import importlib
import os
import pkgutil
import re
import threading

HERE = os.path.dirname(os.path.abspath(__file__))

# id -> (name, host, tags). Tags mirror the badge the site prints on its
# PLAYER # switcher; they are the site's own labels, not our judgement.
PLAYER_META = {
    1: ("vsembed", "vsembed.ru", [("OG", "info")]),
    2: ("vidcore", "vidcore.io", [("4K", "success"), ("BEST", "success")]),
    3: ("videasy", "player.videasy.to", [("GOOD", "info")]),
    4: ("mapple", "mapple.rip", [("4K", "success")]),
    5: ("vidbolt", "vidbolt.xyz", []),
    6: ("cinemaos", "cinemaos.tech", []),
    7: ("zxcstream", "zxcstream.xyz", []),
    8: ("peachify", "peachify.top", []),
    9: ("xpass", "play.xpass.top", [("NEW", "error"), ("THUMBS_UP", "info")]),
    10: ("1embed", "1embed.cc", []),
    11: ("multiembed", "multiembed.mov", []),
    12: ("vidfast", "vidfast.vc", [("4K", "success")]),
}

# the embed URLs the site itself builds for each player
EMBEDS = {
    "movie": {
        1: "https://vsembed.ru/embed/movie?tmdb={id}",
        2: "https://vidcore.io/movie/{id}?autoPlay=true&theme=addc35",
        3: "https://player.videasy.to/movie/{id}?color=addc35&overlay=true",
        4: "https://mapple.rip/watch/movie/{id}?autoPlay=true&theme=addc35",
        5: "https://vidbolt.xyz/movie/{id}?autoplay=true&theme=addc35&title=true&poster=true",
        6: "https://cinemaos.tech/player/{id}?autoPlay=true&theme=addc35",
        7: "https://zxcstream.xyz/player/movie/{id}?autoplay=true&color=addc35&back=false&domainAd=Flixmomo.tv",
        8: "https://peachify.top/embed/movie/{id}?accent=ADDC35&pip=hide&autoPlay=true",
        9: "https://play.xpass.top/e/movie/{id}?autostart=true",
        10: "https://1embed.cc/embed/movie/{id}?autoPlay=1&color=ADDC35",
        11: "https://multiembed.mov/?tmdb=1&video_id={id}",
        12: "https://vidfast.vc/movie/{id}?autoPlay=true&title=true&poster=true&theme=ADDC35",
    },
    "tv": {
        1: "https://vsembed.ru/embed/tv?tmdb={id}&season={s}&episode={e}",
        2: "https://vidcore.io/tv/{id}/{s}/{e}?theme=addc35&nextButton=true&autoPlay=true&autoNext=true",
        3: "https://player.videasy.to/tv/{id}/{s}/{e}?nextEpisode=true&episodeSelector=true&overlay=true&color=addc35&autoplayNextEpisode=true",
        4: "https://mapple.rip/watch/tv/{id}-{s}-{e}?theme=addc35&autoPlay=true&autoNext=true",
        5: "https://vidbolt.xyz/tv/{id}/{s}/{e}?theme=addc35&title=true&poster=true&autonext=true&autoplay=true",
        6: "https://cinemaos.tech/player/{id}/{s}/{e}?theme=addc35&autoPlay=true&autoNext=true",
        7: "https://zxcstream.xyz/player/tv/{id}/{s}/{e}?color=addc35&back=false&domainAd=Flixmomo.tv&autoplay=true",
        8: "https://peachify.top/embed/tv/{id}/{s}/{e}?accent=ADDC35&pip=hide&autoPlay=true",
        9: "https://play.xpass.top/e/tv/{id}/{s}/{e}?autostart=true",
        10: "https://1embed.cc/embed/tv/{id}/{s}/{e}?color=ADDC35&autoPlay=1",
        11: "https://multiembed.mov/?tmdb=1&video_id={id}&s={s}&e={e}",
        12: "https://vidfast.vc/tv/{id}/{s}/{e}?title=true&poster=true&theme=ADDC35&autoPlay=true&nextButton=true&autoNext=true",
    },
}

_MODULES = {}
_LOCK = threading.Lock()


def embed_url(pid, tmdb_id, media_type="movie", season=None, episode=None):
    tpl = EMBEDS.get(media_type, {}).get(pid)
    if not tpl:
        return None
    return tpl.format(id=tmdb_id, s=season or 1, e=episode or 1)


def _module(pid):
    """Import players/p<pid>_<name>.py lazily, tolerating a missing file."""
    if pid in _MODULES:
        return _MODULES[pid]
    with _LOCK:
        if pid in _MODULES:
            return _MODULES[pid]
        name = PLAYER_META.get(pid, (None,))[0]
        mod = None
        if name:
            try:
                mod = importlib.import_module("%s.p%d_%s" % (__package__ or "players", pid, name))
            except ModuleNotFoundError:
                mod = None
            except Exception as e:  # noqa: BLE001  a broken resolver must not 500 the API
                mod = e
        _MODULES[pid] = mod
        return mod


def reasons(pid):
    """Why a player has no resolver, or None when it has one."""
    if pid == 1:
        # player #1 is resolved by ../streams.py, not by a module in this package
        return None
    mod = _module(pid)
    if mod is None:
        return "resolver belum ada untuk player ini"
    if isinstance(mod, Exception):
        return "resolver gagal dimuat: %s" % mod
    if not callable(getattr(mod, "resolve", None)):
        return "resolver tidak punya fungsi resolve()"
    return None


def catalog(tmdb_id, media_type="movie", season=None, episode=None):
    """The 12 players with their embed URLs and whether we can resolve them."""
    out = []
    for pid in sorted(PLAYER_META):
        name, host, tags = PLAYER_META[pid]
        out.append({
            "id": pid,
            "name": name,
            "host": host,
            "tags": [{"label": l, "color": c} for l, c in tags],
            "embed": embed_url(pid, tmdb_id, media_type, season, episode),
            "resolvable": reasons(pid) is None,
            "unresolvedReason": reasons(pid),
        })
    return out


def resolve(pid, tmdb_id, media_type="movie", season=None, episode=None):
    """Run one player's resolver. Raises whatever the resolver raises."""
    why = reasons(pid)
    if why:
        from streams import StreamError
        raise StreamError(501, "player #%s (%s): %s"
                          % (pid, PLAYER_META.get(pid, ("?",""))[0], why))
    if pid == 1:
        # player #1 lives in ../streams.py, so route through it and normalise
        # the result into this package's shape. Calling _module(1) here would
        # be a None dereference.
        import streams
        d = dict(streams.resolve(tmdb_id, media_type, season, episode) or {})
        srcs = d.get("sources") or []
        data = {
            "title": d.get("title"),
            "sources": srcs,
            "streams": d.get("streams") or [s.get("playable") or s.get("raw")
                                            for s in srcs],
            "subtitles": d.get("default_subs") or [],
            "sourceId": d.get("file_name"),
            "allSources": srcs,
            "deadSources": [],
            "chain": d.get("chain") or [],
            "note": ("player #1 (vsembed) di-resolve oleh streams.py lewat chain "
                     "vsembed.ru -> stellarconductornexus.com -> data.vidsrc.sh."),
        }
    else:
        mod = _module(pid)
        data = mod.resolve(tmdb_id, media_type, season, episode)
        data = dict(data or {})
    name, host, _ = PLAYER_META.get(pid, ("?", "?", []))
    data.setdefault("playerId", pid)
    data.setdefault("playerName", name)
    data.setdefault("playerHost", host)
    data.setdefault("embed", embed_url(pid, tmdb_id, media_type, season, episode))
    data.setdefault("streams", [s.get("playable", s.get("url")) if isinstance(s, dict) else s
                                for s in (data.get("sources") or [])])
    return data


def available():
    """Which player ids have a working resolver module right now."""
    return [p for p in sorted(PLAYER_META) if reasons(p) is None]


def hosts():
    return {pid: PLAYER_META[pid][1] for pid in sorted(PLAYER_META)}

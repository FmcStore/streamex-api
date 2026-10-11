#!/usr/bin/env bash
# Smoke test for streamex-api. Drives every route against a live server and
# resolves its targets from real data instead of assuming ids.
#
# NOTE: never test a big body with `printf ... | grep -q`. grep exits on the
# first match, printf then takes SIGPIPE, and under `set -o pipefail` the
# pipeline reports 141 even on a hit: large bodies fail, small ones pass.
# `has` does the substring test in bash, with no pipe to break.
set -uo pipefail
B="${BASE:-http://127.0.0.1:8798}"
PASS=0; FAIL=0

has(){ case "$1" in *"$2"*) return 0;; *) return 1;; esac; }
ok(){ printf 'PASS  %-58s %s\n' "$1" "$2"; PASS=$((PASS+1)); }
no(){ printf 'FAIL  %-58s %s\n' "$1" "$2"; FAIL=$((FAIL+1)); }
# verdict lines for check() go to stderr: check() is called with stdout
# redirected (it returns the body), which would otherwise swallow them.
ok2(){ printf 'PASS  %-58s %s\n' "$1" "$2" >&2; PASS=$((PASS+1)); }
no2(){ printf 'FAIL  %-58s %s\n' "$1" "$2" >&2; FAIL=$((FAIL+1)); }

jq_get(){ python3 -c 'import json,sys
d=json.load(sys.stdin)
for k in sys.argv[1].split("."):
    if k=="": continue
    d=d[int(k)] if isinstance(d,list) else d.get(k)
    if d is None: break
print(json.dumps(d) if isinstance(d,(dict,list)) else d)' "$1" 2>/dev/null; }

check(){ # name url expected-substring
  local name="$1" url="$2" want="${3:-}"
  local body code
  body=$(curl -s -m 90 -w '\n%{http_code}' "$url")
  code=$(printf '%s' "$body" | tail -n1)
  body=$(printf '%s' "$body" | sed '$d')
  if [ "$code" != "200" ]; then no2 "$name" "HTTP $code"; return 1; fi
  if [ -n "$want" ] && ! has "$body" "$want"; then
    no2 "$name" "missing: $want"; printf 'BODY: %.600s\n' "$body" >&2; return 1; fi
  ok2 "$name" "HTTP 200"
  printf '%s' "$body"
  return 0
}

echo "== streamex-api smoke @ $B =="

check "GET / (catalogue)"            "$B/"            'streamex-api'         >/dev/null
check "GET /api (catalogue)"         "$B/api"         'endpoints'            >/dev/null
check "GET /api/health"              "$B/api/health"  '"status": "ok"'       >/dev/null
check "GET /api/home"                "$B/api/home"    'trendingMovies'       >/dev/null

for ep in movies/trending movies/popular movies/recent movies/top-rated \
          tv/trending tv/popular tv/recent tv/top-rated; do
  OUT=$(curl -s -m 90 "$B/api/$ep?page=1")
  N=$(printf '%s' "$OUT" | jq_get data.items.0.tmdbId)
  if [ -n "$N" ] && [ "$N" != "None" ] && has "$OUT" '"hasNext"'; then
    ok "GET /api/$ep" "first id=$N"
  else
    no "GET /api/$ep" "no items"
  fi
done

# --- resolve real targets from the listings, never a hardcoded id
MOVIE=$(curl -s -m 90 "$B/api/movies/trending?page=1" | jq_get data.items.0.tmdbId)
TV=$(curl -s -m 90 "$B/api/tv/trending?page=1" | jq_get data.items.0.tmdbId)
echo "-- resolved targets: movie=$MOVIE tv=$TV"

MD=$(curl -s -m 90 "$B/api/movies/$MOVIE")
has "$MD" '"cast"'         && ok "GET /api/movies/{id}" "$MOVIE" || no "GET /api/movies/{id}" "no cast"
has "$MD" '"playersEndpoint"' && ok "detail.playersEndpoint present" "$MOVIE" || no "detail.playersEndpoint" "missing"

PL=$(curl -s -m 90 "$B/api/players/movie/$MOVIE")
has "$PL" '"count": 12' && ok "GET /api/players/movie/{id}" "12 players" \
  || no "GET /api/players/movie/{id}" "player count"

TD=$(curl -s -m 90 "$B/api/tv/$TV")
has "$TD" '"seasons"' && ok "GET /api/tv/{id}" "$TV" || no "GET /api/tv/{id}" "no seasons"
SN=$(printf '%s' "$TD" | jq_get data.seasons.0.seasonNumber)
SEA=$(curl -s -m 90 "$B/api/tv/$TV/season/$SN")
EC=$(printf '%s' "$SEA" | jq_get data.episodeCount)
has "$SEA" '"episodes"' && ok "GET /api/tv/{id}/season/{n}" "season $SN eps=$EC" \
  || no "GET /api/tv/{id}/season/{n}" "no episodes"
if [ -n "$EC" ] && [ "$EC" != "0" ] && [ "$EC" != "None" ]; then
  EPI=$(curl -s -m 90 "$B/api/tv/$TV/season/$SN/episode/1")
  has "$EPI" '"episodeNumber"' && ok "GET /api/tv/{id}/season/{n}/episode/1" "ok" \
    || no "GET /api/tv/{id}/season/{n}/episode/1" "no episode"
fi

check "GET /api/search?q=fight"      "$B/api/search?q=fight&page=1"            '"items"'                 >/dev/null
check "GET /api/search?type=movie"   "$B/api/search?q=fight&type=movie"        '"mediaType": "movie"'    >/dev/null
check "GET /api/genres?type=movie"   "$B/api/genres?type=movie"                '"genres"'                >/dev/null
check "GET /api/genres?type=tv"      "$B/api/genres?type=tv"                   '"mediaType": "tv"'       >/dev/null
check "GET /api/people/trending"     "$B/api/people/trending"                  '"profile"'               >/dev/null

PID=$(curl -s -m 90 "$B/api/people/trending" | jq_get data.items.0.tmdbId)
PD=$(curl -s -m 90 "$B/api/people/$PID")
has "$PD" '"knownFor"' && ok "GET /api/people/{id}" "$PID" || no "GET /api/people/{id}" "no knownFor"

check "GET /api/comments/movie/{id}"  "$B/api/comments/movie/$MOVIE?page=1&limit=3"        '"comments"'    >/dev/null
check "GET /api/reactions/movie/{id}" "$B/api/reactions/movie/$MOVIE"                       '"counts"'      >/dev/null
check "GET /api/reactions/tv/{id}"    "$B/api/reactions/tv/$TV?season=1&episode=1"          '"totalVotes"'  >/dev/null
check "GET /api/comments/tv/{id}"     "$B/api/comments/tv/$TV?page=1&limit=2&season=1&episode=1" '"season"' >/dev/null

# --- playback: the real thing
ST=$(curl -s -m 240 "$B/api/streams/movie/$MOVIE")
has "$ST" 'master.m3u8' && ok "GET /api/streams/movie/{id}" "$MOVIE" \
  || no "GET /api/streams/movie/{id}" "no m3u8"

STV=$(curl -s -m 240 "$B/api/streams/tv/1396?season=1&episode=1")
has "$STV" 'master.m3u8' && ok "GET /api/streams/tv/{id}" "breaking bad S1E1" \
  || no "GET /api/streams/tv/{id}" "no m3u8"

W=$(curl -s -m 180 "$B/api/movies/$MOVIE/watch")
has "$W" '"resolvedStreams"' && ok "GET /api/movies/{id}/watch" "resolved" \
  || no "GET /api/movies/{id}/watch" "no resolvedStreams"
WT=$(curl -s -m 240 "$B/api/tv/1396/watch?season=1&episode=1")
has "$WT" '"unresolvedPlayers"' && ok "GET /api/tv/{id}/watch" "resolved" \
  || no "GET /api/tv/{id}/watch" "no unresolvedPlayers"

# --- the stream url must actually play (HTTP 200 + m3u8 body), and the raw url
#     must NOT play without its host token (that is what the token step is for)
U=$(printf '%s' "$ST" | python3 -c 'import json,sys;d=json.load(sys.stdin);print((d["data"]["streams"] or [""])[0])')
RAW=$(printf '%s' "$ST" | python3 -c 'import json,sys;d=json.load(sys.stdin);print((d["data"]["sources"] or [{"raw":""}])[0]["raw"])')
HC=$(curl -s -m 60 -o /tmp/stream_probe.m3u8 -w '%{http_code}' -H "Referer: https://streamex.pw/" "$U")
HASX=$(head -c 40 /tmp/stream_probe.m3u8)
if [ "$HC" = "200" ] && has "$HASX" 'EXT-X'; then
  ok "stream url plays (with host token)" "HTTP 200, $(wc -c </tmp/stream_probe.m3u8) bytes"
else
  no "stream url plays (with host token)" "HTTP $HC"
fi
RC=$(curl -s -m 30 -o /dev/null -w '%{http_code}' "$RAW")
[ "$RC" = "401" ] && ok "raw url without token -> 401" "token step is real" \
  || no "raw url without token" "expected 401, got $RC"

# --- error handling / edge cases
t(){ # name url expected-code [method]
  local c
  c=$(curl -s -m 30 -o /dev/null -w '%{http_code}' ${4:+-X "$4"} "$2")
  [ "$c" = "$3" ] && ok "$1" "got $c" || no "$1" "got $c"
}
t "unknown movie -> 404"        "$B/api/movies/99999999"          404
t "non numeric id -> 404"       "$B/api/movies/abc"               404
t "unknown route -> 404"        "$B/api/nope"                     404
t "search without q -> 400"     "$B/api/search"                   400
t "page=abc -> 400"             "$B/api/movies/popular?page=abc"  400
t "bad media type -> 400"       "$B/api/comments/xx/55"           400
t "POST -> 405 read only"       "$B/api/comments/movie/$MOVIE"    405 POST
t "unknown genre -> 400"        "$B/api/movies/popular?genre=NopeGenre" 400
t "bad year -> 400"             "$B/api/movies/popular?year=20x4" 400
t "bad season -> 400"           "$B/api/streams/tv/1396?season=abc&episode=1" 400
t "non-numeric season route -> 404" "$B/api/tv/$TV/season/abc"   404
t "OPTIONS -> 204 CORS"         "$B/api/home"                     204 OPTIONS

# an unknown id must not blow up the resolver with a 500
SX=$(curl -s -m 60 -o /dev/null -w '%{http_code}' "$B/api/streams/movie/99999999")
case "$SX" in 404|502) ok "stream unknown id -> $SX" "not 500";; *) no "stream unknown id" "got $SX";; esac

echo
echo "== $PASS passed, $FAIL failed =="
[ "$FAIL" = "0" ]

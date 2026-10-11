# streamex-api

Read-only JSON REST API di atas **https://streamex.pw**.

Streamex.pw sendiri tidak punya backend data: itu Next.js murni yang memanggil
TMDB langsung dari browser. API ini memetakan permukaan yang sama jadi endpoint
JSON yang stabil, plus resolver playback yang menyelesaikan rantai player
sampai URL `.m3u8` yang betul-betul bisa diputar.

## Jalan

```bash
# manual
python3 /root/streamex-api/server.py --port 8798

# service (sudah aktif + enabled)
systemctl --user restart streamex-api
systemctl --user status streamex-api
```

Base URL: `http://127.0.0.1:8798` (default bind localhost). Storage: **tidak
ada** (murni live proxy + cache memori pendek), jadi tidak ada skema DB yang
perlu dimigrasi.

### Unit systemd

- `streamex-api.service` -> `/usr/bin/python3 /root/streamex-api/server.py --port 8798`
- `streamex-tunnel.service` -> cloudflared quick tunnel ke port 8798

URL tunnel berubah tiap restart (quick tunnel, tanpa akun). Ambil yang aktif:

```bash
systemctl --user status streamex-tunnel -n 80 --no-pager | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | head -1
```

`journalctl --user` kosong di container ini, jadi pakai perintah di atas.

Kunci TMDB situs terbaca publik di bundle-nya, tapi tetap bisa ditimpa:

```bash
TMDB_KEY=<key lain> python3 server.py --port 8798
```

## Endpoint

Katalog
- `GET /` dan `GET /api` : katalog endpoint (self describing)
- `GET /api/health`
- `GET /api/home` : hero + trendingMovies + trendingShows + trendingArtists

Movie
- `GET /api/movies/trending?page=`
- `GET /api/movies/popular?page=&lang=&region=&year=&genre=&minRating=`
- `GET /api/movies/recent?page=&lang=&region=&genre=&minRating=`
- `GET /api/movies/top-rated?page=&lang=&region=&year=&genre=&minRating=`
- `GET /api/movies/by-provider?provider=<tmdb provider id>`
- `GET /api/movies/{tmdbId}` : detail + cast + videos + rekomendasi + playersEndpoint
- `GET /api/movies/{tmdbId}/watch` : detail + daftar player + stream hasil resolve

TV
- `GET /api/tv/trending|popular|recent|top-rated` (query sama seperti movie)
- `GET /api/tv/by-provider?provider=`
- `GET /api/tv/{tmdbId}`
- `GET /api/tv/{tmdbId}/season/{n}`
- `GET /api/tv/{tmdbId}/season/{n}/episode/{e}`
- `GET /api/tv/{tmdbId}/watch?season=&episode=`

Pencarian dan orang
- `GET /api/search?q=&page=&type=multi|movie|tv|person`
- `GET /api/genres?type=movie|tv`
- `GET /api/people/trending`
- `GET /api/person/{tmdbId}`

Sosial (dari backend flixbaba milik situs)
- `GET /api/comments/{movie|tv}/{tmdbId}?page=&limit=&season=&episode=`
- `GET /api/reactions/{movie|tv}/{tmdbId}?season=&episode=`

Playback
- `GET /api/players/{movie|tv}/{tmdbId}?season=&episode=` : daftar 12 player
  (nama, tag, url embed) tanpa me-resolve stream
- `GET /api/streams/{movie|tv}/{tmdbId}?season=&episode=&player=<n|all>` :
  URL `.m3u8` siap pakai + subtitle + thumbnails.
  `player=` pilih satu player (default `1`), `player=all` menyelesaikan semua
  player yang punya resolver dan mengembalikan `resolvedPlayers` /
  `failedPlayers`. Player yang gagal tidak menjatuhkan yang lain.

Semua respons berbentuk `{"ok":true,"data":...}` atau
`{"ok":false,"error":{"status":..,"message":..}}`. Query yang tidak valid
dibalas 400, id yang tidak ada 404, method selain GET/HEAD/OPTIONS 405.
`/api/streams` dengan satu player yang gagal membalas status aslinya (bukan 200
berisi error); hanya `player=all` yang boleh degradasi anggun.

## Resolver playback

Situs menaruh 12 player. Yang sudah punya resolver dan **terbukti hidup**:
**#1 vsembed**, **#5 vidbolt**, **#9 xpass**. Sisanya terdaftar lengkap di
`/api/players/...` dengan `unresolvedReason` masing-masing, dan `/api/streams`
mengembalikannya lewat `failedPlayers` / `unresolvedPlayers` — API ini tidak
pernah mengarang URL stream untuk player yang belum bisa diselesaikan.

### Player #1 — vsembed (`OG`)

```
streamex.pw/<type>/<id>/watch            tombol PLAYER #1
  -> vsembed.ru/vs_src.php?type&id       -> src ke stellarconductornexus.com
     -> stellarconductornexus.com/embed/player/<type>/<id>   -> window.CONFIG
        -> data.vidsrc.sh/api.php?type&tmdb    -> field stream_urls (terenkripsi)
           -> vsdec (wasm ChaCha20)            -> URL HLS bersih
```

1. `stream_urls` di `data.vidsrc.sh` tidak bisa dibaca langsung. Isinya
   base64 ChaCha20 yang dibuka wasm milik player sendiri
   (`.../iframe_player/assets/vsdec.js`). `decrypt/vsdec.mjs` memuat wasm itu
   dari host-nya dan mendekripsi per-window.
2. URL yang dihasilkan **belum bertoken**. Tiap host media (mis.
   `*.workers.dev`) menolak URL tanpa `?token=` dengan 401. Token diambil
   dengan mencetak ulang dari `<origin>/generate.php?id=<media id>` lalu
   ditempel oleh `streams.py`. Jadi `streams[]` = siap putar,
   `sources[].raw` = versi asli tanpa token (untuk perbandingan).

### Player #5 — vidbolt (`players/p5_vidbolt.py`)

vidbolt bukan host, tapi fan-out ke banyak scraper upstream. Kontraknya dibaca
dari chunk-nya sendiri, bukan ditebak:

```
vidbolt.xyz/assets/index-*.js
  -> assets/EmbeddedOnlyEmbedPage-*.js      tabel provider (15 entri)
     -> assets/scraperUrl-*.js              base backend bersama
     -> assets/<provider>-*.js              satu panggilan upstream per provider
```

Sebagian besar provider memanggil
`https://scraper.vidbolt.xyz/scrape/<Nama>/<movie|tv>/<id>`. Dua punya jalur
sendiri: **Orion** (`api.movy.lol/api/source/<tmdb>`) dan **Nebula**
(`/showbox.filmu.in/scrape/VidRock` + header `x-api-key`). Karena tabel provider
dibaca saat runtime, provider baru ikut terbawa tanpa ubah kode.

### Player #9 — xpass (`players/p9_xpass.py`)

Body `/data` player ini terenkripsi dan routing-nya ada di bundle
`javascript-obfuscator`, jadi membaca JS-nya secara statis jalan buntu.
Solusinya menjalankan halaman player aslinya di Chromium headless lewat CDP dan
membaca `window.backups` — persis permintaan yang dilakukan player asli, bukan
scraping DOM (`players/p9_backups.mjs`). `sourceId` stabil per judul, dan
setelah itu endpoint `/mdata|/mvid|/vip/<id>/1/playlist.json` bisa dipanggil
langsung tanpa browser.

Dua jebakan yang sudah ditangani:

- mirror `tik`/`vip`/`mol`.1x2.space membalas **403** tanpa `Referer:
  https://play.xpass.top/`. Resolver mem-probe dengan header itu dan
  menitipkannya di `sources[].headers` supaya pemanggil bisa memutar URL-nya.
- satu port debug Chromium hanya boleh dipakai satu proses; dua run bersamaan
  membuat yang kalah pulang tanpa sumber. Port diturunkan dari judul dan
  dikunci, jadi judul yang sama tidak bisa tumpang-tindih.

### Kejujuran hasil

Player yang punya tahap probe (P5, P9) hanya mengiklankan URL yang sudah
di-fetch dan membalas `200` + `#EXTM3U`/`application/vnd.apple.mpegurl`. Sisanya
masuk `deadSources[]` beserta `reason`-nya, bukan diiklankan sebagai stream
hidup.

P1 tidak memprobe: URL-nya ditandatangani lalu langsung dikembalikan, karena
tiap fetch menambah panggilan ke host yang sama. Kalau ada entri di `sources[]`
dengan `tokenError`, artinya token untuk origin itu tidak didapat (mis. host
membalas 429) dan URL-nya tidak akan jalan.

Token P1 berlaku 4 jam dan terikat IP (`ip_cidr` di JWT-nya), jadi `streams[]`
dari P1 hanya valid untuk pemanggil yang keluar dari IP yang sama dengan server
ini. Jangan disimpan lama; ambil ulang kalau pemutaran gagal.

## Status player

| # | player | status |
|---|--------|--------|
| 1 | vsembed | resolvable, terverifikasi |
| 2 | vidcore | belum ada resolver |
| 3 | videasy | belum ada resolver |
| 4 | mapple | belum ada resolver |
| 5 | vidbolt | resolvable, terverifikasi |
| 6 | - | belum ada resolver |
| 7 | - | belum ada resolver |
| 8 | peachify | mati di sisi pemilik (HTTP 523) |
| 9 | xpass | resolvable, terverifikasi |
| 10 | 1embed | timeout |
| 11 | multiembed | Cloudflare Turnstile |
| 12 | vidfast | belum ada resolver |

Petunjuk yang sudah terkumpul untuk lanjutan: #2, #4, dan #12 ternyata **satu
platform** (memakai Google Analytics id yang sama, endpoint `/api/request-token`),
dan #3 `player.videasy.to` sekeluarga Next.js dengan vidbolt sehingga pola
chunk-chunk P5 kemungkinan besar berlaku. Belum diverifikasi.

## Data sources

- `api.themoviedb.org/3/**` : seluruh metadata, pencarian, genre, orang
  (frontend situs memakai API ini apa adanya)
- `image.tmdb.org` : poster/backdrop
- `dd.flixbaba.me` : `/comments/...` dan `/mediareaction/...` (butuh header
  `Referer: https://streamex.pw/`)
- `vsembed.ru` -> `stellarconductornexus.com` -> `data.vidsrc.sh` : stream P1
- `vidbolt.xyz` -> `scraper.vidbolt.xyz` + upstream per provider : stream P5
- `play.xpass.top` : stream P9

## Instalasi

```bash
git clone https://github.com/FmcStore/streamex-api.git
cd streamex-api
python3 server.py --port 8798        # tanpa dependensi, stdlib saja
```

Kebutuhan: Python 3.8+, dan `node` + Chromium (dipakai P1 untuk wasm dan P9
untuk halaman player). Tanpa `node`, P1 dan P9 melaporkan diri tidak resolvable
dan P5 tetap jalan.

Unit systemd opsional ada di `cloudflared.yml` (tunnel) dan contoh unit di
bagian atas.

## Test

```bash
bash /root/streamex-api/smoke.sh           # 47 assertion
BASE=http://127.0.0.1:8798 bash smoke.sh
```

Hasil terakhir: **47 passed, 0 failed** (termasuk membuktikan URL stream
membalas 200 + `#EXT-X` dan URL tanpa token membalas 401).

Catatan untuk penulis test: jangan uji body besar dengan `printf ... | grep -q`.
`grep -q` keluar pada match pertama, `printf` kena SIGPIPE, dan di bawah
`set -o pipefail` pipa melaporkan 141 walaupun cocok: body besar jadi terlihat
"missing". `smoke.sh` memakai fungsi `has` (substring bash, tanpa pipa).

## Tidak dicerminkan

Login, profil, watchlist, dan posting komentar butuh akun di situs, jadi
sengaja tidak ada. API ini read only: POST dibalas 405.

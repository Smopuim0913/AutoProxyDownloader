# AutoProxyDownloader

<p align="center"><img src="icon.png" width="120" alt="AutoProxyDownloader icon"></p>

**English** | [简体中文](README.zh-CN.md)

A small [aria2](https://aria2.github.io/) wrapper that decides **per URL** whether a download goes through an HTTP proxy (by consulting [`gfwlist`](https://github.com/gfwlist/gfwlist)) and **preserves the server's `Last-Modified` timestamp** on the downloaded file. HLS playlists (`.m3u8` / `.m3u`) are handed to **ffmpeg** instead, so a live stream lands as one playable `.mp4`.

## Why this exists

Most download tools apply a proxy globally: route everything through the tunnel (slow and wasteful for domestic sites) or route nothing (anything blocked simply fails). aria2's `--all-proxy` is a *process-wide* option, so "proxy for this URL, direct for that one" requires one aria2c process per file — which is exactly what this wrapper does, with `gfwlist` answering the per-URL question.

The timestamp part matters for mirroring: `curl -R` / `wget --timestamping` users expect the local file to keep the origin's mtime so that later comparisons are meaningful.

## Features

- **Per-URL proxy decision.** `gfwlist` rules (`||domain`, `|prefix`, `suffix|`, `/regex/`, wildcards, `^` separators, `@@` exceptions) are evaluated first-match-wins; matched URLs get `--all-proxy=<proxy>`, everything else gets an explicitly empty proxy so no ambient config can leak in.
- **Server timestamp preserved.** aria2's own `--remote-time` is the primary path; a `HEAD` (falling back to a ranged `GET`) re-check fixes the mtime with `os.utime` when needed, and the run report says which of the two happened. If the server has no `Last-Modified` (dynamic pages, some CDNs), the tool says so instead of pretending it worked.
- **`gfwlist` cache with auto-refresh.** Fetched through the proxy on first use or when stale, saved verbatim (still base64), previous copy kept as `gfwlist.txt.bak`; if the fetch fails, the cached copy keeps working.
- **Unlisted-but-blocked hosts.** Direct failures of network class automatically retry through the proxy (and vice versa), and the successful flip is remembered per host so the next run starts on the mode that works.
- **Two engines.** Plain files go through aria2c; URLs ending in `.m3u8` / `.m3u` go through ffmpeg (`ffmpeg -i <url> -c copy <name>.mp4`), so an HLS stream becomes one `.mp4` named after the playlist. The proxy decision is still gfwlist's: `-http_proxy` is only added when that URL is judged proxied.
- **Batch, resume, skip.** Thread pool over aria2 processes, `--continue` for partial files, existing complete files are skipped, `--dry-run` and `--json` for scripting.
- **Console feedback.** Every task announces itself (`Direct Downloading: <url>` or `Downloading with proxy (<proxy>): <url>`), and a console run shows a live progress line per task (bytes, percentage, speed, ETA); when output is redirected, progress is printed in 10% steps instead.
- **Bilingual UI.** Messages follow the Windows UI language (Chinese on a Chinese system, English otherwise); override with `--lang auto|zh|en` or `APD_LANG=zh|en`.
- **Optional GUI launcher.** `AutoProxyDownloader_gui.py` shows a URL input dialog, then runs the download in the same console window and waits for any key before closing (Windows shortcut included).

## Requirements

| | |
| --- | --- |
| Python | 3.10+ (uses `X \| None` annotations). Tested on 3.13/3.14 on Windows |
| aria2 | `aria2c` 1.16+ for `--remote-time`; tested with 1.37.0 (Windows build) |
| OS | Windows for the shortcut / console-pause helpers; the CLI itself is portable |

## Setup

```powershell
git clone <this-repo> AutoProxyDownloader
cd AutoProxyDownloader
```

1. Drop `aria2c.exe` next to the scripts (release page: <https://github.com/aria2/aria2/releases>). It is *not* committed — see `.gitignore`.
2. `AutoProxyDownloader.json` already ships usable defaults, so nothing else is needed.

## Quick start

```powershell
# one URL, files land in %USERPROFILE%\Downloads
python -X utf8 AutoProxyDownloader.py https://example.com/a.zip

# batch, 4 parallel downloads
python -X utf8 AutoProxyDownloader.py --input urls.txt --jobs 4

# show what it *would* do, without downloading
python -X utf8 AutoProxyDownloader.py https://www.google.com/ --explain

# refresh the rule cache and exit
python -X utf8 AutoProxyDownloader.py --refresh-gfwlist

# offline self-check of the rule engine
python -X utf8 AutoProxyDownloader.py --selftest
```

`-X utf8` avoids console encoding trouble on Windows.

Double-click `AutoProxyDownloader.lnk` for the GUI flow: type/paste one or more URLs (`Ctrl+Enter` to confirm, `Esc` to cancel), the download then runs in the console, and at the end the window waits for any key. Set `APD_NO_PAUSE=1` to skip that final pause (the pre-rename `ARIA2DL_NO_PAUSE` is still honoured).

![URL dialog](docs/dialog-en.png)

The dialog also carries the options that are worth changing per run:

| Field | Becomes | Kept by “Save as default config” |
| --- | --- | --- |
| Directory | `-d <dir>` | yes |
| Parallel downloads | `--jobs N` | yes |
| Retries | `--retry N` | yes |
| Proxy address / mode | `--proxy`, `--proxy-mode` | yes |
| Preserve server timestamp | `--mtime` / `--no-mtime` | yes |
| Retry through the proxy when direct fails | `--fallback` / `--no-fallback` | yes |

“Save as default config” is off by default. Tick it and these values — including the directory — are written back into `AutoProxyDownloader.json` (other keys in the file are preserved); leave it off and the dialog's values are only passed to this run as command-line flags. Fields you did not touch are not turned into flags at all, so anything already on the command line keeps working.

The URLs themselves are never stored: they live only in the running process, so the config file stays a description of *how* to download, not *what* was downloaded.

## Console output

Each task opens with its egress mode, so it is obvious why one URL is slow and another is not:

```
[1/2] Direct Downloading: https://example.com/a.zip
[2/2] Downloading with proxy (http://127.0.0.1:951): https://www.google.com/robots.txt
```

On an interactive console the line is rewritten in place while aria2 works:

```
[2] robots.txt  12.3KiB/45.1KiB 27% 8.1KiB/s ETA 4s
```

With `--jobs 3` several tasks share that one line (` | ` separated). When stdout is redirected (a log file, a pipe), the same information is printed in 10% steps instead:

```
  progress a.zip: 26% (2.1MiB/8.0MiB 1.0MiB/s)
```

`-q` suppresses all of it; `--json` keeps the machine-readable output free of the 10% lines.

## Language

Messages are English or Chinese depending on the Windows UI language (Chinese system → Chinese). Override it when you want something else:

```powershell
python -X utf8 AutoProxyDownloader.py --lang en https://example.com/a.zip
python -X utf8 AutoProxyDownloader.py --lang zh https://example.com/a.zip
$env:APD_LANG = "en"        # or "zh"; also affects the GUI dialog
```

Priority: `APD_LANG` > `--lang` > system UI language (other platforms fall back to the `locale` setting).

## How the proxy decision works

For each URL, in order:

1. `--proxy-mode never` → direct, `always` → proxy;
2. non-HTTP(S) schemes → direct;
3. loopback / private ranges → direct;
4. `always_proxy` / `always_direct` lists from the config;
5. `gfwlist`: first matching rule wins — `@@` rule → direct, normal rule → proxy, no match → direct.

`--explain` prints the decision and the rule that produced it:

```
https://www.google.com/webhp
  出网：代理 http://127.0.0.1:951
  理由：gfwlist 命中：||google.com
```

## How the timestamp is preserved

1. aria2 runs with `--remote-time=true` (`-R`), writing the origin's `Last-Modified` onto the file;
2. after completion the same egress mode (proxy or direct) is used for a `HEAD` request (`Range: bytes=0-0` GET if HEAD is refused); a difference of 2 s or more is corrected with `os.utime`.

Only mtime/atime can be set — Windows creation time (birth time) is not settable, and BitTorrent-style downloads have no server timestamp at all.

## HLS streams: the ffmpeg engine

aria2 would only save the playlist text of an `.m3u8`, so URLs whose path ends in `.m3u8` (or `.m3u`) are handed to ffmpeg, which follows the playlist and remuxes the segments without re-encoding:

```
ffmpeg -i <url> -c copy <name>.mp4
```

The output keeps the playlist's name: `.../hls/index.m3u8` becomes `index.mp4` (`-o` still wins).

- **The proxy is still gfwlist's call.** An HLS URL goes through the same per-URL decision as everything else (`--proxy-mode always/never` and `always_proxy` / `always_direct` still take precedence), and only a "proxy" verdict adds `-http_proxy <proxy>`. A stream on an unlisted domestic CDN is fetched directly; a blocked one tunnels. If the first attempt fails, the usual direct↔proxy retry applies here too.
- **No server timestamp.** A stream is a playlist plus dozens of segments, so there is no single `Last-Modified` to apply — the report says `skipped` instead of pretending.
- **Finding ffmpeg.** `ffmpeg` config key / `--ffmpeg` → program folder (`ffmpeg.exe`) → `PATH`. When none is found, the run prints where to download one (`https://www.gyan.dev/ffmpeg/builds/`, the `ffmpeg-release-essentials.7z` build) and marks the HLS items as failed, while aria2 items in the same batch still download normally.
- **Progress** comes from ffmpeg's `-progress pipe:1` stream: bytes written plus the `speed=` multiplier. HLS has no known total, so there is no percentage.

## Retry and host learning

When the first egress mode fails with a *network-class* error (timeout, connection reset, DNS failure, incomplete segments, bad response header), the other mode is tried once. Errors that switching cannot fix — `404`, `403`, auth failures, disk full — fail fast instead of burning a second attempt.

The successful flip is recorded per host in `hosts-learned.json` (`"mode": "proxy" | "direct"`), and later runs consult that whenever `gfwlist` has no opinion. Only successful flips are written, so one blip does not pin the host forever.

`--first-tries` (default 2) bounds how many attempts the first mode gets before switching, so an unlisted blocked host costs ~30–40 s instead of minutes.

## Configuration

`AutoProxyDownloader.json` sits next to the scripts and is committed (it holds no secrets — a localhost proxy port and `%USERPROFILE%`-relative paths). Environment variables and `~` are expanded, so the file stays portable across machines. Priority: built-in defaults < `AutoProxyDownloader.json` < command-line flags.

| Key | Default | Meaning |
| --- | --- | --- |
| `aria2` | `aria2c.exe` | aria2c path; empty = auto-detect (script dir, then `PATH`) |
| `ffmpeg` | `ffmpeg.exe` | ffmpeg path, used for HLS only; empty = auto-detect (script dir, then `PATH`) |
| `dir` | `%USERPROFILE%\Downloads` | output directory |
| `proxy` | `http://127.0.0.1:951` | HTTP proxy used for matched URLs |
| `proxy_mode` | `auto` | `auto` / `always` / `never` |
| `always_proxy` / `always_direct` | `[]` | host lists that override the `gfwlist` verdict |
| `gfwlist_enabled` | `true` | set `false` to ignore the list entirely |
| `gfwlist` | `gfwlist.txt` | cache file; relative paths resolve against the script directory |
| `gfwlist_url` | gfwlist master URL | upstream source |
| `gfwlist_max_age` | `24` | hours before the cache is considered stale |
| `refresh_gfwlist` | `false` | force a refresh on every run |
| `jobs` | `3` | concurrent downloads (one aria2c process each) |
| `split` / `connections` / `min_split_size` | `5` / `5` / `1M` | aria2 segmentation |
| `mtime` | `true` | preserve the server timestamp |
| `overwrite` | `false` | overwrite existing files instead of skipping |
| `fallback` | `true` | switch egress mode once after a network-class failure |
| `retry` / `first_tries` | `5` / `2` | aria2 attempts for later rounds / the first round |
| `learn` / `learn_path` | `true` / `hosts-learned.json` | host learning and its record file |
| `timeout` / `connect_timeout` | `60` / `20` | seconds |
| `file_allocation` | `none` | aria2 allocation mode |
| `user_agent`, `referer`, `headers`, `extra_aria2_args` | see file | request headers and raw aria2 passthrough |

## CLI options

Run `python AutoProxyDownloader.py --help` for the complete list. The frequently used ones:

| Flag | Description |
| --- | --- |
| `-d, --dir` | output directory (overrides the config) |
| `-o, --out` | explicit filename, single URL only |
| `--proxy`, `--proxy-mode` | proxy address; `auto` / `always` / `never` |
| `--gfwlist`, `--gfwlist-url`, `--gfwlist-max-age` | cache file, source, TTL |
| `--refresh-gfwlist`, `--no-gfwlist` | force refresh / ignore the list |
| `--jobs`, `--split`, `--connections` | concurrency and segmentation |
| `--no-mtime` / `--mtime`, `--overwrite`, `--no-fallback` / `--fallback` | timestamp / existing files / retry behaviour |
| `--first-tries`, `--no-learn`, `--learn-path`, `--forget-learned` | switching and host learning |
| `--header`, `--referer`, `--extra-arg` | extra request headers, Referer, raw aria2c flags |
| `--aria2`, `--ffmpeg` | explicit paths to the two engines |
| `--lang auto\|zh\|en` | output language (default: follow the system) |
| `--dry-run`, `--explain`, `--json` | inspect decisions and output |
| `--selftest` | offline rule-engine self-test |

## Repository layout

```
AutoProxyDownloader.py                 CLI downloader (single file, standard library only)
AutoProxyDownloader_gui.py             GUI launcher (URL dialog -> console download -> key pause)
AutoProxyDownloader.json               configuration (committed, portable defaults)
AutoProxyDownloader.ico                application icon (16-256 px, used by the shortcut)
icon.png                               icon source for this README
docs/dialog-en.png, dialog-zh.png      dialog screenshots used by the two READMEs
AutoProxyDownloader.lnk                Windows shortcut to the GUI launcher (gitignored, machine-specific)
.gitignore
README.md                              this file
README.zh-CN.md                        中文说明
gfwlist.txt                            rule cache (gitignored, auto-fetched through the proxy)
hosts-learned.json                     learned host preferences (gitignored, created at runtime)
aria2c.exe                             aria2 binary (gitignored)
ffmpeg.exe                             ffmpeg binary, HLS only (gitignored)
```

To recreate the shortcut on another machine:

```powershell
$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut("$PWD\AutoProxyDownloader.lnk")
$lnk.TargetPath = (Get-Command python).Source
$lnk.Arguments = '"' + "$PWD\AutoProxyDownloader_gui.py" + '"'
$lnk.WorkingDirectory = "$PWD"
$lnk.IconLocation = "$PWD\AutoProxyDownloader.ico,0"
$lnk.Save()
```

## Troubleshooting

| Symptom | What to do |
| --- | --- |
| "proxy unreachable" warning | start the proxy, or point `--proxy` at the right port |
| all downloads time out | `--explain` to see the verdict; try `--proxy-mode always` |
| timestamp is "now" | the server sent no `Last-Modified` — expected for dynamic content |
| HLS item failed with "ffmpeg is missing" | put `ffmpeg.exe` next to the scripts, or point `--ffmpeg` at it: <https://www.gyan.dev/ffmpeg/builds/> |
| want the raw aria2 log | add `-v` |
| GUI shows an empty console | the console opens before the dialog; it is where progress is printed |

## Known limitations

- The proxy decision is made on the initial URL only; a direct connection that 302-redirects to a blocked host relies on the retry/learning path.
- `Content-Disposition` filenames are ignored — the name comes from the URL path (use `-o` when it matters).
- Directory-style URLs (`https://host/`) become `host.html`, so several sites do not collide on `index.html`.
- With `--jobs` > 1 the per-host learning only helps across runs, not within one batch (tasks start together).
- HLS is remuxed, not re-encoded: a stream mp4 cannot carry (rare codec combinations) fails at the mux stage, and the last ffmpeg error line is what the report shows.
- HLS is recognised by the URL path only. A playlist served from a path without `.m3u8` / `.m3u` is fetched by aria2 as a plain file (use `-o … .mp4` plus a manual ffmpeg run for those).
- The GUI launcher, the "press any key" pause and the shortcut are Windows-specific; the downloader core is not.

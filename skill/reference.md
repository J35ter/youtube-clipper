# Reference — measurements, failure modes, internals

## Measured on the reference video (`jgDUzzVzuWo`, 1:27:18, 84.8 MB audio)

| Path | Result |
|---|---|
| `yt-dlp` streaming download (default client) | **31 KiB/s**, steady, independent of `-N 4` → ~45 min for this album |
| 3 concurrent yt-dlp section downloads | 36 s for 3 min of audio → throttle is **per connection**, not per IP |
| 6 concurrent | 34 s for 6 min → ≈4.6×, concurrency stops paying past ~6 |
| Direct CDN URL + HTTP `Range` (what `ytrip` does) | 4 MB in **<1 s** → whole 84.8 MB in seconds |
| `--extractor-args youtube:player_client=tv` | fails: *"The page needs to be reloaded"* (PO-token wall) |
| ffmpeg cut + re-encode, 28 tracks | **13 s** wall clock with 4 workers |
| Re-run with everything present | **1.9 s**, no network beyond one metadata call |

The takeaway that shaped the tool: the bytes are the same either way, so the only lever is
*how many connections* fetch them. Range requests on the googlevideo URL are not throttled the
way the streaming client is.

## Why not `--download-sections` per track

Same total bytes, 28 connections, 28 chances to hit a bot check, and
`--force-keyframes-at-cuts` has to rewind to the previous keyframe, which smears the first
second or two of every clip. One master plus local `-ss`/`-to` is sample-accurate (verified:
`-ss 2 -to 20` yields exactly `18.000000` s) and makes every later correction free.

## Failure modes and what to do

| Symptom | Cause | Fix |
|---|---|---|
| Log says `range download failed ... falling back to yt-dlp streaming` | direct URL expired, `Range` unsupported, or a part came up short | Re-run; the URL is re-resolved each run. If it persists, `--parallel 1`, then investigate the message |
| `The page needs to be reloaded` / `Sign in to confirm you're not a bot` | YouTube PO-token / bot check on this IP | `--ytdlp` a newer yt-dlp nightly, or pass cookies: `yt-dlp --cookies-from-browser firefox` style support is on the yt-dlp side, so export `cookies.txt` and add `--cookies` via a wrapper |
| Age-restricted or members-only video | needs login | Export cookies and point `--ytdlp` at a wrapper script that injects `--cookies /path/cookies.txt` |
| Playlist URL pasted instead of a video URL | `--no-playlist` is always passed, so you get the single video | Fine; if they want the whole playlist, run once per video id |
| Every MP3 ~40 ms long | `-frames:v 1` while attaching cover art | Already handled; the duration guard now rejects such output loudly |
| Cuts sound clipped at the start | source has no keyframe there — only affects copy-mode, we re-encode | Not applicable; re-encode is sample-accurate |
| `no yt-dlp found and no uv to bootstrap it` | neither on PATH | Install either, or `--ytdlp /path/to/yt-dlp` |

## Internals worth knowing

- **Format choice**: `bestaudio[ext=m4a]/bestaudio/best`. For this channel that is format 140,
  128 kbit/s AAC. YouTube also offers 251 (Opus ~117 kbit/s). Encoding to 192 k CBR MP3 from a
  128 k source is already transparent; 320 k buys nothing.
- **Range assembly**: N byte ranges are fetched to `*.parts/`, concatenated in order, and the
  total size is compared against `Content-Range`'s length before the file is trusted.
- **Chapter cross-check**: YouTube auto-generates chapters from the video description, so they
  are an independent second opinion on a hand-pasted list. Chapter titles are the *whole*
  description line (`Title - Artist - Year`), so both sides are reduced to a normalised
  title-only key before comparing; artist and year are stripped, unicode folded, punctuation
  dropped.
- **Validation is deliberately conservative**: only an internally impossible list (out of order,
  past the end) is an `ERROR` that blocks a rip. Everything else is a `WARNING`, because the
  user's list is allowed to disagree with YouTube.
- **Idempotency**: a track whose MP3 already exists is skipped unless `--force`. `_master/` is
  reused whenever it probes as readable audio, so a half-finished album resumes.

## Options

```
--out DIR            album directory          (default ~/Music/<video-slug>)
--name SLUG          override just the directory name
--bitrate 192k       LAME constant bitrate
--quality SELECTOR   yt-dlp audio format       (default bestaudio[ext=m4a]/bestaudio/best)
--parallel N         parallel byte-range workers (default 8)
--genre JAZZ         ID3 genre
--no-cover           skip cover art
--no-master          delete _master/ when finished
--master PATH        use an existing audio file, skip the download entirely
--ytdlp PATH         pin a yt-dlp executable
--cache DIR          uv cache dir for bootstrapping yt-dlp
--audio-only         no track list: one MP3 of the whole audio
--accept-anomalies   rip anyway when the list has errors
--dry-run            validate and print the plan, touch nothing
--force              re-cut files that exist
--json               machine-readable summary on stdout
```

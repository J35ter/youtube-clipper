---
name: youtube-clipper
description: Rip YouTube audio into one tagged MP3 per track. Use when the user pastes a YouTube link together with a list of HH:MM:SS timestamps or a tracklist (compilations, "best of" mixes, radio rips, lecture chapters) and wants the individual songs or clips saved as MP3 files on disk. Also handles plain "get me the audio of this video" requests with no timestamp list.
whenToUse: The user gives a YouTube URL and wants audio out of it — either the whole thing as one file, or split into named tracks by timestamp.
metadata:
  short-description: YouTube link + timestamps → tagged MP3s
---

# YouTube clipper

`scripts/ytrip.py` turns **one YouTube URL + one timestamp list** into a directory of numbered,
ID3-tagged MP3s, a playlist, and an audit manifest. It downloads the audio **once** and cuts
locally, so re-cutting costs seconds instead of another download.

## The one command

```bash
python3 scripts/ytrip.py "<youtube-url>" tracks.txt --dry-run   # always start here
python3 scripts/ytrip.py "<youtube-url>" tracks.txt             # then the real rip
```

Default output is `~/Music/<video-title-slug>/`. Override with `--out DIR` or `--name SLUG`.
For a plain "just give me the audio" request with no timestamp list, omit the list and pass
`--audio-only` — one MP3 of the whole thing, same tagging.

## Workflow — follow this order

1. **Save the user's list verbatim.** Write the pasted lines to a file exactly as given,
   including their spelling. Never silently "correct" a timestamp or a name: the whole point of
   step 2 is to catch upstream mistakes, and silent edits destroy the evidence.
2. **Run `--dry-run` first.** It costs one metadata request and prints a `validation` block.
3. **If it prints `ERROR` lines, stop and ask.** Errors mean the list is internally impossible
   (timestamps out of order, or past the end of the video). Report what you found, propose the
   most likely fix, and only then write a *corrected copy* of the list
   (`something.tracks.fixed.txt`) — keep the original file untouched so the change is auditable.
   `--accept-anomalies` exists, but prefer fixing the list.
4. **`WARNING` lines are informational** — mention them in one sentence and proceed. The most
   useful one is `our HH:MM:SS vs YouTube's own chapter at HH:MM:SS`: YouTube auto-generates
   chapters from the video description, so its chapter list is an independent second opinion on
   the user's timestamps. A chapter YouTube has but the list does not usually means a track is
   missing from the list; a track the list has but YouTube dropped usually means that line's
   timestamp was out of order upstream.
5. **Run the real rip.** Download is usually under a minute (see *Speed*), so run it in the
   foreground; if a video is hours long, background it.
6. **Report what landed**: the album directory, the track count, and any warnings.

## Re-ripping is cheap, so do it

The downloaded audio is kept at `<album>/_master/<video-id>.m4a`. If a timestamp turns out to be
wrong, fix the list and re-run with `--force` — the master is reused and 28 cuts take ~13 s.
Deleting `_master/` forces a fresh download.

## Speed — do not fight the throttle the wrong way

yt-dlp's normal streaming path is throttled by Google to **~31 KiB/s per connection**, which
makes an 87-minute album take ~45 minutes. `ytrip` sidesteps it: it asks yt-dlp for the direct
CDN URL (`-g`) and fetches that as **parallel HTTP byte ranges**, which run at multi-MB/s — the
same 85 MB lands in seconds. Consequences:

- Do **not** reach for `--download-sections` per track. It is the same total bytes, 28 separate
  connections, and `--force-keyframes-at-cuts` smears the start of every clip.
- Do **not** try `--extractor-args youtube:player_client=tv` as a speed trick; on this host it
  fails outright with a PO-token error ("The page needs to be reloaded").
- If a download ever falls back to "yt-dlp streaming" in the log, the range path broke — check
  `reference.md` before assuming it is just slow.

## Requirements

`ffmpeg` (with `libmp3lame`) and `yt-dlp`. There is no pip on this host, so if `yt-dlp` is not
on PATH the script bootstraps it with `uv run --no-project --with yt-dlp` and caches under
`~/.cache/ytrip/uv`. Pass `--ytdlp /path/to/yt-dlp` to pin a specific build.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | usage / missing dependency / download failed |
| 3 | timestamp list has errors — fix it or pass `--accept-anomalies` |
| 4 | some tracks failed to cut (see `info.md`) |
| 5 | network error |

## Gotchas already paid for

- `-frames:v 1` when attaching cover art makes ffmpeg stop the muxer after the first video
  frame: every MP3 comes out ~40 ms long with exit code 0. The script omits it and verifies each
  output's duration against the expected length, so a silent truncation fails loudly instead.
- YouTube's best thumbnail is WebP; it is transcoded to JPEG before embedding, because plenty of
  hardware players ignore `image/webp` APIC frames.
- Container metadata from the source m4a leaks into MP3 tags unless `-map_metadata -1` comes
  before the `-metadata` arguments.
- YouTube chapter titles are the whole description line (`Title - Artist - Year`), so matching
  them to track titles requires stripping artist and year first.

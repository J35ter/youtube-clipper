# youtube-clipper

Paste a YouTube link and a list of timestamps, get one cleanly-cut, ID3-tagged MP3 per track.

Built for the "compilation" video: one long upload that is really 28 separate songs, with the
tracklist sitting in the description as `00:00:00 Title - Artist - Year` lines.

```
$ ytrip.py "https://www.youtube.com/watch?v=jgDUzzVzuWo" tracks.txt

parsing: 28 tracks
  jgDUzzVzuWo :: '1930s GENUINE Music | Authentic 1930s Recordings | No AI' :: 01:27:18 :: Classical Tunes :: 27 chapters

validation
  WARNING #16 'Plain Dirt': our 00:44:28 vs YouTube's own chapter at 00:47:28 — off by 180 s
  WARNING #17 'Pleasin’ Paul': no matching YouTube chapter — YouTube usually drops a line whose timestamp is out of order

downloading audio ...
  stream 84.8 MB in 8 parallel ranges ...
  master ready (84.8 MB, via ranges(8x parallel))
cutting 28 tracks ...
done in 13.2s: 28 new, 0 existing, 0 failed -> ~/Music/1930s-genuine-music-authentic-1930s-recordings-no-ai
```

```
01 - Don Redman - Doin' What I Please (1932).mp3     2:53   4.29 MB
02 - Louis Prima - Chasin' Shadows (1935).mp3        3:00   4.46 MB
…
28 - Fletcher Henderson - Sugar Foot Stomp (1929).mp3 3:05   4.59 MB
playlist.m3u8 · manifest.tsv · info.md · _master/jgDUzzVzuWo.m4a
```

## Why it is built this way

Two findings drove the design, both measured rather than assumed:

1. **yt-dlp's streaming path is throttled to ~31 KiB/s per connection** by Google. An 87-minute
   album takes ~45 minutes that way. The throttle is per *connection*, and direct CDN byte-range
   requests are not throttled at all — so `ytrip` asks yt-dlp for the direct URL (`-g`) and
   fetches it in parallel HTTP ranges. The same 84.8 MB lands in seconds.
2. **A 45-minute fetch is too expensive to run against a typo.** So the timestamp list is
   validated *before* any bytes move, and cross-checked against YouTube's own auto-generated
   chapters, which are an independent second opinion derived from the video description. In the
   example above the tool caught that the pasted list had two lines out of order — the same
   mistake that made YouTube itself silently drop one track from its chapter list.

It downloads the audio **once** and cuts locally. Cutting is sample-accurate and costs seconds,
so a wrong timestamp never costs another download: fix the list, re-run with `--force`, 28 tracks
re-cut in 13 seconds.

## Usage

```bash
# always validate first — one metadata call, writes nothing
python3 skill/scripts/ytrip.py "<url>" tracks.txt --dry-run

# then rip
python3 skill/scripts/ytrip.py "<url>" tracks.txt

# whole audio, no track list
python3 skill/scripts/ytrip.py "<url>" --audio-only
```

Track lines look like `HH:MM:SS Title - Artist - Year` (the description format YouTube itself
uses). `MM:SS` works too, artist and year are optional, `#` comments are ignored, and `-` reads
the list from stdin.

Output goes to `~/Music/<video-title-slug>/` unless you pass `--out DIR` or `--name SLUG`.
Each MP3 carries title, artist, album, album-artist, year, track number, genre, the source URL
and timestamp in the comment field, and the video thumbnail as JPEG cover art.

Full option list: `--help`, or see [`skill/reference.md`](skill/reference.md).

## Layout

```
skill/
  SKILL.md          the DSH skill (agent-facing workflow)
  scripts/ytrip.py  the tool — stdlib only, plus ffmpeg and yt-dlp
  reference.md      measurements, failure modes, internals
examples/
  jgDUzzVzuWo.tracks.txt        the original pasted list, verbatim
  jgDUzzVzuWo.tracks.fixed.txt  the corrected copy actually used
install.sh          installs the skill into ~/.dsh/skills
PLAN.md             the design proposal, written before implementing
```

## Requirements

- `ffmpeg` with `libmp3lame`
- `yt-dlp` on PATH — or `uv`, in which case the script bootstraps yt-dlp on demand
  (`uv run --no-project --with yt-dlp`) and caches it under `~/.cache/ytrip/uv`
- Python 3.10+ (stdlib only)

## Installing the skill

```bash
./install.sh          # symlink into ~/.dsh/skills/youtube-clipper
./install.sh --copy   # or a stable copy
./install.sh --dest <project>/.dsh/skills
```

DSH discovers skills from `<project>/.dsh/skills`, `<project>/.agents/skills`, `~/.dsh/skills`
and `~/.agents/skills`. Once installed, pasting a link plus a tracklist into a session is enough.

## Licence

MIT — see [LICENSE](LICENSE).

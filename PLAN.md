# YouTube → MP3 clipper — design & instructions

Goal: paste a YouTube link + a list of `HH:MM:SS Title - Artist - Year` lines, and get one
cleanly-cut MP3 per track in a project subdirectory, tagged and playable as an album.

Status: **implemented and verified** — see §7 for results. The rest of this file is the design
proposal that was written before implementing, kept as the audit trail.

---

## 1. Verified environment facts

| Check | Result |
|---|---|
| `ffmpeg` | 8.0.1, has `libmp3lame` ✅ |
| `yt-dlp` | not installed, but runs via `uv run --no-project --with yt-dlp` → **2026.08.19** ✅ |
| `pip` / `pipx` | absent (`No module named pip`) — `uv` at `~/.local/bin/uv` is the only package path |
| YouTube reachable | yes, **no bot check / no cookies needed** for this video ✅ |
| Target video | `jgDUzzVzuWo` — "1930s GENUINE Music \| Authentic 1930s Recordings \| No AI", **1:27:18** (5238 s), 128 kbit/s AAC audio (format 140) |
| Cut accuracy | `-ss … -to … -i` + re-encode produced **exactly 18.000000 s** ✅ |
| ID3 tagging | `title` / `artist` / `date` / `comment` written, ID3v2.3 ✅ (container junk leaks unless `-map_metadata -1`) |
| Disk | 1.2 TB free on `/home` ✅ |
| File sandbox | only the session workspace is writable; `~/.cache/uv` and `/tmp` are **not** usable → cache + output must live in the workspace |
| **Download speed** | **~31 KiB/s per connection** (Google throttle). Throttle is **per-connection, not per-IP** — measured: 3 concurrent = 36 s for 3 min of audio (3× speedup), 6 concurrent = 34 s for 6 min (≈4.6×, saturating). Sequential full master ≈ 45 min; **6-way parallel ≈ 10 min** |
| `--extractor-args youtube:player_client=tv` | ❌ fails on this video ("The page needs to be reloaded" = PO-token wall), so the usual unthrottled-client trick is out |

The speed number is the one real constraint, and it drives the whole design: fetch the audio
**once, in parallel ranges**, then cut locally (cutting is free; bytes are expensive).

---

## 2. Recommended pipeline

```
link + pasted times
      │
      ├─ A. parse + validate the time list          (pure text, no network)
      ├─ B. yt-dlp metadata: title, duration, chapters
      │       └─ cross-check my list vs YouTube's own chapters
      ├─ C. yt-dlp: download bestaudio ONCE  → _master/<video-id>.m4a
      ├─ D. ffmpeg: cut N segments, re-encode → NN - Artist - Title (Year).mp3 + ID3
      └─ E. write playlist.m3u8, manifest.tsv, info.md
```

**Why one download instead of 28 section downloads:** the byte total is identical either way
(all audio must arrive once), but 28 separate fetches mean 28 handshakes, 28 chances to trip a
bot check, and `--force-keyframes-at-cuts` smearing the first ~2 s of every track. One master +
local `ffmpeg` seeking is sample-accurate and offline-retryable: if a cut is wrong, re-cut in
seconds without touching the network.

**Speed plan (measured, not guessed):** the throttle is per-connection, so the master is
fetched as **N parallel segment ranges** (default N=6) instead of one stream — ~45 min →
**~10 min** for this album. Concurrency past 6 stopped paying off (4.6× at 6-way).
`player_client=tv` is not an option here (PO-token wall). If you ever want it in a hurry,
`bestaudio[abr<=64]` halves the bytes and 60 kbit/s Opus is still honest for 1930s acoustic
transfers.

---

## 3. The interesting part: validation before ripping

A 45-minute fetch is too expensive to run against a typo, so the tool refuses to start on a
suspicious list. Your list already contains two problems:

| # | Line | Issue |
|---|---|---|
| 15 | `00:41:09 My Sweet - Louis Armstrong` | next start is 6:19 later — every other gap is 2:25–3:30 |
| 16 | `00:47:28 Plain Dirt - McKinney's Cotton Pickers` | **out of order**: 22 s *after* line 17 |
| 17 | `00:47:06 Pleasin' Paul - Henry Allen` | YouTube's own chapter list **drops this track entirely** |
| 27 | `01:18:44 Rhymes - Leslie Sarony` | 5:28 long (next-longest is 3:26) — may hide a missing track |

Evidence: YouTube auto-generated 27 chapters from this video's description, and it kept
`Plain Dirt` at 47:28 while silently discarding `Pleasin' Paul`. The most likely truth is that
`Plain Dirt` belongs at **00:44:28** (which makes gaps 3:19 and 2:38 — right in pattern), and
`Pleasin' Paul` at 47:06. That yields your intended 28 tracks.

Rules the tool will enforce:
- starts strictly increasing; each start < video duration; last track ends at video duration
- flag gaps outside 60 s…8 min
- if YouTube has chapters, diff them against your list and report additions/removals/shifts
- **never silently fix** — print the diff, require `--accept-anomalies` (or a corrected list)

Also: your list has small name typos (`Ivie Andreson`→Ivie Anderson, `Mildred Baily`→Mildred
Bailey, `Jimmie Rogers`→Jimmie Rodgers, `Etherl Merman`→Ethel Merman). Ask whether to keep the
list verbatim or normalize.

---

## 4. Packaging — three options

| Option | What it is | Pros | Cons |
|---|---|---|---|
| **A. DSH skill** ⭐ | `~/.dsh/skills/youtube-clipper/SKILL.md` + `scripts/ytrip.py` | Auto-discovered by DSH (verified roots: `<project>/.dsh/skills`, `~/.dsh/skills`, `~/.agents/skills`). Works in *every* future session: paste link + times, done. Cheap to maintain. | Needs one approval to write outside the workspace; skill list refreshes on change |
| **B. Standalone CLI** ⭐ | same `ytrip.py`, run by hand: `./ytrip <url> tracks.txt` | No agent needed; usable from cron/terminal; the skill just wraps it | You must remember the flags |
| **C. DSH plugin** | cordis plugin registering a `/clip` command + tool in this GUI | Nicest UX (a command + a button) | Heaviest: plugin scaffolding, rebuild + page refresh, more to break |

**Recommendation: A + B together** — one script, two entry points. Add C later only if you
actually want the slash command.

Layout (project subdir, already created):

```
youtube-clipper/
├── PLAN.md                  ← this file
├── README.md                ← usage, written after implementation
├── examples/
│   └── jgDUzzVzuWo.tracks.txt   ← your pasted list, saved verbatim
├── scripts/
│   └── ytrip.py             ← the worker (stdlib + yt-dlp + ffmpeg)
└── <album-slug>/            ← one subdir per ripped video
    ├── _master/jgDUzzVzuWo.m4a
    ├── 01 - Don Redman - Doin' What I Please (1932).mp3
    ├── … 28 files …
    ├── playlist.m3u8
    ├── manifest.tsv
    └── info.md
```

Output naming: `<album-slug>` derived from the video title (or your `--name`). Where the album
goes is your call — inside the workspace is sandbox-clean; `~/Music/…` is outside and needs a
wider sandbox grant per run.

---

## 5. Output details (defaults I propose)

- **Format:** MP3, LAME `-q:a 2` (V0, ~245 kbit/s VBR). The master is 128 kbit/s AAC, so V0 is
  already transparent; 320 kbit CBR would just double the bytes.
- **Cuts:** `-ss START -to END` before `-i` (fast + accurate), re-encode, `-map_metadata -1`
  then write our own tags.
- **Tags (ID3v2.3):** `TIT2` title, `TPE1` artist, `TDRC` year, `TALB` = video title,
  `TCON` = Jazz, `TPE2`/album-artist = "Classical Tunes", comment = `source URL @ timestamp`,
  track number `NN/28`. Optional cover art from the video thumbnail.
- **Sidecars:** `playlist.m3u8` (play in order), `manifest.tsv` (index, start, end, title,
  artist, year, filename, duration, bytes — the audit trail), `info.md` (source, title,
  duration, rip date, tool versions).
- **Master file:** keep by default (~84 MB) so re-cuts are free; `--no-master` to delete.
- **Idempotency:** skip a track whose MP3 already exists and matches the manifest; `--force` to
  redo. Interrupted runs resume instead of re-downloading.

---

## 6. Open questions — answered

| # | Question | Decision |
|---|---|---|
| 1 | Output location | `~/Music/<album>/` |
| 2 | Filename order | `NN - Artist - Title (Year).mp3` |
| 3 | MP3 quality | **192 kbit CBR** |
| 4 | Keep the master | kept in `_master/` (default) |
| 5 | `Plain Dirt` timestamp | **moved to 00:44:28**, `Pleasin' Paul` kept → 28 tracks |
| 6 | Name typos | left verbatim (only filesystem-unsafe characters are sanitised) |
| 7 | Packaging | **skill only** (agent-driven); the script is still runnable by hand |
| 8 | Skill location | `~/.dsh/skills/youtube-clipper` |

---

## 7. Results (implemented and verified)

The parallel-range idea beat the estimate badly — in the good way:

| Step | Predicted | Actual |
|---|---|---|
| Download 84.8 MB audio | ~45 min sequential | **seconds** (8 parallel byte ranges on the direct CDN URL) |
| Cut 28 MP3s | ~1–2 min | **13.2 s** |
| Re-run with everything present | — | **1.9 s**, no re-download |

Output: 28 MP3s, 87.3 min of audio (matches the 87:18 video), 129.5 MB, every duration verified
against the plan, ID3v2.3 tags + JPEG cover on each file, plus `playlist.m3u8`, `manifest.tsv`
and `info.md`.

Two bugs the verification pass caught, both silent-success failures worth remembering:

1. `-frames:v 1` while attaching cover art made ffmpeg stop the muxer after the first video
   frame — all 28 files came out **0.057 s** long with exit code 0. Fixed by dropping the flag,
   and a post-cut duration guard now rejects any output that is off by more than
   `max(2 s, 2%)` so this class of failure can never pass quietly again.
2. YouTube's best thumbnail is **WebP**, so the embedded APIC was `image/webp`; it is now
   transcoded to JPEG, which every player understands.

The validation layer earned its keep on the very first run: with the corrected list it reported
exactly the two known upstream anomalies (the 180 s `Plain Dirt` drift versus YouTube's own
chapter, and `Pleasin' Paul` having no chapter because YouTube dropped it) and nothing else; with
the original list it hard-failed with exit 3 on the out-of-order pair.

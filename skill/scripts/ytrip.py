#!/usr/bin/env python3
"""ytrip — rip a YouTube compilation into one tagged MP3 per track.

Feed it a video URL plus a list of "HH:MM:SS Title - Artist - Year" lines (the format
YouTube description timestamps use) and it downloads the audio once, cuts it at the
timestamps, and writes numbered, ID3-tagged MP3s plus a playlist and an audit manifest.

Design notes (measured on the reference video, not guessed):
  * yt-dlp's streaming path is throttled to ~31 KiB/s per connection by Google, which makes
    an 87-minute album take ~45 minutes. The same audio fetched as HTTP byte ranges from the
    googlevideo CDN runs at multi-MB/s. So: ask yt-dlp for the direct URL (`-g`), then fetch
    it in parallel ranges. ~85 MB lands in well under a minute.
  * Cutting is local and sample-accurate (`-ss`/`-to` before `-i`, then re-encode), so a bad
    timestamp costs seconds to redo, never another download.
  * A 45-minute fetch is too expensive to run against a typo, so the timestamp list is
    validated (and cross-checked against YouTube's own auto-chapters) before any bytes move.

Stdlib only; requires ffmpeg and yt-dlp (see --ytdlp / UV bootstrapping in SKILL.md).
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
TS_RE = re.compile(r"^\s*(?:(\d{1,2}):)?(\d{1,2}):(\d{2})(?:[.,](\d{1,3}))?\s+(.*\S)\s*$")
SPLIT_RE = re.compile(r"\s+[-–—]\s+")
YEAR_RE = re.compile(r"^(?:c\.?\s*)?(\d{4})[A-Za-z]?$", re.I)
UNSAFE_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


# --------------------------------------------------------------------------- helpers
def die(msg: str, code: int = 1) -> None:
    print(f"ytrip: error: {msg}", file=sys.stderr)
    raise SystemExit(code)


def run(cmd: list[str], *, timeout: float | None = None, binary: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, timeout=timeout,
        text=not binary, encoding=None if binary else "utf-8", errors="replace",
    )


def ffprobe_duration(path: Path) -> float | None:
    cp = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
              "-of", "default=nw=1:nk=1", str(path)], timeout=120)
    try:
        return float(cp.stdout.strip())
    except (ValueError, TypeError):
        return None


def hms(seconds: float) -> str:
    sign = "-" if seconds < 0 else ""
    s = int(round(abs(seconds)))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{sign}{h:02d}:{m:02d}:{s:02d}"


def slugify(text: str, maxlen: int = 60) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    if len(text) > maxlen:
        text = text[:maxlen].rsplit("-", 1)[0]
    return text or "youtube-album"


def safe_name(text: str, maxlen: int = 120) -> str:
    """Filesystem-safe display name. Keeps unicode and punctuation that players accept."""
    text = unicodedata.normalize("NFC", text).strip()
    text = UNSAFE_RE.sub(" - ", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    if len(text) > maxlen:
        text = text[:maxlen].rstrip(" .")
    return text or "untitled"


def norm_title(text: str) -> str:
    """Aggressive key for matching our titles against YouTube chapter titles."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "", text)


def title_only(text: str) -> str:
    """Strip ' - Artist - Year' so our titles and YouTube chapter titles share a key.

    YouTube builds chapter names out of the whole description line, so the raw strings never
    match directly.
    """
    parts = SPLIT_RE.split(text)
    if len(parts) >= 2 and YEAR_RE.match(parts[-1].strip()):
        parts = parts[:-1]
    return parts[0].strip()


# --------------------------------------------------------------------------- yt-dlp
class Ytdlp:
    def __init__(self, exe: str | None, cache: Path):
        self.cache = cache
        if exe:
            self.base = [exe]
        else:
            if shutil.which("yt-dlp"):
                self.base = ["yt-dlp"]
            elif shutil.which("uv"):
                # No pip on this box; uv materialises yt-dlp on demand and caches it.
                self.base = ["uv", "run", "--no-project", "--with", "yt-dlp", "--", "yt-dlp"]
            else:
                die("no yt-dlp found and no uv to bootstrap it (install either, or pass --ytdlp)")
        self.cache.mkdir(parents=True, exist_ok=True)

    def env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.setdefault("UV_CACHE_DIR", str(self.cache))
        return env

    def call(self, args: list[str], timeout: float = 600) -> subprocess.CompletedProcess:
        env = self.env()
        return subprocess.run(self.base + args, capture_output=True, text=True,
                              timeout=timeout, env=env, errors="replace")

    def meta(self, url: str) -> dict:
        cp = self.call(["--no-warnings", "--no-playlist", "--skip-download",
                        "--user-agent", UA, "-j", url])
        if cp.returncode != 0:
            die(f"yt-dlp metadata failed: {(cp.stderr or cp.stdout).strip()[-400:]}")
        try:
            return json.loads(cp.stdout)
        except json.JSONDecodeError as exc:
            die(f"yt-dlp returned unparsable JSON: {exc}")

    def direct_url(self, url: str, quality: str) -> str:
        cp = self.call(["--no-warnings", "--no-playlist", "--user-agent", UA,
                        "-f", quality, "-g", url])
        if cp.returncode != 0:
            raise RuntimeError((cp.stderr or cp.stdout).strip()[-400:])
        lines = [ln.strip() for ln in cp.stdout.splitlines() if ln.strip().startswith("http")]
        if not lines:
            raise RuntimeError("yt-dlp -g produced no URL")
        return lines[0]


# --------------------------------------------------------------------------- parsing
def parse_tracks(text: str) -> tuple[list[dict], list[str]]:
    tracks, rejected = [], []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = TS_RE.match(line)
        if not m:
            rejected.append(f"line {lineno}: not a timestamp line: {line[:70]}")
            continue
        h, mi, s, frac, rest = m.groups()
        start = int(h or 0) * 3600 + int(mi) * 60 + int(s) + int((frac or "0").ljust(3, "0")) / 1000
        parts = SPLIT_RE.split(rest)
        year = None
        if len(parts) >= 2 and YEAR_RE.match(parts[-1].strip()):
            year = YEAR_RE.match(parts[-1].strip()).group(1)
            parts = parts[:-1]
        if len(parts) >= 2:
            title, artist = parts[0].strip(), " - ".join(p.strip() for p in parts[1:])
        else:
            title, artist = parts[0].strip(), ""
        tracks.append({"start": start, "title": title, "artist": artist, "year": year,
                       "raw": line, "lineno": lineno})
    return tracks, rejected


def validate(tracks: list[dict], duration: float, chapters: list[dict]) -> tuple[list[str], list[str]]:
    """Return (errors, warnings). Errors block a rip unless --accept-anomalies."""
    errors, warnings = [], []

    for i, t in enumerate(tracks):
        if t["start"] >= duration:
            errors.append(f"#{i+1} {hms(t['start'])} {t['title'][:40]!r}: start is past end of video ({hms(duration)})")
        if i and t["start"] <= tracks[i - 1]["start"]:
            errors.append(
                f"#{i+1} {hms(t['start'])} {t['title'][:40]!r} is not after #{i} "
                f"{hms(tracks[i-1]['start'])} {tracks[i-1]['title'][:40]!r} "
                f"(out of order by {hms(tracks[i-1]['start'] - t['start'])})"
            )

    for i, t in enumerate(tracks):
        end = tracks[i + 1]["start"] if i + 1 < len(tracks) else duration
        dur = end - t["start"]
        if dur < 45:
            warnings.append(f"#{i+1} {t['title'][:40]!r} would be only {hms(dur)} long "
                            f"(next start {hms(end)}) — suspicious timestamp")
        elif dur > 480:
            warnings.append(f"#{i+1} {t['title'][:40]!r} would run {hms(dur)} — unusually long, "
                            f"a track may be missing from the list")

    if chapters:
        by_key: dict[str, dict] = {}
        for c in chapters:
            by_key.setdefault(norm_title(title_only(c.get("title", ""))), c)
        ours = [norm_title(title_only(t["title"])) for t in tracks]

        def lookup(key: str) -> dict | None:
            ch = by_key.get(key)
            if ch is not None:
                return ch
            # tolerate small upstream typos ("Ivie Andreson" vs "Ivie Anderson" style noise)
            best = None
            for k, cand in by_key.items():
                if not key or not k:
                    continue
                if key in k or k in key:
                    if best is None or abs(len(k) - len(key)) < abs(len(best[0]) - len(key)):
                        best = (k, cand)
            return best[1] if best else None

        matched = 0
        seen = set()
        for i, (t, key) in enumerate(zip(tracks, ours)):
            ch = lookup(key)
            if ch is None:
                t["_chapter"] = None
                continue
            matched += 1
            t["_chapter"] = ch
            seen.add(norm_title(title_only(ch.get("title", ""))))
            drift = abs(ch["start_time"] - t["start"])
            if drift > 2:
                warnings.append(f"#{i+1} {t['title'][:40]!r}: our {hms(t['start'])} vs YouTube's own "
                                f"chapter at {hms(ch['start_time'])} — off by {drift:.0f} s")
        if matched == 0:
            warnings.append(f"chapter cross-check unavailable: none of the {len(tracks)} titles match "
                            f"YouTube's {len(chapters)} auto-chapters (different naming, not an error)")
        elif matched < len(tracks):
            for i, t in enumerate(tracks):
                if t.get("_chapter") is None:
                    warnings.append(f"#{i+1} {t['title'][:40]!r}: no matching YouTube chapter — YouTube "
                                    f"usually drops a line whose timestamp is out of order")
        for c in chapters:
            if norm_title(title_only(c.get("title", ""))) not in seen:
                warnings.append(f"YouTube has a chapter we were not given: {hms(c['start_time'])} "
                                f"{title_only(c['title'])[:50]!r} — a track may be missing from the list")
    return errors, warnings


# --------------------------------------------------------------------------- download
def fetch_range(url: str, start: int, stop: int, dest: Path) -> int:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Range": f"bytes={start}-{stop}"})
    got = 0
    with urllib.request.urlopen(req, timeout=180) as resp:
        if resp.status not in (200, 206):
            raise RuntimeError(f"range {start}-{stop}: HTTP {resp.status}")
        if resp.status == 200 and stop - start > 0:
            raise RuntimeError("server ignored the Range header")
        with dest.open("wb") as fh:
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                fh.write(chunk)
                got += len(chunk)
    expect = stop - start + 1
    if got != expect:
        raise RuntimeError(f"range {start}-{stop}: got {got} of {expect} bytes")
    return got


def download_master(url: str, quality: str, ydl: Ytdlp, master: Path, parallel: int,
                    quiet: bool = False) -> tuple[Path, str]:
    """Fetch the whole audio stream once, as parallel byte ranges. Falls back to yt-dlp."""
    master.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    try:
        direct = ydl.direct_url(url, quality)
        req = urllib.request.Request(direct, headers={"User-Agent": UA, "Range": "bytes=0-0"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            cr = resp.headers.get("Content-Range", "")
            total = int(cr.rsplit("/", 1)[-1]) if "/" in cr else int(resp.headers["Content-Length"])
        if total < 4096:
            raise RuntimeError(f"suspicious stream size {total}")
        n = max(1, min(parallel, total // (1 << 20) + 1))
        step = (total + n - 1) // n
        ranges = [(i * step, min(total, (i + 1) * step) - 1) for i in range(n)]
        tmp = master.with_suffix(master.suffix + ".parts")
        tmp.mkdir(parents=True, exist_ok=True)
        for stale in tmp.glob("part-*"):
            stale.unlink()
        if not quiet:
            print(f"  stream {total/1e6:.1f} MB in {n} parallel ranges ...", flush=True)
        with cf.ThreadPoolExecutor(max_workers=n) as pool:
            futs = {pool.submit(fetch_range, direct, a, b, tmp / f"part-{i:03d}"): (i, a, b)
                    for i, (a, b) in enumerate(ranges)}
            for fut in cf.as_completed(futs):
                i, a, b = futs[fut]
                try:
                    fut.result()
                except Exception as exc:  # noqa: BLE001 - one bad range aborts to the fallback path
                    raise RuntimeError(f"range {i} failed: {exc}") from exc
        with master.open("wb") as out:
            for i in range(n):
                part = tmp / f"part-{i:03d}"
                with part.open("rb") as fh:
                    shutil.copyfileobj(fh, out, 1 << 20)
        shutil.rmtree(tmp, ignore_errors=True)
        if master.stat().st_size != total:
            raise RuntimeError(f"assembled {master.stat().st_size} bytes, expected {total}")
        return master, f"ranges({n}x parallel)"
    except Exception as exc:  # noqa: BLE001
        if not quiet:
            print(f"  range download failed ({exc}); falling back to yt-dlp streaming", flush=True)
        master.unlink(missing_ok=True)
        cp = ydl.call(["--no-warnings", "--no-playlist", "--force-overwrites", "-f", quality,
                       "--user-agent", UA, "-o", str(master.with_suffix(".%(ext)s")), url],
                      timeout=7200)
        if cp.returncode != 0:
            die(f"yt-dlp download failed: {(cp.stderr or cp.stdout).strip()[-400:]}")
        found = sorted(master.parent.glob(master.stem + ".*"))
        found = [p for p in found if p.suffix != ".part"]
        if not found:
            die("yt-dlp reported success but left no master file")
        if found[0] != master:
            master = found[0]
        return master, "yt-dlp streaming"


# --------------------------------------------------------------------------- cutting
def cut_track(master: Path, track: dict, index: int, total: int, out: Path, *,
              bitrate: str, album: str, album_artist: str, genre: str,
              source: str, cover: Path | None) -> None:
    meta = [
        "-metadata", f"title={track['title']}",
        "-metadata", f"artist={track['artist'] or 'Unknown Artist'}",
        "-metadata", f"album={album}",
        "-metadata", f"album_artist={album_artist}",
        "-metadata", f"track={index}/{total}",
        "-metadata", f"genre={genre}",
        "-metadata", f"comment=Source: {source} @ {hms(track['start'])}",
    ]
    if track["year"]:
        meta += ["-metadata", f"date={track['year']}"]
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
           "-ss", f"{track['start']:.3f}", "-to", f"{track['end']:.3f}", "-i", str(master)]
    if cover is not None and cover.exists():
        # No -frames:v here: it makes ffmpeg stop the whole muxer after the first video frame,
        # which silently truncates every MP3 to ~40 ms.
        cmd += ["-i", str(cover), "-map", "0:a:0", "-map", "1:v:0", "-c:v", "copy",
                "-metadata:s:v", "title=Album cover",
                "-metadata:s:v", "comment=Cover (front)"]
    cmd += ["-map_metadata", "-1", "-c:a", "libmp3lame", "-b:a", bitrate,
            "-id3v2_version", "3", "-write_id3v1", "1"] + meta + [str(out)]
    cp = run(cmd, timeout=1800)
    if cp.returncode != 0:
        raise RuntimeError(f"ffmpeg cut failed for #{index}: {cp.stderr.strip()[-300:]}")
    if not out.exists() or out.stat().st_size < 1024:
        raise RuntimeError(f"ffmpeg produced no usable file for #{index}")
    # Guard against silent truncation (muxers can stop early and still exit 0).
    want = track["end"] - track["start"]
    got = ffprobe_duration(out) or 0.0
    if abs(got - want) > max(2.0, want * 0.02):
        raise RuntimeError(f"#{index} came out {got:.1f}s but should be {want:.1f}s "
                           f"(ffmpeg exited 0 — output rejected)")


def fetch_cover(thumb_url: str | None, dest: Path) -> Path | None:
    """Download the video thumbnail and normalise it to a real JPEG.

    YouTube serves the best thumbnail as WebP, but ID3 APIC with image/jpeg is what players
    universally understand, so transcode instead of embedding the WebP.
    """
    if not thumb_url:
        return None
    try:
        req = urllib.request.Request(thumb_url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read()
        if len(data) < 1024:
            return None
        dest.parent.mkdir(parents=True, exist_ok=True)
        raw = dest.with_suffix(".raw")
        raw.write_bytes(data)
        cp = run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                  "-i", str(raw), "-frames:v", "1", "-q:v", "3", str(dest)], timeout=120)
        raw.unlink(missing_ok=True)
        if cp.returncode != 0 or not dest.exists() or dest.stat().st_size < 1024:
            return None
        return dest
    except Exception:  # noqa: BLE001 - cover art is cosmetic
        return None


# --------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(
        description="Rip a YouTube compilation into one tagged MP3 per track.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("url", help="YouTube video URL (playlist params are ignored)")
    ap.add_argument("tracks", nargs="?",
                    help="file with 'HH:MM:SS Title - Artist - Year' lines ('-' for stdin); "
                         "omit it with --audio-only")
    ap.add_argument("--out", default=None, help="album directory (default: ~/Music/<video-slug>)")
    ap.add_argument("--name", default=None, help="override the album directory name")
    ap.add_argument("--bitrate", default="192k", help="LAME constant bitrate")
    ap.add_argument("--quality", default="bestaudio[ext=m4a]/bestaudio/best",
                    help="yt-dlp audio format selector")
    ap.add_argument("--parallel", type=int, default=8, help="parallel byte-range workers")
    ap.add_argument("--genre", default="Jazz", help="ID3 genre")
    ap.add_argument("--cover", dest="cover", action="store_true", default=True,
                    help="embed the video thumbnail as cover art")
    ap.add_argument("--no-cover", dest="cover", action="store_false")
    ap.add_argument("--keep-master", dest="keep_master", action="store_true", default=True,
                    help="keep the downloaded master audio in _master/")
    ap.add_argument("--no-master", dest="keep_master", action="store_false")
    ap.add_argument("--master", default=None, help="use an existing master audio file, skip download")
    ap.add_argument("--ytdlp", default=None, help="path to a yt-dlp executable")
    ap.add_argument("--cache", default=str(Path.home() / ".cache" / "ytrip" / "uv"),
                    help="uv cache dir used when yt-dlp must be bootstrapped")
    ap.add_argument("--accept-anomalies", action="store_true",
                    help="proceed even if the timestamp list looks wrong")
    ap.add_argument("--audio-only", action="store_true",
                    help="no track list: save the whole audio as one MP3 instead of cutting")
    ap.add_argument("--dry-run", action="store_true", help="validate and print the plan, touch nothing")
    ap.add_argument("--force", action="store_true", help="re-cut files that already exist")
    ap.add_argument("--json", dest="as_json", action="store_true", help="machine-readable summary")
    args = ap.parse_args()

    if shutil.which("ffmpeg") is None:
        die("ffmpeg not found on PATH")

    tracks, rejected = [], []
    if args.tracks:
        src = sys.stdin.read() if args.tracks == "-" else Path(args.tracks).expanduser().read_text(
            encoding="utf-8", errors="replace")
        tracks, rejected = parse_tracks(src)
        if not tracks:
            die("no timestamp lines found in the track list")
    elif not args.audio_only:
        die("no track list given — pass a file, '-' for stdin, or --audio-only")

    quiet = args.as_json
    say = (lambda *a: None) if quiet else (lambda *a: print(*a, flush=True))

    say(f"parsing: {len(tracks)} tracks", *( [f"  skipped: {r}" for r in rejected] or [] ))

    ydl = Ytdlp(args.ytdlp, Path(args.cache).expanduser())
    say("resolving video metadata ...")
    info = ydl.meta(args.url)
    vid, duration = info.get("id") or "?", float(info.get("duration") or 0)
    vtitle = info.get("title") or vid
    uploader = info.get("channel") or info.get("uploader") or info.get("uploader_id") or ""
    chapters = info.get("chapters") or []
    clean_url = f"https://www.youtube.com/watch?v={vid}"
    if not duration:
        die("yt-dlp reported no duration for this video")
    say(f"  {vid} :: {vtitle!r} :: {hms(duration)} :: {uploader} :: {len(chapters)} chapters")

    errors, warnings = validate(tracks, duration, chapters) if tracks else ([], [])
    if errors or warnings:
        say("")
        say("validation")
        for e in errors:
            say(f"  ERROR   {e}")
        for w in warnings:
            say(f"  WARNING {w}")
        say("")
    if errors and not args.accept_anomalies:
        die(f"{len(errors)} timestamp error(s); fix the list or pass --accept-anomalies", 3)

    # fill in ends
    for i, t in enumerate(tracks):
        t["end"] = tracks[i + 1]["start"] if i + 1 < len(tracks) else duration

    album = args.name or slugify(vtitle)
    out = Path(args.out).expanduser() if args.out else Path.home() / "Music" / album
    master = (Path(args.master).expanduser() if args.master
              else out / "_master" / f"{vid}.m4a")

    say(f"album dir: {out}")
    say(f"plan: {'whole audio as one MP3' if args.audio_only else str(len(tracks)) + ' MP3s'} "
        f"at {args.bitrate} CBR")
    for i, t in enumerate(tracks, 1):
        fname = f"{i:02d} - {safe_name(t['artist'])} - {safe_name(t['title'])}"
        if t["year"]:
            fname += f" ({t['year']})"
        t["file"] = out / (fname + ".mp3")
        say(f"  {i:02d} {hms(t['start'])}-{hms(t['end'])}  {t['file'].name}")

    if args.dry_run:
        say("")
        say("dry run: nothing downloaded, nothing written")
        if errors:
            say(f"  ({len(errors)} timestamp error(s) would block a real run)")
        return 0

    if args.master:
        if not master.exists():
            die(f"--master file not found: {master}")
        how = "supplied"
    elif master.exists() and (ffprobe_duration(master) or 0) > 0:
        how = "cached"
        say(f"master already present, reusing: {master}")
    else:
        say("downloading audio ...")
        master, how = download_master(args.url, args.quality, ydl, master, args.parallel, quiet)
        dur = ffprobe_duration(master)
        if not dur:
            die(f"downloaded master is not readable audio: {master}")
        if abs(dur - duration) > 5:
            say(f"  note: master is {hms(dur)} vs video {hms(duration)}")
        say(f"  master ready ({master.stat().st_size/1e6:.1f} MB, via {how})")

    cover = None
    if args.cover:
        cover = fetch_cover(info.get("thumbnail"), out / "_master" / f"{vid}-cover.jpg")
        say(f"cover art: {'ready' if cover else 'unavailable'}")

    if args.audio_only:
        single = out / f"{safe_name(vtitle)}.mp3"
        single.parent.mkdir(parents=True, exist_ok=True)
        if single.exists() and not args.force:
            say(f"already present, use --force to redo: {single}")
        else:
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                   "-i", str(master)]
            if cover is not None and cover.exists():
                cmd += ["-i", str(cover), "-map", "0:a:0", "-map", "1:v:0", "-c:v", "copy",
                        "-metadata:s:v", "title=Album cover",
                        "-metadata:s:v", "comment=Cover (front)"]
            cmd += ["-map_metadata", "-1", "-c:a", "libmp3lame", "-b:a", args.bitrate,
                    "-id3v2_version", "3", "-write_id3v1", "1",
                    "-metadata", f"title={vtitle}", "-metadata", f"artist={uploader}",
                    "-metadata", f"album={vtitle}", "-metadata", f"genre={args.genre}",
                    "-metadata", f"comment=Source: {clean_url}", str(single)]
            cp = run(cmd, timeout=3600)
            if cp.returncode != 0 or not single.exists():
                die(f"ffmpeg failed writing the whole-audio file: {cp.stderr.strip()[-300:]}")
            say(f"  wrote {single.name} ({single.stat().st_size/1e6:.1f} MB)")
        (out / "info.md").write_text(
            f"# {vtitle}\n\n- Source: <{clean_url}>\n- Whole audio, no cuts\n"
            f"- File: {single.name}\n- Audio: MP3 {args.bitrate} CBR ({how})\n"
            f"- Channel: {uploader}\n"
            f"- Ripped: {time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n", encoding="utf-8")
        if not args.keep_master:
            shutil.rmtree(out / "_master", ignore_errors=True)
        say(f"\ndone -> {out}")
        if args.as_json:
            print(json.dumps({"video": {"id": vid, "title": vtitle, "duration": duration,
                                        "url": clean_url}, "album_dir": str(out),
                               "file": str(single), "audio_only": True}, indent=2))
        return 0

    say(f"cutting {len(tracks)} tracks ...")
    t0 = time.time()
    done, skipped, failed = 0, 0, []

    def work(item):
        i, t = item
        if t["file"].exists() and not args.force:
            return (i, "skipped", None)
        t["file"].parent.mkdir(parents=True, exist_ok=True)
        try:
            cut_track(master, t, i, len(tracks), t["file"], bitrate=args.bitrate,
                      album=vtitle, album_artist=uploader, genre=args.genre,
                      source=clean_url, cover=cover)
            return (i, "ok", None)
        except Exception as exc:  # noqa: BLE001
            return (i, "failed", str(exc))

    workers = max(1, min(4, (os.cpu_count() or 2)))
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        for i, status, err in pool.map(work, enumerate(tracks, 1)):
            if status == "ok":
                done += 1
            elif status == "skipped":
                skipped += 1
            else:
                failed.append(f"#{i}: {err}")
            say(f"  [{done+skipped+len(failed):02d}/{len(tracks)}] {tracks[i-1]['file'].name}")

    # sidecars
    manifest = out / "manifest.tsv"
    with manifest.open("w", encoding="utf-8") as fh:
        fh.write("index\tstart\tend\tduration\ttitle\tartist\tyear\tfile\tbytes\tseconds\n")
        for i, t in enumerate(tracks, 1):
            f = t["file"]
            b = f.stat().st_size if f.exists() else 0
            d = ffprobe_duration(f) or 0
            fh.write(f"{i}\t{hms(t['start'])}\t{hms(t['end'])}\t{hms(t['end']-t['start'])}\t"
                     f"{t['title']}\t{t['artist']}\t{t['year'] or ''}\t{f.name}\t{b}\t{d:.2f}\n")
    playlist = out / "playlist.m3u8"
    with playlist.open("w", encoding="utf-8") as fh:
        fh.write("#EXTM3U\n")
        for i, t in enumerate(tracks, 1):
            if t["file"].exists():
                secs = (ffprobe_duration(t["file"]) or t["end"] - t["start"])
                fh.write(f"#EXTINF:{int(round(secs))},{t['artist']} - {t['title']}\n")
                fh.write(t["file"].name + "\n")
    (out / "info.md").write_text(
        f"# {vtitle}\n\n"
        f"- Source: <{clean_url}>\n- Video duration: {hms(duration)}\n"
        f"- Tracks: {len(tracks)} ({done} written, {skipped} already present, {len(failed)} failed)\n"
        f"- Audio: MP3 {args.bitrate} CBR from {master.name} ({how})\n"
        f"- Channel: {uploader}\n- YouTube chapters in source: {len(chapters)}\n"
        f"- Ripped: {time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
        + ("\n## Validation notes\n" + "\n".join(f"- {e}" for e in errors + warnings) if (errors or warnings) else "")
        + ("\n## Failures\n" + "\n".join(f"- {f}" for f in failed) if failed else ""),
        encoding="utf-8")

    if not args.keep_master:
        shutil.rmtree(out / "_master", ignore_errors=True)

    say("")
    say(f"done in {time.time()-t0:.1f}s: {done} new, {skipped} existing, {len(failed)} failed -> {out}")
    if failed:
        for f in failed:
            say(f"  FAILED {f}")
    if quiet:
        print(json.dumps({
            "video": {"id": vid, "title": vtitle, "duration": duration, "uploader": uploader,
                      "url": clean_url, "chapters": len(chapters)},
            "album_dir": str(out), "tracks": len(tracks), "written": done,
            "skipped": skipped, "failed": failed, "errors": errors, "warnings": warnings,
            "master": str(master) if args.keep_master else None,
            "bitrate": args.bitrate, "download": how,
        }, indent=2, ensure_ascii=False))
    return 4 if failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        die("interrupted", 130)
    except urllib.error.URLError as exc:
        die(f"network error: {exc}", 5)

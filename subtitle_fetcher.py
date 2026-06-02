"""Chinese-first subtitle resolver (extract-only, no AI translation).

Waterfall:
  L1  existing CHS file alongside video        -> use as-is (zh)
  L1  embedded CHS/CHT in MKV                   -> extract <stem>.zh.srt (zh)
  L2  embedded English in MKV                   -> extract <stem>.en.srt (en, for manual translation)
  L3  external English via subdl (proxied)      -> <stem>.en.srt (en)
  L4  nothing                                   -> None (caller marks no_subtitle)

The caller (api_server._fetch_sub) infers status from the returned filename:
  *.zh.srt -> "zh", *.en.srt -> "en_only", None -> "no_subtitle".
"""
from __future__ import annotations
import asyncio
import re
from pathlib import Path
from typing import Callable, Optional

from config import settings
from store import log_event, get_movie


# ────────────────────────────────────────────────────────────────
# Public entry point
# ────────────────────────────────────────────────────────────────
async def fetch_for_video(
    video_path: Path,
    imdb_id: str,
    on_progress: Optional[Callable[[str], None]] = None,
) -> Optional[Path]:
    """Return best subtitle path (.zh.srt preferred, else .en.srt), or None."""
    if not video_path.exists():
        log_event("warn", f"subtitle: video missing {video_path}", imdb_id)
        return None

    # L1a: existing Chinese subtitle file alongside the video
    bundled = _find_existing_chs(video_path.parent)
    if bundled:
        log_event("info", f"subtitle: using existing CHS sub {bundled.name}", imdb_id)
        return bundled

    # L1b/L2: extract embedded subs (CHS/CHT -> .zh.srt, else English -> .en.srt)
    if on_progress:
        on_progress("提取内嵌字幕")
    extracted = await asyncio.to_thread(extract_subs_from_mkv, video_path, imdb_id)
    if extracted:
        return extracted

    # L3: external English download via subdl (no translation)
    if settings.SUBDL_API_KEY:
        if on_progress:
            on_progress("外部下载英文字幕")
        downloaded = await _download_subdl_en(video_path, imdb_id)
        if downloaded:
            return downloaded

    return None


# ────────────────────────────────────────────────────────────────
# Detect existing subtitle files on disk
# ────────────────────────────────────────────────────────────────
_CHS_KEYWORDS = re.compile(
    r"(chinese[._\-]?(simplified|simp|chs|zhs|zh[-_]s|sc)|"
    r"简体|chs|zhs|zh[-_]s|\bsc\b|chinese)",
    re.I,
)
_SUB_EXTS = {".srt", ".ass", ".ssa", ".vtt"}


def _find_existing_chs(directory: Path) -> Optional[Path]:
    """Return a Chinese subtitle file in directory if present."""
    if not directory.is_dir():
        return None
    for f in sorted(directory.iterdir()):
        if f.suffix.lower() in _SUB_EXTS:
            stem = f.stem.lower()
            if _CHS_KEYWORDS.search(stem) or stem.endswith(".zh") or stem.endswith(".chs"):
                return f
    return None


def extract_bundled_subs(torrent_root: Path, dest_dir: Path) -> list[Path]:
    """
    Called from organize_to_library to copy subtitle files from the torrent
    folder (including a Subs/ subfolder) into dest_dir.

    Returns list of copied subtitle paths.
    """
    if not torrent_root.exists():
        return []
    copied: list[Path] = []
    for sub in torrent_root.rglob("*"):
        if sub.suffix.lower() not in _SUB_EXTS or not sub.is_file():
            continue
        dest = dest_dir / sub.name
        if dest.resolve() == sub.resolve():
            copied.append(dest)
            continue
        try:
            import shutil
            shutil.copy2(str(sub), str(dest))
            copied.append(dest)
        except Exception:
            pass
    return copied


# ────────────────────────────────────────────────────────────────
# Extract embedded subtitles from MKV (ffmpeg)
# ────────────────────────────────────────────────────────────────
_CHS_LANG = re.compile(r"(chi.*simpl|zho.*simpl|chs|zh.*hans)", re.I)
_CHT_LANG = re.compile(r"(chi|zho|chinese)", re.I)
_ENG_LANG = re.compile(r"(eng)", re.I)


def _ffprobe_subs(video_path: Path) -> list[dict]:
    """Return list of subtitle stream info dicts from ffprobe."""
    import subprocess, json as _json
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", "-select_streams", "s", str(video_path)],
            capture_output=True, timeout=15
        )
        return _json.loads(r.stdout).get("streams", [])
    except Exception:
        return []


def _ffmpeg_extract(video_path: Path, stream_idx: int, out_path: Path) -> bool:
    """Extract a single subtitle stream to out_path (.srt)."""
    import subprocess
    size_mb = video_path.stat().st_size / 1024 / 1024 if video_path.exists() else 0
    timeout = max(120, int(30 + size_mb * 0.05))
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(video_path),
             "-map", f"0:{stream_idx}", "-c:s", "srt", str(out_path)],
            capture_output=True, timeout=timeout
        )
        if out_path.exists() and out_path.stat().st_size > 100:
            return True
        if out_path.exists():
            out_path.unlink(missing_ok=True)
        return False
    except subprocess.TimeoutExpired:
        log_event("error", f"subtitle: ffmpeg extract timed out after {timeout}s (stream {stream_idx})")
        if out_path.exists():
            out_path.unlink(missing_ok=True)
        return False
    except Exception as e:
        log_event("error", f"subtitle: ffmpeg extract failed: {repr(e)} (stream {stream_idx})")
        if out_path.exists():
            out_path.unlink(missing_ok=True)
        return False


def extract_subs_from_mkv(video_path: Path, imdb_id: str) -> Optional[Path]:
    """
    Extract embedded subtitles. Priority: CHS > CHT (-> .zh.srt) > English (-> .en.srt).
    Returns the extracted subtitle path, or None if no usable stream.
    """
    if video_path.suffix.lower() not in (".mkv", ".mp4", ".m4v"):
        return None
    streams = _ffprobe_subs(video_path)
    if not streams:
        return None

    chs_idx = cht_idx = eng_idx = None
    for s in streams:
        idx = s["index"]
        lang = s.get("tags", {}).get("language", "")
        title = s.get("tags", {}).get("title", "")
        tag = f"{lang} {title}"
        if _CHS_LANG.search(tag) and chs_idx is None:
            chs_idx = idx
        elif _CHT_LANG.search(tag) and cht_idx is None:
            cht_idx = idx
        elif _ENG_LANG.search(lang) and eng_idx is None:
            eng_idx = idx

    stem = video_path.stem

    # Simplified Chinese — use directly
    if chs_idx is not None:
        out = video_path.parent / f"{stem}.zh.srt"
        if _ffmpeg_extract(video_path, chs_idx, out):
            log_event("info", f"subtitle: extracted CHS from MKV stream {chs_idx}", imdb_id)
            return out

    # Traditional Chinese — use directly
    if cht_idx is not None:
        out = video_path.parent / f"{stem}.zh.srt"
        if _ffmpeg_extract(video_path, cht_idx, out):
            log_event("info", f"subtitle: extracted CHT from MKV stream {cht_idx}", imdb_id)
            return out

    # English — deliver as-is for manual translation
    if eng_idx is not None:
        out = video_path.parent / f"{stem}.en.srt"
        if _ffmpeg_extract(video_path, eng_idx, out):
            log_event("info", f"subtitle: extracted ENG from MKV stream {eng_idx} (manual translate)", imdb_id)
            return out

    return None


# ────────────────────────────────────────────────────────────────
# External English subtitle via subdl.com (download only, no translation)
# ────────────────────────────────────────────────────────────────
def _clean_title(raw: str) -> str:
    """'The Book of Life (2014) [2160p] [WEBRip]...' -> 'The Book of Life'."""
    return re.split(r"\s*[\(\[]", raw or "", maxsplit=1)[0].strip()


async def _download_subdl_en(video_path: Path, imdb_id: str) -> Optional[Path]:
    """Fetch an English subtitle from subdl.com and save as <stem>.en.srt."""
    import io
    import zipfile
    import httpx

    movie = get_movie(imdb_id) or {}
    film_name = _clean_title(movie.get("title", ""))
    year = movie.get("year")
    if not film_name:
        return None

    proxy = settings.SUB_PROXY or None
    params = {
        "api_key": settings.SUBDL_API_KEY,
        "film_name": film_name,
        "languages": "EN",
        "type": "movie",
        "subs_per_page": 10,
    }
    if year:
        params["year"] = year

    try:
        async with httpx.AsyncClient(timeout=30, proxy=proxy, follow_redirects=True) as cli:
            r = await cli.get("https://api.subdl.com/api/v1/subtitles", params=params)
            r.raise_for_status()
            data = r.json()
            subs = data.get("subtitles") or []
            if not subs:
                log_event("info", f"subtitle: subdl no EN result for '{film_name}'", imdb_id)
                return None

            for sub in subs:
                url = sub.get("url")
                if not url:
                    continue
                zip_url = url if url.startswith("http") else f"https://dl.subdl.com{url}"
                zr = await cli.get(zip_url)
                if zr.status_code != 200:
                    continue
                try:
                    zf = zipfile.ZipFile(io.BytesIO(zr.content))
                except zipfile.BadZipFile:
                    continue
                names = [n for n in zf.namelist() if n.lower().endswith(".srt")]
                if not names:
                    continue
                raw = zf.read(names[0])
                text = raw.decode("utf-8", errors="replace")
                out = video_path.parent / f"{video_path.stem}.en.srt"
                out.write_text(text, encoding="utf-8")
                log_event("info", f"subtitle: subdl EN downloaded ({sub.get('release_name','')})", imdb_id)
                return out
    except Exception as e:
        log_event("warn", f"subtitle: subdl fetch failed: {repr(e)}", imdb_id)
    return None


# ────────────────────────────────────────────────────────────────
# On-demand AI translation: .en.srt -> .zh.srt  (HTTP, concurrent)
# ────────────────────────────────────────────────────────────────
def _parse_srt(path: Path) -> list[tuple[str, str, str]]:
    """Parse SRT -> list of (index_str, timing_str, text_str)."""
    text = path.read_text(encoding="utf-8", errors="replace")
    blocks = re.split(r"\n{2,}", text.strip())
    captions = []
    for block in blocks:
        lines = block.strip().splitlines()
        if len(lines) >= 3:
            idx = lines[0].strip()
            timing = lines[1].strip()
            body = " ".join(l.strip() for l in lines[2:] if l.strip())
            body = re.sub(r"<[^>]+>", "", body)
            if body:
                captions.append((idx, timing, body))
    return captions


def _write_srt(path: Path, captions: list[tuple[str, str, str]]) -> None:
    blocks = [f"{idx}\n{timing}\n{text}" for idx, timing, text in captions]
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")


async def translate_en_to_zh(
    video_path: Path,
    imdb_id: str,
    on_progress: Optional[Callable[[str], None]] = None,
) -> Optional[Path]:
    """Translate <stem>.en.srt -> <stem>.zh.srt using the configured HTTP LLM endpoint."""
    import json
    import httpx

    en_srt = video_path.with_suffix("").with_suffix("").parent / f"{video_path.stem}.en.srt"
    # Also accept video_path itself as directory hint
    if not en_srt.exists():
        en_srt = video_path.parent / f"{video_path.stem}.en.srt"
    if not en_srt.exists():
        log_event("warn", "subtitle: no .en.srt found to translate", imdb_id)
        return None

    captions = _parse_srt(en_srt)
    if not captions:
        return None

    batch_size = getattr(settings, "TRANS_BATCH_SIZE", 20)
    concurrent = getattr(settings, "TRANS_CONCURRENT", 6)
    model = getattr(settings, "TRANS_MODEL", "deepseek-v4-flash")
    base_url = getattr(settings, "TRANS_BASE_URL", "")
    api_key = getattr(settings, "TRANS_API_KEY", "")
    proxy = settings.YTS_API_PROXY or None

    if not base_url:
        log_event("warn", "subtitle: TRANS_BASE_URL not set, cannot translate", imdb_id)
        return None

    batches = [captions[i:i+batch_size] for i in range(0, len(captions), batch_size)]
    total = len(batches)
    results: dict[int, list[str]] = {}
    sem = asyncio.Semaphore(concurrent)

    async def _translate_batch(client: httpx.AsyncClient, idx: int, batch: list) -> None:
        texts = [c[2] for c in batch]
        async with sem:
            for attempt in range(3):
                try:
                    if attempt:
                        await asyncio.sleep(2 ** attempt)
                    resp = await client.post(
                        f"{base_url}/chat/completions",
                        headers={"Authorization": f"Bearer {api_key}"},
                        json={
                            "model": model,
                            "messages": [
                                {"role": "system", "content": "You are a professional movie subtitle translator. Output only JSON arrays."},
                                {"role": "user", "content": (
                                    "Translate the following subtitle lines to Simplified Chinese. "
                                    "Return ONLY a JSON array of translated strings, same count as input. "
                                    "Keep lines concise for subtitles. No explanations.\n\n"
                                    f"INPUT:\n{json.dumps(texts, ensure_ascii=False)}"
                                )},
                            ],
                        },
                        timeout=60.0,
                    )
                    resp.raise_for_status()
                    content = resp.json()["choices"][0]["message"]["content"]
                    m = re.search(r"\[.*\]", content, re.DOTALL)
                    parsed = json.loads(m.group(0) if m else content)
                    if isinstance(parsed, list) and len(parsed) == len(texts):
                        results[idx] = [str(r).strip() or orig for r, orig in zip(parsed, texts)]
                        return
                    raise ValueError(f"count mismatch: {len(parsed)} vs {len(texts)}")
                except Exception as e:
                    if attempt == 2:
                        log_event("warn", f"subtitle: translate batch {idx} failed: {repr(e)}", imdb_id)
                        results[idx] = texts

    log_event("info", f"subtitle: translating {len(captions)} lines in {total} batches (concurrent={concurrent})", imdb_id)
    if on_progress:
        on_progress(f"AI 翻译中 · {len(captions)} 行")

    async with httpx.AsyncClient(proxy=proxy, timeout=httpx.Timeout(60.0, connect=10.0)) as client:
        tasks = [_translate_batch(client, i, batch) for i, batch in enumerate(batches)]
        for i, coro in enumerate(asyncio.as_completed(tasks)):
            await coro
            if on_progress:
                done = sum(1 for k in results if k is not None)
                on_progress(f"翻译进度 {done*100//total}% ({done}/{total} 批)")

    out_captions = []
    for i, batch in enumerate(batches):
        zh_texts = results.get(i, [c[2] for c in batch])
        for (idx, timing, _), zh in zip(batch, zh_texts):
            out_captions.append((idx, timing, zh))

    out_path = en_srt.with_name(f"{video_path.stem}.zh.srt")
    _write_srt(out_path, out_captions)
    log_event("info", f"subtitle: translated -> {out_path.name} ({len(out_captions)} lines)", imdb_id)
    return out_path

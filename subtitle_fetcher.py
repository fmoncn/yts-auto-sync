"""Chinese-first subtitle resolver (extract-only, no AI translation).

Waterfall:
  L1  existing CHS file alongside video        -> use as-is (zh)
  L1  embedded CHS/CHT in MKV                   -> extract <stem>.zh.srt (zh)
  L2  embedded English in MKV                   -> extract <stem>.en.srt (en, for manual translation)
  L3  external English via subdl (proxied)      -> <stem>.en.srt (en)
  L4  external English via OpenSubtitles        -> <stem>.en.srt (en)
  L5  nothing                                   -> None (caller marks no_subtitle)

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

    # L2.5: pick the best English subtitle from the directory.
    # YTS packs often include a plain .srt that is actually a "forced" subtitle
    # (only foreign-language lines), while a fuller SDH / English.srt exists nearby.
    en_srt = video_path.parent / f"{video_path.stem}.en.srt"
    best_en = _pick_best_english_sub(video_path.parent, video_path.stem)
    if best_en:
        if not en_srt.exists() or best_en.resolve() != en_srt.resolve():
            import shutil as _shutil
            _shutil.copy2(str(best_en), str(en_srt))
            log_event("info", f"subtitle: picked {best_en.name} as .en.srt ({_count_srt_entries(best_en)} entries)", imdb_id)
        return en_srt

    # L3: external English download via subdl (no translation)
    # Skip ONLY if there are extractable text subtitle streams (not PGS/image-only streams)
    if settings.SUBDL_API_KEY:
        skip_subdl = False
        if video_path.suffix.lower() in (".mkv", ".mp4", ".m4v"):
            streams = await asyncio.to_thread(_ffprobe_subs, video_path)
            skip_subdl = _has_extractable_text_subs(streams)
        if skip_subdl:
            log_event("info", "subtitle: extractable text stream found, skipping subdl", imdb_id)
        else:
            if on_progress:
                on_progress("subdl 下载英文字幕")
            downloaded = await _download_subdl_en(video_path, imdb_id)
            if downloaded:
                return downloaded

    # L4: OpenSubtitles fallback
    if settings.OPENSUBTITLES_API_KEY:
        if on_progress:
            on_progress("OpenSubtitles 下载英文字幕")
        downloaded = await _download_opensubtitles_en(video_path, imdb_id)
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



_MIN_FULL_SUB_ENTRIES = 50

def _count_srt_entries(path: "Path") -> int:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        return len(re.findall(r"^\d+\s*$", text, re.MULTILINE))
    except Exception:
        return 0

_ENG_SUB_PATTERNS = re.compile(r"(english|eng\\.srt|sdh.*eng|\\.en\\.srt)", re.I)

_NON_ENG_KEYWORDS = ("chi", "chs", "cht", "zhs", "spa", "fre", "ger", "ita",
    "por", "dut", "dan", "swe", "nor", "fin", "rum", "hin", "ukr", "slv",
    "forced", "latin", "brazilian")

def _pick_best_english_sub(directory: "Path", video_stem: str):
    candidates = []
    plain_srt = directory / f"{video_stem}.srt"
    for f in sorted(directory.iterdir()):
        if f.suffix.lower() != ".srt" or not f.is_file():
            continue
        stem_lower = f.stem.lower()
        if any(kw in stem_lower for kw in _NON_ENG_KEYWORDS):
            continue
        if not _is_likely_english_srt(f):
            continue
        count = _count_srt_entries(f)
        candidates.append((count, f))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    best_count, best_file = candidates[0]
    if best_file.resolve() == plain_srt.resolve() and best_count < _MIN_FULL_SUB_ENTRIES:
        for count, f in candidates[1:]:
            if count >= _MIN_FULL_SUB_ENTRIES:
                return f
    return best_file

def _is_likely_english_srt(path: Path, sample_lines: int = 30) -> bool:
    """Sample up to sample_lines of text from an SRT and check it looks like English."""
    try:
        text = path.read_bytes()[:8000].decode("utf-8", errors="ignore")
    except Exception:
        return False
    # Reject if Chinese/CJK characters present
    if re.search(r"[一-鿿぀-ヿ가-힯]", text):
        return False
    # Must have at least some ASCII words
    words = re.findall(r"[a-zA-Z]{3,}", text)
    return len(words) >= 5


# Image-based subtitle codecs that ffmpeg cannot convert to SRT
_IMAGE_SUB_CODECS = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvdsub", "dvb_subtitle"}

def _has_extractable_text_subs(streams: list[dict]) -> bool:
    """Return True if any stream is a text-based subtitle extractable to SRT."""
    return any(
        s.get("codec_name", "").lower() not in _IMAGE_SUB_CODECS
        for s in streams
    )


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
# External English subtitle via opensubtitles.com REST API
# ────────────────────────────────────────────────────────────────
_os_token: Optional[str] = None
_os_token_expiry: float = 0.0


async def _opensubtitles_login(client: "httpx.AsyncClient") -> Optional[str]:
    global _os_token, _os_token_expiry
    import time
    if _os_token and time.time() < _os_token_expiry:
        return _os_token
    if not (settings.OPENSUBTITLES_USERNAME and settings.OPENSUBTITLES_PASSWORD):
        return None
    try:
        r = await client.post(
            "https://api.opensubtitles.com/api/v1/login",
            headers={"Api-Key": settings.OPENSUBTITLES_API_KEY, "Content-Type": "application/json"},
            json={"username": settings.OPENSUBTITLES_USERNAME, "password": settings.OPENSUBTITLES_PASSWORD},
        )
        r.raise_for_status()
        data = r.json()
        _os_token = data.get("token")
        _os_token_expiry = time.time() + 23 * 3600  # tokens valid 24h
        return _os_token
    except Exception as e:
        log_event("warn", f"subtitle: opensubtitles login failed: {repr(e)}")
        return None


async def _download_opensubtitles_en(video_path: Path, imdb_id: str) -> Optional[Path]:
    """Fetch an English subtitle from opensubtitles.com and save as <stem>.en.srt."""
    import httpx

    movie = get_movie(imdb_id) or {}
    film_name = _clean_title(movie.get("title", ""))
    year = movie.get("year")
    if not film_name:
        return None

    proxy = settings.SUB_PROXY or None
    headers = {
        "Api-Key": settings.OPENSUBTITLES_API_KEY,
        "Content-Type": "application/json",
        "User-Agent": "yts-auto-sync v1.0",
    }

    try:
        async with httpx.AsyncClient(timeout=30, proxy=proxy, follow_redirects=True, headers=headers) as cli:
            token = await _opensubtitles_login(cli)
            if token:
                headers["Authorization"] = f"Bearer {token}"
                cli.headers.update({"Authorization": f"Bearer {token}"})

            params: dict = {"languages": "en", "type": "movie", "order_by": "download_count"}
            # Prefer IMDB ID lookup for accuracy
            real_imdb = (movie.get("imdb_url") or "").rstrip("/").rsplit("/", 1)[-1]
            if real_imdb and real_imdb.startswith("tt"):
                params["imdb_id"] = real_imdb.lstrip("tt")
            else:
                params["query"] = film_name
                if year:
                    params["year"] = year

            r = await cli.get("https://api.opensubtitles.com/api/v1/subtitles", params=params)
            r.raise_for_status()
            data = r.json()
            results = (data.get("data") or [])
            if not results:
                log_event("info", f"subtitle: opensubtitles no EN result for '{film_name}'", imdb_id)
                return None

            if not token:
                log_event("info", f"subtitle: opensubtitles skipping download (no auth token)", imdb_id)
                return None

            for item in results[:5]:
                attrs = item.get("attributes", {})
                files = attrs.get("files") or []
                if not files:
                    continue
                file_id = files[0].get("file_id")
                if not file_id:
                    continue

                dr = await cli.post(
                    "https://api.opensubtitles.com/api/v1/download",
                    json={"file_id": file_id, "sub_format": "srt"},
                )
                dr.raise_for_status()
                dl_data = dr.json()
                link = dl_data.get("link")
                if not link:
                    continue

                sr = await cli.get(link)
                if sr.status_code != 200:
                    continue
                text = sr.content.decode("utf-8", errors="replace")
                if not text.strip():
                    continue

                out = video_path.parent / f"{video_path.stem}.en.srt"
                out.write_text(text, encoding="utf-8")
                release = attrs.get("release") or ""
                log_event("info", f"subtitle: opensubtitles EN downloaded ({release})", imdb_id)
                return out

    except Exception as e:
        log_event("warn", f"subtitle: opensubtitles fetch failed: {repr(e)}", imdb_id)
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

    en_srt = video_path.parent / f"{video_path.stem}.en.srt"
    # Always check for a better source — existing .en.srt may be forced/partial
    best = _pick_best_english_sub(video_path.parent, video_path.stem)
    if best:
        best_count = _count_srt_entries(best)
        cur_count = _count_srt_entries(en_srt) if en_srt.exists() else 0
        if best_count > cur_count:
            import shutil as _shutil
            _shutil.copy2(str(best), str(en_srt))
            log_event("info", f"subtitle: upgraded .en.srt from {best.name} ({best_count} vs {cur_count} entries)", imdb_id)
    elif not en_srt.exists():
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
    proxy = settings.TRANS_PROXY or None

    if not base_url:
        log_event("warn", "subtitle: TRANS_BASE_URL not set, cannot translate", imdb_id)
        return None

    movie_meta = get_movie(imdb_id) or {}
    film_ctx = _clean_title(movie_meta.get("title", "")) or ""
    if movie_meta.get("year"):
        film_ctx = f"{film_ctx} ({movie_meta['year']})"

    batches = [captions[i:i+batch_size] for i in range(0, len(captions), batch_size)]
    total = len(batches)
    results: dict[int, list[str]] = {}
    sem = asyncio.Semaphore(concurrent)

    async def _translate_batch(client: httpx.AsyncClient, idx: int, batch: list) -> None:
        texts = [c[2] for c in batch]
        numbered_input = "\n".join(f"{i+1}. {t}" for i, t in enumerate(texts))
        last_parsed: list | None = None
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
                                {"role": "system", "content": f"You are a professional movie subtitle translator for the film {film_ctx!r}. Output only a JSON array of strings."},
                                {"role": "user", "content": (
                                    f"Film: {film_ctx}\nTranslate these {len(texts)} subtitle lines to Simplified Chinese. "
                                    "Return ONLY a JSON array with EXACTLY the same number of strings as input. "
                                    "Translate each line independently — never merge or split lines. "
                                    "Keep lines concise. No explanations.\n\n"
                                    f"{numbered_input}"
                                )},
                            ],
                        },
                        timeout=60.0,
                    )
                    resp.raise_for_status()
                    content = resp.json()["choices"][0]["message"]["content"]
                    m = re.search(r"\[.*\]", content, re.DOTALL)
                    parsed = json.loads(m.group(0) if m else content)
                    if not isinstance(parsed, list):
                        raise ValueError("response is not a list")
                    last_parsed = parsed
                    if len(parsed) == len(texts):
                        results[idx] = [str(r).strip() or orig for r, orig in zip(parsed, texts)]
                        return
                    raise ValueError(f"count mismatch: {len(parsed)} vs {len(texts)}")
                except Exception as e:
                    if attempt == 2:
                        log_event("warn", f"subtitle: translate batch {idx} failed: {repr(e)}", imdb_id)
                        if last_parsed:
                            # partial salvage: use translated lines we got, fall back to original for the rest
                            results[idx] = [
                                str(last_parsed[i]).strip() if i < len(last_parsed) and str(last_parsed[i]).strip() else texts[i]
                                for i in range(len(texts))
                            ]
                        else:
                            results[idx] = texts

    log_event("info", f"subtitle: translating {len(captions)} lines in {total} batches (concurrent={concurrent})", imdb_id)
    if on_progress:
        on_progress(f"AI 翻译中 · {len(captions)} 行")

    async with httpx.AsyncClient(proxy=proxy, timeout=httpx.Timeout(60.0, connect=10.0)) as client:
        tasks = [_translate_batch(client, i, batch) for i, batch in enumerate(batches)]
        for i, coro in enumerate(asyncio.as_completed(tasks)):
            await coro
            if on_progress:
                done = len(results)
                on_progress(f"翻译进度 {done*100//total}% ({done}/{total} 批)")

    out_captions = []
    for i, batch in enumerate(batches):
        zh_texts = results.get(i, [c[2] for c in batch])
        for (idx, timing, _), zh in zip(batch, zh_texts):
            out_captions.append((idx, timing, zh))

    # ── Review pass: use stronger model to proofread the full translation ──
    review_model = getattr(settings, "TRANS_REVIEW_MODEL", "")
    review_batch_size = getattr(settings, "TRANS_REVIEW_BATCH", 80)
    if review_model and review_model != model:
        log_event("info", f"subtitle: review pass with {review_model} ({len(out_captions)} lines)", imdb_id)
        if on_progress:
            on_progress(f"校审中 · {review_model}")

        # Pair original EN with translated ZH for review context
        paired = list(zip(captions, out_captions))  # ((idx,timing,en), (idx,timing,zh))
        review_chunks = [paired[i:i+review_batch_size] for i in range(0, len(paired), review_batch_size)]
        reviewed: dict[int, list[str]] = {}

        async def _review_chunk(client: httpx.AsyncClient, chunk_idx: int, chunk: list) -> None:
            en_lines = [c[0][2] for c in chunk]
            zh_lines = [c[1][2] for c in chunk]
            payload = "\n".join(
                f"{i+1}. EN: {en}\n   ZH: {zh}"
                for i, (en, zh) in enumerate(zip(en_lines, zh_lines))
            )
            last_parsed: list | None = None
            async with sem:
                for attempt in range(3):
                    try:
                        if attempt:
                            await asyncio.sleep(2 ** attempt)
                        resp = await client.post(
                            f"{base_url}/chat/completions",
                            headers={"Authorization": f"Bearer {api_key}"},
                            json={
                                "model": review_model,
                                "messages": [
                                    {"role": "system", "content": "你是专业电影字幕校审员。只输出 JSON 数组。"},
                                    {"role": "user", "content": (
                                        f"以下是电影字幕英文原文和初译中文，共 {len(zh_lines)} 行，请逐行校审改善译文。\n"
                                        "要求：纠正错译、统一术语、简洁口语化、符合字幕长度。\n"
                                        f"只返回 JSON 字符串数组，行数必须恰好为 {len(zh_lines)}，不要序号不要解释。\n\n"
                                        f"{payload}"
                                    )},
                                ],
                            },
                            timeout=120.0,
                        )
                        resp.raise_for_status()
                        content = resp.json()["choices"][0]["message"]["content"]
                        m = re.search(r"\[.*\]", content, re.DOTALL)
                        parsed = json.loads(m.group(0) if m else content)
                        if not isinstance(parsed, list):
                            raise ValueError("response is not a list")
                        last_parsed = parsed
                        if len(parsed) == len(zh_lines):
                            reviewed[chunk_idx] = [str(r).strip() or orig for r, orig in zip(parsed, zh_lines)]
                            return
                        raise ValueError(f"count mismatch: {len(parsed)} vs {len(zh_lines)}")
                    except Exception as e:
                        if attempt == 2:
                            log_event("warn", f"subtitle: review chunk {chunk_idx} failed: {repr(e)}", imdb_id)
                            if last_parsed:
                                reviewed[chunk_idx] = [
                                    str(last_parsed[i]).strip() if i < len(last_parsed) and str(last_parsed[i]).strip() else zh_lines[i]
                                    for i in range(len(zh_lines))
                                ]
                            else:
                                reviewed[chunk_idx] = zh_lines

        async with httpx.AsyncClient(proxy=proxy, timeout=httpx.Timeout(120.0, connect=10.0)) as client:
            review_tasks = [_review_chunk(client, i, chunk) for i, chunk in enumerate(review_chunks)]
            for coro in asyncio.as_completed(review_tasks):
                await coro

        # Merge reviewed lines back into out_captions
        out_captions = []
        for i, chunk in enumerate(review_chunks):
            improved = reviewed.get(i, [c[1][2] for c in chunk])
            for (orig_cap, _), zh in zip(chunk, improved):
                out_captions.append((orig_cap[0], orig_cap[1], zh))
        log_event("info", f"subtitle: review complete ({len(out_captions)} lines)", imdb_id)

    out_path = en_srt.with_name(f"{video_path.stem}.zh.srt")
    _write_srt(out_path, out_captions)
    log_event("info", f"subtitle: translated -> {out_path.name} ({len(out_captions)} lines)", imdb_id)
    return out_path

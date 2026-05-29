"""Chinese subtitle: MKV embedded Chinese → AI translate English fallback."""
from __future__ import annotations
import asyncio
import json
import re
from pathlib import Path
from typing import Callable, Optional

from config import settings
from store import log_event


# ────────────────────────────────────────────────────────────────
# Public entry point
# ────────────────────────────────────────────────────────────────
async def fetch_for_video(
    video_path: Path,
    imdb_id: str,
    on_progress: Optional[Callable[[str], None]] = None,
) -> Optional[Path]:
    """Return a Chinese subtitle file path, or None."""
    if not video_path.exists():
        log_event("warn", f"subtitle: video missing {video_path}", imdb_id)
        return None

    # Step 1: check if Chinese sub already exists alongside the video
    bundled = _find_existing_chs(video_path.parent)
    if bundled:
        log_event("info", f"subtitle: using existing CHS sub {bundled.name}", imdb_id)
        return bundled

    # Step 2: extract from MKV — Chinese directly, or English for translation
    await asyncio.to_thread(extract_subs_from_mkv, video_path, imdb_id)

    # Check again after MKV extraction (may have found Chinese)
    bundled = _find_existing_chs(video_path.parent)
    if bundled:
        return bundled

    # Step 3: translate extracted English SRT → Chinese
    if settings.TRANS_ENABLED:
        eng = _find_english_srt(video_path.parent)
        if eng:
            captions = _parse_srt(eng)
            n_lines = len(captions)
            n_batches = max(1, (n_lines + settings.TRANS_BATCH_SIZE - 1) // settings.TRANS_BATCH_SIZE)
            log_event("info", f"subtitle: translating {eng.name} → zh", imdb_id)
            if on_progress:
                on_progress(f"AI 翻译字幕 · {n_lines} 行 / {n_batches} 批")
            return await _translate_srt(eng, video_path, imdb_id, on_progress=on_progress)

    return None


# ────────────────────────────────────────────────────────────────
# P0 helpers: detect existing Chinese subtitle files
# ────────────────────────────────────────────────────────────────
_CHS_KEYWORDS = re.compile(
    r"(chinese[._\-]?(simplified|simp|chs|zhs|zh[-_]s|sc)|"
    r"简体|chs|zhs|zh[-_]s|\bsc\b|chinese)",
    re.I,
)
_ENG_KEYWORDS = re.compile(
    r"(english|eng\b|en\b)",
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


def _find_english_srt(directory: Path) -> Optional[Path]:
    """Find the best English subtitle in directory (prefer .srt)."""
    if not directory.is_dir():
        return None
    # Prefer MKV-extracted English srt
    for f in sorted(directory.iterdir()):
        if f.name.endswith(".extracted_eng.srt"):
            return f
    candidates = []
    for f in sorted(directory.iterdir()):
        if f.suffix.lower() in _SUB_EXTS:
            stem = f.stem.lower()
            if _ENG_KEYWORDS.search(stem):
                candidates.append(f)
    # Prefer .srt over .ass
    for f in candidates:
        if f.suffix.lower() == ".srt":
            return f
    return candidates[0] if candidates else None


def extract_bundled_subs(torrent_root: Path, dest_dir: Path) -> list[Path]:
    """
    Called from organize_to_library to extract subtitle files from the
    torrent folder (including Subs/ subfolder) into dest_dir.

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
# P0.5: extract embedded subtitles from MKV
# ────────────────────────────────────────────────────────────────
_CHS_LANG = re.compile(r"(chi.*simpl|zho.*simpl|chs|zh.*hans)", re.I)
_CHT_LANG = re.compile(r"(chi|zho|chinese)", re.I)
_ENG_LANG  = re.compile(r"(eng)", re.I)

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
    # Timeout: 120s base + 1s per 20MB (handles 3h+ movies at slow extraction speed)
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
    Extract embedded subtitles from MKV.
    Priority: CHS simplified > eng (stored for AI translation).
    Returns CHS path if found directly, else None (eng stored as .extracted_eng.srt).
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

    # Try CHS simplified directly
    if chs_idx is not None:
        out = video_path.parent / f"{stem}.zh.srt"
        if _ffmpeg_extract(video_path, chs_idx, out):
            log_event("info", f"subtitle: extracted CHS from MKV stream {chs_idx}", imdb_id)
            return out

    # 2. Traditional Chinese — use directly, no translation needed
    if cht_idx is not None:
        out = video_path.parent / f"{stem}.zh.srt"
        if _ffmpeg_extract(video_path, cht_idx, out):
            log_event("info", f"subtitle: extracted CHT from MKV stream {cht_idx}", imdb_id)
            return out

    # 3. Store English for AI translation (P2 picks it up via _find_english_srt)
    if eng_idx is not None:
        eng_out = video_path.parent / f"{stem}.extracted_eng.srt"
        if _ffmpeg_extract(video_path, eng_idx, eng_out):
            log_event("info", f"subtitle: extracted ENG from MKV stream {eng_idx} for translation", imdb_id)
    return None


# ────────────────────────────────────────────────────────────────
# P2: AI translation (VSM approach, simplified for subtitle-only use)
# ────────────────────────────────────────────────────────────────
def _parse_srt(path: Path) -> list[tuple[str, str, str]]:
    """Parse SRT → list of (index_str, timing_str, text_str)."""
    text = path.read_text(encoding="utf-8", errors="replace")
    blocks = re.split(r"\n{2,}", text.strip())
    captions = []
    for block in blocks:
        lines = block.strip().splitlines()
        if len(lines) >= 3:
            idx = lines[0].strip()
            timing = lines[1].strip()
            body = " ".join(l.strip() for l in lines[2:] if l.strip())
            body = re.sub(r"<[^>]+>", "", body)  # strip HTML tags
            if body:
                captions.append((idx, timing, body))
    return captions


def _write_srt(path: Path, captions: list[tuple[str, str, str]]) -> None:
    blocks = []
    for idx, timing, text in captions:
        blocks.append(f"{idx}\n{timing}\n{text}")
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")


async def _translate_batch(
    client,
    texts: list[str],
    context: list[str] | None = None,
) -> list[str]:
    """Translate a batch of subtitle lines to Simplified Chinese."""
    prompt = (
        "Translate the following movie subtitle lines into Simplified Chinese.\n"
        "Rules:\n"
        "1. Return ONLY a JSON array of translated strings, same count as input.\n"
        "2. Keep translations natural and colloquial, suitable for subtitles.\n"
        "3. Preserve names, technical terms, and numbers.\n"
        "4. Keep each line concise — subtitle timing is fixed.\n"
        "5. NO explanations, NO extra text outside the JSON array."
    )
    if context:
        prompt += f"\n\nPrevious lines for context (do NOT include in output):\n{json.dumps(context, ensure_ascii=False)}"

    import httpx
    for attempt in range(3):
        try:
            if attempt:
                await asyncio.sleep(2 ** attempt)
            resp = await client.post(
                f"{settings.TRANS_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {settings.TRANS_API_KEY}"},
                json={
                    "model": settings.TRANS_MODEL,
                    "messages": [
                        {"role": "system", "content": "You are a professional movie subtitle translator. Output only JSON arrays."},
                        {"role": "user", "content": f"{prompt}\n\nINPUT:\n{json.dumps(texts, ensure_ascii=False)}"},
                    ],
                },
                timeout=60.0,
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            m = re.search(r"\[.*\]", content, re.DOTALL)
            result = json.loads(m.group(0) if m else content)
            if isinstance(result, list) and len(result) == len(texts):
                return [str(r).strip() or orig for r, orig in zip(result, texts)]
            raise ValueError(f"count mismatch: got {len(result)}, need {len(texts)}")
        except Exception as e:
            if attempt == 2:
                log_event("warn", f"translate batch failed: {repr(e)}")
                return texts  # return originals on total failure
    return texts


async def _translate_srt(
    eng_srt: Path,
    video_path: Path,
    imdb_id: str,
    on_progress: Optional[Callable[[str], None]] = None,
) -> Optional[Path]:
    """Translate English .srt to Chinese .srt and save alongside video."""
    try:
        import httpx
        captions = _parse_srt(eng_srt)
        if not captions:
            return None

        batch_size = settings.TRANS_BATCH_SIZE

        batches = [captions[i:i+batch_size] for i in range(0, len(captions), batch_size)]
        translated_texts: dict[int, list[str]] = {}
        total = len(batches)
        # Milestones at 25%, 50%, 75%
        milestones = {max(1, total * p // 100) for p in (25, 50, 75)}

        proxy = settings.YTS_API_PROXY or None
        async with httpx.AsyncClient(proxy=proxy, timeout=httpx.Timeout(60.0, connect=10.0)) as client:
            for batch_idx, batch in enumerate(batches):
                texts = [c[2] for c in batch]
                ctx = translated_texts[batch_idx - 1][-3:] if batch_idx > 0 else None
                result = await _translate_batch(client, texts, ctx)
                translated_texts[batch_idx] = result
                if on_progress and (batch_idx + 1) in milestones:
                    pct = (batch_idx + 1) * 100 // total
                    on_progress(f"翻译进度 {pct}% ({batch_idx + 1}/{total} 批)")

        # Rebuild captions with translated text
        out_captions = []
        for batch_idx, batch in enumerate(batches):
            zh_texts = translated_texts.get(batch_idx, [c[2] for c in batch])
            for (idx, timing, _orig), zh_text in zip(batch, zh_texts):
                out_captions.append((idx, timing, zh_text))

        # Save as video_stem.zh.srt
        out_path = video_path.with_name(f"{video_path.stem}.zh.srt")
        _write_srt(out_path, out_captions)
        log_event("info", f"subtitle: AI translated → {out_path.name} ({len(out_captions)} lines)", imdb_id)
        return out_path

    except Exception as e:
        log_event("error", f"subtitle translate: {repr(e)}", imdb_id)
        return None



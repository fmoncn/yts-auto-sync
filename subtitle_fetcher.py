"""Chinese subtitle fetcher: subliminal + subdl.com + zimuku (concurrent race)."""
from __future__ import annotations
import asyncio
import os
import re
import zipfile
from pathlib import Path
from typing import Optional

from config import settings
from store import log_event


async def fetch_for_video(video_path: Path, imdb_id: str) -> Optional[Path]:
    """Race multiple subtitle sources; return first successful .srt/.ass path."""
    if not video_path.exists():
        log_event("warn", f"subtitle: video missing {video_path}", imdb_id)
        return None

    tasks = [
        asyncio.create_task(_try_subliminal(video_path, imdb_id)),
        asyncio.create_task(_try_subdl(video_path, imdb_id)),
        asyncio.create_task(_try_zimuku(video_path, imdb_id)),
    ]
    result = None
    try:
        for fut in asyncio.as_completed(tasks):
            res = await fut
            if res:
                result = res
                break
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
    return result


# ────────────────────────────────────────────────────────────────
# 1. subliminal
# ────────────────────────────────────────────────────────────────
async def _try_subliminal(video_path: Path, imdb_id: str) -> Optional[Path]:
    def _run() -> Optional[Path]:
        try:
            from babelfish import Language
            from subliminal import Video, download_best_subtitles, save_subtitles, region
        except Exception as e:
            log_event("warn", f"subliminal import: {e}", imdb_id)
            return None

        if not region.is_configured:
            region.configure("dogpile.cache.dbm", arguments={"filename": str(settings.data_dir / "sub_cache.dbm")})

        try:
            video = Video.fromname(video_path.name)
            video.imdb_id = imdb_id
        except Exception as e:
            log_event("warn", f"subliminal Video parse: {e}", imdb_id)
            return None

        langs = set()
        for code in settings.sub_langs:
            try:
                langs.add(Language(code))
            except Exception:
                pass
        if not langs:
            from babelfish import Language as L
            langs.add(L("zho"))

        provider_configs = {}
        if settings.OPENSUBTITLES_API_KEY:
            provider_configs["opensubtitlescom"] = {
                "api_key": settings.OPENSUBTITLES_API_KEY,
                "username": settings.OPENSUBTITLES_USERNAME or None,
                "password": settings.OPENSUBTITLES_PASSWORD or None,
            }

        # providers=None means use all; providers=[] means use none — avoid empty list
        providers = list(provider_configs.keys()) if provider_configs else None

        try:
            best = download_best_subtitles(
                [video], langs,
                providers=providers,
                provider_configs=provider_configs if provider_configs else None,
            )
        except Exception as e:
            log_event("warn", f"subliminal download: {repr(e)}", imdb_id)
            return None

        subs = best.get(video) or []
        if not subs:
            return None
        save_subtitles(video, subs, directory=str(video_path.parent), single=False)
        for srt in video_path.parent.glob(f"{video_path.stem}.*.srt"):
            return srt
        return None

    return await asyncio.to_thread(_run)


# ────────────────────────────────────────────────────────────────
# 2. subdl.com (free JSON API, good Chinese coverage)
# ────────────────────────────────────────────────────────────────
_SUBDL_BASE = "https://api.subdl.com/api/v1"


async def _try_subdl(video_path: Path, imdb_id: str) -> Optional[Path]:
    import httpx
    from store import get_movie

    proxies = {"https://": settings.SUB_PROXY, "http://": settings.SUB_PROXY} if settings.SUB_PROXY else {}
    title = _clean_title(video_path.stem)

    def _do() -> Optional[Path]:
        try:
            params: dict = {"languages": "ZH", "subs_per_page": 5}
            if re.match(r"^tt\d+$", imdb_id):
                params["imdb_id"] = imdb_id
            else:
                params["film_name"] = title
                params["type"] = "movie"
            if settings.SUBDL_API_KEY:
                params["api_key"] = settings.SUBDL_API_KEY

            with httpx.Client(timeout=15, proxy=settings.SUB_PROXY or None) as cli:
                r = cli.get(f"{_SUBDL_BASE}/subtitles", params=params)
                r.raise_for_status()
                data = r.json()

            subs = data.get("subtitles") or []
            if not subs:
                return None

            # Prefer full_season=false, hi=false
            subs.sort(key=lambda s: (s.get("full_season", True), s.get("hi", True)))
            dl_url = subs[0].get("url") or subs[0].get("download_url")
            if not dl_url:
                return None
            if not dl_url.startswith("http"):
                dl_url = "https://dl.subdl.com" + dl_url

            with httpx.Client(timeout=30, proxy=settings.SUB_PROXY or None, follow_redirects=True) as cli:
                r2 = cli.get(dl_url)
                r2.raise_for_status()

            tmp = video_path.parent / (video_path.stem + ".subdl.zip")
            tmp.write_bytes(r2.content)
            return _extract_srt(tmp, video_path)
        except Exception as e:
            log_event("warn", f"subdl error: {repr(e)}", imdb_id)
            return None

    return await asyncio.to_thread(_do)


# ────────────────────────────────────────────────────────────────
# 3. zimuku scraping (fallback)
# ────────────────────────────────────────────────────────────────
_ZIMUKU_BASE = "https://zmk.pw"


async def _try_zimuku(video_path: Path, imdb_id: str) -> Optional[Path]:
    try:
        from curl_cffi import requests as cc
    except Exception:
        return None

    title = _clean_title(video_path.stem)
    proxies = {"https": settings.SUB_PROXY, "http": settings.SUB_PROXY} if settings.SUB_PROXY else None

    def _do() -> Optional[Path]:
        try:
            r = cc.get(
                f"{_ZIMUKU_BASE}/search",
                params={"q": title},
                impersonate="chrome120",
                timeout=15,
                proxies=proxies,
            )
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(r.text, "lxml")
            link = soup.select_one("div.persub a, a.title")
            if not link:
                return None
            detail_url = link.get("href", "")
            if not detail_url.startswith("http"):
                detail_url = _ZIMUKU_BASE + detail_url

            r2 = cc.get(detail_url, impersonate="chrome120", timeout=15, proxies=proxies)
            soup2 = BeautifulSoup(r2.text, "lxml")
            dl = soup2.select_one("li.dlsub a, a.btn-danger, a.download")
            if not dl:
                return None
            file_url = dl.get("href", "")
            if not file_url.startswith("http"):
                file_url = _ZIMUKU_BASE + file_url

            r3 = cc.get(file_url, impersonate="chrome120", timeout=30, proxies=proxies)
            tmp = video_path.parent / (video_path.stem + ".zimuku.zip")
            tmp.write_bytes(r3.content)
            return _extract_srt(tmp, video_path)
        except Exception as e:
            log_event("warn", f"zimuku error: {repr(e)}", imdb_id)
            return None

    return await asyncio.to_thread(_do)


def _extract_srt(zip_path: Path, video_path: Path) -> Optional[Path]:
    try:
        with zipfile.ZipFile(zip_path) as z:
            srt = next((n for n in z.namelist() if n.lower().endswith((".srt", ".ass"))), None)
            if not srt:
                return None
            out = video_path.with_name(f"{video_path.stem}.zh{Path(srt).suffix.lower()}")
            with z.open(srt) as f, open(out, "wb") as g:
                g.write(f.read())
        try:
            zip_path.unlink()
        except OSError:
            pass
        return out
    except Exception:
        return None


def _clean_title(stem: str) -> str:
    s = re.sub(
        r"\b(1080p|720p|2160p|x264|x265|h264|h265|bluray|webrip|web-dl|hdrip|yify|yts[.\w]*)\b",
        "", stem, flags=re.I
    )
    s = re.sub(r"[._]+", " ", s)
    return s.strip()

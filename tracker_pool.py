"""Public tracker list fetcher (ngosang/trackerslist)."""
import asyncio
import time
from typing import List

import httpx

from config import settings

_cache: List[str] = []
_fetched_at: float = 0.0
_lock = asyncio.Lock()


async def get_trackers(force: bool = False) -> List[str]:
    global _cache, _fetched_at
    ttl = settings.TRACKERS_REFRESH_HOURS * 3600
    cache_file = settings.data_dir / "trackers.txt"
    
    async with _lock:
        if not force and _cache and (time.time() - _fetched_at) < ttl:
            return _cache
            
        # Try loading from local file if memory cache is empty
        if not force and not _cache and cache_file.exists():
            try:
                content = cache_file.read_text(encoding="utf-8")
                trackers = [
                    line.strip()
                    for line in content.splitlines()
                    if line.strip() and not line.startswith("#")
                ]
                if trackers:
                    _cache = trackers
                    _fetched_at = cache_file.stat().st_mtime
                    if (time.time() - _fetched_at) < ttl:
                        return _cache
            except Exception:
                pass
                
        try:
            async with httpx.AsyncClient(timeout=15) as cli:
                r = await cli.get(settings.TRACKERS_URL)
                r.raise_for_status()
                trackers = [
                    line.strip()
                    for line in r.text.splitlines()
                    if line.strip() and not line.startswith("#")
                ]
            if trackers:
                _cache = trackers
                _fetched_at = time.time()
                try:
                    cache_file.write_text("\n".join(trackers), encoding="utf-8")
                except Exception:
                    pass
        except Exception:
            if not _cache:
                _cache = _FALLBACK
        return _cache


def augment_magnet(magnet: str, trackers: List[str]) -> str:
    """Append trackers to a magnet URI if not already present."""
    if not trackers or "magnet:" not in magnet:
        return magnet
    existing = magnet
    extras = []
    for t in trackers:
        if t and t not in existing:
            extras.append("&tr=" + httpx.QueryParams({"x": t})["x"].replace("+", "%20"))
    return existing + "".join(extras)


_FALLBACK = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.demonii.com:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://exodus.desync.com:6969/announce",
]

"""qBittorrent Web API thin wrapper."""
from __future__ import annotations
import asyncio
from typing import Iterable, Optional

import qbittorrentapi

from config import settings
from store import log_event


_client: Optional[qbittorrentapi.Client] = None


def client() -> qbittorrentapi.Client:
    global _client
    if _client is None:
        _client = qbittorrentapi.Client(
            host=settings.QBIT_URL,
            username=settings.QBIT_USER,
            password=settings.QBIT_PASS,
            REQUESTS_ARGS={"timeout": (5, 30)},
        )
    return _client


def _ensure_login() -> None:
    c = client()
    try:
        c.auth_log_in()
    except qbittorrentapi.LoginFailed as e:
        log_event("error", f"qBit login failed: {e}")
        raise


def ensure_category() -> None:
    _ensure_login()
    c = client()
    cats = c.torrents_categories() or {}
    if settings.QBIT_CATEGORY not in cats:
        c.torrents_create_category(
            name=settings.QBIT_CATEGORY,
            save_path=settings.QBIT_SAVE_PATH,
        )


def _hash_from_magnet(magnet: str) -> Optional[str]:
    import re
    m = re.search(r"btih:([a-fA-F0-9]{40})", magnet)
    return m.group(1).lower() if m else None


def add_magnet(magnet: str, imdb_id: str, name: str) -> Optional[str]:
    """Add a magnet to qBit. Returns torrent hash."""
    _ensure_login()
    ensure_category()
    c = client()
    tags = f"yts,imdb:{imdb_id}"
    h = _hash_from_magnet(magnet)
    res = c.torrents_add(
        urls=magnet,
        category=settings.QBIT_CATEGORY,
        save_path=settings.QBIT_SAVE_PATH,
        tags=tags,
        use_auto_torrent_management=False,
        is_paused=False,
    )
    # qbittorrent-api: old API returns "Ok.", new API returns TorrentsAddedMetadata
    ok = (
        res == "Ok."
        or (hasattr(res, "success_count") and (res.success_count or len(getattr(res, "added_torrent_ids", []) or []) > 0))
    )
    if not ok:
        log_event("error", f"qBit add returned: {res!r}", imdb_id)
        return None
    if h:
        return h
    added = getattr(res, "added_torrent_ids", None)
    if added:
        return added[0].lower()
    for t in c.torrents_info(category=settings.QBIT_CATEGORY, tag=f"imdb:{imdb_id}"):
        return t.hash
    return None


def torrent_info(qbit_hash: str) -> Optional[dict]:
    _ensure_login()
    arr = client().torrents_info(torrent_hashes=qbit_hash)
    return _row(arr[0]) if arr else None


def stop_torrent(qbit_hash: str) -> None:
    _ensure_login()
    c = client()
    try:
        c.torrents_stop(torrent_hashes=qbit_hash)
    except Exception:
        c.torrents_pause(torrent_hashes=qbit_hash)


def remove_torrent(qbit_hash: str, delete_files: bool = False) -> None:
    _ensure_login()
    client().torrents_delete(delete_files=delete_files, torrent_hashes=qbit_hash)


def _row(t) -> dict:
    return {
        "hash": t.hash,
        "name": t.name,
        "state": t.state,
        "progress": float(t.progress),
        "dlspeed": int(t.dlspeed),
        "upspeed": int(t.upspeed),
        "eta": int(t.eta),
        "size": int(t.size),
        "downloaded": int(t.downloaded),
        "save_path": t.save_path,
        "content_path": getattr(t, "content_path", t.save_path),
        "num_seeds": int(t.num_seeds),
        "num_leechs": int(t.num_leechs),
        "ratio": float(t.ratio),
    }


def list_yts_torrents() -> list[dict]:
    _ensure_login()
    return [_row(t) for t in client().torrents_info(category=settings.QBIT_CATEGORY)]


def push_trackers(qbit_hash: str, trackers: Iterable[str]) -> None:
    _ensure_login()
    urls = "\n".join(trackers)
    if urls:
        client().torrents_add_trackers(torrent_hash=qbit_hash, urls=urls)


async def health() -> dict:
    def _h():
        try:
            _ensure_login()
            return {"ok": True, "version": client().app_version()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    return await asyncio.to_thread(_h)

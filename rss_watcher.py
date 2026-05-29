"""YTS RSS background poller + YTS API metadata enrichment."""
from __future__ import annotations
import asyncio
import re
import time
import traceback
from typing import Optional

import feedparser
import httpx

from config import settings
from store import upsert_movie, update_movie, log_event, get_movie
import tracker_pool
import qbit_client


_IMDB_RE = re.compile(r"tt\d{7,9}")
_HASH_RE = re.compile(r"btih:([a-fA-F0-9]{40})")
_HASH_URL_RE = re.compile(r"([A-Fa-f0-9]{40})")
_POSTER_RE = re.compile(r'<img[^>]+src="([^"]+)"', re.I)
_SIZE_RE = re.compile(r"Size:\s*([0-9.]+)\s*(GB|MB)", re.I)
_RATING_RE = re.compile(r"Rating:\s*([0-9.]+)", re.I)
_GENRE_RE = re.compile(r"Genre:\s*([^<\n]+)", re.I)
_YEAR_RE = re.compile(r"\((\d{4})\)")
_QUALITY_RE = re.compile(r"\b(720p|1080p|2160p|3D)\b", re.I)

_subscribers: list[asyncio.Queue] = []


def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=200)
    _subscribers.append(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    try:
        _subscribers.remove(q)
    except ValueError:
        pass


def _publish(event: dict) -> None:
    for q in list(_subscribers):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass


async def _fetch_rss(quality: str) -> list[dict]:
    url = settings.YTS_RSS_URL.format(quality=quality)
    proxy = settings.YTS_RSS_PROXY or None
    async with httpx.AsyncClient(timeout=20, proxy=proxy, follow_redirects=True) as cli:
        r = await cli.get(url, headers={"User-Agent": "Mozilla/5.0 yts-auto-sync"})
        r.raise_for_status()
        feed = feedparser.parse(r.text)

    items = []
    for entry in feed.entries:
        desc = (entry.get("description") or "") + " " + (entry.get("summary") or "")
        title = entry.get("title", "")
        link = entry.get("link", "")
        magnet = ""
        info_hash = ""
        enclosure_url = ""
        for enc in entry.get("enclosures", []) or []:
            href = enc.get("href", "") or enc.get("url", "")
            if href.startswith("magnet:"):
                magnet = href
                break
            if href and not enclosure_url:
                enclosure_url = href
        if not magnet:
            for k in ("torrent_magneturi", "magneturi"):
                if entry.get(k):
                    magnet = entry[k]
                    break
        m = _HASH_RE.search(magnet) or _HASH_URL_RE.search(enclosure_url)
        if m:
            info_hash = m.group(1).lower()
        if not magnet and info_hash:
            import urllib.parse
            magnet = (
                f"magnet:?xt=urn:btih:{info_hash}"
                f"&dn={urllib.parse.quote(title)}"
            )

        imdb_match = (
            _IMDB_RE.search(link)
            or _IMDB_RE.search(desc)
            or _IMDB_RE.search(entry.get("id", "") or "")
        )
        if imdb_match:
            imdb_id = imdb_match.group(0)
        elif info_hash:
            imdb_id = info_hash
        else:
            imdb_id = link.rstrip("/").rsplit("/", 1)[-1] or link

        poster_url = ""
        pm = _POSTER_RE.search(desc)
        if pm:
            poster_url = pm.group(1)

        size_bytes = 0
        sm = _SIZE_RE.search(desc)
        if sm:
            n = float(sm.group(1))
            size_bytes = int(n * (1024**3 if sm.group(2).upper() == "GB" else 1024**2))

        rating = 0.0
        rm = _RATING_RE.search(desc)
        if rm:
            try:
                rating = float(rm.group(1))
            except ValueError:
                pass

        ym = _YEAR_RE.search(title)
        year = int(ym.group(1)) if ym else 0
        qm = _QUALITY_RE.search(title) or _QUALITY_RE.search(desc)
        q_str = (qm.group(1) if qm else quality).lower()

        genres = ""
        gm = _GENRE_RE.search(desc)
        if gm:
            genres = gm.group(1).strip()

        pub_t = 0
        if entry.get("published_parsed"):
            pub_t = int(time.mktime(entry.published_parsed))

        items.append(dict(
            imdb_id=imdb_id,
            title=title,
            year=year,
            quality=q_str,
            size_bytes=size_bytes,
            rating=rating,
            genres=genres,
            poster_url=poster_url,
            synopsis=None,
            imdb_url=None,
            magnet=magnet,
            info_hash=info_hash,
            yts_url=link,
            rss_pub_at=pub_t,
            added_at=int(time.time()),
            status="discovered",
            qbit_hash=None,
            save_path=None,
            final_video=None,
            subtitle_path=None,
            subtitle_status="pending",
            note=None,
        ))
    return items


async def _enrich_from_yts_api(movie: dict) -> None:
    """Call YTS API to fill in real IMDB ID, better poster, synopsis, genres."""
    if not movie.get("title"):
        return
    proxy = settings.YTS_API_PROXY or None
    try:
        params: dict = {}
        # Prefer lookup by existing IMDB ID
        if re.match(r"^tt\d+$", movie.get("imdb_id", "")):
            params["imdb_id"] = movie["imdb_id"]
        else:
            # Search by title + year
            params["query_term"] = movie["title"]
            if movie.get("year"):
                params["query_term"] += f" {movie['year']}"
            params["limit"] = 1

        endpoint = (
            f"{settings.YTS_API_URL}/movie_details.json"
            if "imdb_id" in params
            else f"{settings.YTS_API_URL}/list_movies.json"
        )
        async with httpx.AsyncClient(timeout=15, proxy=proxy, follow_redirects=True) as cli:
            r = await cli.get(endpoint, params=params)
            r.raise_for_status()
            data = r.json()

        # Unify movie record from both endpoints
        if "imdb_id" in params:
            rec = data.get("data", {}).get("movie")
        else:
            movies_list = data.get("data", {}).get("movies") or []
            rec = movies_list[0] if movies_list else None

        if not rec:
            return

        patch: dict = {}
        real_imdb = rec.get("imdb_code") or rec.get("imdb_id") or ""
        if real_imdb and real_imdb != movie["imdb_id"]:
            # Only update supplementary fields; keep original imdb_id as DB key
            patch["imdb_url"] = f"https://www.imdb.com/title/{real_imdb}/"

        if rec.get("large_cover_image"):
            patch["poster_url"] = rec["large_cover_image"]
        elif rec.get("medium_cover_image"):
            patch["poster_url"] = rec["medium_cover_image"]

        if rec.get("description_full"):
            patch["synopsis"] = rec["description_full"][:800]
        elif rec.get("synopsis"):
            patch["synopsis"] = rec["synopsis"][:800]

        if rec.get("rating") and not movie.get("rating"):
            patch["rating"] = float(rec["rating"])

        if rec.get("genres") and not movie.get("genres"):
            patch["genres"] = ", ".join(rec["genres"])

        if patch:
            update_movie(movie["imdb_id"], **patch)
            movie.update(patch)
    except Exception as e:
        log_event("warn", f"YTS API enrich failed for {movie.get('title')}: {repr(e)}", movie["imdb_id"])


def _should_auto_download(m: dict) -> tuple[bool, str]:
    if not settings.AUTO_DOWNLOAD:
        return False, "auto disabled"
    if settings.MIN_IMDB_RATING and m.get("rating") and m["rating"] < settings.MIN_IMDB_RATING:
        return False, f"rating {m['rating']} < {settings.MIN_IMDB_RATING}"
    if settings.MAX_SIZE_GB and m.get("size_bytes"):
        gb = m["size_bytes"] / (1024**3)
        if gb > settings.MAX_SIZE_GB:
            return False, f"size {gb:.1f}G > {settings.MAX_SIZE_GB}G"
    return True, "ok"


async def _enqueue_to_qbit(m: dict) -> None:
    trackers = await tracker_pool.get_trackers()
    magnet = tracker_pool.augment_magnet(m["magnet"], trackers)
    try:
        h = await asyncio.to_thread(
            qbit_client.add_magnet, magnet, m["imdb_id"], m["title"]
        )
    except Exception as e:
        update_movie(m["imdb_id"], status="error", note=f"qBit add failed: {e}")
        log_event("error", f"qBit add {m['imdb_id']}: {repr(e)}", m["imdb_id"])
        _publish({"type": "movie.updated", "imdb_id": m["imdb_id"], "status": "error"})
        return
    update_movie(m["imdb_id"], status="downloading", qbit_hash=h)
    log_event("info", f"queued {m['title']}", m["imdb_id"])
    _publish({"type": "movie.queued", "imdb_id": m["imdb_id"], "qbit_hash": h})


async def poll_once() -> dict:
    summary = {"fetched": 0, "new": 0, "queued": 0, "errors": []}
    for quality in settings.qualities:
        items = None
        for _attempt in range(2):
            try:
                items = await _fetch_rss(quality)
                break
            except Exception as e:
                if _attempt == 1:
                    err_msg = f"{quality}: {repr(e)}"
                    summary["errors"].append(err_msg)
                    log_event("error", f"RSS fetch {err_msg}\n{traceback.format_exc()[-300:]}")
                else:
                    await asyncio.sleep(15)
        if items is None:
            continue
        summary["fetched"] += len(items)
        for m in items:
            is_new = upsert_movie(m)
            if is_new:
                summary["new"] += 1
                _publish({"type": "movie.new", "movie": m})
                log_event("info", f"new RSS item: {m['title']}", m["imdb_id"])
                # Enrich metadata from YTS API asynchronously
                asyncio.create_task(_enrich_from_yts_api(m))
                allow, reason = _should_auto_download(m)
                if allow:
                    await _enqueue_to_qbit(m)
                    summary["queued"] += 1
                else:
                    update_movie(m["imdb_id"], status="skipped", note=reason)
    summary["ts"] = int(time.time())
    _publish({"type": "rss.polled", "summary": summary})
    return summary


async def run_watcher(stop_event: asyncio.Event) -> None:
    log_event("info", "RSS watcher started")
    while not stop_event.is_set():
        try:
            await poll_once()
        except Exception as e:
            log_event("error", f"watcher loop: {repr(e)}\n{traceback.format_exc()[-300:]}")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=settings.YTS_POLL_INTERVAL)
        except asyncio.TimeoutError:
            pass
    log_event("info", "RSS watcher stopped")

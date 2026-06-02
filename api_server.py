"""FastAPI app: REST + SSE for yts-auto-sync."""
from __future__ import annotations
import asyncio
import json
import os
import re
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from config import settings
from store import (
    list_movies, get_movie, update_movie, delete_movie, log_event,
    recent_events, find_by_hash, backup_db, wal_checkpoint, prune_events,
)
import rss_watcher
import qbit_client
import tracker_pool
import subtitle_fetcher
from subtitle_fetcher import extract_bundled_subs
import cloud_uploader
import notifier


STATIC_DIR = Path(__file__).parent / "static"
_stop_event: Optional[asyncio.Event] = None
_watcher_task: Optional[asyncio.Task] = None
_progress_task: Optional[asyncio.Task] = None
_backup_task: Optional[asyncio.Task] = None


# ──────────────────────────────────────────────────────────────────
# Auth
# ──────────────────────────────────────────────────────────────────
async def _check_token(authorization: Optional[str] = Header(None), token: Optional[str] = Query(None)) -> None:
    if not settings.API_TOKEN:
        return
    req_token = token
    if authorization and authorization.startswith("Bearer "):
        req_token = authorization[7:]
    if not req_token:
        raise HTTPException(401, "Missing token")
    if req_token != settings.API_TOKEN:
        raise HTTPException(403, "Invalid token")

_auth = Depends(_check_token)


# ──────────────────────────────────────────────────────────────────
# Lifespan
# ──────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _stop_event, _watcher_task, _progress_task, _backup_task
    _stop_event = asyncio.Event()
    _watcher_task = asyncio.create_task(rss_watcher.run_watcher(_stop_event))
    _progress_task = asyncio.create_task(_progress_loop(_stop_event))
    _backup_task = asyncio.create_task(_backup_loop(_stop_event))
    
    # Recover stuck subtitles
    stuck_movies = list_movies()
    recovered = 0
    for m in stuck_movies:
        if m.get("subtitle_status") in ("searching", "translating"):
            update_movie(m["imdb_id"], subtitle_status="pending")
            if m.get("final_video"):
                asyncio.create_task(_fetch_sub(m["imdb_id"], Path(m["final_video"])))
            recovered += 1
    if recovered > 0:
        log_event("info", f"recovered {recovered} stuck subtitle tasks")
        
    log_event("info", f"yts-auto-sync started on :{settings.YTS_PORT}")
    yield
    if _stop_event:
        _stop_event.set()
    for t in (_watcher_task, _progress_task, _backup_task):
        if t:
            try:
                await asyncio.wait_for(t, timeout=3)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass


app = FastAPI(title="YTS Auto Sync", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


# ──────────────────────────────────────────────────────────────────
# Background loops
# ──────────────────────────────────────────────────────────────────
async def _progress_loop(stop: asyncio.Event) -> None:
    """Poll qBit for downloading torrents; adaptive interval."""
    while not stop.is_set():
        try:
            try:
                torrents = await asyncio.to_thread(qbit_client.list_yts_torrents)
            except Exception as e:
                log_event("warn", f"qBit poll: {repr(e)}")
                torrents = []

            active = 0
            for t in torrents:
                if not t:
                    continue
                m = find_by_hash(t["hash"])
                if not m:
                    continue
                state = t["state"]
                new_status = m["status"]
                if state in ("downloading", "stalledDL", "metaDL", "queuedDL",
                             "checkingDL", "forcedDL", "allocating"):
                    new_status = "downloading"
                    active += 1
                elif state in ("uploading", "stalledUP", "queuedUP", "forcedUP"):
                    new_status = "seeding"
                elif state in ("pausedDL", "pausedUP"):
                    new_status = "paused"
                elif state == "error":
                    new_status = "error"

                if new_status != m["status"]:
                    update_movie(m["imdb_id"], status=new_status)

                rss_watcher._publish({
                    "type": "movie.progress",
                    "imdb_id": m["imdb_id"],
                    "state": state,
                    "progress": t["progress"],
                    "dlspeed": t["dlspeed"],
                    "upspeed": t["upspeed"],
                    "eta": t["eta"],
                    "num_seeds": t["num_seeds"],
                    "num_leechs": t["num_leechs"],
                    "status": new_status,
                })

                if t["progress"] >= 1.0 and m["status"] != "done":
                    update_movie(m["imdb_id"], status="done")
                    log_event("info", f"download done: {m['title']}", m["imdb_id"])
                    rss_watcher._publish({"type": "movie.done", "imdb_id": m["imdb_id"]})
                    asyncio.create_task(_post_complete(m, t))
                    asyncio.create_task(notifier.notify(
                        "下载完成",
                        f"{m['title']} ({m.get('year','')})\n{m.get('quality','')}"
                    ))

            # Clean up ghost downloading/seeding tasks that are no longer in qBit
            current_hashes = {t["hash"].lower() for t in torrents if t and "hash" in t}
            for status in ("downloading", "seeding"):
                for m in list_movies(status=status):
                    h = m.get("qbit_hash")
                    if h and h.lower() not in current_hashes:
                        update_movie(m["imdb_id"], status="error", note="Torrent was removed from qBittorrent")
                        log_event("warn", f"torrent {m['title']} was removed from qBit, marking status as error", m["imdb_id"])
                        rss_watcher._publish({"type": "movie.updated", "imdb_id": m["imdb_id"], "status": "error"})
        except Exception as e:
            log_event("error", f"progress loop: {repr(e)}")

        # Adaptive: fast poll when downloading, slow when idle
        interval = 5 if active > 0 else 30
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


async def _backup_loop(stop: asyncio.Event) -> None:
    """Daily DB backup."""
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=86400)
        except asyncio.TimeoutError:
            pass
        if not stop.is_set():
            await asyncio.to_thread(wal_checkpoint)
            pruned = await asyncio.to_thread(prune_events, 30)
            if pruned:
                log_event("info", f"pruned {pruned} old events")
            dest = await asyncio.to_thread(backup_db)
            if dest:
                log_event("info", f"DB backup: {dest.name}")


# ──────────────────────────────────────────────────────────────────
# Post-download processing
# ──────────────────────────────────────────────────────────────────
def _find_video(content: Path) -> Optional[Path]:
    exts = {".mp4", ".mkv", ".avi", ".mov", ".webm"}
    if content.is_file() and content.suffix.lower() in exts:
        return content
    if content.is_dir():
        candidates = sorted(
            (p for p in content.rglob("*") if p.suffix.lower() in exts and p.is_file()),
            key=lambda p: p.stat().st_size, reverse=True,
        )
        return candidates[0] if candidates else None
    return None


_BAD_NAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _clean_library_dirname(title: str, year: Optional[int]) -> str:
    name = re.sub(r"\s*\[[^\]]*\]\s*", " ", title or "").strip()
    if year and f"({year})" not in name:
        name = f"{name} ({year})"
    name = _BAD_NAME_RE.sub(" ", name)
    return re.sub(r"\s+", " ", name).strip() or "Unknown"


def organize_to_library(movie: dict, qbit_content_path: Optional[str]) -> Optional[Path]:
    if not qbit_content_path:
        return None
    src = Path(qbit_content_path)
    video = _find_video(src)
    if not video or not video.exists():
        log_event("warn", f"organize: no video in {src}", movie["imdb_id"])
        return None

    lib_root = Path(settings.LIBRARY_DIR)
    lib_root.mkdir(parents=True, exist_ok=True)
    dest_dir = lib_root / _clean_library_dirname(movie["title"], movie.get("year"))
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / video.name

    if dest.resolve() == video.resolve():
        return dest

    try:
        shutil.move(str(video), str(dest))
    except Exception as e:
        log_event("error", f"organize mv: {repr(e)}", movie["imdb_id"])
        return None

    # Extract ALL subtitle files from the torrent folder (incl. Subs/ subfolder)
    # YTS packs typically include 30+ language subs in a Subs/ directory
    torrent_root = src if src.is_dir() else src.parent
    extract_bundled_subs(torrent_root, dest_dir)

    log_event("info", f"organized → {dest}", movie["imdb_id"])
    return dest


async def _post_complete(movie: dict, qbit_torrent: dict) -> None:
    # Guard: re-fetch movie to check if already processed
    current = get_movie(movie["imdb_id"])
    if current and current.get("status") == "done" and current.get("final_video"):
        return  # already organized, skip duplicate trigger
    raw = qbit_torrent.get("content_path") or qbit_torrent.get("save_path", "")
    content = settings.host_path(raw)
    new_video: Optional[Path] = None
    if settings.AUTO_ORGANIZE:
        new_video = await asyncio.to_thread(organize_to_library, movie, content)

    if new_video:
        update_movie(movie["imdb_id"], final_video=str(new_video))
        rss_watcher._publish({"type": "movie.organized", "imdb_id": movie["imdb_id"], "path": str(new_video)})
    else:
        v = _find_video(Path(content)) if content else None
        if v:
            update_movie(movie["imdb_id"], final_video=str(v))
            new_video = v

    if settings.AUTO_SUBTITLE and new_video:
        await _fetch_sub(movie["imdb_id"], new_video)

    if settings.CLOUD_UPLOAD_ENABLED and new_video:
        asyncio.create_task(_upload_movie(movie["imdb_id"], new_video.parent))

    if settings.DELETE_QBIT_AFTER_ORGANIZE and movie.get("qbit_hash"):
        # Safety: only delete files from qBit if organize actually succeeded.
        # If organize failed (new_video is None), keep files so nothing is lost.
        organized_ok = new_video is not None
        try:
            await asyncio.to_thread(
                qbit_client.remove_torrent, movie["qbit_hash"],
                delete_files=organized_ok,
            )
            update_movie(movie["imdb_id"], qbit_hash=None)
            action = "files deleted" if organized_ok else "files kept (organize failed)"
            log_event("info", f"removed from qBit ({action})", movie["imdb_id"])
            rss_watcher._publish({"type": "movie.unseeded", "imdb_id": movie["imdb_id"]})
        except Exception as e:
            log_event("warn", f"qBit remove: {repr(e)}", movie["imdb_id"])


async def _upload_movie(imdb_id: str, movie_dir: Path) -> None:
    log_event("info", "upload: starting...", imdb_id)
    ok = await cloud_uploader.upload_movie(movie_dir, imdb_id)
    if ok:
        asyncio.create_task(notifier.notify("上传完成", movie_dir.name))


async def _fetch_sub(imdb_id: str, video_path: Path) -> None:
    update_movie(imdb_id, subtitle_status="searching")
    rss_watcher._publish({"type": "sub.searching", "imdb_id": imdb_id})

    def _progress(msg: str) -> None:
        rss_watcher._publish({"type": "sub.progress", "imdb_id": imdb_id, "msg": msg})

    try:
        srt = await subtitle_fetcher.fetch_for_video(video_path, imdb_id, on_progress=_progress)
        if srt:
            # .zh.srt -> ready Chinese; .en.srt -> English only, awaiting manual translation
            is_zh = srt.name.lower().endswith(".zh.srt")
            status = "zh" if is_zh else "en_only"
            update_movie(imdb_id, subtitle_status=status, subtitle_path=str(srt))
            log_event("info", f"subtitle [{status}]: {srt.name}", imdb_id)
            rss_watcher._publish({"type": "sub.found", "imdb_id": imdb_id, "path": str(srt), "kind": status})
            title = "中文字幕就位" if is_zh else "英文字幕就位 · 待手动翻译"
            asyncio.create_task(notifier.notify(title, f"{srt.name}"))
        else:
            update_movie(imdb_id, subtitle_status="no_subtitle")
            log_event("warn", "no subtitle found (embedded + subdl all missed)", imdb_id)
            rss_watcher._publish({"type": "sub.missing", "imdb_id": imdb_id})
    except Exception as e:
        update_movie(imdb_id, subtitle_status="error", note=str(e))
        log_event("error", f"subtitle: {repr(e)}", imdb_id)
        rss_watcher._publish({"type": "sub.error", "imdb_id": imdb_id, "error": str(e)})


# ──────────────────────────────────────────────────────────────────
# REST API
# ──────────────────────────────────────────────────────────────────
@app.get("/api/movies")
def api_movies(status: Optional[str] = Query(None), limit: int = 500):
    return {"movies": list_movies(status=status, limit=limit)}


@app.get("/api/movies/{imdb_id}")
def api_movie(imdb_id: str):
    m = get_movie(imdb_id)
    if not m:
        raise HTTPException(404, "not found")
    return m


@app.post("/api/movies/{imdb_id}/download", dependencies=[_auth])
async def api_download(imdb_id: str):
    m = get_movie(imdb_id)
    if not m:
        raise HTTPException(404, "not found")
    if not m.get("magnet"):
        raise HTTPException(400, "no magnet on record")
    await rss_watcher._enqueue_to_qbit(m)
    return {"ok": True}


@app.post("/api/movies/{imdb_id}/subtitle", dependencies=[_auth])
async def api_resub(imdb_id: str):
    m = get_movie(imdb_id)
    if not m or not m.get("final_video"):
        raise HTTPException(404, "no downloaded video")
    asyncio.create_task(_fetch_sub(imdb_id, Path(m["final_video"])))
    return {"ok": True}


@app.post("/api/movies/{imdb_id}/organize", dependencies=[_auth])
async def api_organize(imdb_id: str):
    m = get_movie(imdb_id)
    if not m:
        raise HTTPException(404, "not found")
    if not m.get("qbit_hash"):
        if m.get("final_video"):
            content = str(Path(m["final_video"]).parent)
        else:
            raise HTTPException(400, "no qbit hash and no final_video")
        t = {"content_path": content, "save_path": content}
    else:
        try:
            t = await asyncio.to_thread(qbit_client.torrent_info, m["qbit_hash"])
        except Exception as e:
            raise HTTPException(503, f"qBit: {e}")
        if not t:
            if m.get("save_path") or m.get("final_video"):
                content = m.get("save_path") or str(Path(m["final_video"]).parent)
                t = {"content_path": content, "save_path": content}
            else:
                raise HTTPException(404, "torrent gone from qBit and no fallback path")
    asyncio.create_task(_post_complete(m, t))
    return {"ok": True}


@app.delete("/api/movies/{imdb_id}", dependencies=[_auth])
def api_delete(imdb_id: str, delete_files: bool = False):
    m = get_movie(imdb_id)
    if not m:
        raise HTTPException(404, "not found")
    if m.get("qbit_hash"):
        try:
            qbit_client.remove_torrent(m["qbit_hash"], delete_files=delete_files)
        except Exception as e:
            log_event("warn", f"qBit remove: {repr(e)}", imdb_id)
    delete_movie(imdb_id)
    return {"ok": True}


@app.post("/api/rss/refresh", dependencies=[_auth])
async def api_refresh():
    return await rss_watcher.poll_once()


@app.get("/api/qbit/health")
async def api_qbit_health():
    return await qbit_client.health()


@app.get("/api/qbit/torrents")
def api_qbit_torrents():
    try:
        return {"torrents": qbit_client.list_yts_torrents()}
    except Exception as e:
        raise HTTPException(503, str(e))


@app.get("/api/config")
async def api_config():
    trackers = await tracker_pool.get_trackers()
    return {
        "rss_url": settings.YTS_RSS_URL,
        "qualities": settings.qualities,
        "poll_interval": settings.YTS_POLL_INTERVAL,
        "library_dir": settings.LIBRARY_DIR,
        "auto_organize": settings.AUTO_ORGANIZE,
        "delete_qbit_after_organize": settings.DELETE_QBIT_AFTER_ORGANIZE,
        "auto_download": settings.AUTO_DOWNLOAD,
        "min_rating": settings.MIN_IMDB_RATING,
        "max_size_gb": settings.MAX_SIZE_GB,
        "auto_subtitle": settings.AUTO_SUBTITLE,
        "save_path": settings.QBIT_SAVE_PATH,
        "sub_langs": settings.sub_langs,
        "trans_enabled": settings.TRANS_ENABLED,
        "trans_model": settings.TRANS_MODEL,
        "trans_base_url": settings.TRANS_BASE_URL,
        "trans_api_key": settings.TRANS_API_KEY,
        "trans_batch_size": settings.TRANS_BATCH_SIZE,
        "tracker_count": len(trackers),
        "auth_enabled": bool(settings.API_TOKEN),
        "telegram_enabled": bool(settings.NOTIFY_TELEGRAM_TOKEN),
    }


class ConfigPatch(BaseModel):
    auto_download: Optional[bool] = None
    min_rating: Optional[float] = None
    max_size_gb: Optional[float] = None
    auto_subtitle: Optional[bool] = None
    auto_organize: Optional[bool] = None
    poll_interval: Optional[int] = None
    trans_enabled: Optional[bool] = None
    trans_model: Optional[str] = None
    trans_base_url: Optional[str] = None
    trans_api_key: Optional[str] = None
    trans_batch_size: Optional[int] = None


@app.patch("/api/config", dependencies=[_auth])
async def api_patch_config(patch: ConfigPatch):
    """Hot-patch .env settings without restart."""
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        raise HTTPException(500, ".env not found")

    lines = env_path.read_text().splitlines()
    mapping = {
        "auto_download": "AUTO_DOWNLOAD",
        "min_rating": "MIN_IMDB_RATING",
        "max_size_gb": "MAX_SIZE_GB",
        "auto_subtitle": "AUTO_SUBTITLE",
        "auto_organize": "AUTO_ORGANIZE",
        "poll_interval": "YTS_POLL_INTERVAL",
        "trans_enabled": "TRANS_ENABLED",
        "trans_model": "TRANS_MODEL",
        "trans_base_url": "TRANS_BASE_URL",
        "trans_api_key": "TRANS_API_KEY",
        "trans_batch_size": "TRANS_BATCH_SIZE",
    }
    applied = {}
    for field, env_key in mapping.items():
        val = getattr(patch, field)
        if val is None:
            continue
        str_val = str(val).lower() if isinstance(val, bool) else str(val)
        new_lines = []
        found = False
        for line in lines:
            if line.startswith(f"{env_key}=") or line.startswith(f"{env_key} ="):
                new_lines.append(f"{env_key}={str_val}")
                found = True
            else:
                new_lines.append(line)
        if not found:
            new_lines.append(f"{env_key}={str_val}")
        lines = new_lines
        # Apply in-process immediately
        setattr(settings, env_key.replace("YTS_", "YTS_").upper(), val)
        applied[field] = val

    env_path.write_text("\n".join(lines) + "\n")
    log_event("info", f"config patched: {applied}")
    return {"ok": True, "applied": applied}


@app.get("/api/disk")
async def api_disk():
    _shutil = shutil
    paths = {
        "library": settings.LIBRARY_DIR,
        "downloads": settings.QBIT_SAVE_PATH.replace("/downloads/movies", "/mnt/extdata/movies"),
        "data": str(settings.data_dir),
    }
    result = {}
    for name, p in paths.items():
        try:
            usage = _shutil.disk_usage(p)
            result[name] = {
                "path": p,
                "total_gb": round(usage.total / 1024**3, 1),
                "used_gb": round(usage.used / 1024**3, 1),
                "free_gb": round(usage.free / 1024**3, 1),
                "pct": round(usage.used / usage.total * 100, 1),
            }
        except Exception:
            result[name] = {"path": p, "error": "unavailable"}
    low = any(
        v.get("free_gb", 999) < settings.DISK_MIN_GB
        for v in result.values() if "free_gb" in v
    )
    return {"disks": result, "low_disk": low}


@app.post("/api/backup", dependencies=[_auth])
async def api_backup():
    dest = await asyncio.to_thread(backup_db)
    if not dest:
        raise HTTPException(500, "backup failed")
    return {"ok": True, "file": dest.name}


@app.get("/api/events")
def api_events(limit: int = 200):
    return {"events": recent_events(limit=limit)}


# ──────────────────────────────────────────────────────────────────
# SSE stream
# ──────────────────────────────────────────────────────────────────
@app.get("/api/stream", dependencies=[_auth])
async def api_stream():
    q = rss_watcher.subscribe()

    async def gen():
        try:
            yield _sse("snapshot", {"movies": list_movies(limit=500)})
            while True:
                try:
                    evt = await asyncio.wait_for(q.get(), timeout=25)
                    yield _sse(evt.get("type", "event"), evt)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            rss_watcher.unsubscribe(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


# ──────────────────────────────────────────────────────────────────
# Frontend
# ──────────────────────────────────────────────────────────────────
@app.get("/")
async def root():
    f = STATIC_DIR / "index.html"
    if f.exists():
        return HTMLResponse(f.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Put index.html in static/</h1>")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=settings.YTS_HOST, port=settings.YTS_PORT)

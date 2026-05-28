"""Upload movie folder (MKV + subtitles) to cloud via rclone WebDAV."""
from __future__ import annotations
import asyncio
import subprocess
from pathlib import Path
from typing import Optional

from config import settings
from store import log_event


async def upload_movie(movie_dir: Path, imdb_id: str) -> bool:
    """
    Upload all MKV and subtitle files in movie_dir to cloud storage.
    Returns True on success.
    """
    if not settings.CLOUD_UPLOAD_ENABLED:
        return False
    if not movie_dir.is_dir():
        log_event("warn", f"upload: directory not found {movie_dir}", imdb_id)
        return False

    dest = f"{settings.CLOUD_DEST_DIR.rstrip('/')}/{movie_dir.name}"

    def _run() -> bool:
        env = {
            "RCLONE_CONFIG_CMCC_TYPE": "webdav",
            "RCLONE_CONFIG_CMCC_URL": settings.CLOUD_WEBDAV_URL,
            "RCLONE_CONFIG_CMCC_VENDOR": "other",
            "RCLONE_CONFIG_CMCC_USER": settings.CLOUD_WEBDAV_USER,
            "RCLONE_CONFIG_CMCC_PASS": _obscure(settings.CLOUD_WEBDAV_PASS),
        }
        import os
        full_env = {**os.environ, **env}
        try:
            r = subprocess.run(
                [
                    "rclone", "copy", str(movie_dir), f"cmcc:{dest}",
                    "--filter", "- *.extracted_eng.srt",  # skip temp files
                    "--filter", "+ *.mkv",
                    "--filter", "+ *.mp4",
                    "--filter", "+ *.srt",
                    "--filter", "+ *.ass",
                    "--filter", "- **",
                    "--stats-one-line",
                    "--log-level", "ERROR",
                ],
                env=full_env,
                capture_output=True,
                text=True,
                timeout=3600,
            )
            if r.returncode != 0:
                log_event("error", f"upload: rclone error: {r.stderr.strip()}", imdb_id)
                return False
            log_event("info", f"upload: done → {dest}", imdb_id)
            return True
        except subprocess.TimeoutExpired:
            log_event("error", "upload: timeout after 1h", imdb_id)
            return False
        except Exception as e:
            log_event("error", f"upload: {repr(e)}", imdb_id)
            return False

    return await asyncio.to_thread(_run)


def _obscure(password: str) -> str:
    """Use rclone to obscure a password (required by rclone WebDAV auth)."""
    try:
        r = subprocess.run(
            ["rclone", "obscure", password],
            capture_output=True, text=True, timeout=5
        )
        return r.stdout.strip()
    except Exception:
        return password

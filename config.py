from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore")

    YTS_HOST: str = "0.0.0.0"
    YTS_PORT: int = 4003

    YTS_RSS_URL: str = "https://yts.mx/rss/0/{quality}/all/6/en"
    YTS_QUALITIES: str = "2160p"
    YTS_POLL_INTERVAL: int = 600
    YTS_RSS_PROXY: str = ""
    YTS_API_PROXY: str = "http://127.0.0.1:20171"
    YTS_API_URL: str = "https://yts.mx/api/v2"

    QBIT_URL: str = "http://127.0.0.1:8080"
    QBIT_USER: str = "admin"
    QBIT_PASS: str = "adminadmin"
    QBIT_CATEGORY: str = "yts"
    QBIT_SAVE_PATH: str = "/downloads/movies"
    QBIT_INCOMPLETE_PATH: str = "/downloads/incomplete"

    TRACKERS_URL: str = "https://raw.githubusercontent.com/ngosang/trackerslist/master/trackers_best.txt"
    TRACKERS_REFRESH_HOURS: int = 6

    SUB_LANGS: str = "zho"
    OPENSUBTITLES_API_KEY: str = ""
    OPENSUBTITLES_USERNAME: str = ""
    OPENSUBTITLES_PASSWORD: str = ""
    SUBDL_API_KEY: str = ""
    SUB_PROXY: str = "http://127.0.0.1:20171"

    # 字幕翻译反代（复用 VSM 方案）
    TRANS_BASE_URL: str = "http://YOUR_LLM_HOST:8317/v1"
    TRANS_API_KEY: str = "cliproxy-local"
    TRANS_MODEL: str = "deepseek-v4-flash"
    TRANS_BATCH_SIZE: int = 20
    TRANS_CONCURRENT: int = 4
    # 开关：False=不翻译（依赖外部字幕源）
    TRANS_ENABLED: bool = True

    # ── Cloud upload (rclone WebDAV / AList) ──────────────────────
    CLOUD_UPLOAD_ENABLED: bool = False
    CLOUD_WEBDAV_URL: str = "http://127.0.0.1:5244/dav"
    CLOUD_WEBDAV_USER: str = "admin"
    CLOUD_WEBDAV_PASS: str = ""
    CLOUD_DEST_DIR: str = "电影"   # remote path inside WebDAV root

    AUTO_DOWNLOAD: bool = False
    # AUTO_DOWNLOAD_RULES determines scoring bonuses/penalties and thresholds
    AUTO_DOWNLOAD_RULES: str = '{"genres_bonus": {"Sci-Fi": 10, "Thriller": 5, "Action": 5, "Musical": -20, "Documentary": -20}, "min_auto_score": 80, "min_review_score": 65}'
    MIN_IMDB_RATING: float = 6.5
    MAX_SIZE_GB: float = 12.0
    AUTO_SUBTITLE: bool = True

    LIBRARY_DIR: str = "/mnt/extdata/library"
    AUTO_ORGANIZE: bool = True
    DELETE_QBIT_AFTER_ORGANIZE: bool = True
    CONVERT_TO_MP4: bool = True   # fast remux MKV→MP4 after download
    PINYIN_NAMES: bool = True     # use Pinyin for folder/file names
    QBIT_PATH_MAP: str = "/downloads/movies:/mnt/extdata/movies;/downloads/incomplete:/mnt/extdata/torrents/incomplete"

    API_TOKEN: str = ""
    NOTIFY_TELEGRAM_TOKEN: str = ""
    NOTIFY_TELEGRAM_CHAT_ID: str = ""
    DISK_MIN_GB: float = 5.0
    DB_BACKUP_KEEP_DAYS: int = 7
    LIBRARY_KEEP_DAYS: int = 30

    def host_path(self, container_path: str) -> str:
        if not container_path:
            return container_path
        
        c_path = Path(container_path)
        for pair in self.QBIT_PATH_MAP.split(";"):
            if ":" not in pair:
                continue
            src, dst = pair.split(":", 1)
            src_path = Path(src.strip())
            dst_path = Path(dst.strip())
            try:
                # 严格判定前缀，且通过 relative_to 解析
                if c_path == src_path or c_path.is_relative_to(src_path):
                    rel = c_path.relative_to(src_path)
                    return str(dst_path / rel)
            except ValueError:
                pass
        return container_path

    @property
    def qualities(self) -> list[str]:
        return [q.strip() for q in self.YTS_QUALITIES.split(",") if q.strip()]

    @property
    def sub_langs(self) -> list[str]:
        return [l.strip() for l in self.SUB_LANGS.split(",") if l.strip()]

    @property
    def base_dir(self) -> Path:
        return Path(__file__).parent

    @property
    def data_dir(self) -> Path:
        d = self.base_dir / "data"
        d.mkdir(exist_ok=True)
        return d


settings = Settings()

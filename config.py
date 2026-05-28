from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore")

    YTS_HOST: str = "0.0.0.0"
    YTS_PORT: int = 4003

    YTS_RSS_URL: str = "https://yts.am/rss/0/{quality}/all/7/en"
    YTS_QUALITIES: str = "1080p"
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

    AUTO_DOWNLOAD: bool = False
    MIN_IMDB_RATING: float = 6.5
    MAX_SIZE_GB: float = 12.0
    AUTO_SUBTITLE: bool = True

    LIBRARY_DIR: str = "/mnt/extdata/library"
    AUTO_ORGANIZE: bool = True
    DELETE_QBIT_AFTER_ORGANIZE: bool = True
    QBIT_PATH_MAP: str = "/downloads/movies:/mnt/extdata/movies;/downloads/incomplete:/mnt/extdata/torrents/incomplete"

    # API 访问令牌（空=不启用）
    API_TOKEN: str = ""

    # Telegram 通知
    NOTIFY_TELEGRAM_TOKEN: str = ""
    NOTIFY_TELEGRAM_CHAT_ID: str = ""

    # 磁盘低水位告警 (GB)
    DISK_MIN_GB: float = 5.0

    # 数据库备份保留天数
    DB_BACKUP_KEEP_DAYS: int = 7

    def host_path(self, container_path: str) -> str:
        if not container_path:
            return container_path
        for pair in self.QBIT_PATH_MAP.split(";"):
            if ":" not in pair:
                continue
            src, dst = pair.split(":", 1)
            src, dst = src.strip(), dst.strip()
            if container_path == src or container_path.startswith(src + "/"):
                return dst + container_path[len(src):]
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

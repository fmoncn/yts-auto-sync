# yts-auto-sync

**中文** | [English](#english)

自动从 YTS 发现并下载高质量英文电影，整理到本地媒体库并翻译中文字幕。

**核心流程：** 片源发现 → 筛选 → qBittorrent 下载 → 文件整理 → 字幕匹配 → LLM 翻译

Web UI 端口 `4003`，与 [video-subtitle-maker](https://github.com/fmoncn/video-subtitle-maker) 同一视觉风格。

---

## 功能

### 片源发现（双轨）

- **热门榜**：轮询 YTS API `sort_by=download_count`，抓近年（默认 7 年内）全站热门，带 language / genres / rating 信息
- **RSS 新片**：按上传时间拉最新入库影片
- 两路在 `poll_once` 合并去重，热门优先

### 筛选规则

- IMDB 评分硬门槛（`MIN_IMDB_RATING`，默认 6.0）
- 主类型黑名单（`BLOCK_GENRES`，默认屏蔽 Documentary / Music / Musical / Concert）
- 评分加成打分制，超过 `min_auto_score` 自动入队，超过 `min_review_score` 进待审列表
- 画质过滤（默认 2160p），文件大小上限（`MAX_SIZE_GB`）

### 下载与整理

- 通过 qBittorrent Web API 添加磁力，自动注入公共 tracker 列表（每 6h 刷新）
- 下载完成后移动到 `LIBRARY_DIR`，删除 qBit 残留文件
- 重复片目检测（标题 + 年份 + IMDB ID）

### 字幕与翻译

字幕抓取优先级：

1. 视频内嵌中文字幕
2. [subdl.com](https://subdl.com) API（`SUBDL_API_KEY`）
3. [OpenSubtitles.com](https://www.opensubtitles.com) API（`OPENSUBTITLES_API_KEY`）
4. 视频内嵌英文字幕 → 触发翻译
5. 前三路找到英文 SRT → 触发翻译

翻译管线：

- 按段批量发给 OpenAI 兼容 LLM（`TRANS_BASE_URL`），并发 `TRANS_CONCURRENT` 批
- 附加影片名称作上下文，提升专名翻译准确度
- 翻译后触发 review pass（`TRANS_REVIEW_MODEL`）二次校审
- count mismatch 时部分救援，不整批回退英文

### 可靠性

- SQLite WAL 模式 + `threading.RLock` 防并发写冲突
- qBittorrent 连接失败自动重置客户端状态
- 字幕失败前 2 次保留视频文件，第 3 次再删，避免字幕服务临时宕机误删文件（`retry_count` 列记录）

---

## 部署

```bash
git clone https://github.com/fmoncn/yts-auto-sync ~/yts-auto-sync
cd ~/yts-auto-sync
cp .env.example .env        # 按需修改
bash deploy/install.sh
```

安装脚本会：

- 创建 Python venv，安装依赖
- 注册并启动 `yts-auto-sync.service`
- 部署 qBittorrent Docker 容器（`deploy/docker-compose.qbit.yml`）

完成后访问：

- `http://<host>:4003` — 管理界面
- `http://<host>:8080` — qBittorrent WebUI

---

## 配置（`.env`）

| 变量 | 默认 | 说明 |
|------|------|------|
| `YTS_PORT` | `4003` | Web UI 监听端口 |
| `YTS_QUALITIES` | `2160p` | 画质，逗号分隔：`720p,1080p,2160p` |
| `YTS_POLL_INTERVAL` | `600` | 轮询间隔（秒） |
| `YTS_API_PROXY` | — | YTS API 代理（境外服务，国内需要） |
| `QBIT_URL` | `http://127.0.0.1:8080` | qBittorrent Web API 地址 |
| `QBIT_SAVE_PATH` | — | 下载保存路径（qBit 容器内路径） |
| `LIBRARY_DIR` | `/mnt/extdata/library` | 整理后的媒体库目录 |
| `LIBRARY_KEEP_DAYS` | `0` | 库内文件保留天数，0 = 永不自动删除 |
| `MIN_IMDB_RATING` | `6.0` | 最低评分门槛 |
| `MAX_SIZE_GB` | `12` | 单文件大小上限（GB） |
| `BLOCK_GENRES` | `Documentary,...` | 主类型黑名单（逗号分隔） |
| `AUTO_DOWNLOAD` | `false` | 是否自动入队下载 |
| `SUBDL_API_KEY` | — | subdl.com 字幕 API key |
| `OPENSUBTITLES_API_KEY` | — | opensubtitles.com API key |
| `SUB_PROXY` | — | 字幕抓取代理 |
| `TRANS_BASE_URL` | — | 翻译 LLM 的 OpenAI 兼容接口地址 |
| `TRANS_API_KEY` | — | 翻译接口 API key |
| `TRANS_MODEL` | `deepseek-v4-flash` | 翻译模型 |
| `TRANS_REVIEW_MODEL` | — | 校审模型（留空跳过二次校审） |
| `TRANS_BATCH_SIZE` | `20` | 每批翻译行数 |
| `TRANS_CONCURRENT` | `4` | 并发翻译批次数 |
| `TRANS_ENABLED` | `true` | 是否启用自动翻译 |

---

## 关键文件

| 文件 | 作用 |
|------|------|
| `api_server.py` | FastAPI 入口，REST API + SSE 实时推送 |
| `rss_watcher.py` | 双源片源发现、筛选、入队调度 |
| `qbit_client.py` | qBittorrent Web API 封装 |
| `subtitle_fetcher.py` | 字幕抓取 + LLM 翻译管线 |
| `store.py` | SQLite 持久化（movies / events / kv 三张表） |
| `config.py` | 全部配置，Pydantic Settings |
| `tracker_pool.py` | ngosang 公共 tracker 列表，自动刷新 |
| `static/index.html` | Vue 3 单页前端 |
| `.env.example` | 配置模板 |

---

## 常用命令

```bash
# 查看实时日志
journalctl -u yts-auto-sync -f

# 重启服务
systemctl restart yts-auto-sync

# 手动触发一次轮询
curl -X POST http://localhost:4003/api/poll

# 手动添加影片（绕过筛选规则）
curl -X POST http://localhost:4003/api/movies/search \
     -H "Content-Type: application/json" \
     -d '{"query": "Inception"}'

# qBittorrent 日志
docker logs -f qbittorrent
```

---

## 端口

| 端口 | 服务 |
|------|------|
| 4003 | yts-auto-sync Web UI |
| 8080 | qBittorrent WebUI |
| 6881 | qBit BT 入站（TCP+UDP，建议路由器端口转发） |

---

## License

MIT

---

## English

Automated movie downloader that discovers films from YTS, downloads via qBittorrent, organizes your library, and translates English subtitles to Simplified Chinese using an LLM pipeline.

**Pipeline:** Discover → Filter → Download → Organize → Subtitle → Translate

Web UI on port `4003`, sharing the same visual style as [video-subtitle-maker](https://github.com/fmoncn/video-subtitle-maker).

### Features

#### Dual-source Discovery

- **Popular ranking** — Polls YTS API `sort_by=download_count`, filters to recent years (default: last 7). Carries language / genres / rating metadata.
- **RSS new releases** — Pulls latest uploads by publish date.
- Both sources merged and deduplicated per `poll_once`; popular takes priority on conflict.

#### Filtering

- Hard IMDB rating floor (`MIN_IMDB_RATING`, default 6.0)
- Primary genre blocklist (`BLOCK_GENRES`, defaults: Documentary / Music / Musical / Concert)
- Score-based rules: `min_auto_score` triggers auto-queue; `min_review_score` adds to review list
- Quality filter (default 2160p) and max file size cap (`MAX_SIZE_GB`)

#### Download & Organization

- Adds magnet links to qBittorrent via Web API with automatic public tracker injection (refreshed every 6h)
- Moves completed downloads to `LIBRARY_DIR`, cleans up qBit residuals
- Duplicate detection by title + year + IMDB ID

#### Subtitle & Translation

Subtitle lookup priority:

1. Embedded Chinese subtitles in video
2. [subdl.com](https://subdl.com) API (`SUBDL_API_KEY`)
3. [OpenSubtitles.com](https://www.opensubtitles.com) API (`OPENSUBTITLES_API_KEY`)
4. Embedded English subtitles → triggers translation
5. English SRT from above sources → triggers translation

Translation pipeline:

- Batched requests to an OpenAI-compatible LLM (`TRANS_BASE_URL`), `TRANS_CONCURRENT` parallel batches
- Movie title injected as context for accurate proper-noun translation
- Optional review pass (`TRANS_REVIEW_MODEL`) for quality audit
- Partial rescue on line-count mismatch — never falls back an entire batch to English

#### Reliability

- SQLite WAL mode + `threading.RLock` for safe concurrent writes
- qBittorrent client auto-resets on connection failure
- Missing subtitle keeps the video file for the first 2 attempts; only deletes on the 3rd failure (`retry_count` column tracks this, preventing data loss during subtitle API outages)

### Quick Start

```bash
git clone https://github.com/fmoncn/yts-auto-sync ~/yts-auto-sync
cd ~/yts-auto-sync
cp .env.example .env   # edit as needed
bash deploy/install.sh
```

The installer creates a Python venv, registers `yts-auto-sync.service`, and deploys a qBittorrent Docker container.

### Key Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `YTS_QUALITIES` | `2160p` | Comma-separated quality list: `720p,1080p,2160p` |
| `YTS_API_PROXY` | — | Proxy for YTS API (required in mainland China) |
| `QBIT_URL` | `http://127.0.0.1:8080` | qBittorrent Web API URL |
| `LIBRARY_DIR` | `/mnt/extdata/library` | Organized media library path |
| `MIN_IMDB_RATING` | `6.0` | Minimum IMDB rating to consider |
| `MAX_SIZE_GB` | `12` | Max file size per movie (GB) |
| `AUTO_DOWNLOAD` | `false` | Auto-queue without manual approval |
| `SUBDL_API_KEY` | — | subdl.com subtitle API key |
| `OPENSUBTITLES_API_KEY` | — | opensubtitles.com API key |
| `TRANS_BASE_URL` | — | OpenAI-compatible LLM endpoint for translation |
| `TRANS_API_KEY` | — | LLM API key |
| `TRANS_MODEL` | `deepseek-v4-flash` | Translation model |
| `TRANS_REVIEW_MODEL` | — | Review/editing model (leave blank to skip) |
| `TRANS_ENABLED` | `true` | Enable auto-translation |

### Ports

| Port | Service |
|------|---------|
| 4003 | yts-auto-sync Web UI |
| 8080 | qBittorrent WebUI |
| 6881 | qBit BT incoming (TCP+UDP — forward at your router) |

### License

MIT

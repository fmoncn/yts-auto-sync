# yts-auto-sync

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

- IMDB 评分硬门槛（`MIN_IMDB_RATING`，默认 6.5）
- 主类型黑名单（`BLOCK_GENRES`，默认屏蔽 Documentary / Music / Musical / Concert）
- 评分加成打分制，超过 `min_auto_score` 自动入队，超过 `min_review_score` 进待审列表
- 画质过滤（默认 2160p），文件大小上限（`MAX_SIZE_GB`）

### 下载与整理

- 通过 qBittorrent Web API 添加磁力，自动注入公共 tracker 列表
- 下载完成后移动到 `LIBRARY_DIR`，删除 qBit 残留文件
- 重复片目检测（标题 + 年份 + IMDB ID）

### 字幕与翻译

字幕抓取优先级：

1. 视频内嵌中文字幕
2. [subdl.com](https://subdl.com) API（`SUBDL_API_KEY`）
3. [OpenSubtitles.com](https://www.opensubtitles.com) API（`OPENSUBTITLES_API_KEY`）
4. 视频内嵌英文字幕，触发翻译
5. 前三路找到英文 SRT，触发翻译

翻译管线（`subtitle_fetcher.py`）：

- 按段批量发给 OpenAI 兼容 LLM（`TRANS_BASE_URL`），并发 `TRANS_CONCURRENT` 批
- 附加影片名称作上下文，提升专名翻译准确度
- 翻译后触发 review pass（`TRANS_REVIEW_MODEL`）二次校审
- count mismatch 时部分救援，不整批回退英文
- 找不到字幕时保留文件，连续 3 次失败后才移除，用 `retry_count` 列记录

### 安全与可靠

- SQLite WAL 模式 + `threading.RLock` 防并发写冲突
- qBittorrent 连接失败自动重置客户端状态
- 字幕失败前 2 次保留视频，第 3 次再删，避免字幕服务临时宕机误删文件

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
| `QBIT_URL` | `http://127.0.0.1:8080` | qBittorrent Web API |
| `QBIT_SAVE_PATH` | — | 下载保存路径（qBit 容器内） |
| `LIBRARY_DIR` | `/mnt/extdata/library` | 整理后的媒体库目录 |
| `LIBRARY_KEEP_DAYS` | `0` | 库内文件保留天数，0 = 永不自动删除 |
| `MIN_IMDB_RATING` | `6.5` | 最低评分门槛 |
| `MAX_SIZE_GB` | `12` | 单文件大小上限（GB） |
| `BLOCK_GENRES` | `Documentary,...` | 主类型黑名单（逗号分隔） |
| `AUTO_DOWNLOAD` | `false` | 是否自动入队下载 |
| `SUBDL_API_KEY` | — | subdl.com 字幕 API key |
| `OPENSUBTITLES_API_KEY` | — | opensubtitles.com API key |
| `SUB_PROXY` | — | 字幕抓取代理 |
| `TRANS_BASE_URL` | — | 翻译 LLM 的 OpenAI 兼容接口 |
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

# 手动触发一次轮询（API）
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

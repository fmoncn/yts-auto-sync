# yts-auto-sync

YTS RSS → qBittorrent → 中文字幕自动匹配。Web UI 端口 4003，复刻 VSM (4002) 视觉风格。

## 部署

```bash
git clone <repo> ~/yts-auto-sync
cd ~/yts-auto-sync
bash deploy/install.sh
```

完成后：
- `http://10.10.10.10:4003` — 管理界面
- `http://10.10.10.10:8080` — qBittorrent (默认账号 admin，密码写在 `.env` `QBIT_PASS`)

## 关键文件

| 文件 | 作用 |
|------|------|
| `api_server.py`        | FastAPI 入口 + SSE |
| `rss_watcher.py`       | YTS RSS 轮询 + tracker 增强 + 入队 |
| `qbit_client.py`       | qBittorrent Web API 封装 |
| `subtitle_fetcher.py`  | subliminal + zimuku 兜底 |
| `tracker_pool.py`      | ngosang 公共 tracker 列表 |
| `store.py`             | SQLite 持久化 |
| `static/index.html`    | Vue3 单页前端 |
| `.env`                 | 全部配置 |

## 端口

| 端口 | 服务 |
|------|------|
| 4003 | yts-auto-sync Web UI |
| 8080 | qBittorrent WebUI |
| 6881 | qBit BT incoming (TCP+UDP，**建议路由器手动转发**) |

## 速度优化

- **公共 tracker 自动注入**：拉取 ngosang/trackerslist，每 6h 更新，所有新种子追加。
- **qBit Docker host 网络**：免 NAT 损耗。
- **数据放外挂盘 /mnt/extdata** (104 GB 空闲)。

进一步可在 qBit WebUI 设置中：
- 全局最大连接 1000 / 单种 200
- 启用 DHT / PEX / LSD / µTP / IPv6
- 协议加密：优先但允许明文

## 字幕

默认尝试两路并发：
1. **subliminal** — 支持 OpenSubtitles.com、Addic7ed 等。若提供 `OPENSUBTITLES_API_KEY` 命中率最高。
2. **zimuku (zmk.pw)** — `curl_cffi` 模拟浏览器绕过 CF。

谁先返回中文 .srt 谁胜。

## 常用命令

```bash
journalctl -u yts-auto-sync -f          # 跟踪日志
systemctl restart yts-auto-sync         # 重启服务
docker logs -f qbittorrent              # qBit 日志
docker compose -f deploy/docker-compose.qbit.yml restart qbittorrent
```

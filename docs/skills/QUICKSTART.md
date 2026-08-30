# 下次开工速查（新一周素材制作）

## 最省事的方式：一条命令

```bash
cd /Users/jax/bestdancer && source .venv/bin/activate
python3 scripts/auto_episode.py
```

它会依次做完：CDP 调试 Chrome 预检（没起就自己拉 `~/.chrome-debug-profile`）→
跨平台发现 → 下载 → 组稿 → 渲染 → `pipeline/evaluate_demo.py` 打分 → 不及格就剔掉
坏素材重来（默认最多 2 轮）。及格才允许发布，加 `--publish` 才会真的进上传脚本。

常用开关：`--skip-discover`（用现有候选池）、`--skip-download`、`--threshold 85`、
`--no-llm`（评估只跑硬指标）、`--week 2026-W31 --edition C`（指定期号）。

## 手动分步（排查用）

假设做 `2026-W31-C`：

```bash
cd /Users/jax/bestdancer && source .venv/bin/activate
unset http_proxy https_proxy all_proxy

# 1. CDP 就绪（注意必须带独立 profile，Chrome 136+ 默认 profile 会无视调试端口）
curl -s http://127.0.0.1:9222/json/version || \
  open -na "Google Chrome" --args --remote-debugging-port=9222 \
    --user-data-dir="$HOME/.chrome-debug-profile"

# 2. 跨平台发现（每个 平台×关键词 一个独立进程，避免 Playwright 内存累积）
python3 -u scripts/discover_loop.py --week 2026-W31-C --per-run-timeout 300

# 3. 下载（抖音走 CDP 拦截 playAddr，其余走 yt-dlp）
python3 -u scripts/douyin_download_picks.py --week 2026-W31-C
python3 -u scripts/download_cross_platform.py --week 2026-W31-C

# 4. 组稿 + 渲染
python3 -u scripts/daily_auto_generate.py

# 5. 评估
python3 pipeline/evaluate_demo.py 2026-W31-C --threshold 80
open output/2026-W31-C_demo.mp4
```

## 常见故障

| 症状 | 原因 | 处理 |
|---|---|---|
| `curl 9222` 不通，但 Chrome 明明带了参数 | Chrome 136+ 在默认 profile 上直接无视 `--remote-debugging-port` | 加 `--user-data-dir="$HOME/.chrome-debug-profile"` 起第二个实例 |
| **YouTube 能发现但下不动**（0 字节 / 403） | YouTube 的 SABR + PO token 机制；本机出网还要过代理，媒体 URL 里签的 `ip=` 和实际下载连接的出口 IP 对不上 | 已知限制：**YouTube 当前只用于发现，不作为素材源**。`download_cross_platform.py` 的 `platform_ready()` 会在 75 秒内探明并整个平台跳过，不会卡住流水线。要真正下载需接 PO token provider（bgutil）或换稳定出口 IP |
| 某平台候选有量但元数据全 0% | 该平台的提取器在爬 DOM，而页面网格里根本没有作者/点赞/日期 | 改走**接口拦截**：Instagram 截 `/graphql/query`，TikTok 截 `/api/search/item/full/`，字段齐全得多 |
| 某平台一直 `-> 0 cards` | 结果是懒加载的（抖音要 ~12s），或首页搜索框把你带到了另一套版式（抖音 `/jingxuan/search/` 里没有 `a[href*="/video/"]`） | `search_and_wait()` 已处理：先轮询等结果，再回退到规范搜索 URL |
| discover 跑完没生成 candidates | 旧代码在最后一个平台跑完后还要冷却 90-240s 才落盘，被上层 timeout 杀掉 | 已改成"每个平台跑完立刻落盘，最后一个平台不冷却" |
| `合规候选不足 5 支` | `all_downloaded()` 只认 `<平台>_<hex>.mp4`，漏掉裸 `<id>.mp4`/youtube/`.info.mp4` | 已放宽正则；仍不足就是真没素材了，去 discover |
| `no-detail` 全片段 | Chrome 抖音未登录 / cookies 过期 | 手动打开 douyin.com 扫码 → 重跑 dump_cookies.py |
| 下载卡在某支 | HLS master 返回 / 单支超时 | kill 脚本，跑 `scripts/douyin_fetch_remaining.py`（从 /tmp/fetch.log 里补 KEEP） |
| curl 无响应 | 系统代理 | `unset http_proxy https_proxy all_proxy` |
| 画面上出现方框缺字 | 抖音标题/昵称里的 emoji 和私用区图标，正文字体没有字形 | `render_demo.clean()` 已过滤 emoji 区段 |
| 口播出现光秃秃的"来自" | 候选没有作者名 | `mkvo()` 已改成作者为空就不带这一句 |
| BGM 太大 | render 里 sidechain 参数 | `pipeline/render_demo.py` amix filter 里 `[bg0]` volume 或 `sidechaincompress` ratio |
| 段太短 | render 里 min_dur | `pipeline/render_demo.py` `min_dur` dict |
| 字幕缺失 | seg dict 缺 `vo_caption` 或 `subtitles` | 检查 `build_segments()` |
| 片尾丢失 | outro seg 时长太短没注意 | `default_dur["outro"]` 拉到 6+ |

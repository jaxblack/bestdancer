# 下次开工速查（新一周素材制作）

假设做 `2026-W30`：

```bash
cd /Users/jax/bestdancer && source .venv/bin/activate

# 1. Chrome CDP 就绪（若挂了）
curl -s http://127.0.0.1:9222/json/version || \
  open -na "Google Chrome" --args --remote-debugging-port=9222

# 2. 若 cookies 过期
python3 scripts/dump_cookies.py

# 3. 改 scripts/douyin_fetch_clean.py 顶部 WEEK 变量为 "2026-W30"（当前硬编码）
#    改 scripts/rebuild_from_dl2.py 顶部 WEEK 同上
sed -i '' 's/2026-W29/2026-W30/g' scripts/douyin_fetch_clean.py scripts/rebuild_from_dl2.py

# 4. 抓 + 过滤 + 下载（5-10 分钟）
python3 -u scripts/douyin_fetch_clean.py > /tmp/fetch.log 2>&1 &
tail -f /tmp/fetch.log      # 看进度，等 "DONE: N videos" 或 6+ 个 mp4

# 5. 重建 config
python3 scripts/rebuild_from_dl2.py

# 6. 渲染 + 预览
rm -f output/tts/2026-W30/*.wav
python3 -u pipeline/render_demo.py 2026-W30 2>&1 | tee /tmp/render.log
open output/2026-W30_demo.mp4
```

## 常见故障

| 症状 | 原因 | 处理 |
|---|---|---|
| `no-detail` 全片段 | Chrome 抖音未登录 / cookies 过期 | 手动打开 douyin.com 扫码 → 重跑 dump_cookies.py |
| 下载卡在某支 | HLS master 返回 / 单支超时 | kill 脚本，跑 `scripts/douyin_fetch_remaining.py`（从 /tmp/fetch.log 里补 KEEP） |
| curl 无响应 | 系统代理 | `unset http_proxy https_proxy all_proxy` |
| BGM 太大 | render 里 sidechain 参数 | `pipeline/render_demo.py` amix filter 里 `[bg0]` volume 或 `sidechaincompress` ratio |
| 段太短 | render 里 min_dur | `pipeline/render_demo.py` `min_dur` dict |
| 字幕缺失 | seg dict 缺 `vo_caption` 或 `subtitles` | 检查 `build_segments()` |
| 片尾丢失 | outro seg 时长太短没注意 | `default_dur["outro"]` 拉到 6+ |

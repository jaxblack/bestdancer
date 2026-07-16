# W30-A Progress Log (2026-07-16)

## Goal
成人向街舞周刊 W30-A，跨平台真实素材 → 10-20s/段总 1-3 分钟成片，
字幕「平台 空格 @作者」无标点，含片尾。

## Result Snapshot
| # | 平台 | 作者 | 状态 |
|---|---|---|---|
| 1 | TikTok | @layla_.mckenzie | ✅ 11.9MB (yt-dlp) |
| 2 | 抖音 | @NNUNA | ✅ 4.5MB (CDP grab open tab) |
| 3 | 抖音 | @蛋丝儿🍳 | ✅ 1.8MB (CDP grab open tab) |
| 4 | TikTok | @vivacious.dc | ✅ 1.5MB (yt-dlp) |
| 5 | 抖音 | @唉游喂^ | ⚠️ blob:/MSE stream, segment capture in progress |
| 6 | (special) | — | 用户挑了但未在 picks[] 落盘 (dashboard 保存问题) |

候选池: 抖音 20 + 小红书 20 + TikTok 20 + Instagram 20 = 80 (trim from 374 raw).

## Systems Improved Today

### 1. Discovery pipeline
- **`scripts/discover_universal.py`** 现在读 `admin/settings.json` 里
  的 keywords/platforms/recent_days，不再 hardcode 默认（用户吐槽
  「关键词和我保存的不一样」的根本修复）。
- **`scripts/discover_loop.py`** 一个 subprocess/(platform,keyword)，
  绕开单 Playwright 进程跑 5 关键词后 silent-die 的问题。
- **抖音卡片字段顺序修正**：`[duration, like, title, @author, "N天前"]`
  ——之前当 TikTok 结构解析导致 author=`"34.9万"`, title=`"00:19"`。
- **每平台 trim top 20** (recent bonus + like desc)，替代之前 pool=100。
  `scripts/trim_and_import.py` 一键 trim + POST /api/import。

### 2. Dashboard (admin/)
- **`_infer_source(item)`** 修复候选卡片来源全 fallback 到"抖音"的 bug；
  优先 `item.platform`，其次 URL substring，最后 hardcode。
- **`_infer_creator(item)`** scraper 写 `author`，dashboard 读 `creator`，
  在 normalize 边界自动 fallback 到 `@author`。
- **`import_downloads` 支持多平台 URL 重建**（dl2 里的 tiktok/xhs/ig
  不再被 hardcode 成 `douyin.com/video/<id>`）。
- **前端分页 12/页** + `editBuffer` Map 保存跨页编辑。
- **已入选置顶** + 粉色边框 + `✓已入选` badge（修复用户「已编排的找不到」）。
- **候选卡 UI 单行压缩**：开始秒数 / 结束秒数 / 音色 / 语速 / 预览按钮
  合并到 `.clip-voice-row` 一行。
- Dashboard 计数显示 `候选池 105（已入选 5）`。

### 3. Downloads
- **`scripts/download_picks.py`** 只下 `picks[]`，跳过 candidates 全量。
  TikTok 走 yt-dlp（很稳），抖音 fallback 到 CDP 拦截 aweme/detail XHR。
- **`scripts/douyin_download_picks.py`** CDP 版：navigate + 拦截 XHR
  + curl play_addr —— 但会触发 rc-verifycenter 滑块。
- **`scripts/douyin_grab_open_tabs.py`** 关键新方案：**不 goto**，
  直接从已打开 tab 的 `<video>.currentSrc` 抠 zjcdn 直链 curl。
  绕开风控。用户手动打开 3 tab，脚本秒抓 2 支成功。

### 4. Render
- **`pipeline/render_demo.py:275`** 画面字幕从"原创 @xxx"改为
  「平台 空格 @作者」无标点（用户偏好）；VO 口播保留标点供 TTS 断句。

## Key Lessons (all upstreamed to `local-chrome-cdp` skill)
1. 抖音 yt-dlp 永远失败 (`Fresh cookies (not necessarily logged in) are
   needed`)，别浪费时间刷 cookies，直接走 CDP 路径。
2. 抖音 blob:/MSE 视频 → 优先从**已打开 tab** 抓 currentSrc；
   XHR intercept 会被 verify iframe 阻塞。
3. Playwright 单进程跑 5 关键词后 silent-die → 每 keyword 一个 subprocess。
4. 用户 saved settings 永远优先于脚本 CLI 默认。
5. 候选池写完必须 POST /api/import，dashboard 才看得到。
6. 分页列表里已入选项必须置顶（用户找不到自己已选的）。

## New Skill Templates
- `~/.hermes/skills/computer-use/local-chrome-cdp/scripts/douyin_grab_open_tabs_template.py`
- `~/.hermes/skills/computer-use/local-chrome-cdp/scripts/douyin_capture_segments_template.py`
- Skill 新增 pitfall #6: MSE/blob 抖音视频的 recovery ladder。

## Files Modified (uncommitted)
- `admin/app.py` (source/creator normalize, multi-platform URL rebuild)
- `admin/app.js` (pagination, editBuffer, chosen-pinned sort)
- `admin/index.html` (candidate template — 单行 clip+voice)
- `admin/overrides.css` (`.pagination`, `.clip-voice-row`, `.is-chosen`)
- `pipeline/render_demo.py` (byline = 平台+@作者)
- `scripts/discover_universal.py` (settings.json 读取 + 抖音字段修正)
- `scripts/discover_loop.py` (NEW orchestrator)
- `scripts/trim_and_import.py` (NEW)
- `scripts/download_picks.py` (NEW)
- `scripts/douyin_download_picks.py` (NEW, CDP intercept)
- `scripts/douyin_grab_open_tabs.py` (NEW, MSE workaround)
- `scripts/douyin_capture_segments.py` (NEW, segment record fallback)

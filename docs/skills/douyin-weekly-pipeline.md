# Skill: bestdancer 抖音周刊制作流程

> 首次成功于 2026-W29 —— 从零抓抖音真素材、过滤完整成人编舞、渲染出 72s 竖版成片。
> 本文档记录**完整可复现**的步骤、坑、代码位置。下次做新一周直接照抄。

---

## 用户 & 项目硬约束（不要忘）

- **成人向街舞周刊**：只要**完整舞段**。禁止：教学/分解/tutorial/零基础/基本功/路演/vlog/花絮/慢动作/萌娃/少儿/battle/裁判秀/reaction/翻车。
- 每段 **10-20 秒**（自动用真片自身长度 clamp），总时长 **1-3 分钟合理**，不是硬限制。
- **必须有字幕 + 必须有片尾**（"谢谢观看/关注追更"卡片）。
- **字幕不用标点，用空格分隔**（`Urban   @dbgtmlt`）。VO 口播保留标点（TTS 断句需要），但画面字幕的 `vo_caption` 会 `re.sub` 去掉 `，。？！、,.!?`。
- **文案禁止 AI 编造、禁止扒别人 caption**。只能写「舞种 · @真实作者」。
- **署名必须真实**：只能从 `aweme_detail.author.nickname` 取，不能挂 `@1milliondance` 这种默认值。
- 说话时 **BGM 自动 duck**（sidechain compressor），别盖住人声。
- 竖版 9:16。中文 edge-tts `zh-CN-XiaoyiNeural` 女声。

## 未确认改进方向（用户已提，未做）
1. 字幕淡出 —— 不全程常驻
2. 显示舞蹈/曲名 —— 从 `aweme_detail.desc` 或 `music.title` 提取
3. 加个"难度配置后台" —— 现在星级写死 3.0
4. **未来用户会提供关键词列表 + 关注博主列表** —— 届时改用**博主主页直取 + 抖音自带筛选框**，不要再一个个人工翻视频页

---

## 完整流程（下次做 2026-W30 直接跑）

### 0. 前置：Chrome CDP + 抖音登录
```bash
# 关掉所有 Chrome 后
open -na "Google Chrome" --args --remote-debugging-port=9222
# 首次要 open douyin.com 扫码登录
curl -s http://127.0.0.1:9222/json/version    # 验证在线
```

坑：
- macOS 默认 profile 拒开 debug 端口；如遇报错换 `--user-data-dir=~/.chrome-debug-profile`
- `--cookies-from-browser chrome:...` 会弹 keychain 密码框卡死。**永远不要用**，改用 CDP 导 cookies。

### 1. 导 cookies（一次性，除非过期）
```bash
cd /Users/jax/bestdancer && source .venv/bin/activate
python3 scripts/dump_cookies.py     # 682 条 -> assets/incoming/<week>/cookies.txt
```

### 2. 搜索 + 过滤 + 下载（一体化）
```bash
python3 -u scripts/douyin_fetch_clean.py > /tmp/fetch.log 2>&1 &
```

脚本做的事（`scripts/douyin_fetch_clean.py`）：
1. 用 3 个关键词搜（`urban dance 编舞` / `编舞 完整` / `kpop dance cover`），`publish_time=30&sort_type=2` (30天内最多点赞)
2. 从搜索页 regex 提 `/video/(\d{15,25})` 收 id 池
3. 每个 id 逐页访问，**用 `page.expect_response()` 拦截** `/aweme/v1/web/aweme/detail/` XHR
4. 解析出 `author.nickname` / `desc` / `duration` / `stats.digg_count` / `text_extra[].hashtag_name` / `video.play_addr.url_list[0]`
5. 过滤：`desc+tags` 命中 EXCLUDE 词 → drop；`15s <= duration <= 100s` → keep
6. 按点赞 desc 排序，前 8 支 curl 下载 → `dl2/<vid>.mp4` + `dl2/<vid>.json`（元数据）

**关键 XHR endpoint**：`https://www.douyin.com/aweme/v1/web/aweme/detail/`（GET，含 `aweme_id=` 参数）

**关键代码模式**（拦截 detail，用 expect_response 而不是轮询 `captured` dict）：
```python
with page.expect_response(
    lambda r, v=vid: "/aweme/v1/web/aweme/detail/" in r.url and v in r.url,
    timeout=12000
):
    page.goto(vurl, wait_until="commit", timeout=12000)
```

坑：
- `wait_until="commit"` + `expect_response` 才够快。用 `domcontentloaded` 会卡 30-60s/页。
- 搜索每关键词 ~30s（要 scroll 加载）；detail 阶段每页 3-5s；下载 30s/支。总 5-10 分钟 40 页。
- 会看到 playwright `pyee` 的 `KeyError: <function Waiter.reject_on_event...>` traceback —— **忽略**，不影响功能（listener cleanup race）。
- yt-dlp 抖音 extractor 已挂：`Fresh cookies needed` / `Failed to parse JSON`。**用 curl + play_addr 直下**。
- 系统代理 `http_proxy=127.0.0.1:1087` 会毒 curl 抖音 tos-cdn；`unset` 再下。
- Douyin 有时返回 HLS master (~80KB)，用 `size > 300_000` 过滤。

### 3. 重建 config
```bash
python3 scripts/rebuild_from_dl2.py
```
- 读所有 `dl2/*.json`，按 like 排序取前 6
- 拷贝到 `assets/incoming/<week>/{c1..c5,k1}__douyin__<vid>.mp4`
- 写 `config/weekly/<week>.json`：`this_week_candidates` / `picks` / `classic_comeback` / `narration`
- **narration 里的 vo 留空**，render 时会用 `f"第{rank}名，{dance_type}，来自 {creator}。"` 现场生成

### 4. 渲染
```bash
rm -f output/tts/<week>/*.wav          # 强制重生成 TTS
python3 -u pipeline/render_demo.py <week> > /tmp/render.log 2>&1
open output/<week>_demo.mp4
```

日志会打 `📝 文案 REVIEW` block 供人工过目，可以在渲染前 abort。

---

## 关键文件地图

| 用途 | 路径 |
|---|---|
| CDP 登录检查 | `scripts/douyin_check.py` |
| 导 cookies | `scripts/dump_cookies.py` |
| Debug aweme API | `scripts/douyin_debug.py` |
| **一体化抓取** | `scripts/douyin_fetch_clean.py` ⭐ |
| 补下漏抓的 KEEP | `scripts/douyin_fetch_remaining.py` |
| **重建 config** | `scripts/rebuild_from_dl2.py` ⭐ |
| 老搜索脚本（仅 id） | `scripts/douyin_search.py` |
| 老下载脚本（无 meta） | `scripts/douyin_dl.py` |
| 渲染器 | `pipeline/render_demo.py` |
| 输出 | `output/<week>_demo.mp4` |
| 元数据 | `assets/incoming/<week>/dl2/<vid>.json` |

## `render_demo.py` 已做的重要改动（不要回退）

1. `build_segments()` 中 vo 强制现场生成：`f"第{rank}名，{dance_type}，来自 {creator}。"`
2. `subtitles` 只放一条：`f"{dance_type}   {creator}"`（三空格分隔，无标点）
3. `vo_caption` 用 `re.sub(r"[，。！？、,\.!\?]+", " ", full_vo)` 去掉画面字幕的标点
4. 段时长逻辑：**用真片自身 duration（ffprobe 探测）**，clamp 到 `min_dur=10s / max_dur=20s`（top/classic），`default=15s`；VO 长了也会拉长
5. `concat_wavs(files, out, target_durs=[...])` 支持 pad 静音到目标段时长（音画同步关键）
6. VO 大字幕黑底叠加（画面中下部，`vo_caption` 字段驱动）
7. 混音 sidechain ducking：`[1:a]volume=2.2,asplit=2[vo][voside]; [2:a]volume=0.75; sidechaincompress=threshold=0.05:ratio=12:attack=8:release=350:makeup=1`
8. `default_dur.outro=6s / max_dur.outro=8s` 保片尾能看清

## Config schema 简化版

```json
{
  "episode": {"week":"2026-W29","theme":"...","voice":"young_female","top_n":5,"classic_n":1},
  "this_week_candidates": [
    {"id":"c1","creator":"@dbgtmlt","dance_type":"Urban Dance",
     "duration_sec":25.5,"like":1709,"tags":[...],
     "url":"https://www.douyin.com/video/<vid>",
     "local_path":"assets/incoming/<week>/c1__douyin__<vid>.mp4"}
  ],
  "classics_pool":[{...k1...}],
  "picks":[{"rank":1,"id":"c1","difficulty":{"stars":3.0,...}},...],
  "classic_comeback":{"id":"k1","difficulty":{...}},
  "narration":[
    {"segment":"top","rank":1,"vo":"","subtitle":[],
     "on_screen":{"stars":3.0,"tag":"本周No.1","core_moves":["Urban Dance"]}}
  ]
}
```

## 舞种识别 map（在 `douyin_fetch_clean.py` 里）
```
urban → Urban Dance
jazz/爵士 → Jazz
hiphop/嘻哈 → Hip-hop
popping/机械 → Popping
locking → Locking
kpop/女团/翻跳/cover → K-pop
choreo/编舞 → 编舞 Choreography
```

## 用户反馈的历史坑（都是真实教训）

1. **首版 config 挂假 IG 链接、`@1milliondance` 全体默认** → 不真实的署名不能忍。必须从 aweme detail 拿真 author。
2. **AI 生成的 VO 全是废话** → 用户直接否决"AI 说的都是废话"。文案要么留空要么只写事实（舞种+作者）。
3. **抓来一堆分解/教学/萌娃/battle** → 关键词太宽（"街舞"）+ 没过滤。改窄关键词 + EXCLUDE 词表。
4. **舞段太短（4-5s）** → default_dur/min_dur 拉到 10s+，voice wav pad 静音。
5. **BGM 盖住人声** → sidechain compressor ducking。
6. **片尾"消失"** → 其实一直在（4s 太短没注意）。拉到 6s。
7. **一个个翻视频页太慢** → 用户已明示未来改博主主页直取 + 筛选框。

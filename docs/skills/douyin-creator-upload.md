# 抖音创作者中心 · 自动上传（CDP 驱动）

把渲染好的成片自动传到 `creator.douyin.com/creator-micro/content/upload`，
填标题/简介/话题；只有 evaluation 已通过且显式加 `--publish` 才自动点击发布。

前置：本地已启动 debug Chrome (`--remote-debugging-port=9222`) 并已登录
抖音创作者中心。详见 `local-chrome-cdp` skill。

---

## 头号坑：大文件上传

`Locator.set_input_files` / `FileChooser.set_files` 都会抛：

```
Cannot transfer files larger than 50Mb to a browser not co-located with the server
```

成片一般 70-100MB。旧方案走原始 CDP `DOM.setFileInputFiles` 绕过 50MB 上限，
但 Chrome 152 实测连续两次在 77MB 注入后 `Target crashed`。

当前标准方案：**发布前自动压到 48MiB 以下，再走 Playwright 原生
`set_input_files`**。压缩只降低视频码率，音频直接 copy，时长和内容不变；
实测 77.2MB → 32.9MB 后一次发布成功。

```python
from playwright.sync_api import sync_playwright

MP4 = '/Users/jax/bestdancer/output/2026-W30-B_demo.mp4'
with sync_playwright() as p:
    b = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
    ctx = b.contexts[0]
    pg = next(x for x in ctx.pages if 'creator.douyin.com' in (x.url or ''))
    pg.bring_to_front()
    cdp = ctx.new_cdp_session(pg)
    doc = cdp.send('DOM.getDocument', {'depth': -1, 'pierce': True})
    node = cdp.send('DOM.querySelector',
                    {'nodeId': doc['root']['nodeId'],
                     'selector': 'input[type="file"]'})
    cdp.send('DOM.setFileInputFiles',
             {'files': [MP4], 'nodeId': node['nodeId']})
```

上传启动后页面自动跳到
`creator.douyin.com/creator-micro/content/post/video?enter_from=publish_page`，
右侧出现上传进度面板 —— 这就是"上传已开始"的可靠信号。

---

## 完整流程

1. **从 config 生成标题+简介**（不要硬编码）。
   - 标题上限 **30 字**（输入框显示 `N/30`）。
   - 简介带排行榜（作者+原链接）+ hashtag 行。
   - 落到 `/tmp/*.txt` 供后续步骤读。

   ```python
   import json
   b = json.load(open('config/weekly/2026-W30-B.json'))
   title = "26年第30周热舞榜（下）｜跨平台编舞精选"   # <= 30 字
   lines = [title, "", "本期排行榜："]
   for i, p in enumerate(b['picks'], 1):
       lines.append(f"{i}. {p['creator']}  {p['url']}")
   sp = b['classic_comeback']
   lines.append(f"特别加映： {sp['creator']}  {sp['url']}")
   lines += ["", "#热舞榜 #编舞 #街舞 #dance #BestDancer"]
   desc = "\n".join(lines)
   open('/tmp/title.txt', 'w').write(title)
   open('/tmp/desc.txt', 'w').write(desc)
   ```

2. **确认上传页已开且登录**：screenshot + `vision_analyze`，看到红色"上传视频"按钮
   和右上角头像即为登录状态。否则 `pg.goto(upload_url,
   wait_until='domcontentloaded')`。

3. **CDP `DOM.setFileInputFiles` 传文件**（见上）。页面只有一个
   `input[type="file"]`。

4. **填标题 + 简介**：

   ```python
   ti = pg.locator('input[placeholder*="标题"]').first
   ti.click(); ti.fill(TITLE)
   desc_el = pg.locator('[contenteditable="true"]').first
   desc_el.click()
   pg.keyboard.type(DESC)   # 简介是 contenteditable，用 type() 不是 fill()
   ```

   - 标题是普通 `<input>`（placeholder 含"标题"）。
   - 简介是 **`contenteditable` div**，`.fill()` 不稳，必须 `click()` +
     `keyboard.type()`。
   - `#话题` token 抖音自动识别成 chip，无需单独交互。

5. **verify**：截图 + vision，标题显示 `N/30`、简介有内容、话题 chip 出现、
   读进度百分比和剩余时间。

6. **发布闸门**：`upload_to_douyin.py` 读取对应 `report.json` / `report_vN.json`，
   必须 `passed=true` 且 LLM `verdict=pass`，否则拒绝上传。
7. 带 `--publish` 时等待按钮 enabled，点击发布并确认跳转到内容管理页；写
   `output/publish/<week>.json` 回执。后台显示“审核中”时状态记为
   `submitted_reviewing`，不能误写成已经公开的 `published`。没有 `--publish`
   则只上传填表，留给人工检查。
8. 已有成功回执时拒绝重复发布；确需重发必须显式 `--force`。

7. **收尾杀掉后台 poll 进程**。如果你为了等上传完成开了 background 循环，
   用户说自己发布时立刻 `process(action='kill')`——它占着 CDP 会话，还会
   `bring_to_front()` 抢焦点。

---

## Pitfalls 速查

| 症状 | 根因 | 修 |
|---|---|---|
| `Cannot transfer files larger than 50Mb` | Playwright 桥限制 | 脚本自动生成 <48MiB 的 `_publish.mp4` |
| `Target crashed`（推入大文件后） | Chrome 152 的大文件 CDP 注入不稳定 | 不再走大文件 CDP；压缩后原生上传 |
| `.fill()` 简介后内容空 | 简介是 contenteditable 不是 textarea | `click()` + `keyboard.type()` |
| 标题末尾被吞 | 超过 30 字硬上限 | 提前 `title[:30]` |
| 上传"卡住" | 88MB @ 100KB/s 需 15 分钟很正常 | 别当挂了 |
| Hermes 前台 180s 超时 | 等上传完成的 loop 太长 | `background=True, notify_on_complete=True` 或直接放手 |
| 后续 CDP 脚本连不上 | 之前的后台 poll 还在占会话 | `process(action='kill')` |
| 上传后没跳转 | URL 应从 `/content/upload` 变成 `/content/post/video?enter_from=publish_page` | 若没跳转说明文件推入失败，重试 |

---

## 简介模板（本项目专用）

```
<title，<=30 字>

<一句 hook>

本期排行榜：
1. @creator  https://.../video/<id>
2. @creator  https://.../video/<id>
...
5. @creator  https://.../video/<id>
特别加映： @creator  https://.../video/<id>

#热舞榜 #编舞 #街舞 #dance #BestDancer
```

数据源：`config/weekly/<week>.json` 的 `picks[]` + `classic_comeback`。

---

## 参考脚本

见 `scripts/upload_to_douyin.py`：

```bash
# 只上传填表
python3 scripts/upload_to_douyin.py --week 2026-W31-C

# evaluation 通过后自动发布
python3 scripts/upload_to_douyin.py --week 2026-W31-C --publish

# 发布指定的通过版本
python3 scripts/upload_to_douyin.py --week 2026-W31-C \
  --mp4 output/2026-W31-C_demo_v7.mp4 --publish
```

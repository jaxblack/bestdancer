# 抖音创作者中心 · 自动上传（CDP 驱动）

把渲染好的成片自动传到 `creator.douyin.com/creator-micro/content/upload`，
填标题/简介/话题；不代替用户点"发布"（本项目约定用户自己确认发布）。

前置：本地已启动 debug Chrome (`--remote-debugging-port=9222`) 并已登录
抖音创作者中心。详见 `local-chrome-cdp` skill。

---

## 头号坑：Playwright 50MB 传输上限

`Locator.set_input_files` / `FileChooser.set_files` 都会抛：

```
Cannot transfer files larger than 50Mb to a browser not co-located with the server
```

成片一般 80-100MB，两条 Playwright 路径全废。**解法：走原始 CDP
`DOM.setFileInputFiles`**，让 Chrome 直接读本地路径，没有中转就没有大小限制：

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

6. **不要代替用户点"发布"**。本项目约定："很好，可以了，我自己发布"。上传
   5-15 分钟（~100-300 KB/s），填完就交给用户。

7. **收尾杀掉后台 poll 进程**。如果你为了等上传完成开了 background 循环，
   用户说自己发布时立刻 `process(action='kill')`——它占着 CDP 会话，还会
   `bring_to_front()` 抢焦点。

---

## Pitfalls 速查

| 症状 | 根因 | 修 |
|---|---|---|
| `Cannot transfer files larger than 50Mb` | Playwright 桥限制 | CDP `DOM.setFileInputFiles` |
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

见 `scripts/upload_to_douyin.py`（把上述流程封装为 CLI，参数 `--week 2026-W30-B`）。

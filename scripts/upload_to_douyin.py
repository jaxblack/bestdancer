#!/usr/bin/env python3
"""upload_to_douyin.py — 把本周成片自动传到抖音创作者中心。

用法：
    python3 scripts/upload_to_douyin.py --week 2026-W30-B

前置：
    1. 本地 debug Chrome 已启动 (--remote-debugging-port=9222)
    2. 已登录 creator.douyin.com
    3. 成片存在 output/<week>_demo.mp4

流程：
    - 从 config/weekly/<week>.json 读 picks + classic_comeback
    - 生成标题（<=30 字）+ 简介（含排行榜链接 + hashtags）
    - CDP DOM.setFileInputFiles 传视频（绕开 Playwright 50MB 限制）
    - 填标题 + 简介（简介是 contenteditable，用 keyboard.type）
    - 截图 verify
    - STOP —— 让用户自己点"发布"

详见 docs/skills/douyin-creator-upload.md
"""
from __future__ import annotations
import argparse
import json
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

REPO = Path(__file__).resolve().parents[1]
UPLOAD_URL = "https://creator.douyin.com/creator-micro/content/upload"


def build_title(week: str) -> str:
    """从 week key 生成标题（<=30 字）。2026-W30-B → '26年第30周热舞榜（下）｜跨平台编舞精选'"""
    m = re.match(r"(\d{4})-W(\d{1,2})(?:-([A-Z]))?", week)
    if not m:
        return f"热舞榜 {week}"
    yr = m.group(1)[-2:]
    wn = int(m.group(2))
    letter = m.group(3) or ""
    # A=第一篇 B=第二篇 ... Z=第二十六篇；无 edition = 单期
    ORD = ["一","二","三","四","五","六","七","八","九","十",
           "十一","十二","十三","十四","十五","十六","十七","十八","十九","二十",
           "二十一","二十二","二十三","二十四","二十五","二十六"]
    ed_part = f"·第{ORD[ord(letter)-65]}篇" if letter else ""
    title = f"{yr}年第{wn}周热舞榜{ed_part}｜本周编舞精选"
    return title[:30]


def build_description(cfg: dict, title: str) -> str:
    """描述里不放跨平台 URL/字眼(只留 @作者), 抖音简介不允许外链导流也更干净。"""
    picks = cfg.get("picks", [])
    sp = cfg.get("classic_comeback", {}) or {}
    lines = [
        title,
        "",
        "本周编舞精选，你最喜欢哪一支？",
        "",
        "本期排行榜：",
    ]
    for i, p in enumerate(picks, 1):
        creator = p.get("creator", "").strip() or "@待补充"
        lines.append(f"{i}. {creator}")
    if sp.get("creator"):
        lines.append(f"特别加映： {sp.get('creator','').strip()}")
    lines += ["", "#热舞榜 #编舞 #街舞 #dance #BestDancer"]
    return "\n".join(lines)


def find_or_open_upload_page(ctx):
    # 先精确匹配 upload/post 页；其他 creator 页(manage/home)不算
    for x in ctx.pages:
        u = x.url or ""
        if "creator.douyin.com/creator-micro/content/upload" in u \
           or "creator.douyin.com/creator-micro/content/post" in u:
            return x
    # 有 creator tab 但在别的页(manage/home) -> 复用它导航
    for x in ctx.pages:
        if "creator.douyin.com" in (x.url or ""):
            x.goto(UPLOAD_URL, wait_until="domcontentloaded", timeout=30_000)
            return x
    pg = ctx.new_page()
    pg.goto(UPLOAD_URL, wait_until="domcontentloaded", timeout=30_000)
    return pg


def push_file_via_cdp(ctx, pg, mp4: Path):
    """绕开 Playwright 50MB 限制 —— CDP 直传本地路径。"""
    cdp = ctx.new_cdp_session(pg)
    doc = cdp.send("DOM.getDocument", {"depth": -1, "pierce": True})
    node = cdp.send(
        "DOM.querySelector",
        {"nodeId": doc["root"]["nodeId"], "selector": 'input[type="file"]'},
    )
    if not node.get("nodeId"):
        raise RuntimeError("找不到 input[type=file] —— 页面结构可能变了")
    cdp.send(
        "DOM.setFileInputFiles",
        {"files": [str(mp4)], "nodeId": node["nodeId"]},
    )


def fill_metadata(pg, title: str, desc: str):
    # 标题：普通 input
    ti = pg.locator('input[placeholder*="标题"]').first
    ti.click()
    ti.fill(title)
    # 简介：contenteditable，不能 .fill()，必须 keyboard.type()
    time.sleep(0.5)
    desc_el = pg.locator('[contenteditable="true"]').first
    desc_el.click()
    pg.keyboard.type(desc)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--week", required=True, help="e.g. 2026-W30-B")
    ap.add_argument(
        "--mp4",
        help="视频路径。默认 output/<week>_demo.mp4",
    )
    ap.add_argument("--cdp", default="http://127.0.0.1:9222")
    ap.add_argument(
        "--screenshot",
        default="/tmp/douyin_upload_verify.png",
        help="上传+填表后落地截图给人肉/vision 检查",
    )
    args = ap.parse_args()

    cfg_path = REPO / "config" / "weekly" / f"{args.week}.json"
    if not cfg_path.exists():
        sys.exit(f"config 不存在: {cfg_path}")
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))

    mp4 = Path(args.mp4) if args.mp4 else REPO / "output" / f"{args.week}_demo.mp4"
    if not mp4.exists():
        sys.exit(f"成片不存在: {mp4}")
    size_mb = mp4.stat().st_size / 1_000_000
    print(f"[+] 成片 {mp4.name}  {size_mb:.1f} MB")

    title = build_title(args.week)
    desc = build_description(cfg, title)
    print(f"[+] 标题 ({len(title)}/30): {title}")
    print(f"[+] 简介 {len(desc)} 字符")

    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp(args.cdp)
        if not b.contexts:
            sys.exit("CDP 无 context —— Chrome 未启动 debug 端口?")
        ctx = b.contexts[0]

        pg = find_or_open_upload_page(ctx)
        pg.bring_to_front()
        time.sleep(2)

        # 若已在 post/video（前一次残留），直接跳回 upload
        if "content/post/video" in (pg.url or ""):
            pg.goto(UPLOAD_URL, wait_until="domcontentloaded", timeout=30_000)
            time.sleep(2)

        print(f"[+] 页面: {pg.url}")
        print("[+] 推入 mp4 via CDP.setFileInputFiles ...")
        push_file_via_cdp(ctx, pg, mp4)

        # 等页面跳到 post/video；抖音有时上传启动前会先弹认证/预审
        # 弹窗，跳转可能延后 20-60 秒，别一超时就放弃
        for i in range(90):
            time.sleep(1)
            if "content/post/video" in (pg.url or ""):
                break
            if i and i % 15 == 0:
                print(f"    等待跳转... {i}s (URL 仍是 {pg.url})")
        else:
            print("[warn] URL 未跳转，但文件可能已入队；继续尝试填表")
        print(f"[+] 上传已启动: {pg.url}")

        time.sleep(3)
        print("[+] 填标题 + 简介 ...")
        fill_metadata(pg, title, desc)

        time.sleep(2)
        pg.screenshot(path=args.screenshot)
        print(f"[+] 已截图: {args.screenshot}")
        print()
        print("=" * 60)
        print("上传进行中。标题/简介/话题已填。")
        print("请在浏览器里等上传条到 100%，检查后自己点【发布】。")
        print("=" * 60)


if __name__ == "__main__":
    main()

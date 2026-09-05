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
    - 校验 evaluation 已通过
    - 截图 verify
    - 带 --publish 时等待上传完成、点击发布并验证成功

详见 docs/skills/douyin-creator-upload.md
"""
from __future__ import annotations
import argparse
import hashlib
import json
import random
import re
import subprocess
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
    # 先访问 creator 首页 -> 有 referer / session 再到 upload
    try:
        pg.goto("https://creator.douyin.com/", wait_until="domcontentloaded", timeout=30_000)
        time.sleep(random.uniform(1.5, 3.0))
    except Exception:
        pass
    pg.goto(UPLOAD_URL, wait_until="domcontentloaded", timeout=30_000)
    return pg


def push_file_via_cdp(ctx, pg, mp4: Path):
    """绕开 Playwright 50MB 限制 —— CDP 直传本地路径。"""
    # 页面是异步挂载的。旧实现固定等 2 秒就 querySelector，偶尔 DOM 还没出现；
    # Playwright 随后能看到 input，CDP 那一刻却返回 0。先明确等 attached。
    pg.locator('input[type="file"]').first.wait_for(
        state="attached", timeout=30_000)
    cdp = ctx.new_cdp_session(pg)
    # Runtime.evaluate + DOM.requestNode 比 getDocument/querySelector 更可靠：
    # React 动态树/iframe 重挂载时，后者拿到的 document snapshot 可能已过期。
    result = cdp.send("Runtime.evaluate", {
        "expression": 'document.querySelector(\'input[type="file"]\')',
        "returnByValue": False,
    })
    object_id = (result.get("result") or {}).get("objectId")
    node_id = 0
    if object_id:
        node_id = cdp.send(
            "DOM.requestNode", {"objectId": object_id}).get("nodeId", 0)
    if not node_id:
        doc = cdp.send("DOM.getDocument", {"depth": -1, "pierce": True})
        node_id = cdp.send(
            "DOM.querySelector",
            {"nodeId": doc["root"]["nodeId"],
             "selector": 'input[type="file"]'},
        ).get("nodeId", 0)
    if not node_id:
        raise RuntimeError("找不到 input[type=file] —— 页面结构可能变了")
    cdp.send(
        "DOM.setFileInputFiles",
        {"files": [str(mp4)], "nodeId": node_id},
    )


def push_file(ctx, pg, mp4: Path):
    """发布文件已由 prepare_upload_file 控制在 48MiB 内，走原生上传最稳定。"""
    if mp4.stat().st_size >= 48 * 1024 * 1024:
        raise RuntimeError("发布文件仍超过 48MiB，拒绝走已知会崩溃的大文件 CDP 路径")
    pg.locator('input[type="file"]').first.set_input_files(str(mp4))


def prepare_upload_file(mp4: Path) -> Path:
    """大文件自动生成 <48MiB 的发布副本，画面/时长不变，仅降低码率。

    Chrome 152 实测连续两次在 77MB 文件 DOM.setFileInputFiles 后 target crashed；
    32.9MB 副本走 Playwright 原生上传一次成功。
    """
    limit = 48 * 1024 * 1024
    if mp4.stat().st_size < limit:
        return mp4
    output = mp4.with_name(mp4.stem + "_publish.mp4")
    if (output.exists() and output.stat().st_size < limit
            and output.stat().st_mtime >= mp4.stat().st_mtime):
        return output
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nk=1:nw=1", str(mp4)],
        capture_output=True, text=True, check=True)
    duration = float(probe.stdout.strip())
    # 目标 39MiB，给容器/音频留余量。不能设 1.8Mbps 的硬下限：
    # 210s 视频即使 1.8Mbps + 192kbps 音频也会超过 48MiB。
    video_bps = int(max(
        600_000,
        min(4_500_000, (39 * 1024 * 1024 * 8 / duration) - 220_000)))
    for attempt in range(2):
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(mp4),
             "-c:v", "libx264", "-preset", "medium",
             "-b:v", str(video_bps), "-maxrate", str(int(video_bps * 1.12)),
             "-bufsize", str(int(video_bps * 2.24)),
             "-c:a", "copy", "-movflags", "+faststart", str(output)],
            check=True)
        if output.exists() and output.stat().st_size < limit:
            break
        video_bps = int(video_bps * 0.78)
    if not output.exists() or output.stat().st_size >= limit:
        raise RuntimeError(
            f"发布副本压缩失败或仍超过 48MiB: {output}")
    print(f"[+] 大文件自动压缩: {mp4.name} -> {output.name} "
          f"({output.stat().st_size / 1_000_000:.1f} MB)")
    return output


def _human_type(pg, text: str):
    for i, ch in enumerate(text):
        pg.keyboard.type(ch)
        d = random.uniform(0.05, 0.16)
        if i > 0 and i % random.randint(6, 14) == 0:
            d += random.uniform(0.2, 0.5)
        time.sleep(d)


def fill_metadata(pg, title: str, desc: str):
    # 标题：普通 input, 逐字符输入
    ti = pg.locator('input[placeholder*="标题"]').first
    ti.click()
    time.sleep(random.uniform(0.3, 0.6))
    pg.keyboard.press("Meta+A"); time.sleep(0.1)
    pg.keyboard.press("Backspace"); time.sleep(random.uniform(0.2, 0.4))
    _human_type(pg, title)
    # 简介：contenteditable，不能 .fill()，必须 keyboard.type()
    time.sleep(random.uniform(0.6, 1.2))
    desc_el = pg.locator('[contenteditable="true"]').first
    desc_el.click()
    time.sleep(random.uniform(0.3, 0.6))
    _human_type(pg, desc)


def require_passed_evaluation(week: str, mp4: Path) -> dict:
    """只有 score>=threshold 且 verdict=pass 的版本才能发布。"""
    if not re.fullmatch(
            rf"{re.escape(week)}_demo(?:_v\d+)?\.mp4", mp4.name):
        raise RuntimeError(
            f"视频文件名与期号不匹配或不是可评估源文件: {mp4.name}")
    eval_dir = REPO / "output" / "eval" / week
    m = re.search(r"_v(\d+)(?:_publish)?\.mp4$", mp4.name)
    report_path = eval_dir / (f"report_v{m.group(1)}.json" if m else "report.json")
    if not report_path.exists():
        raise RuntimeError(f"缺少 evaluation 报告: {report_path}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    verdict = (report.get("llm") or {}).get("verdict")
    if not report.get("passed") or verdict != "pass":
        raise RuntimeError(
            f"evaluation 未通过: score={report.get('final_score')} "
            f"threshold={report.get('threshold')} verdict={verdict}")
    expected_digest = report.get("video_sha256")
    if not expected_digest:
        raise RuntimeError(
            "evaluation 报告没有 video_sha256，必须用当前版本 evaluator 重跑后再发布")
    digest = hashlib.sha256()
    with mp4.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != expected_digest:
        raise RuntimeError(
            "视频 SHA-256 与 evaluation 报告不一致，拒绝用别的文件套通过报告")
    return report


def exact_button(pg, text: str):
    return pg.locator("button").filter(has_text=re.compile(rf"^\s*{re.escape(text)}\s*$"))


def wait_and_publish(pg, timeout_sec: int, screenshot: Path) -> dict:
    """等上传/平台审核完成，点击发布并验证成功。"""
    deadline = time.time() + timeout_sec
    publish_button = None
    last_state = ""
    while time.time() < deadline:
        body = ""
        try:
            body = pg.locator("body").inner_text(timeout=5_000)
        except Exception:
            pass
        if any(text in body for text in ("上传失败", "发布失败", "视频处理失败")):
            pg.screenshot(path=str(screenshot))
            raise RuntimeError("页面显示上传/处理失败，请查看截图")

        candidates = exact_button(pg, "发布")
        for i in range(candidates.count()):
            button = candidates.nth(i)
            try:
                if button.is_visible() and button.is_enabled():
                    publish_button = button
                    break
            except Exception:
                continue
        if publish_button is not None:
            break

        state = next(
            (text for text in ("上传中", "处理中", "审核中", "上传完成")
             if text in body), "等待发布按钮")
        if state != last_state:
            print(f"    {state} ...", flush=True)
            last_state = state
        time.sleep(5)
    if publish_button is None:
        pg.screenshot(path=str(screenshot))
        raise TimeoutError(f"{timeout_sec}s 内发布按钮未就绪")

    pg.screenshot(path=str(screenshot))
    print("[+] 上传已完成，点击【发布】 ...", flush=True)
    pg.once("dialog", lambda dialog: dialog.accept())
    publish_button.click()

    # 有些版本会再弹一个 DOM 确认框。
    time.sleep(2)
    confirm = exact_button(pg, "确认发布")
    for i in range(confirm.count()):
        button = confirm.nth(i)
        if button.is_visible() and button.is_enabled():
            button.click()
            break

    success_deadline = time.time() + 180
    while time.time() < success_deadline:
        url = pg.url or ""
        body = ""
        try:
            body = pg.locator("body").inner_text(timeout=5_000)
        except Exception:
            pass
        if "/content/manage" in url:
            status = "submitted_reviewing" if "审核中" in body else "submitted"
            if "已发布" in body and "审核中" not in body:
                status = "published"
            return {"status": status, "url": url, "submitted_at": time.time()}
        if "发布成功" in body:
            return {"status": "submitted", "url": url, "submitted_at": time.time()}
        if any(text in body for text in ("发布失败", "审核不通过")):
            pg.screenshot(path=str(screenshot))
            raise RuntimeError("点击发布后页面显示失败，请查看截图")
        time.sleep(3)
    pg.screenshot(path=str(screenshot))
    raise TimeoutError("点击发布后 180s 内未确认发布成功")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--week", required=True, help="e.g. 2026-W30-B")
    ap.add_argument(
        "--mp4",
        help="视频路径。默认 output/<week>_demo.mp4",
    )
    ap.add_argument("--cdp", default="http://127.0.0.1:9222")
    ap.add_argument("--publish", action="store_true",
                    help="等待上传完成后自动点击发布；不加则只上传并填表")
    ap.add_argument("--force", action="store_true",
                    help="忽略已有发布回执并允许重发")
    ap.add_argument("--wait-timeout", type=int, default=1800,
                    help="等待上传完成的最长秒数，默认 30 分钟")
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
    report = require_passed_evaluation(args.week, mp4)
    source_mp4 = mp4
    mp4 = prepare_upload_file(mp4)
    size_mb = mp4.stat().st_size / 1_000_000
    print(f"[+] 成片 {mp4.name}  {size_mb:.1f} MB")
    print(f"[+] evaluation PASS: {report['final_score']}/{report['threshold']}")

    receipt_path = REPO / "output" / "publish" / f"{args.week}.json"
    if receipt_path.exists() and not args.force:
        previous = json.loads(receipt_path.read_text(encoding="utf-8"))
        if previous.get("status") in {
                "published", "submitted", "submitted_reviewing"}:
            sys.exit(f"本期已有发布回执，拒绝重复发布: {receipt_path}（重发需 --force）")

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
        push_file(ctx, pg, mp4)

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
        if not args.publish:
            print()
            print("=" * 60)
            print("上传进行中。标题/简介/话题已填。")
            print("未加 --publish：请在浏览器里检查后自己点【发布】。")
            print("=" * 60)
            return

        receipt = wait_and_publish(
            pg, args.wait_timeout, Path(args.screenshot))
        receipt.update({
            "week": args.week,
            "video": str(mp4.resolve()),
            "source_video": str(source_mp4.resolve()),
            "title": title,
            "evaluation_score": report["final_score"],
        })
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2),
            encoding="utf-8")
        print(f"[+] 已提交抖音（状态 {receipt['status']}），回执: "
              f"{receipt_path.relative_to(REPO)}")


if __name__ == "__main__":
    main()

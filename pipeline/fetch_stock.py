#!/usr/bin/env python3
"""本周热舞 · 合法真实素材抓取

默认 **Mixkit**（免密钥，许可允许免费下载与使用），可选 **Pexels**（需免费 key，竖版更好）。
不抓取 YouTube / 抖音 / Instagram —— 违反其服务条款且涉及他人版权。

用法:
    python pipeline/fetch_stock.py 2026-W29                         # Mixkit, dance, c1..c5
    python pipeline/fetch_stock.py 2026-W29 --query dancing --n 6
    python pipeline/fetch_stock.py 2026-W29 --source pexels --query "kpop dance"
    python pipeline/fetch_stock.py 2026-W29 --ids c1,c2,c3,c4,c5,k1

下载到 assets/incoming/<week>/<id>__<source>__<vid>.mp4，并写 CREDITS.txt（署名/来源）。
之后 python pipeline/render_demo.py <week> 会自动把真片当背景。仅用标准库。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120 Safari/537.36"}


def _download(url, dst):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=180) as r, open(dst, "wb") as f:
        f.write(r.read())


def from_mixkit(query, n):
    cat = re.sub(r"\s+", "-", query.strip().lower()) or "dance"
    page = f"https://mixkit.co/free-stock-video/{cat}/"
    with urllib.request.urlopen(urllib.request.Request(page, headers=UA), timeout=30) as r:
        html = r.read().decode("utf-8", "ignore")
    seen, out = set(), []
    for u in re.findall(r"https://assets\.mixkit\.co/videos/\d+/\d+-1080\.mp4", html):
        if u not in seen:
            seen.add(u)
            out.append(("mixkit", u, u.split("/")[4], f"Mixkit free license | {page}"))
        if len(out) >= n:
            break
    return out


def from_pexels(query, n, key):
    url = (f"https://api.pexels.com/videos/search?query={urllib.parse.quote(query)}"
           f"&orientation=portrait&per_page={max(n + 5, 10)}")
    with urllib.request.urlopen(urllib.request.Request(url, headers={"Authorization": key}),
                                timeout=30) as r:
        vids = json.loads(r.read().decode("utf-8")).get("videos", [])
    out = []
    for v in vids[:n]:
        files = v.get("video_files", [])
        p = [f for f in files if f.get("height", 0) > f.get("width", 0)] or files
        p.sort(key=lambda f: abs(f.get("height", 0) - 1600))
        if p:
            out.append(("pexels", p[0]["link"], str(v["id"]),
                        f"{v.get('user', {}).get('name', '')} | {v.get('url', '')}"))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("week")
    ap.add_argument("--source", choices=["mixkit", "pexels"], default="mixkit")
    ap.add_argument("--query", default="dance")
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--ids", default="", help="逗号分隔，如 c1,c2,c3；默认 c1..cN")
    args = ap.parse_args()

    ids = [x.strip() for x in args.ids.split(",") if x.strip()] or \
          [f"c{i}" for i in range(1, args.n + 1)]

    try:
        if args.source == "pexels":
            key = os.environ.get("PEXELS_API_KEY")
            if not key:
                print("[x] 未设置 PEXELS_API_KEY。去 https://www.pexels.com/api/ 免费申请后：")
                print('    $env:PEXELS_API_KEY = "你的key"，再重跑。')
                return 2
            items = from_pexels(args.query, len(ids), key)
        else:
            items = from_mixkit(args.query, len(ids))
    except Exception as e:  # noqa: BLE001
        print(f"[x] 抓取失败：{e}")
        return 1

    if not items:
        print("[x] 没找到素材，换个 --query 再试。")
        return 1

    out_dir = REPO / "assets" / "incoming" / args.week
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*.mp4"):
        old.unlink()

    credits, got = ["file,source"], 0
    for cid, (src, link, vid, cred) in zip(ids, items):
        dst = out_dir / f"{cid}__{src}__{vid}.mp4"
        try:
            _download(link, dst)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] 下载失败 {cid}: {e}")
            continue
        got += 1
        credits.append(f"{dst.name},{cred}")
        print(f"[ok] {cid} <- {link}")

    (out_dir / "CREDITS.txt").write_text("\n".join(credits) + "\n", "utf-8")
    print(f"\n完成：{got}/{len(ids)} -> {out_dir.relative_to(REPO).as_posix()}")
    print("下一步：python pipeline/render_demo.py " + args.week)
    return 0 if got else 1


if __name__ == "__main__":
    raise SystemExit(main())

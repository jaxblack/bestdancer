#!/usr/bin/env python3
"""daily_auto_generate.py — 每天自动生成本周下一期的候选池 + 成片,供 dashboard review.

流程 (每天跑一次):
  1. 判定当前 ISO 周 (YYYY-Www), 决定下一 edition (A/B/C/... 找第一个还没 render 的字母)
  2. 复用 A 池 (若存在) 或触发 discover (未实装 -- daily 只做 rebuild+render)
  3. 从 A 池挑合规候选 (≤180s + 未黑名单 + 未在其他 edition 用过)
  4. render 出 output/<week>_demo.mp4
  5. dashboard 通过 /api/state 自动看到新 demo

用户 review 后手动上传 (upload_to_douyin.py). 本脚本**不上传**.
"""
from __future__ import annotations
import json, re, subprocess, sys, datetime, copy, os
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WEEKLY = REPO / "config" / "weekly"
BL = REPO / "config" / "blacklist.json"


def iso_week() -> str:
    y, w, _ = datetime.date.today().isocalendar()
    return f"{y}-W{w:02d}"


def used_urls(exclude_week: str) -> set[str]:
    """收集需要排除的 URL: 黑名单 + '同 ISO 周内其他 edition' 已用.
    跨周(如 W30-*) 不算数, 让 W31-* 可以复用 W30 素材(隔一周对观众是新内容)."""
    used = set(json.load(open(BL))["urls"])
    # exclude_week: '2026-W31-B' -> week_prefix='2026-W31'
    week_prefix = exclude_week.rsplit("-", 1)[0] if exclude_week.count("-") >= 2 else exclude_week
    for p in WEEKLY.glob("*.json"):
        if p.stem == exclude_week: continue
        if not p.stem.startswith(week_prefix): continue  # 仅同 ISO 周
        try: c = json.load(open(p))
        except Exception: continue
        for pk in c.get("picks", []):
            if pk.get("url"): used.add(pk["url"])
            for u in pk.get("source_urls", []) or []:
                if u: used.add(u)
        sp = c.get("classic_comeback") or {}
        if sp.get("url"): used.add(sp["url"])
        for pk in sp.get("picks", []) if isinstance(sp, dict) else []:
            for u in (pk.get("source_urls") or []):
                if u: used.add(u)
    return used

def probe_dur(mp4: Path) -> float:
    try:
        r = subprocess.run(["ffprobe","-v","error","-show_entries","format=duration",
                            "-of","default=nk=1:nw=1",str(mp4)], capture_output=True, text=True)
        return float(r.stdout.strip() or 0)
    except Exception:
        return 0.0


def source_pool() -> Path:
    """A 池 = 本周或最近 W\\d{2}-A.json (若无 -A, fall back 到无 edition)."""
    week = iso_week()
    p = WEEKLY / f"{week}-A.json"
    if p.exists(): return p
    p = WEEKLY / f"{week}.json"
    if p.exists(): return p
    # 最近的
    cands = sorted(WEEKLY.glob("????-W??-A.json"), reverse=True)
    return cands[0] if cands else None


def next_target() -> tuple[str, str]:
    """决定下一期 week+edition:
    找已存在的 <week>-<L>.json 里最新的一个(按 week desc, letter desc),
    然后 letter+1 (Z 到顶就 week+1 从 A 开始). 若一个都没有, 用 iso_week()+A.
    """
    import re as _re
    entries = []
    for p in WEEKLY.glob("????-W??-?.json"):
        m = _re.match(r"(\d{4})-W(\d{1,2})-([A-Z])$", p.stem)
        if m:
            entries.append((int(m.group(1)), int(m.group(2)), m.group(3), p.stem))
    if not entries:
        return iso_week(), "A"
    entries.sort(reverse=True)
    yr, wn, letter, stem = entries[0]
    if letter < "Z":
        return f"{yr}-W{wn:02d}", chr(ord(letter) + 1)
    # 满到 Z: 进入下一 ISO 周
    import datetime as _dt
    nxt = _dt.date.fromisocalendar(yr, wn, 1) + _dt.timedelta(days=7)
    y2, w2, _ = nxt.isocalendar()
    return f"{y2}-W{w2:02d}", "A"


def all_downloaded() -> dict[str, Path]:
    dl = {}
    for wk in REPO.glob("assets/incoming/*/dl2"):
        for f in wk.glob("*.mp4"):
            m = re.match(r"([a-z]+)_([0-9a-f]+)\.mp4", f.name)
            if m: dl[m.group(2)] = f
    return dl


def build_week(week: str, edition: str) -> tuple[Path, list[str]]:
    target = f"{week}-{edition}"
    pool_p = source_pool()
    if not pool_p:
        raise SystemExit("no A pool found")
    a = json.load(open(pool_p))
    used = used_urls(target)
    dl = all_downloaded()

    def extract_vid(url: str) -> str | None:
        m = re.search(r"/video/(\d+)|/(?:explore|search_result)/([0-9a-f]+)", url or "")
        return (m.group(1) or m.group(2)) if m else None

    # 收合规候选 (未用+已下载+8-180s)
    opts = []
    for x in a.get("this_week_candidates", []) + a.get("classics_pool", []):
        url = x.get("url", "") or ""
        if not url or url in used: continue
        vid = extract_vid(url)
        if not vid or vid not in dl: continue
        dur = probe_dur(dl[vid])
        if dur > 180 or dur < 8: continue
        opts.append((x.get("like", 0), dur, x, dl[vid], vid))
    opts.sort(key=lambda t: -t[0])

    if len(opts) < 5:
        raise SystemExit(f"合规候选不足 5 支 (只有 {len(opts)} 支). 需先补 discover/download.")

    top = opts[:5]
    warnings = []
    if len(opts) < 6:
        warnings.append("无加映候选 (只出 TOP5)")

    # 建 config
    tpl_p = next((WEEKLY / n for n in [f"{week}-A.json", "2026-W30-A.json"] if (WEEKLY / n).exists()), pool_p)
    cfg = copy.deepcopy(json.load(open(tpl_p)))
    cfg["week"] = target
    # 关键 bug 防护: episode.week 也要同步, 不然 intro_vo 会读到模板的旧 week/edition
    if "episode" in cfg and isinstance(cfg["episode"], dict):
        cfg["episode"]["week"] = target
    cfg["this_week_candidates"] = [copy.deepcopy(x) for x in a.get("this_week_candidates", []) if x.get("url","") not in used]
    cfg["classics_pool"] = [copy.deepcopy(x) for x in a.get("classics_pool", []) if x.get("url","") not in used]
    cfg["deleted_ids"] = []

    def mkpick(rec, rank, dt="Urban", stars=3):
        pp = copy.deepcopy(rec)
        pp["rank"] = rank; pp["dance_type"] = dt
        pp["difficulty"] = {"stars": stars, "fit": dt, "scores": {}}
        return pp

    cfg["picks"] = [mkpick(t[2], i+1) for i, t in enumerate(top)]
    if len(opts) >= 6:
        cfg["classic_comeback"] = mkpick(opts[5][2], 6)
    else:
        cfg["classic_comeback"] = {}

    # narration
    def mkvo(rank, dt, song, creator, classic=False):
        creator = (creator or "").lstrip("@"); sp = f" {song}" if song else ""
        if classic: return f"特别加映，{dt}街舞{sp}，来自 {creator}。"
        return f"第{rank}名，{dt}街舞{sp}，来自 {creator}。"

    narr = []
    for pp in cfg["picks"]:
        narr.append({"segment":"top","rank":pp["rank"],"vo":mkvo(pp["rank"],pp["dance_type"],pp.get("song",""),pp.get("creator","")),
                     "voice":"zh-CN-XiaoyiNeural","voice_rate":"+20%","subtitle":[],
                     "on_screen":{"stars":int(pp["difficulty"]["stars"]),"tag":f"本周No.{pp['rank']}","core_moves":[pp["dance_type"]]},"beginner_tip":""})
    sp = cfg["classic_comeback"]
    if sp:
        narr.append({"segment":"classic","rank":None,"vo":mkvo(None,sp["dance_type"],sp.get("song",""),sp.get("creator",""),True),
                     "voice":"zh-CN-XiaoyiNeural","voice_rate":"+20%","subtitle":[],
                     "on_screen":{"stars":int(sp["difficulty"]["stars"]),"tag":"特别加映","core_moves":[sp["dance_type"]]},"beginner_tip":""})
    cfg["narration"] = narr

    # 铺 staging
    stage = REPO / f"assets/incoming/{target}"
    stage.mkdir(exist_ok=True)
    (stage / "dl2").mkdir(exist_ok=True)
    for t in top + ([opts[5]] if len(opts) >= 6 else []):
        rec, dl_f, vid = t[2], t[3], t[4]
        cid = rec["id"]
        # 平台前缀
        plat_m = re.match(r"([a-z]+)_", dl_f.name)
        plat = plat_m.group(1) if plat_m else "xiaohongshu"
        tgt = stage / f"{cid}__{plat}__{vid}.mp4"
        if tgt.exists(): tgt.unlink()
        os.link(dl_f, tgt)
        meta_src = dl_f.with_suffix(".json")
        meta_tgt = stage / "dl2" / f"{plat}_{vid}.json"
        if meta_src.exists() and not meta_tgt.exists():
            meta_tgt.write_text(meta_src.read_text())
        dl_tgt = stage / "dl2" / f"{plat}_{vid}.mp4"
        if not dl_tgt.exists(): os.link(dl_f, dl_tgt)

    out_p = WEEKLY / f"{target}.json"
    json.dump(cfg, open(out_p, "w"), ensure_ascii=False, indent=2)
    return out_p, warnings


def main() -> None:
    week, edition = next_target()
    target = f"{week}-{edition}"
    print(f"[daily] target = {target}")
    try:
        cfg_p, warnings = build_week(week, edition)
    except SystemExit as e:
        print(f"[daily] SKIP: {e}"); return
    for w in warnings: print(f"[daily] WARN: {w}")

    # render
    for d in ["output/tts", "output/tmp"]:
        p = REPO / d / target
        if p.exists(): subprocess.run(["rm","-rf",str(p)])
    demo = REPO / "output" / f"{target}_demo.mp4"
    if demo.exists(): demo.unlink()

    cmd = [sys.executable, "-u", str(REPO / "pipeline/render_demo.py"), target]
    print(f"[daily] render: {' '.join(cmd)}")
    r = subprocess.run(cmd, cwd=str(REPO))
    if r.returncode != 0:
        print(f"[daily] render FAILED rc={r.returncode}"); sys.exit(r.returncode)

    if demo.exists():
        size_mb = demo.stat().st_size / 1024 / 1024
        print(f"[daily] OK -> {demo.relative_to(REPO)} ({size_mb:.1f} MB)")
        print(f"[daily] review at http://127.0.0.1:8787/?week={target}")
    else:
        print("[daily] demo file missing after render")
        sys.exit(1)


if __name__ == "__main__":
    main()

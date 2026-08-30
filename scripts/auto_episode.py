#!/usr/bin/env python3
"""auto_episode.py — 一条命令跑完一期: 发现 → 下载 → 组稿 → 渲染 → 评估 → 闸门。

    python3 scripts/auto_episode.py                       # 全自动出下一期
    python3 scripts/auto_episode.py --skip-discover       # 用现有候选池, 不再抓
    python3 scripts/auto_episode.py --week 2026-W31 --edition C
    python3 scripts/auto_episode.py --threshold 85 --max-attempts 3
    python3 scripts/auto_episode.py --publish             # 及格后直接进发布脚本

设计要点:
  * 每一步都能单独跳过, 卡住时不用从头再来;
  * 评估不及格会自动"换素材再渲一次", 最多 --max-attempts 次;
  * 没及格就绝不进入发布分支 (评估脚本 fail-closed)。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WEEKLY = REPO / "config" / "weekly"
INCOMING = REPO / "assets" / "incoming"
PY = sys.executable

CDP_URL = "http://127.0.0.1:9222/json/version"
CHROME_APP = "Google Chrome"
# Chrome 136+ 在默认 profile 上会直接无视 --remote-debugging-port,
# 必须用独立的 user-data-dir, 这个目录里是各平台已登录的会话。
CHROME_PROFILE = Path.home() / ".chrome-debug-profile"


def load_daily_module():
    spec = importlib.util.spec_from_file_location(
        "daily_auto_generate", REPO / "scripts" / "daily_auto_generate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


daily = load_daily_module()


def log(msg: str) -> None:
    print(f"[auto] {msg}", flush=True)


def sh(cmd: list[str], **kw) -> int:
    log("$ " + " ".join(str(c) for c in cmd))
    return subprocess.run(cmd, cwd=str(REPO), **kw).returncode


# ────────────────────────── 0. 前置检查 ──────────────────────────

def cdp_alive(timeout: float = 3.0) -> bool:
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(CDP_URL, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def mute_browser_once() -> None:
    """已经在跑的 Chrome 可能没带 --mute-audio, 补一刀。"""
    subprocess.run([PY, str(REPO / "scripts" / "mute_browser.py"), "--once"],
                   cwd=str(REPO), capture_output=True)


def start_mute_watchdog() -> subprocess.Popen | None:
    """抓取过程中不断给新开的页面静音, 结束后由调用方 terminate。"""
    try:
        return subprocess.Popen([PY, str(REPO / "scripts" / "mute_browser.py")],
                                cwd=str(REPO), stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
    except Exception:
        return None


def ensure_cdp(auto_launch: bool = True, wait_s: int = 25) -> bool:
    if cdp_alive():
        log("CDP Chrome 已就绪")
        mute_browser_once()
        return True
    if not auto_launch:
        return False
    log(f"CDP 没起来, 用调试 profile 拉一个: {CHROME_PROFILE}")
    subprocess.run(["open", "-na", CHROME_APP, "--args",
                    "--remote-debugging-port=9222",
                    f"--user-data-dir={CHROME_PROFILE}",
                    # 抓取时会打开抖音/TikTok 详情页并自动播放, 一律静音, 别吵到人
                    "--mute-audio"], check=False)
    for _ in range(wait_s):
        time.sleep(1)
        if cdp_alive():
            log("CDP Chrome 起来了 (已静音)")
            return True
    log("CDP 仍未就绪 —— 抓取类步骤会失败")
    return False


def preflight() -> list[str]:
    problems = []
    for binary in ("ffmpeg", "ffprobe"):
        if subprocess.run(["which", binary], capture_output=True).returncode != 0:
            problems.append(f"缺 {binary}")
    if not CHROME_PROFILE.exists():
        problems.append(f"调试 profile 不存在: {CHROME_PROFILE} (需先手动登录各平台一次)")
    return problems


# ────────────────────────── 1. 选期号 ──────────────────────────

def resolve_target(week: str | None, edition: str | None) -> tuple[str, str]:
    if week and edition:
        return week, edition
    # daily.next_target() 是"最新已存在的期号 +1"。但本脚本一开始就会把候选池写进
    # config/weekly/<target>.json, 所以中途失败后重跑会被当成"这期已存在"而跳到下一封号,
    # 留下一个半成品。这里先看最新一期有没有出片, 没出就接着做它。
    latest = sorted(WEEKLY.glob("????-W??-?.json"), reverse=True)
    if latest and not week and not edition:
        stem = latest[0].stem
        m = re.match(r"(\d{4}-W\d{2})-([A-Z])$", stem)
        if m and not (REPO / "output" / f"{stem}_demo.mp4").exists():
            log(f"{stem} 有配置但没成片, 接着做这一期而不是新开一期")
            return m.group(1), m.group(2)
    w, e = daily.next_target()
    return week or w, edition or e


# ────────────────────────── 2. 候选池 → 周配置 ──────────────────────────

DANCE_MAP = [
    (r"urban", "Urban"), (r"jazz|爵士", "Jazz"),
    (r"hiphop|hip[- ]?hop|嘻哈", "Hip-hop"), (r"popping|机械", "Popping"),
    (r"locking", "Locking"), (r"kpop|k-pop|女团|男团|翻跳|cover", "K-pop"),
]


def infer_dance(text: str) -> str:
    t = (text or "").lower()
    for pat, name in DANCE_MAP:
        if re.search(pat, t):
            return name
    return "Urban"


def newest_template() -> Path:
    """拿最近一期正式配置当模板, 继承 episode / intro / outro / 品牌设定。"""
    cands = sorted((p for p in WEEKLY.glob("????-W??-?.json")), reverse=True)
    if cands:
        return cands[0]
    return WEEKLY / "2026-W29.json"


def build_pool_config(target: str, max_candidates: int, provisional_picks: int) -> tuple[Path, int]:
    """把 discover 出来的 candidates/*.json 汇成周配置的候选池, 并挑出一批
    provisional picks 交给下载器。写回 config/weekly/<target>.json。

    注意: used_urls(exclude_week=target) 会跳过 target 自己, 所以这里先写进去的
    临时 picks 不会污染后面的跨期去重。
    """
    cand_dir = INCOMING / target / "candidates"
    raw: list[dict] = []
    for f in sorted(cand_dir.glob("*.json")):
        if f.stem.startswith("_"):
            continue
        try:
            items = json.loads(f.read_text())
        except json.JSONDecodeError:
            continue
        if isinstance(items, list):
            raw.extend(items)

    used = daily.used_urls(target)
    seen: set[str] = set()
    fresh: list[dict] = []
    for c in raw:
        url = c.get("url") or ""
        if not url or url in used or url in seen:
            continue
        seen.add(url)
        fresh.append(c)

    def freshness(c: dict) -> tuple[int, int]:
        days = daily_days_since(c.get("published_at"))
        recent = 1 if (days is not None and days <= 7) else 0
        return (recent, c.get("like") or 0)

    fresh.sort(key=freshness, reverse=True)
    fresh = fresh[:max_candidates]

    cfg = json.loads(newest_template().read_text())
    cfg["week"] = target
    if isinstance(cfg.get("episode"), dict):
        cfg["episode"]["week"] = target
    cfg["classics_pool"] = []

    # 保住上一轮 verify_clips.py 的成果: 画面判定和淘汰名单都是花了 token 算出来的,
    # 重跑 auto_episode 不该把它们冲掉 (否则被判为"非舞蹈"的素材又会溜回榜单)。
    prev_path = WEEKLY / f"{target}.json"
    prev_vision: dict[str, dict] = {}
    prev_deleted: set[str] = set()
    prev_rejected_urls: set[str] = set()
    if prev_path.exists():
        try:
            prev = json.loads(prev_path.read_text())
        except json.JSONDecodeError:
            prev = {}
        prev_deleted = set(prev.get("deleted_ids", []))
        for c in prev.get("this_week_candidates", []):
            if c.get("url") and c.get("vision"):
                prev_vision[c["url"]] = c
        # 已经被 verify 剔出候选池的, 靠 URL 记住 —— 候选 id 每轮重编号, 记 id 没用
        prev_rejected_urls = set(prev.get("_rejected_urls", []) or [])

    kept: list[dict] = []
    for c in fresh:
        url = c.get("url")
        if url in prev_rejected_urls:
            continue
        kept.append(c)
    fresh = kept

    cfg["deleted_ids"] = sorted(prev_deleted)
    cfg["this_week_candidates"] = [{
        "id": f"c{i}",
        "platform": c.get("platform", ""),
        "source": c.get("platform", ""),
        "creator": "@" + (c.get("author") or "").lstrip("@"),
        "title": c.get("title", "")[:120],
        "song": "",
        "source_desc": c.get("source_desc", "")[:400],
        "like": c.get("like") or 0,
        "play": 0,
        "duration_sec": 0,
        "published_at": c.get("published_at"),
        "url": c.get("url"),
        "dance_type": infer_dance(f"{c.get('title','')} {c.get('keyword','')} {c.get('source_desc','')}"),
        "download_status": "pending",
        "local_path": "",
        "candidate_tier": "top" if i <= provisional_picks else "backup",
    } for i, c in enumerate(fresh, 1)]

    # 把上一轮的画面判定结果贴回去 (舞种以画面为准, 别再退回按标题猜)
    carried = 0
    for cand in cfg["this_week_candidates"]:
        old = prev_vision.get(cand.get("url"))
        if not old:
            continue
        cand["vision"] = old["vision"]
        if old.get("dance_type"):
            cand["dance_type"] = old["dance_type"]
        for key in ("creator", "title", "source_desc", "duration_sec", "local_path",
                    "download_status"):
            if old.get(key):
                cand[key] = old[key]
        carried += 1
    if carried:
        print(f"[auto] 沿用上一轮画面核对结果 {carried} 支", flush=True)

    cfg["_rejected_urls"] = sorted(prev_rejected_urls)
    cfg["picks"] = [dict(c, rank=i) for i, c in
                    enumerate(cfg["this_week_candidates"][:provisional_picks], 1)]
    cfg["classic_comeback"] = {}

    out = WEEKLY / f"{target}.json"
    out.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return out, len(fresh)


def daily_days_since(s: str | None) -> int | None:
    if not s:
        return None
    import datetime as dt
    try:
        return (dt.date.today() - dt.date.fromisoformat(s)).days
    except ValueError:
        return None


# ────────────────────────── 3. 各步骤 ──────────────────────────

def step_discover(target: str, timeout_per_run: int) -> int:
    watchdog = start_mute_watchdog()
    try:
        return sh([PY, "-u", "scripts/discover_loop.py", "--week", target,
                   "--per-run-timeout", str(timeout_per_run)])
    finally:
        if watchdog:
            watchdog.terminate()


def step_download(target: str) -> int:
    """抖音走 CDP 拦截 playAddr; 其余平台走 yt-dlp。有一个成功就算这步没白跑。"""
    watchdog = start_mute_watchdog()
    try:
        rc_douyin = sh([PY, "-u", "scripts/douyin_download_picks.py", "--week", target])
        rc_other = sh([PY, "-u", "scripts/download_cross_platform.py", "--week", target,
                       "--platforms", "tiktok|instagram|youtube", "--max-per-platform", "6"])
        return 0 if (rc_douyin == 0 or rc_other == 0) else 1
    finally:
        if watchdog:
            watchdog.terminate()


def step_verify(target: str, no_llm: bool, model: str | None) -> int:
    """下载完成后核对"画面 vs 名字": 权威元数据回填 + 抽帧判定舞种/是否真在跳舞。
    非舞蹈素材直接从候选池剔除, 免得进了成片才被评估器打回。"""
    cmd = [PY, "-u", "scripts/verify_clips.py", target, "--apply"]
    if no_llm:
        cmd.append("--no-vision")
    if model:
        cmd += ["--model", model]
    return sh(cmd)


def step_build(week: str, edition: str, pool_path: Path) -> tuple[Path, list[str]]:
    return daily.build_week(week, edition, pool_path=pool_path)


def step_render(target: str) -> int:
    for sub in ("output/tts", "output/tmp"):
        p = REPO / sub / target
        if p.exists():
            subprocess.run(["rm", "-rf", str(p)], check=False)
    return sh([PY, "-u", "pipeline/render_demo.py", target])


def step_evaluate(target: str, threshold: int, no_llm: bool, model: str | None) -> tuple[bool, dict]:
    cmd = [PY, "-u", "pipeline/evaluate_demo.py", target, "--threshold", str(threshold)]
    if no_llm:
        cmd.append("--no-llm")
    if model:
        cmd += ["--model", model]
    rc = sh(cmd)
    report_path = REPO / "output" / "eval" / target / "report.json"
    report = {}
    if report_path.exists():
        try:
            report = json.loads(report_path.read_text())
        except json.JSONDecodeError:
            pass
    return rc == 0, report


def apply_autofix(target: str, report: dict) -> bool:
    """把评估里指名道姓的坏段落从候选池里剔掉, 让下一轮换素材重排。

    只处理"换掉这一段就能好"的问题 (黑屏 / 无真片 / 画面内容对不上);
    别的问题交给人, 免得脚本在原地空转。
    """
    cfg_path = WEEKLY / f"{target}.json"
    manifest_path = REPO / "output" / f"{target}_manifest.json"
    if not cfg_path.exists() or not manifest_path.exists():
        return False
    cfg = json.loads(cfg_path.read_text())
    manifest = json.loads(manifest_path.read_text())
    seg_to_cid = {s["index"]: s.get("candidate_id") for s in manifest.get("segments", [])}

    fixable_areas = ("黑屏", "素材", "content_accuracy", "visual_quality")
    bad_cids: set[str] = set()
    for issue in report.get("issues", []):
        if issue.get("severity") not in ("blocker", "major"):
            continue
        if not any(a in str(issue.get("area", "")) for a in fixable_areas):
            continue
        cid = seg_to_cid.get(issue.get("segment_index"))
        if cid:
            bad_cids.add(cid)

    if not bad_cids:
        return False
    log(f"自动修复: 剔除有问题的段落素材 {sorted(bad_cids)}")
    cfg["deleted_ids"] = sorted(set(cfg.get("deleted_ids", [])) | bad_cids)
    cfg["this_week_candidates"] = [c for c in cfg.get("this_week_candidates", [])
                                   if c.get("id") not in bad_cids]
    cfg["picks"] = [p for p in cfg.get("picks", []) if p.get("id") not in bad_cids]
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


# ────────────────────────── main ──────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="一条命令出一期本周热舞")
    ap.add_argument("--week", default=None, help="如 2026-W31; 不给就接着最新一期往后排")
    ap.add_argument("--edition", default=None, help="如 C")
    ap.add_argument("--skip-discover", action="store_true")
    ap.add_argument("--skip-download", action="store_true")
    ap.add_argument("--skip-verify", action="store_true",
                    help="跳过素材画面核对 (舞种/作者是否对得上)")
    ap.add_argument("--skip-render", action="store_true")
    ap.add_argument("--threshold", type=int, default=80, help="评估及格线")
    ap.add_argument("--max-attempts", type=int, default=2, help="不及格时最多重做几轮")
    ap.add_argument("--max-candidates", type=int, default=60)
    ap.add_argument("--provisional-picks", type=int, default=12,
                    help="先下这么多支, 再从真正下到的里面挑 TOP5")
    ap.add_argument("--discover-timeout", type=int, default=300)
    ap.add_argument("--no-llm", action="store_true", help="评估只跑硬指标")
    ap.add_argument("--model", default=None, help="评估用的 codex 模型")
    ap.add_argument("--publish", action="store_true", help="及格后调用抖音上传脚本")
    args = ap.parse_args()

    problems = preflight()
    for p in problems:
        log(f"前置警告: {p}")

    week, edition = resolve_target(args.week, args.edition)
    target = f"{week}-{edition}"
    log(f"目标期号 = {target}")

    if not args.skip_discover:
        if not ensure_cdp():
            log("没有 CDP 就抓不了素材; 想用现成候选池请加 --skip-discover")
            return 2
        step_discover(target, args.discover_timeout)

    pool_path, n_fresh = build_pool_config(target, args.max_candidates, args.provisional_picks)
    log(f"候选池: {n_fresh} 支未用过的新候选 -> {pool_path.relative_to(REPO)}")
    if n_fresh == 0:
        log("候选池是空的, 先跑 discover 或放宽去重")
        return 2

    if not args.skip_download:
        if not ensure_cdp():
            return 2
        step_download(target)

    if not args.skip_verify:
        step_verify(target, args.no_llm, args.model)

    attempt = 0
    passed = False
    report: dict = {}
    max_attempts = max(1, args.max_attempts)
    while attempt < max_attempts:
        attempt += 1
        log(f"── 第 {attempt}/{max_attempts} 轮组稿 + 渲染 ──")
        try:
            cfg_path, warnings = step_build(week, edition, pool_path)
        except SystemExit as e:
            log(f"组稿失败: {e}")
            return 3
        for w in warnings:
            log(f"警告: {w}")
        log(f"配置就绪: {cfg_path.relative_to(REPO)}")

        if args.skip_render:
            log("按要求跳过渲染")
            return 0
        if step_render(target) != 0:
            log("渲染失败")
            return 4

        passed, report = step_evaluate(target, args.threshold, args.no_llm, args.model)
        score = report.get("final_score")
        if passed:
            log(f"✅ 评估通过 (总分 {score} ≥ {args.threshold})")
            break
        log(f"❌ 评估不通过 (总分 {score}, 阈值 {args.threshold})")
        for i in report.get("issues", [])[:8]:
            log(f"   [{i.get('severity')}] {i.get('area')}: {i.get('description')}")
        if attempt >= max_attempts:
            log("重做次数用尽, 交给人工处理")
            return 1
        if not apply_autofix(target, report):
            log("这些问题没法靠换素材自动解决, 交给人工处理")
            return 1
        pool_path = WEEKLY / f"{target}.json"

    if not passed:
        # 兜底: 任何没走到"评估通过"的路径都不许进发布分支
        log("没有取得及格结论, 不发布")
        return 1

    demo = REPO / "output" / f"{target}_demo.mp4"
    log(f"成片: {demo.relative_to(REPO)}")
    log(f"看板复核: http://127.0.0.1:8787/?week={target}")

    if args.publish:
        log("进入发布流程")
        return sh([PY, "-u", "scripts/upload_to_douyin.py", "--week", target])
    log("未加 --publish, 到此为止 (发布仍需人工确认)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

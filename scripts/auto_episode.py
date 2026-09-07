#!/usr/bin/env python3
"""auto_episode.py — 一条命令跑完一期: 发现 → 下载 → 组稿 → 渲染 → 评估 → 闸门。

    python3 scripts/auto_episode.py                       # 全自动出下一期
    python3 scripts/auto_episode.py --skip-discover       # 用现有候选池, 不再抓
    python3 scripts/auto_episode.py --week 2026-W31 --edition C
    python3 scripts/auto_episode.py --threshold 85 --max-attempts 3
    python3 scripts/auto_episode.py --publish             # 及格后直接进发布脚本

设计要点:
  * 每一步都能单独跳过, 卡住时不用从头再来;
  * 每轮保留 output/<week>_demo_vN.mp4 + 对应 config/manifest/report/frames;
  * 评估不及格会执行结构化修改建议再渲, 最多 --max-attempts 次;
  * 没及格就绝不进入发布分支 (评估脚本 fail-closed)。
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WEEKLY = REPO / "config" / "weekly"
INCOMING = REPO / "assets" / "incoming"
PY = sys.executable
DAILY_ARTIFACT_DATE: str | None = None

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

def calendar_target() -> tuple[str, str]:
    """每日任务按当前 ISO 周开期；失败重跑同一期，已提交才进入下一字母。"""
    week = daily.iso_week()
    entries = sorted(WEEKLY.glob(f"{week}-?.json"))
    if not entries:
        return week, "A"
    latest = entries[-1]
    letter = latest.stem.rsplit("-", 1)[-1]
    receipt = REPO / "output" / "publish" / f"{latest.stem}.json"
    status = ""
    if receipt.exists():
        try:
            status = json.loads(receipt.read_text()).get("status", "")
        except json.JSONDecodeError:
            pass
    if status in {"submitted", "submitted_reviewing", "published"}:
        if letter < "Z":
            return week, chr(ord(letter) + 1)
        next_week = dt.date.fromisocalendar(
            int(week[:4]), int(week[-2:]), 1) + dt.timedelta(days=7)
        y, w, _ = next_week.isocalendar()
        return f"{y}-W{w:02d}", "A"
    return week, letter


def resolve_target(week: str | None, edition: str | None,
                   use_calendar: bool = False) -> tuple[str, str]:
    if week and edition:
        return week, edition
    if use_calendar and not week and not edition:
        return calendar_target()
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
    prev_by_url: dict[str, dict] = {}
    prev_render_settings: dict = {}
    prev_deleted: set[str] = set()
    prev_rejected_urls: set[str] = set()
    if prev_path.exists():
        try:
            prev = json.loads(prev_path.read_text())
        except json.JSONDecodeError:
            prev = {}
        prev_deleted = set(prev.get("deleted_ids", []))
        prev_render_settings = copy.deepcopy(prev.get("render_settings") or {})
        for c in prev.get("this_week_candidates", []):
            if c.get("url"):
                prev_by_url[c["url"]] = c
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
        previous = prev_by_url.get(cand.get("url"))
        if old:
            cand["vision"] = old["vision"]
            if old.get("dance_type"):
                cand["dance_type"] = old["dance_type"]
        if not previous:
            continue
        for key in ("creator", "title", "source_desc", "duration_sec", "local_path",
                    "download_status", "title_override", "dance_type_override",
                    "creator_override", "difficulty_override",
                    "target_duration_sec", "brightness",
                    "clip_start_sec", "clip_start_explicit", "clip_end_sec"):
            if previous.get(key) not in (None, ""):
                cand[key] = previous[key]
        carried += 1
    if carried:
        print(f"[auto] 沿用上一轮画面核对结果 {carried} 支", flush=True)

    cfg["_rejected_urls"] = sorted(prev_rejected_urls)
    if prev_render_settings:
        cfg["render_settings"] = prev_render_settings
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

def step_discover(target: str, timeout_per_run: int,
                  recent_days: int | None = None,
                  strict_recent: bool = False) -> int:
    watchdog = start_mute_watchdog()
    try:
        cmd = [PY, "-u", "scripts/discover_loop.py", "--week", target,
               "--per-run-timeout", str(timeout_per_run)]
        if recent_days is not None:
            cmd += ["--recent-days", str(recent_days)]
        if strict_recent:
            cmd.append("--strict-recent")
        return sh(cmd)
    finally:
        if watchdog:
            watchdog.terminate()


def step_discovery_evaluate(target: str, recent_days: int | None) -> int:
    cmd = [PY, "-u", "pipeline/evaluate_discovery.py", target, "--compare"]
    if recent_days is not None:
        cmd += ["--recent-days", str(recent_days)]
    return sh(cmd)


def step_download(target: str, max_per_platform: int = 12) -> int:
    """抖音走 CDP 拦截 playAddr; 其余平台走 yt-dlp。有一个成功就算这步没白跑。"""
    watchdog = start_mute_watchdog()
    try:
        rc_douyin = sh([PY, "-u", "scripts/douyin_download_picks.py", "--week", target])
        rc_other = sh([PY, "-u", "scripts/download_cross_platform.py", "--week", target,
                       "--platforms", "tiktok|instagram|youtube",
                       "--max-per-platform", str(max_per_platform)])
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


def stage_existing_config(target: str, cfg: dict) -> list[str]:
    """按现有 picks 的稳定 URL/video-id 重建 staging 硬链接。

    每次 build_week 会清空 `<target>/*.mp4`，所以较早 vN 的 config 虽然保留，
    它的 cN staging 可能已被后续版本替换。`--reuse-config` 必须能独立重渲任意版本。
    """
    stage = INCOMING / target
    stage.mkdir(parents=True, exist_ok=True)
    (stage / "dl2").mkdir(exist_ok=True)
    for old in stage.glob("*.mp4"):
        old.unlink()

    downloads = daily.all_downloaded()
    selected = list(cfg.get("picks", []))
    if cfg.get("classic_comeback"):
        selected.append(cfg["classic_comeback"])
    missing = []
    for pick in selected:
        vid = daily.extract_vid(pick.get("url", ""))
        src = downloads.get(vid or "")
        if not vid or src is None or not src.exists():
            missing.append(pick.get("id") or pick.get("url") or "?")
            continue
        platform = pick.get("platform") or pick.get("source") or ""
        if not platform:
            m = re.match(r"([a-z]+)_", src.name)
            platform = m.group(1) if m else "unknown"
        target_clip = stage / f"{pick['id']}__{platform}__{vid}.mp4"
        os.link(src, target_clip)
        dl_copy = stage / "dl2" / f"{platform}_{vid}.mp4"
        if not dl_copy.exists():
            os.link(src, dl_copy)
        meta_src = src.with_suffix(".json")
        meta_dest = stage / "dl2" / f"{platform}_{vid}.json"
        if meta_src.exists() and not meta_dest.exists():
            shutil.copy2(meta_src, meta_dest)
    return missing


def step_segments(target: str, model: str | None) -> int:
    """逐段验收入选舞段: 舞种/标题/难度回写, 表现力太差的淘汰。
    返回码 1 表示有段被淘汰, 需要重新组稿让后面的舞段顶上。"""
    cmd = [PY, "-u", "pipeline/evaluate_segments.py", target, "--apply"]
    if model:
        cmd += ["--model", model]
    return sh(cmd)


def step_render(target: str) -> int:
    for sub in ("output/tts", "output/tmp"):
        p = REPO / sub / target
        if p.exists():
            subprocess.run(["rm", "-rf", str(p)], check=False)
    return sh([PY, "-u", "pipeline/render_demo.py", target])


def step_evaluate(target: str, threshold: int, no_llm: bool, model: str | None) -> tuple[bool, dict]:
    # 每轮先清当前槽位, 避免 evaluator 崩溃或 --no-llm 时把上一版的
    # report/prompt/frames 错配到新视频上。带 vN 的归档不受影响。
    eval_dir = REPO / "output" / "eval" / target
    for name in ("report.json", "report.md", "prompt.md"):
        (eval_dir / name).unlink(missing_ok=True)
    shutil.rmtree(eval_dir / "frames", ignore_errors=True)

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


def next_version(target: str) -> int:
    found = []
    for path in (REPO / "output").glob(f"{target}_demo_v*.mp4"):
        m = re.fullmatch(rf"{re.escape(target)}_demo_v(\d+)\.mp4", path.name)
        if m:
            found.append(int(m.group(1)))
    return max(found, default=0) + 1


def same_artifact(a: Path, b: Path) -> bool:
    """copy2 会保留 mtime; 大视频用 size+mtime 判断即可, 避免每轮 hash 90MB。"""
    if not a.exists() or not b.exists():
        return False
    sa, sb = a.stat(), b.stat()
    return sa.st_size == sb.st_size and int(sa.st_mtime) == int(sb.st_mtime)


def archive_iteration(target: str, version: int, report: dict | None = None,
                      include_eval: bool = True,
                      include_manifest: bool = True) -> Path:
    """保留这一轮的 video / manifest / config / report / frames。

    用户要能并排看 v1/v2/...，canonical 文件仍指向最新一版，发布脚本不用改。
    """
    out = REPO / "output"
    video = out / f"{target}_demo.mp4"
    versioned = out / f"{target}_demo_v{version}.mp4"
    if video.exists():
        shutil.copy2(video, versioned)

    # 每日任务对外使用日期命名；内部仍保留 week-edition 作为配置主键，
    # 避免破坏历史去重和现有 evaluation 关联。
    if DAILY_ARTIFACT_DATE and versioned.exists():
        daily_dir = out / "daily" / DAILY_ARTIFACT_DATE
        daily_dir.mkdir(parents=True, exist_ok=True)
        # 同一天可能人工重跑另一 target；文件名带内部 week-edition，不能互相覆盖。
        target_suffix = target.removeprefix(f"{DAILY_ARTIFACT_DATE[:4]}-")
        daily_stem = f"{DAILY_ARTIFACT_DATE}_{target_suffix}"
        daily_version = daily_dir / f"{daily_stem}_v{version}.mp4"
        daily_latest = daily_dir / f"{daily_stem}.mp4"
        for dest in (daily_version, daily_latest):
            dest.unlink(missing_ok=True)
            try:
                os.link(versioned, dest)
            except OSError:
                shutil.copy2(versioned, dest)

    manifest = out / f"{target}_manifest.json"
    if include_manifest and manifest.exists():
        archived_manifest = json.loads(manifest.read_text())
        archived_manifest["video"] = versioned.relative_to(REPO).as_posix()
        (out / f"{target}_manifest_v{version}.json").write_text(
            json.dumps(archived_manifest, ensure_ascii=False, indent=2),
            encoding="utf-8")
    cfg = WEEKLY / f"{target}.json"
    if cfg.exists():
        shutil.copy2(cfg, out / f"{target}_config_v{version}.json")

    eval_dir = out / "eval" / target
    eval_dir.mkdir(parents=True, exist_ok=True)
    if include_eval:
        for name in ("report.json", "report.md", "prompt.md"):
            src = eval_dir / name
            if src.exists():
                stem, suffix = src.stem, src.suffix
                dest = eval_dir / f"{stem}_v{version}{suffix}"
                if name == "report.json":
                    archived_report = json.loads(src.read_text())
                    archived_report["video"] = versioned.relative_to(REPO).as_posix()
                    dest.write_text(
                        json.dumps(archived_report, ensure_ascii=False, indent=2),
                        encoding="utf-8")
                else:
                    text = src.read_text()
                    text = text.replace(
                        f"output/{target}_demo.mp4",
                        versioned.relative_to(REPO).as_posix())
                    dest.write_text(text, encoding="utf-8")
        frames = eval_dir / "frames"
        if frames.exists():
            shutil.copytree(frames, eval_dir / f"frames_v{version}",
                            dirs_exist_ok=True)

    summary = {
        "version": version,
        "video": versioned.relative_to(REPO).as_posix(),
        "score": (report or {}).get("final_score"),
        "passed": (report or {}).get("passed"),
        "issues": len((report or {}).get("issues", [])),
    }
    history = eval_dir / "versions.jsonl"
    with history.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(summary, ensure_ascii=False) + "\n")
    log(f"保留 v{version}: {versioned.relative_to(REPO)}"
        f" (分数 {summary['score']})")
    return versioned


def archive_existing_current(target: str) -> None:
    """第一次进入新版循环时, 先把尚未编号的现有成片保成 v1。"""
    video = REPO / "output" / f"{target}_demo.mp4"
    if not video.exists():
        return
    versions = sorted(
        (REPO / "output").glob(f"{target}_demo_v*.mp4"),
        key=lambda p: int(re.search(r"_v(\d+)\.mp4$", p.name).group(1)))
    if versions and same_artifact(video, versions[-1]):
        return
    report_path = REPO / "output" / "eval" / target / "report.json"
    report = {}
    eval_fresh = (report_path.exists()
                  and report_path.stat().st_mtime >= video.stat().st_mtime)
    manifest_path = REPO / "output" / f"{target}_manifest.json"
    manifest_fresh = (manifest_path.exists()
                      and manifest_path.stat().st_mtime >= video.stat().st_mtime)
    if eval_fresh:
        try:
            report = json.loads(report_path.read_text())
        except json.JSONDecodeError:
            pass
    archive_iteration(target, next_version(target), report,
                      include_eval=eval_fresh,
                      include_manifest=manifest_fresh)


def apply_autofix(target: str, report: dict) -> bool:
    """执行 evaluation 给出的结构化 repair_actions。

    不再只会“评估拦截”。标题、舞种、难度、起点、时长、亮度、片头和换段都能
    直接进入下一版；没有结构化动作时才用旧的 major visual/content 换段兜底。
    """
    cfg_path = WEEKLY / f"{target}.json"
    manifest_path = REPO / "output" / f"{target}_manifest.json"
    if not cfg_path.exists() or not manifest_path.exists():
        return False
    cfg = json.loads(cfg_path.read_text())
    manifest = json.loads(manifest_path.read_text())
    seg_to_cid = {s["index"]: s.get("candidate_id") for s in manifest.get("segments", [])}
    candidates = {c.get("id"): c for c in cfg.get("this_week_candidates", [])}
    classics = {c.get("id"): c for c in cfg.get("classics_pool", [])}
    picks = {p.get("id"): p for p in cfg.get("picks", [])}
    classic = cfg.get("classic_comeback") or {}
    before = json.dumps(cfg, sort_keys=True, ensure_ascii=False)
    bad_cids: set[str] = set()
    replacements = 0
    handled = 0
    addressed_areas: dict[int, set[str]] = {}

    def mark_addressed(seg_i: int, *areas: str) -> None:
        addressed_areas.setdefault(seg_i, set()).update(areas)

    def targets(cid: str | None) -> list[dict]:
        if not cid:
            return []
        out = []
        for obj in (candidates.get(cid), classics.get(cid), picks.get(cid),
                    classic if classic.get("id") == cid else None):
            if obj is not None:
                out.append(obj)
        return out

    actions = (report.get("llm") or {}).get("repair_actions") or []
    for action in actions:
        kind = action.get("action")
        seg_i = int(action.get("segment_index", -1))
        cid = seg_to_cid.get(seg_i)
        value_s = str(action.get("value_string") or "").strip()
        try:
            value_n = float(action.get("value_number") or 0)
        except (TypeError, ValueError):
            value_n = 0.0
        rationale = action.get("rationale") or ""

        if kind == "replace_segment" and cid and replacements < 2:
            if cid not in bad_cids:
                bad_cids.add(cid)
                replacements += 1
            mark_addressed(seg_i, "all")
        elif kind == "retitle_segment" and cid and value_s:
            for obj in targets(cid):
                obj["title"] = value_s[:40]
                obj["title_override"] = value_s[:40]
            mark_addressed(seg_i, "text_readability", "content_accuracy")
        elif kind == "set_creator" and cid and value_s:
            creator = value_s if value_s.startswith("@") else "@" + value_s
            for obj in targets(cid):
                obj["creator"] = creator[:100]
                obj["creator_override"] = creator[:100]
            mark_addressed(seg_i, "compliance", "content_accuracy",
                           "text_readability")
        elif kind == "set_dance_type" and cid and value_s:
            for obj in targets(cid):
                obj["dance_type"] = value_s[:40]
                obj["dance_type_override"] = value_s[:40]
            mark_addressed(seg_i, "content_accuracy")
        elif kind == "set_difficulty" and cid and 1 <= value_n <= 5:
            for obj in targets(cid):
                obj.setdefault("difficulty", {})["stars"] = int(round(value_n))
                obj["difficulty_override"] = int(round(value_n))
            mark_addressed(seg_i, "content_accuracy")
        elif kind == "set_clip_start" and cid and value_n >= 0:
            for obj in targets(cid):
                obj["clip_start_sec"] = round(value_n, 2)
                obj["clip_start_explicit"] = True
            mark_addressed(seg_i, "visual_quality", "pacing",
                           "hook_strength", "hook_per_segment")
        elif kind == "shorten_segment" and cid and 10 <= value_n <= 15:
            for obj in targets(cid):
                obj["target_duration_sec"] = round(value_n, 2)
            mark_addressed(seg_i, "pacing")
        elif kind == "brighten_segment" and cid and 0.03 <= value_n <= 0.25:
            for obj in targets(cid):
                obj["brightness"] = round(
                    max(-0.3, min(0.3, float(obj.get("brightness", 0)) + value_n)), 3)
            mark_addressed(seg_i, "visual_quality")
        elif kind == "shorten_all_segments" and 0.7 <= value_n <= 0.95:
            settings = cfg.setdefault("render_settings", {})
            settings["duration_scale"] = round(
                min(float(settings.get("duration_scale", 1.0)), value_n), 3)
        elif kind == "strengthen_intro":
            settings = cfg.setdefault("render_settings", {})
            if 1.6 <= value_n <= 2.6:
                settings["intro_duration_sec"] = round(value_n, 2)
            settings["intro_scrim_alpha"] = min(
                int(settings.get("intro_scrim_alpha", 150)), 95)
            settings["compact_intro"] = True
        else:
            continue
        handled += 1
        log(f"执行建议: {kind} seg={seg_i} value={value_s or value_n}"
            f" — {rationale}")

    # 兼容旧报告/模型漏 action。尤其不能出现“有别的 action，所以未处理的 blocker
    # 被整个跳过”：v4 就曾有署名不一致 blocker，但模型只给片头/时长动作。
    fixable_areas = ("黑屏", "素材", "content_accuracy", "visual_quality", "compliance")
    for issue in report.get("issues", []):
        if issue.get("severity") not in ("blocker", "major"):
            continue
        seg_i = int(issue.get("segment_index", -1))
        issue_areas = {a for a in fixable_areas
                       if a in str(issue.get("area", ""))}
        covered = addressed_areas.get(seg_i, set())
        if "all" in covered or (issue_areas and issue_areas & covered):
            continue
        if not any(a in str(issue.get("area", "")) for a in fixable_areas):
            continue
        cid = seg_to_cid.get(seg_i)
        if cid and cid not in bad_cids and replacements < 2:
            bad_cids.add(cid)
            replacements += 1

    if bad_cids:
        log(f"自动换段: {sorted(bad_cids)} 由后面的候选顶上")
        cfg["deleted_ids"] = sorted(set(cfg.get("deleted_ids", [])) | bad_cids)
        all_candidates = {**classics, **candidates}
        rejected = {all_candidates[cid].get("url") for cid in bad_cids
                    if cid in all_candidates and all_candidates[cid].get("url")}
        cfg["_rejected_urls"] = sorted(
            set(cfg.get("_rejected_urls", [])) | rejected)
        cfg["this_week_candidates"] = [
            c for c in cfg.get("this_week_candidates", [])
            if c.get("id") not in bad_cids]
        cfg["classics_pool"] = [
            c for c in cfg.get("classics_pool", [])
            if c.get("id") not in bad_cids]
        cfg["picks"] = [
            p for p in cfg.get("picks", []) if p.get("id") not in bad_cids]
        if classic.get("id") in bad_cids:
            cfg["classic_comeback"] = {}

    after = json.dumps(cfg, sort_keys=True, ensure_ascii=False)
    if after == before:
        return False
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    return True


# ────────────────────────── main ──────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="一条命令出一期本周热舞")
    ap.add_argument("--week", default=None, help="如 2026-W31; 不给就接着最新一期往后排")
    ap.add_argument("--edition", default=None, help="如 C")
    ap.add_argument("--calendar-target", action="store_true",
                    help="按当前 ISO 周开期；失败重跑同一期，已有发布回执才进入下一期")
    ap.add_argument("--daily-filename", action="store_true",
                    help="同时输出 output/daily/YYYY-MM-DD/YYYY-MM-DD[_vN].mp4")
    ap.add_argument("--skip-discover", action="store_true")
    ap.add_argument("--recent-days", type=int, default=None,
                    help="发现时间窗口；每日任务使用 1")
    ap.add_argument("--strict-recent", action="store_true",
                    help="严格丢弃无日期或超过 recent-days 的候选")
    ap.add_argument("--skip-download", action="store_true")
    ap.add_argument("--skip-verify", action="store_true",
                    help="跳过素材画面核对 (舞种/作者是否对得上)")
    ap.add_argument("--skip-segments", action="store_true",
                    help="跳过逐段验收 (舞种/标题/难度/表现力)")
    ap.add_argument("--segment-rounds", type=int, default=5,
                    help="逐段验收最多换几轮替补")
    ap.add_argument("--skip-render", action="store_true")
    ap.add_argument("--reuse-config", action="store_true",
                    help="复用现有 picks/config，只重渲；适合调音频/视觉模板时避免无谓换榜")
    ap.add_argument("--threshold", type=int, default=80, help="评估及格线")
    ap.add_argument("--max-attempts", type=int, default=5,
                    help="render→evaluation→修改 的最多迭代轮数 (默认 5)")
    ap.add_argument("--max-candidates", type=int, default=100,
                    help="进入周配置的候选容量；默认100，为多轮换段保留低热度后备")
    ap.add_argument("--provisional-picks", type=int, default=12,
                    help="先下这么多支, 再从真正下到的里面挑 TOP5")
    ap.add_argument("--discover-timeout", type=int, default=300)
    ap.add_argument("--no-llm", action="store_true", help="评估只跑硬指标")
    ap.add_argument("--model", default=None, help="评估用的 codex 模型")
    ap.add_argument("--publish", action="store_true", help="及格后调用抖音上传脚本")
    args = ap.parse_args()
    global DAILY_ARTIFACT_DATE
    if args.daily_filename or args.calendar_target:
        DAILY_ARTIFACT_DATE = dt.date.today().isoformat()

    problems = preflight()
    for p in problems:
        log(f"前置警告: {p}")

    if args.strict_recent and (args.recent_days is None or args.recent_days < 1):
        ap.error("--strict-recent 必须同时提供 --recent-days >= 1")
    week, edition = resolve_target(
        args.week, args.edition, use_calendar=args.calendar_target)
    target = f"{week}-{edition}"
    log(f"目标期号 = {target}")
    archive_existing_current(target)

    if not args.skip_discover:
        if not ensure_cdp():
            log("没有 CDP 就抓不了素材; 想用现成候选池请加 --skip-discover")
            return 2
        if step_discover(
                target, args.discover_timeout,
                recent_days=args.recent_days,
                strict_recent=args.strict_recent) != 0:
            log("发现步骤失败，拒绝继续使用不完整候选池")
            return 2
        step_discovery_evaluate(target, args.recent_days)

    if args.reuse_config:
        pool_path = WEEKLY / f"{target}.json"
        if not pool_path.exists():
            log(f"--reuse-config 但配置不存在: {pool_path}")
            return 2
        current = json.loads(pool_path.read_text())
        n_fresh = len(current.get("this_week_candidates", []))
        log(f"复用现有阵容: picks={len(current.get('picks', []))} "
            f"candidates={n_fresh}")
        missing = stage_existing_config(target, current)
        if missing:
            log(f"复用配置的素材无法从 dl2 重建: {missing}")
            return 2
    else:
        pool_path, n_fresh = build_pool_config(
            target, args.max_candidates, args.provisional_picks)
        log(f"候选池: {n_fresh} 支未用过的新候选 -> {pool_path.relative_to(REPO)}")
        if n_fresh == 0:
            log("候选池是空的, 先跑 discover 或放宽去重")
            return 2

    if not args.skip_download:
        if not ensure_cdp():
            return 2
        step_download(target)

    if not args.skip_verify:
        if step_verify(target, args.no_llm, args.model) != 0:
            log("素材画面核对未完整通过，拒绝继续组稿")
            return 3

    attempt = 0
    passed = False
    report: dict = {}
    max_attempts = max(1, args.max_attempts)
    while attempt < max_attempts:
        attempt += 1
        log(f"── 第 {attempt}/{max_attempts} 轮组稿 + 渲染 ──")
        try:
            if args.reuse_config and attempt == 1:
                cfg_path, warnings = pool_path, []
            else:
                cfg_path, warnings = step_build(week, edition, pool_path)
        except SystemExit as e:
            log(f"组稿失败: {e}")
            return 3
        for w in warnings:
            log(f"警告: {w}")
        log(f"配置就绪: {cfg_path.relative_to(REPO)}")

        # 逐段验收: 表现力/竖版适配太差的直接淘汰, 由后面的舞段顶替名次。
        # 放在渲染之前 —— 与其渲完再被整片评估打回, 不如先把烂素材换掉。
        if not args.skip_segments:
            segments_ready = False
            for seg_round in range(args.segment_rounds):
                segment_rc = step_segments(target, args.model)
                if segment_rc == 0:
                    segments_ready = True
                    break
                if segment_rc == 2:
                    log("逐段 evaluation 未完整通过，拒绝渲染未评估舞段")
                    return 4
                log(f"有舞段被淘汰, 重新组稿让后面的顶上 (第 {seg_round + 1} 轮)")
                try:
                    cfg_path, warnings = step_build(week, edition, WEEKLY / f"{target}.json")
                except SystemExit as e:
                    if args.skip_download:
                        log(f"替补不足且已指定 --skip-download: {e}")
                        break
                    log(f"替补不足，补下载后备素材再试: {e}")
                    if step_download(target, max_per_platform=18) != 0:
                        log("后备素材下载失败")
                        break
                    try:
                        cfg_path, warnings = step_build(
                            week, edition, WEEKLY / f"{target}.json")
                    except SystemExit as retry_error:
                        log(f"补下载后仍无足够替补: {retry_error}")
                        break
                for w in warnings:
                    log(f"警告: {w}")
            if not segments_ready:
                log("逐段 evaluation 换段轮数耗尽；新替补尚未验收，拒绝渲染")
                return 4

        if args.skip_render:
            log("按要求跳过渲染")
            return 0
        if step_render(target) != 0:
            log("渲染失败")
            return 4

        passed, report = step_evaluate(target, args.threshold, args.no_llm, args.model)
        score = report.get("final_score")
        archive_iteration(target, next_version(target), report)
        if passed:
            log(f"✅ 评估通过 (总分 {score} ≥ {args.threshold})")
            break
        log(f"❌ 评估不通过 (总分 {score}, 阈值 {args.threshold})")
        for i in report.get("issues", [])[:8]:
            log(f"   [{i.get('severity')}] {i.get('area')}: {i.get('description')}")
        if attempt >= max_attempts:
            log(f"重做 {max_attempts} 轮仍未到阈值, 所有版本已保留供人工比较")
            return 1
        if not apply_autofix(target, report):
            log("evaluation 没有产生新的可执行修改, 停止空转; 已保留所有版本")
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
        return sh([PY, "-u", "scripts/upload_to_douyin.py",
                   "--week", target, "--publish"])
    log("未加 --publish, 到此为止 (发布仍需人工确认)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

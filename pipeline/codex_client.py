#!/usr/bin/env python3
"""codex_client.py — 调用本机 codex CLI 做结构化(JSON schema)推理的共享封装。

pipeline/evaluate_demo.py (成片评估) 和 scripts/verify_clips.py (逐支素材核对)
都要"喂图 + 要结构化 JSON", 逻辑统一放这里。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

CODEX_ENV = "BESTDANCER_CODEX_BIN"
# codex 不一定在 PATH 上 —— 本机是随 VS Code 的 ChatGPT 扩展装的
CODEX_FALLBACK_GLOBS = [
    ".vscode/extensions/openai.chatgpt-*/bin/*/codex",
    ".vscode-insiders/extensions/openai.chatgpt-*/bin/*/codex",
    ".cursor/extensions/openai.chatgpt-*/bin/*/codex",
]


def find_codex() -> str | None:
    env_bin = os.environ.get(CODEX_ENV)
    if env_bin and Path(env_bin).is_file():
        return env_bin
    which = shutil.which("codex")
    if which:
        return which
    home = Path.home()
    for pattern in CODEX_FALLBACK_GLOBS:
        for m in sorted(home.glob(pattern), reverse=True):
            if m.is_file() and os.access(m, os.X_OK):
                return str(m)
    return None


def run_codex_json(prompt: str, schema: dict, work_dir: Path,
                   images: list[Path] | None = None,
                   model: str | None = None, timeout: int = 900,
                   codex_bin: str | None = None,
                   tag: str = "codex") -> tuple[dict | None, str]:
    """跑一次 codex, 要求它按 schema 输出 JSON。返回 (结果, 错误说明)。"""
    codex_bin = codex_bin or find_codex()
    if not codex_bin:
        return None, (f"找不到 codex CLI (PATH 和 VS Code 扩展目录都没有)。"
                      f" 可以用 {CODEX_ENV}=/path/to/codex 指定。")

    work_dir.mkdir(parents=True, exist_ok=True)
    schema_path = work_dir / f"{tag}_schema.json"
    schema_path.write_text(json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8")
    result_path = work_dir / f"{tag}_last_message.json"
    if result_path.exists():
        result_path.unlink()

    cmd = [codex_bin, "exec",
           "--skip-git-repo-check",
           "--sandbox", "read-only",
           "--output-schema", str(schema_path),
           "-o", str(result_path),
           "--color", "never",
           "-C", str(work_dir)]
    if model:
        cmd += ["-m", model]
    for img in (images or []):
        cmd += ["-i", str(img)]
    # prompt 必须走 stdin: `-i/--image` 是可变长参数, 位置参数会被它吞掉
    try:
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, f"codex 超时 (>{timeout}s)"

    if not result_path.exists():
        detail = ((r.stderr or "") + (r.stdout or ""))[-800:]
        return None, f"codex 没有产出结果 (rc={r.returncode}): {detail}"
    raw = result_path.read_text().strip()
    fenced = re.search(r"```(?:json)?\s*([\s\S]+?)```", raw)
    if fenced:
        raw = fenced.group(1).strip()
    try:
        return json.loads(raw), ""
    except json.JSONDecodeError as e:
        return None, f"codex 结果不是合法 JSON: {e}; 原文前 400 字: {raw[:400]}"

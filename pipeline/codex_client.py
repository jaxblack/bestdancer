#!/usr/bin/env python3
"""codex_client.py — 调用 Copilot/Codex CLI 做结构化 JSON 推理的共享封装。

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
AI_PROVIDER_ENV = "BESTDANCER_AI_PROVIDER"
KEYCHAIN_SERVICE_ENV = "BESTDANCER_COPILOT_KEYCHAIN_SERVICE"
# codex 不一定在 PATH 上 —— 本机是随 VS Code 的 ChatGPT 扩展装的
CODEX_FALLBACK_GLOBS = [
    ".vscode/extensions/openai.chatgpt-*/bin/*/codex",
    ".vscode-insiders/extensions/openai.chatgpt-*/bin/*/codex",
    ".cursor/extensions/openai.chatgpt-*/bin/*/codex",
]


def find_codex() -> str | None:
    if os.environ.get(AI_PROVIDER_ENV, "codex").lower() == "copilot":
        return shutil.which("copilot")
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


def _copilot_env() -> tuple[dict[str, str] | None, str]:
    """只把 token 交给 copilot 子进程，不让 ffmpeg/yt-dlp/浏览器等继承。"""
    env = os.environ.copy()
    if env.get("COPILOT_GITHUB_TOKEN"):
        return env, ""
    service = env.get(KEYCHAIN_SERVICE_ENV, "bestdancer-copilot-github-token")
    account = env.get("USER", "jax")
    result = subprocess.run(
        ["security", "find-generic-password", "-a", account,
         "-s", service, "-w"],
        capture_output=True, text=True)
    token = result.stdout.strip()
    if result.returncode != 0 or not token:
        return None, f"macOS Keychain 缺 Copilot token: {service}"
    env["COPILOT_GITHUB_TOKEN"] = token
    return env, ""


def _validate_schema(value, schema: dict, path: str = "$") -> list[str]:
    """校验本项目使用到的 JSON Schema 子集，避免 Copilot 自由输出导致无人值守崩溃。"""
    errors: list[str] = []
    expected = schema.get("type")
    type_ok = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
    }.get(expected, True)
    if not type_ok:
        return [f"{path}: expected {expected}, got {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} not in enum")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: below minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: above maximum {schema['maximum']}")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}: missing required key {key}")
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in properties:
                    errors.append(f"{path}: unexpected key {key}")
        for key, child in value.items():
            if key in properties:
                errors += _validate_schema(
                    child, properties[key], f"{path}.{key}")
    if isinstance(value, list) and "items" in schema:
        for index, child in enumerate(value):
            errors += _validate_schema(
                child, schema["items"], f"{path}[{index}]")
    return errors


def _strip_json_controls(raw: str) -> str:
    """移除 Copilot 终端换行。

    Copilot CLI 会在任意列宽处插入 CR/LF/tab，甚至把 JSON key 和数字折成
    `"visible_han\ndle"` / `9\n4`。只修字符串内部不够；JSON 本身不需要这些
    控制空白，统一删除后才能稳定 parse + schema validate。
    """
    return raw.replace("\r", "").replace("\n", "").replace("\t", "")


def run_codex_json(prompt: str, schema: dict, work_dir: Path,
                   images: list[Path] | None = None,
                   model: str | None = None, timeout: int = 900,
                   codex_bin: str | None = None,
                   tag: str = "codex") -> tuple[dict | None, str]:
    """按 BESTDANCER_AI_PROVIDER 调 Copilot 或 Codex，返回结构化 JSON。"""
    provider = os.environ.get(AI_PROVIDER_ENV, "codex").lower()
    codex_bin = codex_bin or find_codex()
    if not codex_bin:
        return None, (
            f"找不到 {provider} CLI。Copilot 模式要求 PATH 中有 copilot；"
            f"Codex 模式可用 {CODEX_ENV}=/path/to/codex 指定。")

    work_dir.mkdir(parents=True, exist_ok=True)
    schema_path = work_dir / f"{tag}_schema.json"
    schema_path.write_text(json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8")
    result_path = work_dir / f"{tag}_last_message.json"
    if result_path.exists():
        result_path.unlink()

    if provider == "copilot":
        copilot_env, env_error = _copilot_env()
        if copilot_env is None:
            return None, env_error
        full_prompt = (
            prompt
            + "\n\n必须只输出一个 JSON 对象，不能带 Markdown 围栏或解释。"
            + "所有字符串必须在同一行，字符串内部不得使用裸换行或制表符。"
            + "\nJSON Schema:\n"
            + json.dumps(schema, ensure_ascii=False))
        cmd = [codex_bin, "-p", full_prompt, "--silent", "--stream", "off",
               "--no-color", "-C", str(work_dir)]
        if model:
            cmd += ["--model", model]
        for img in (images or []):
            cmd += ["--attachment", str(img)]
        try:
            r = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
                env=copilot_env)
        except subprocess.TimeoutExpired:
            return None, f"copilot 超时 (>{timeout}s)"
        if r.returncode != 0:
            detail = ((r.stderr or "") + (r.stdout or ""))[-800:]
            return None, f"copilot 没有产出结果 (rc={r.returncode}): {detail}"
        raw = (r.stdout or "").strip()
        result_path.write_text(raw, encoding="utf-8")
    else:
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
            r = subprocess.run(
                cmd, input=prompt, capture_output=True, text=True,
                timeout=timeout)
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
        parsed = json.loads(_strip_json_controls(raw))
    except json.JSONDecodeError as e:
        return None, f"{provider} 结果不是合法 JSON: {e}; 原文前 400 字: {raw[:400]}"
    schema_errors = _validate_schema(parsed, schema)
    if schema_errors:
        return None, (
            f"{provider} JSON 不符合 schema: {'; '.join(schema_errors[:8])}")
    return parsed, ""

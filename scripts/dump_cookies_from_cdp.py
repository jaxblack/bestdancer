#!/usr/bin/env python3
"""Dump cookies from running Chrome CDP for a given domain to Netscape cookies.txt."""
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

domain = sys.argv[1]                  # e.g. .youtube.com or .instagram.com or .xiaohongshu.com
out_path = Path(sys.argv[2])          # output cookies.txt

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
    ctx = browser.contexts[0]
    cookies = [c for c in ctx.cookies() if domain.lstrip(".") in c["domain"]]
    print(f"Found {len(cookies)} cookies matching {domain}", file=sys.stderr)

# Netscape format
lines = ["# Netscape HTTP Cookie File", ""]
for c in cookies:
    d = c["domain"]
    include_sub = "TRUE" if d.startswith(".") else "FALSE"
    path = c.get("path", "/")
    secure = "TRUE" if c.get("secure") else "FALSE"
    expires = int(c.get("expires", 0)) if c.get("expires", -1) > 0 else 0
    name = c["name"]
    val = c["value"]
    lines.append("\t".join([d, include_sub, path, secure, str(expires), name, val]))
out_path.parent.mkdir(parents=True, exist_ok=True)
out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
print(f"Wrote {out_path} ({len(cookies)} cookies)", file=sys.stderr)

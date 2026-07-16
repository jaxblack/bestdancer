"""Fetch metadata for existing IG candidates by visiting each detail page."""
import json, re, time, sys
from pathlib import Path
from playwright.sync_api import sync_playwright

WEEK = sys.argv[1] if len(sys.argv) > 1 else "2026-W30-A"
MAX = int(sys.argv[2]) if len(sys.argv) > 2 else 30

cand_path = Path(f"/Users/jax/bestdancer/assets/incoming/{WEEK}/candidates/instagram.json")
data = json.loads(cand_path.read_text())
print(f"loaded {len(data)} IG candidates; enriching first {MAX}")

with sync_playwright() as p:
    b = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
    ctx = b.contexts[0]
    pg = ctx.new_page()
    for i, c in enumerate(data[:MAX]):
        url = c.get("url") or ""
        if not url:
            continue
        try:
            pg.goto(url, timeout=30000, wait_until="domcontentloaded")
            time.sleep(3)
            info = pg.evaluate("""() => {
                // look for JSON-LD or meta tags
                const meta = document.querySelector('meta[property=\"og:description\"]');
                const desc = meta ? meta.getAttribute('content') : '';
                // author: meta og:title or first user link
                const ogTitle = document.querySelector('meta[property=\"og:title\"]')?.getAttribute('content') || '';
                // date: time element
                const timeEl = document.querySelector('time');
                const dt = timeEl ? timeEl.getAttribute('datetime') : '';
                return {desc, ogTitle, dt};
            }""")
            desc = info.get("desc","") or ""
            og   = info.get("ogTitle","") or ""
            # og_title like "Han Jia Yi on Instagram: "..."
            m = re.match(r"^(.+?)\s+on Instagram", og)
            author = m.group(1) if m else c.get("author","unknown")
            # desc like "1,234 likes, 56 comments - handle on July 12, 2026: "…""
            like = 0
            m2 = re.search(r"([\d,]+)\s+likes?", desc)
            if m2:
                like = int(m2.group(1).replace(",",""))
            pub = None
            if info.get("dt"):
                pub = info["dt"][:10]  # YYYY-MM-DD
            title = re.sub(r'^.*?:\s*"?', "", desc).split('"')[0][:200] if desc else c.get("title","")
            c["author"] = author[:80]
            c["like"] = like
            c["published_at"] = pub
            if title: c["title"] = title
            print(f"[{i+1}/{MAX}] ❤{like:<6} {pub or '?'} {author[:20]} | {title[:40]}")
        except Exception as e:
            print(f"[{i+1}/{MAX}] ERR {e.__class__.__name__}: {str(e)[:80]}")
    pg.close()

cand_path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
print(f"wrote {cand_path}")

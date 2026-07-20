"""human.py — 拟人化 Playwright 操作 helpers.

避免以下容易被风控识别的特征:
  - 完全一致的 sleep 时长
  - 直接 page.goto(target_url) 一步到位, 无 referrer / 无历史 / 无浏览动作
  - page.mouse.wheel 固定像素固定间隔
  - .fill() / .type() 无延迟秒填
  - 无鼠标移动、无 hover、无停留看画面

调用方全部改走这里, 保持行为多样性。
"""
from __future__ import annotations
import random
import time
from typing import Optional
from urllib.parse import quote


# ── timing ───────────────────────────────────────────────

def jitter_sleep(base: float, jitter: float = 0.35, tail_prob: float = 0.08, tail_mult: float = 3.0) -> None:
    """Gaussian jitter around `base` (min 0.15s). tail_prob 概率触发一次"分神停留"。"""
    if random.random() < tail_prob:
        base *= tail_mult * (0.6 + random.random() * 0.8)
    dur = max(0.15, random.gauss(base, base * jitter))
    time.sleep(dur)


def idle(min_s: float = 1.5, max_s: float = 3.5) -> None:
    """看画面的停顿。用于两次动作之间, 长于 jitter_sleep。"""
    time.sleep(random.uniform(min_s, max_s))


def cooldown(min_s: float = 30, max_s: float = 90) -> None:
    """平台之间/大动作之间的降速停顿。抓取几个关键词后 cooldown 一次。"""
    dur = random.uniform(min_s, max_s)
    time.sleep(dur)


# ── mouse / scroll ───────────────────────────────────────

def wiggle_cursor(page, moves: int = 3) -> None:
    """屏幕内几次不规则鼠标移动 (远快于真人但比不动强)。"""
    try:
        vw = page.viewport_size or {"width": 1440, "height": 900}
        w, h = vw["width"], vw["height"]
        for _ in range(moves):
            x = random.randint(60, max(80, w - 60))
            y = random.randint(60, max(80, h - 60))
            page.mouse.move(x, y, steps=random.randint(6, 18))
            time.sleep(random.uniform(0.08, 0.25))
    except Exception:
        pass


def human_scroll(page, total: int = 2000, direction: int = 1) -> None:
    """把一次大 scroll 拆成 3-6 个不等大小的 wheel + 中间停顿。"""
    remaining = total
    while remaining > 100:
        chunk = int(random.uniform(180, min(remaining, 520)))
        try:
            page.mouse.wheel(0, chunk * direction)
        except Exception:
            break
        remaining -= chunk
        time.sleep(random.uniform(0.25, 0.85))
    # 偶尔小回滚, 模拟看漏一眼往上找
    if random.random() < 0.15:
        try:
            page.mouse.wheel(0, -random.randint(120, 320) * direction)
            time.sleep(random.uniform(0.3, 0.7))
        except Exception:
            pass


# ── text input ───────────────────────────────────────────

def human_type(page, text: str, per_key_min: float = 0.05, per_key_max: float = 0.16) -> None:
    """逐字符 type + 每键随机延迟 + 偶尔小停顿 (like 想一下)。"""
    for i, ch in enumerate(text):
        page.keyboard.type(ch)
        d = random.uniform(per_key_min, per_key_max)
        # 每 6-14 字符插一次"停顿思考"
        if i > 0 and i % random.randint(6, 14) == 0:
            d += random.uniform(0.18, 0.55)
        time.sleep(d)


def clear_and_type(page, locator, text: str) -> None:
    """click → select all → 删 → 拟人 type。抖音标题/简介都能用。"""
    try:
        locator.click()
    except Exception:
        pass
    time.sleep(random.uniform(0.2, 0.5))
    page.keyboard.press("Meta+A")
    time.sleep(random.uniform(0.1, 0.25))
    page.keyboard.press("Backspace")
    time.sleep(random.uniform(0.2, 0.5))
    human_type(page, text)


# ── navigation ───────────────────────────────────────────

def visit_home_first(page, home_url: str, warmup_scrolls: int = 2) -> None:
    """先访问平台首页, scroll 一下模仿真人 warmup, 建立 referer / cookies。"""
    try:
        page.goto(home_url, wait_until="domcontentloaded", timeout=45_000)
    except Exception:
        return
    idle(2.0, 4.5)
    wiggle_cursor(page)
    for _ in range(warmup_scrolls):
        human_scroll(page, total=random.randint(400, 1200))
        idle(0.8, 2.0)


def human_search(page, home_url: str, search_url_tpl: str, keyword: str,
                 search_input_selector: Optional[str] = None) -> None:
    """
    模拟真人搜索: 先访问首页 → (若有 search input) 点它 → 逐字打字 → 回车,
    找不到 input 就 fallback 到 goto(search_url_tpl.format(kw=...)) 但保留 referer。
    """
    try:
        # 已经在同域就不重复访问首页, 避免频繁刷首页也可疑
        if home_url not in (page.url or ""):
            page.goto(home_url, wait_until="domcontentloaded", timeout=45_000)
            idle(2.0, 4.0)
            wiggle_cursor(page)
    except Exception:
        pass

    used_ui = False
    if search_input_selector:
        try:
            box = page.locator(search_input_selector).first
            if box.count():
                box.click()
                time.sleep(random.uniform(0.3, 0.7))
                human_type(page, keyword)
                time.sleep(random.uniform(0.4, 0.9))
                page.keyboard.press("Enter")
                used_ui = True
        except Exception:
            used_ui = False

    if not used_ui:
        # fallback: 直接 goto 搜索 URL, 但因为已经在首页, 有 referer
        try:
            page.goto(search_url_tpl.format(kw=quote(keyword)),
                      wait_until="domcontentloaded", timeout=45_000)
        except Exception:
            pass

    idle(2.5, 5.0)
    wiggle_cursor(page)


# ── open link from a page ────────────────────────────────

def open_link_new_tab(ctx, anchor_url: str, referer_page):
    """
    模仿 Ctrl+Click / 右键新标签打开链接: 通过 ctx.new_page() + goto,
    但在 referer_page 上先做 hover + 短暂停留, 让 referrer/session 更自然。
    返回新 page。
    """
    try:
        # 尝试在源页面上定位到该链接 hover 一下
        for a in referer_page.query_selector_all("a"):
            try:
                if (a.get_attribute("href") or "") == anchor_url:
                    a.hover(timeout=1500)
                    time.sleep(random.uniform(0.3, 0.9))
                    break
            except Exception:
                continue
    except Exception:
        pass
    new_pg = ctx.new_page()
    idle(0.4, 1.0)
    try:
        new_pg.goto(anchor_url, wait_until="domcontentloaded", timeout=45_000)
    except Exception:
        pass
    idle(1.5, 3.5)
    return new_pg

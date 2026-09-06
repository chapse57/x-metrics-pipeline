"""Playwright collector. Reads a logged-in X timeline the way a person does —
one profile at a time, scrolling, no API, no third-party scraper.

Design rules (the "boring things done right"):
  session reuse   persistent browser profile (log in once by hand; never store a password here)
  rate limit      jittered sleeps between scrolls and accounts; backs off on X's "Something went wrong"
  retry           navigation retried with exponential backoff
  resume          reads `accounts.status='pending'` from the store, so a crash or Ctrl-C loses nothing
  evidence        every parsed aria-label is stored next to the numbers it produced

Selectors were captured live on 2026-09-06 (fixtures/aria_samples.json). If X changes
markup, `CollectError` is raised and the account is marked 'error' — never silently 0.
"""
from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timezone
from pathlib import Path

from .metrics import Post, select_posts, summarize
from .parse import ParseError, parse_button_labels, parse_count, parse_group_label, parse_status_id
from .store import Store

log = logging.getLogger("xmetrics.collect")


class CollectError(RuntimeError):
    pass


# --- JS evaluated in the page: returns the raw strings, parsing happens in Python -------
_JS_ARTICLES = """
() => [...document.querySelectorAll('article')].map(a => ({
  group: a.querySelector('[role="group"]')?.getAttribute('aria-label') ?? null,
  buttons: [...a.querySelectorAll('[data-testid="reply"],[data-testid="retweet"],[data-testid="like"],a[href$="/analytics"]')].map(b => b.getAttribute('aria-label')),
  time: a.querySelector('time')?.getAttribute('datetime') ?? null,
  href: a.querySelector('a[href*="/status/"]')?.getAttribute('href') ?? null,
  social: a.querySelector('[data-testid="socialContext"]')?.innerText ?? '',
  text: a.querySelector('[data-testid="tweetText"]')?.innerText ?? '',
  reply: /^(Replying to|.*님에게 보내는 답글)/m.test(a.innerText),
}))
"""
_JS_FOLLOWERS = """
() => {
  const a = document.querySelector('a[href$="/verified_followers"], a[href$="/followers"]');
  if (!a) return null;
  const s = [...a.querySelectorAll('span')].map(x => x.innerText.trim()).find(t => /^[\\d.,]+([KkMm]|천|만|억)?$/.test(t));
  return s ?? null;
}
"""
_JS_BIO = """
() => {
  const u = document.querySelector('[data-testid="UserUrl"]');
  const nameBlock = document.querySelector('[data-testid="UserName"]')?.innerText ?? '';
  const handle = (nameBlock.match(/@([A-Za-z0-9_]+)/) || [])[1] ?? null;   // case as X displays it
  return {
    bio: document.querySelector('[data-testid="UserDescription"]')?.innerText ?? '',
    url: u?.getAttribute('title') || u?.innerText || '',                    // title holds the full URL; innerText is ellipsised
    dm: !!document.querySelector('[data-testid="sendDMFromProfile"]'),
    display: handle,
  };
}
"""


def _sleep(lo: float, hi: float) -> None:
    time.sleep(random.uniform(lo, hi))


COOKIE_FILE = "xmetrics_session.json"   # inside the profile dir; written by `import-cookies`, re-applied on every launch

_JS_LOGGED_IN = """
() => !!(document.querySelector('[data-testid="SideNav_AccountSwitcher_Button"]')
      || document.querySelector('[data-testid="AppTabBar_Home_Link"]')
      || document.querySelector('a[href="/compose/post"]'))
"""


def apply_saved_session(ctx, profile_dir: Path) -> bool:
    """Re-add session cookies saved by `import-cookies` (belt and braces: Chromium's own cookie jar
    is not always shared between headless and headed launches of the same profile)."""
    import json
    f = Path(profile_dir) / COOKIE_FILE
    if not f.exists():
        return False
    ctx.add_cookies(json.loads(f.read_text(encoding="utf-8")))
    return True


def is_logged_in(page) -> bool:
    """True only when the logged-in shell is present (account switcher / compose button),
    not merely when a public profile page rendered — X shows those to logged-out visitors too."""
    try:
        page.wait_for_selector('[data-testid="SideNav_AccountSwitcher_Button"], [data-testid="loginButton"], a[href="/login"]', timeout=15_000)
    except Exception:
        pass
    return bool(page.evaluate(_JS_LOGGED_IN))


class Collector:
    def __init__(self, store: Store, profile_dir: str | Path, *, headless: bool = False,
                 posts_per_account: int = 20, max_scrolls: int = 30, record_dir: str | Path | None = None):
        self.store = store
        self.profile_dir = Path(profile_dir)
        self.headless = headless
        self.n = posts_per_account
        self.max_scrolls = max_scrolls
        self.record_dir = Path(record_dir) if record_dir else None  # Playwright video of the run (evidence / demo GIF)

    # ------------------------------------------------------------------
    def run(self, handles: list[str] | None = None, *, note: str | None = None) -> str:
        from playwright.sync_api import sync_playwright

        if handles:  # explicitly named handles are always (re)measured, whatever their previous status
            for h in handles:
                if not self.store.get_account(h):
                    self.store.upsert_account(h)
                self.store.set_status(h, "pending")
        todo = self.store.pending_handles()
        if not todo:
            log.info("nothing pending — pass handles to (re)measure, or check `xmetrics status`")
            return ""
        run_id = self.store.start_run("playwright", note)
        with sync_playwright() as p:
            launch_kw = {"headless": self.headless, "locale": "en-US", "viewport": {"width": 1280, "height": 900}}
            if self.record_dir:
                self.record_dir.mkdir(parents=True, exist_ok=True)
                launch_kw |= {"record_video_dir": str(self.record_dir), "record_video_size": {"width": 1280, "height": 900}}
            ctx = p.chromium.launch_persistent_context(str(self.profile_dir), **launch_kw)
            apply_saved_session(ctx, self.profile_dir)
            page = ctx.new_page()
            self._assert_logged_in(page)
            for i, h in enumerate(todo, 1):
                log.info("[%d/%d] @%s", i, len(todo), h)
                try:
                    self.collect_one(page, h, run_id)
                except CollectError as e:
                    log.warning("@%s error: %s", h, e)
                    self.store.set_status(h, "error", str(e))
                _sleep(2.5, 6.0)  # between accounts
            ctx.close()
        self.store.finish_run(run_id)
        return run_id

    def _assert_logged_in(self, page) -> None:
        self._goto(page, "https://x.com/home")
        if not is_logged_in(page):
            raise CollectError("not logged in: run `xmetrics login` or `xmetrics import-cookies` first")

    def _goto(self, page, url: str, attempts: int = 4) -> None:
        delay = 3.0
        for k in range(attempts):
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                page.wait_for_timeout(1500)
                if "Something went wrong" in page.content() or "Rate limit" in page.content():
                    raise CollectError("rate limited")
                return
            except Exception as e:  # noqa: BLE001 — retried, then surfaced
                log.warning("goto %s failed (%d/%d): %s", url, k + 1, attempts, e)
                time.sleep(delay)
                delay *= 2
        raise CollectError(f"navigation failed: {url}")

    # ------------------------------------------------------------------
    def collect_one(self, page, handle: str, run_id: str) -> None:
        self._goto(page, f"https://x.com/{handle}")
        if page.locator('text="This account doesn’t exist"').count() or page.locator('text="Account suspended"').count():
            self.store.set_status(handle, "screened_out", "account missing/suspended")
            return
        f_text = page.evaluate(_JS_FOLLOWERS)
        if not f_text:
            raise CollectError("followers not found (markup changed?)")
        followers = parse_count(f_text)
        prof = page.evaluate(_JS_BIO)
        extra = {"display": prof["display"]} if prof.get("display") else {}
        self.store.upsert_account(handle, bio=prof["bio"], bio_url=prof["url"], dm_open=int(prof["dm"]), **extra)

        seen: dict[str, tuple[Post, str, str]] = {}  # status_id -> (post, raw_label, text)
        for _ in range(self.max_scrolls):
            for art in page.evaluate(_JS_ARTICLES):
                sid = parse_status_id(art["href"])
                if not sid or sid in seen or not art["time"]:
                    continue
                try:
                    counts = parse_group_label(art["group"]) if art["group"] else parse_button_labels(art["buttons"])
                except ParseError:
                    continue  # promoted/quoted card without an action bar; not a post of this account
                social = (art["social"] or "").lower()
                # X's UI language follows the account settings: English and Korean labels captured 2026-09-06
                is_repost = ("reposted" in social) or ("재게시" in social)
                is_pinned = ("pinned" in social) or ("메인에 올림" in social) or ("고정" in social)
                post = Post(status_id=sid, posted_at=datetime.fromisoformat(art["time"].replace("Z", "+00:00")),
                            counts=counts, is_repost=is_repost, is_pinned=is_pinned, is_reply=bool(art["reply"]))
                seen[sid] = (post, art["group"] or " | ".join(b for b in art["buttons"] if b), art["text"])
            if len(select_posts([p for p, _, _ in seen.values()], self.n)) >= self.n:
                break
            page.mouse.wheel(0, 2400)
            _sleep(0.9, 1.8)

        posts = select_posts([p for p, _, _ in seen.values()], self.n)
        if not posts:
            raise CollectError("no original posts parsed")
        summary = summarize(followers, posts, now=datetime.now(timezone.utc))
        self.store.save_measurement(handle, run_id, summary, posts, raw_labels={sid: raw for sid, (_, raw, _) in seen.items()})
        # keep post texts for the classifier (evidence quotes must be verbatim substrings of these)
        texts = [seen[p.status_id][2] for p in posts if seen[p.status_id][2]]
        self._save_texts(handle, run_id, texts)
        log.info("@%s followers=%s posts=%d ER=%.2f%% VR=%.1f%%", handle, followers, len(posts),
                 summary.engagement_rate, summary.views_to_followers)

    def _save_texts(self, handle: str, run_id: str, texts: list[str]) -> None:
        self.store.conn.execute("CREATE TABLE IF NOT EXISTS post_texts (handle TEXT, run_id TEXT, idx INTEGER, text TEXT, PRIMARY KEY (handle, run_id, idx))")
        with self.store.tx() as c:
            for i, t in enumerate(texts):
                c.execute("INSERT OR REPLACE INTO post_texts VALUES (?,?,?,?)", (handle.lower().lstrip("@"), run_id, i, t))

"""Engagement metrics. Pure functions, no I/O, so every number is re-computable
from stored raw posts (see validate.py, which does exactly that)."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from statistics import median

from .parse import PostCounts


@dataclass(frozen=True)
class Post:
    status_id: str
    posted_at: datetime  # tz-aware UTC
    counts: PostCounts
    is_repost: bool = False
    is_pinned: bool = False
    is_reply: bool = False


@dataclass(frozen=True)
class Summary:
    followers: int
    posts_measured: int
    range_start: str  # YYYY-MM-DD
    range_end: str
    median_likes: float
    median_replies: float
    median_reposts: float
    median_views: float
    engagement_rate: float   # percent: median(likes+replies+reposts) / followers * 100
    views_to_followers: float  # percent: median(views) / followers * 100
    tier: str
    days_since_last_post: int

    def as_dict(self) -> dict:
        return asdict(self)


TIERS = (("Micro (<25K)", 25_000), ("Mid (25-100K)", 100_000), ("Macro (100K+)", None))


def tier_for(followers: int) -> str:
    for name, upper in TIERS:
        if upper is None or followers < upper:
            return name
    raise AssertionError("unreachable")


def _pct(numer: float, denom: int) -> float:
    return round(numer / denom * 100, 4) if denom else 0.0


def select_posts(posts: list[Post], n: int = 20, include_replies: bool = False) -> list[Post]:
    """Most recent `n` original posts. Reposts and pinned posts are excluded:
    a repost carries someone else's numbers, a pinned post is cherry-picked."""
    own = [p for p in posts if not p.is_repost and not p.is_pinned and (include_replies or not p.is_reply)]
    own.sort(key=lambda p: p.posted_at, reverse=True)
    return own[:n]


def summarize(followers: int, posts: list[Post], *, now: datetime | None = None) -> Summary:
    if followers <= 0:
        raise ValueError("followers must be > 0")
    if not posts:
        raise ValueError("no posts to summarize")
    now = now or datetime.now(timezone.utc)
    m_int = median(p.counts.interactions for p in posts)
    m_views = median(p.counts.views for p in posts)
    dates = sorted(p.posted_at for p in posts)
    return Summary(
        followers=followers,
        posts_measured=len(posts),
        range_start=dates[0].date().isoformat(),
        range_end=dates[-1].date().isoformat(),
        median_likes=float(median(p.counts.likes for p in posts)),
        median_replies=float(median(p.counts.replies for p in posts)),
        median_reposts=float(median(p.counts.reposts for p in posts)),
        median_views=float(m_views),
        engagement_rate=_pct(m_int, followers),
        views_to_followers=_pct(m_views, followers),
        tier=tier_for(followers),
        days_since_last_post=(now - dates[-1]).days,
    )


def rates_from_medians(followers: int, m_likes: float, m_replies: float, m_reposts: float, m_views: float) -> tuple[float, float]:
    """Recompute the two headline rates from stored medians (legacy rows have no per-post data).

    NOTE: median(likes)+median(replies)+median(reposts) != median(likes+replies+reposts) in
    general. The legacy 2026-09 deliverable used the sum-of-medians form; we keep that
    definition for legacy rows so validation compares like with like, and use the
    per-post form (summarize) for everything collected by this pipeline.
    """
    return _pct(m_likes + m_replies + m_reposts, followers), _pct(m_views, followers)

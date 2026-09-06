"""Parsers for the raw strings X (Twitter) renders in the DOM.

Every format here was captured from a live, logged-in timeline on 2026-09-06
(see fixtures/aria_samples.json). Nothing is guessed: if X changes markup,
the tests in tests/test_parse.py fail before any number reaches a client.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# "3 replies, 1 repost, 45 likes, 10 bookmarks, 2911 views"
# Zero-valued fields are OMITTED by X ("13 likes, 1157 views"), so every
# metric defaults to 0 and only present fields are filled in.
_GROUP_ITEM = re.compile(r"(?P<n>[\d,\.]+[KkMm]?)\s+(?P<name>replies|reply|reposts|repost|likes|like|bookmarks|bookmark|views|view)\b")

# Button aria-labels: "3 Replies. Reply" / "1 repost. Repost" / "45 Likes. Like" / "2911 views. View post analytics"
_BUTTON = re.compile(r"^(?P<n>[\d,\.]+[KkMm]?)\s+(?P<name>replies|reply|reposts|repost|likes|like|views|view)\b", re.I)

_NUM = re.compile(r"^(?P<num>\d+(?:[.,]\d+)*)(?P<suffix>[KkMm]|천|만|억)?$")
_SUFFIX = {"k": 1_000, "m": 1_000_000, "천": 1_000, "만": 10_000, "억": 100_000_000}


class ParseError(ValueError):
    """Raised when a string does not match the captured X format."""


def parse_count(text: str) -> int:
    """'3,143' -> 3143 ; '12.5K' -> 12500 ; '1.3M' -> 1300000 ; '45' -> 45 ; '1.2만' -> 12000 (Korean UI).

    X shows exact counts on hover/aria and abbreviated counts in profile headers.
    """
    t = text.strip().replace(" ", "")
    m = _NUM.match(t)
    if not m:
        raise ParseError(f"not a count: {text!r}")
    num, suffix = m.group("num"), m.group("suffix")
    if suffix:
        value = float(num.replace(",", "."))  # abbreviated form uses '.' as decimal
        return int(round(value * _SUFFIX[suffix.lower()]))
    return int(num.replace(",", ""))


@dataclass(frozen=True)
class PostCounts:
    replies: int = 0
    reposts: int = 0
    likes: int = 0
    bookmarks: int = 0
    views: int = 0
    present: tuple[str, ...] = field(default=())  # which fields the label actually carried

    @property
    def interactions(self) -> int:
        """likes + replies + reposts (bookmarks excluded: not public engagement)."""
        return self.likes + self.replies + self.reposts


_CANON = {
    "reply": "replies", "replies": "replies",
    "repost": "reposts", "reposts": "reposts",
    "like": "likes", "likes": "likes",
    "bookmark": "bookmarks", "bookmarks": "bookmarks",
    "view": "views", "views": "views",
}


def parse_group_label(label: str | None) -> PostCounts:
    """Parse the aria-label of the post's action bar ([role="group"]).

    >>> parse_group_label("3 replies, 1 repost, 45 likes, 10 bookmarks, 2911 views")
    PostCounts(replies=3, reposts=1, likes=45, bookmarks=10, views=2911, present=('replies', 'reposts', 'likes', 'bookmarks', 'views'))
    >>> parse_group_label("13 likes, 1157 views").reposts
    0
    """
    if not label or not label.strip():
        raise ParseError("empty group label")
    found: dict[str, int] = {}
    order: list[str] = []
    for m in _GROUP_ITEM.finditer(label):
        key = _CANON[m.group("name").lower()]
        found[key] = parse_count(m.group("n"))
        order.append(key)
    if not found:
        raise ParseError(f"no metrics in group label: {label!r}")
    if "views" not in found and "likes" not in found:
        # A label with neither is not an action bar we recognise.
        raise ParseError(f"group label missing views/likes: {label!r}")
    return PostCounts(**found, present=tuple(order))


def parse_button_labels(labels: list[str | None]) -> PostCounts:
    """Fallback: parse the individual button aria-labels (reply/retweet/like/analytics).

    Used when the [role=group] label is absent (X occasionally renders it late).
    """
    found: dict[str, int] = {}
    order: list[str] = []
    for lab in labels:
        if not lab:
            continue
        m = _BUTTON.match(lab.strip())
        if not m:
            continue
        key = _CANON[m.group("name").lower()]
        found[key] = parse_count(m.group("n"))
        order.append(key)
    if not found:
        raise ParseError("no metrics in button labels")
    return PostCounts(**found, present=tuple(order))


def parse_status_id(href: str | None) -> str | None:
    """'/realFatCat1/status/2095567850060599360/analytics' -> '2095567850060599360'."""
    if not href:
        return None
    m = re.search(r"/status/(\d+)", href)
    return m.group(1) if m else None


def normalize_handle(handle: str) -> str:
    """'@RealFatCat1 ' -> 'realfatcat1' (X handles are case-insensitive)."""
    h = handle.strip()
    if h.startswith("https://") or h.startswith("http://"):
        h = h.rstrip("/").rsplit("/", 1)[-1]
    return h.lstrip("@").lower()

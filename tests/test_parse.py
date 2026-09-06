import json
from pathlib import Path

import pytest

from xmetrics.parse import (ParseError, normalize_handle, parse_button_labels, parse_count,
                            parse_group_label, parse_status_id)

FIX = json.loads((Path(__file__).parent.parent / "fixtures" / "aria_samples.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("text,expected", [("3,143", 3143), ("12.5K", 12500), ("1.3M", 1_300_000), ("45", 45), ("2911", 2911), ("1,300,000", 1_300_000), ("1.2만", 12_000), ("7천", 7_000), ("1.5억", 150_000_000)])
def test_parse_count(text, expected):
    assert parse_count(text) == expected


def test_parse_count_rejects_garbage():
    with pytest.raises(ParseError):
        parse_count("Followers")


def test_group_label_full():
    c = parse_group_label(FIX["group_aria_labels"][0])
    assert (c.replies, c.reposts, c.likes, c.bookmarks, c.views) == (3, 1, 45, 10, 2911)
    assert c.interactions == 49
    assert c.present == ("replies", "reposts", "likes", "bookmarks", "views")


def test_group_label_omits_zero_fields():
    c = parse_group_label("13 likes, 1157 views")
    assert (c.replies, c.reposts, c.likes, c.views) == (0, 0, 13, 1157)
    assert "replies" not in c.present  # we know it was absent, not zero-by-assumption


def test_group_label_all_fixture_samples_parse():
    for lab in FIX["group_aria_labels"]:
        assert parse_group_label(lab).views > 0


def test_group_label_rejects_non_action_bar():
    with pytest.raises(ParseError):
        parse_group_label("10 bookmarks")
    with pytest.raises(ParseError):
        parse_group_label("")


def test_button_labels_fallback():
    c = parse_button_labels(FIX["button_aria_labels"][1])
    assert (c.replies, c.reposts, c.likes, c.views) == (0, 0, 13, 1157)


def test_status_id():
    assert parse_status_id("/realFatCat1/status/2095567850060599360/analytics") == "2095567850060599360"
    assert parse_status_id(None) is None


@pytest.mark.parametrize("h", ["@RealFatCat1", "realfatcat1 ", "https://x.com/RealFatCat1/"])
def test_normalize_handle(h):
    assert normalize_handle(h) == "realfatcat1"

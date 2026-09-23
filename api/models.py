"""Response shapes. A client can rely on these: a field that is here is always present (null
when unknown), and a field that is not here never appears. FastAPI renders them at /docs."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class Account(BaseModel):
    handle: str
    display: str | None
    niche: str | None
    status: str
    dm_open: bool | None
    run_id: str = Field(description="the run this latest measurement came from")
    source: str = Field(description="'playwright' (collected) or 'legacy-import' (hand-measured)")
    measured_at: datetime = Field(description="when the numbers were taken (legacy rows: end of their window)")
    followers: int
    tier: str
    engagement_rate: float = Field(description="percent: 0.75 means 0.75 %")
    views_to_followers: float = Field(description="percent")
    posts_measured: int
    days_since_last_post: int | None


class AccountPage(BaseModel):
    total: int = Field(description="rows matching the filters, before paging")
    limit: int
    offset: int
    sort: str
    items: list[Account]


class Measurement(BaseModel):
    run_id: str
    source: str
    measured_at: datetime
    followers: int
    tier: str
    engagement_rate: float
    views_to_followers: float
    posts_measured: int
    days_since_last_post: int | None


class AccountDetail(Account):
    median_likes: float | None
    median_replies: float | None
    median_reposts: float | None
    median_views: float | None
    history: list[Measurement] = Field(description="every measurement of this account, oldest first")


class Change(BaseModel):
    handle: str
    display: str | None
    kind: str = Field(description="new | dropped | changed | unchanged")
    flags: list[str] = Field(description="thresholds that fired, e.g. followers_up, went_silent")
    followers_prev: int | None
    followers_now: int | None
    followers_delta: int | None
    followers_delta_pct: float | None
    engagement_prev: float | None
    engagement_now: float | None
    engagement_delta_pp: float | None
    views_prev: float | None
    views_now: float | None
    views_delta_pct: float | None
    tier_prev: str | None
    tier_now: str | None
    days_since_last_post_prev: int | None
    days_since_last_post_now: int | None
    posts_measured_prev: int | None
    posts_measured_now: int | None
    run_prev: str | None = Field(description="the run this account's previous value came from")
    run_now: str | None


class ChangeReport(BaseModel):
    run: str | None = Field(description="the run being reported; null when there is nothing to compare yet")
    counts: dict[str, int] = Field(description="flagged / new / dropped / unchanged / total")
    changes: list[Change]


class RunStatus(BaseModel):
    run_id: str
    source: str
    note: str | None
    started_at: datetime
    finished_at: datetime | None
    taken_at: datetime = Field(description="when the run's numbers were taken (its start, if it measured nothing)")
    targets: int = Field(description="accounts the run set out to measure")
    measured: int
    missing: int = Field(description="went looking; account gone or suspended")
    failed: int = Field(description="went looking; could not read it")
    not_reached: int = Field(description="the run stopped before getting there")
    complete: bool = Field(description="false while any target is not_reached")


class Health(BaseModel):
    ok: bool = Field(description="database reachable and data not stale")
    database: str = Field(description="'ok' or the error")
    latest_run: str | None
    data_taken_at: datetime | None
    data_age_seconds: int | None
    stale: bool = Field(description="true when the newest data is older than stale_after_days")
    stale_after_days: int
    latest_run_complete: bool | None = Field(description="false when the newest run left accounts unreached")
    runs: int = Field(description="runs with measurements")

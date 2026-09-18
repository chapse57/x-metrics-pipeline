# x-metrics-pipeline

[github.com/chapse57/x-metrics-pipeline](https://github.com/chapse57/x-metrics-pipeline)

Measure X (Twitter) accounts from a logged-in timeline, classify them with an LLM **behind deterministic guardrails**, validate every number, and ship a CSV + a single-file dashboard — on a schedule.

Built from a paid engagement (100 trading-niche accounts for a day-trading platform's outreach list, Sept 2026). The first 100 were measured by hand; this is the tool that makes that the last time.

Sister project: [data-refinery](https://github.com/chapse57/data-refinery) — the same guardrail and validation ideas applied to cleaning messy contact/company spreadsheets.

![what the agent does and what catches it](docs/architecture.png)

![dashboard](docs/dashboard.png)

## What you get

```
influencers.csv        17 columns, one row per account
dashboard.html         one file — search, filter, sort, scatter. No install, no login.
validation_report.md   every check that ran, and what it found
agent_audit.json       every model call: verdict + which check fired
changes.md / .json     what moved since the previous run: flagged · new · dropped · within thresholds
```

| | columns |
|---|---|
| identity | Handle · Profile Link · Bio Contact |
| size | Followers · Tier |
| engagement | Engagement Rate · Views-to-Followers · Median Likes / Replies / Reposts / Views |
| provenance | Posts Measured · Date Range Measured |
| judgement | Niche · Hook Link · Hook Note · Why This Account Fits |

**Every rate is recomputed from the raw counts and compared to the number being delivered.** Rows that do not reconcile are excluded from the CSV; borderline rows ship flagged, never silently. Engagement is a median over the last N *original* posts — reposts and pinned posts are out of the sample. Each account carries one line saying why it fits, and a link to the post that line came from.

Last run: 95 rows checked, 0 errors, 2 warnings (two accounts sharing one bio URL — same firm, both legitimate).

## What it does

```
collect ──▶ store ──▶ classify ──▶ validate ──▶ export ──▶ diff
Playwright   SQLite    Claude +     recompute    CSV          this run vs
logged-in    4 tables  guardrails   + rules      dashboard    the previous one:
session      + audit   + review     + report     validation   changes.md
                       queue                     agent_audit  changes.json
```

| stage | what it guarantees |
|---|---|
| **collect** (`collect.py`) | Persistent browser profile (log in once, no password stored). Jittered rate limiting, exponential-backoff retries, backs off on X's rate-limit page. **Resumable**: pending accounts are read from the DB, so a crash loses nothing. Reposts and pinned posts are excluded from the sample. The exact `aria-label` string behind every count is stored next to the number. |
| **parse** (`parse.py`) | Formats captured live (`fixtures/aria_samples.json`). X omits zero-valued fields (`"13 likes, 1157 views"`), so absence is recorded as *absent*, never assumed. Markup change → `ParseError`, never a silent 0. |
| **metrics** (`metrics.py`) | Pure functions. Engagement % = median over the last N original posts of (likes+replies+reposts) ÷ followers. Views/followers %. Tier. Days since last post. Everything re-computable from stored posts. |
| **store** (`store.py`) | SQLite: `accounts`, `runs`, `measurements`, `posts`, `agent_audit`. Idempotent upserts; re-running a week overwrites, never duplicates. |
| **classify** (`agent.py`) | Claude proposes niche / spam / confidence / **verbatim evidence quotes**. Output goes through `guard()` — see below. Model id is a flag (`--model`); the offline `RuleClassifier` keeps the pipeline runnable without a key. |
| **validate** (`validate.py`) | Recompute every rate from raw data and compare; min posts; recency; contact present; duplicate bio URLs; cross-check against an external sheet. `error` rows are excluded from the deliverable, `warn` rows are delivered flagged. |
| **export** (`export.py`) | Client CSV (same columns as the hand-made sheet) + `dashboard.html` (no dependencies, opens from disk: search, niche/tier filters, engagement slider, sortable table, followers-vs-engagement scatter, per-account detail). |
| **diff** (`diff.py`) | Compares the two most recent runs per account (or any two by id). Followers, engagement, views/followers, tier, days-since-last-post — every delta reported; a change is *flagged* only past a threshold (`--followers-pct 5`, `--engagement-pp 0.2`, `--views-pct 25`, `--silent-days 14`). Accounts that appear or disappear between runs are listed separately. `--fail-on-flags` lets a scheduler page someone only when something moved. Deltas are computed from stored rows, never estimated. |
| **schedule** | `.github/workflows/weekly.yml`: weekly cron + on push. Tests, then validate `--strict` against the hand-made sheet (a failed check fails the job), export, `diff`; the Slack summary leads with what changed. With no pushed DB it seeds from the 2026-09 deliverable in `fixtures/`, so CI never validates an empty file. Collection stays on a self-hosted runner (needs the logged-in profile). `n8n/x-metrics-weekly.json` for teams on n8n. |
| **MCP** (`mcp_server.py`) | Read-only MCP server so Claude can query the dataset: `search_accounts`, `account`, `validation_summary`, `agent_audit`. |

## The guardrail — what the agent gets wrong, and what catches it

The model never writes to the deliverable. Every answer passes `guard()`:

| check | catches | verdict |
|---|---|---|
| `schema` | prose instead of JSON, missing keys, wrong types, confidence outside 0–1 | rejected |
| `label_set` | invented categories ("Quant futures educator") — labels must come from a closed set | rejected |
| `evidence` | **hallucinated justification**: every evidence quote must be a verbatim substring of the bio/posts the model was shown | rejected |
| `confidence` | below threshold (0.7) | review queue (a human decides) |
| `rule_conflict` | model and a five-pattern keyword screen disagree | review queue |

Every attempt is logged to `agent_audit` with the raw output and which checks fired. Two runs against `claude-haiku-4-5-20251001` (2026-09-07) — the input matters more than the model does:

| input | accepted | review | rejected | checks fired |
|---|---|---|---|---|
| 95 accounts, bio only | 72 | 23 | 0 | `confidence` 16, `rule_conflict` 7 |
| 3 accounts, bio + 20 posts each | 2 | 0 | 1 | `evidence` 1 |

A one-line bio gives the model almost nothing to quote, so `evidence` has little to bite on. What thin input produces instead is uncertainty — and 23 accounts went to a human rather than into the sheet. Give the model the actual posts and `evidence` starts working:

### What it caught

| | |
|---|---|
| the model's quote | `"Short from imbalance, post prior day high sweep. Scales at VWAP, -50% OR extension... Net +2R"` |
| the actual post | `"…-50% OR extension, and wanted opposite side for imbalance to imbalance as ES made the move, but NQ decided to come trail me out at mid range of OR. Net +2R"` |
| the difference | 20 words hidden behind `...` — the part where the trade went against him |
| the model's confidence | **0.92** |
| verdict | **rejected — it never reached the sheet** |

Confidence would have shipped it. The full answer, and the second error inside it, are in [In detail](#in-detail) at the bottom.

`tests/test_agent_guardrails.py` feeds deliberately wrong model outputs (invented category, plausible-but-fabricated quote, paraphrased quote, low confidence, fence-wrapped JSON, prose) and asserts each is caught without the model's cooperation.

## What changed since last week

One measurement says what an account is like. Two say what moved — which is the question a
weekly retainer actually pays for. `diff` compares the latest run with the one before it.

First real pair, the 3 accounts of the 2026-09-06 live run re-measured on 2026-09-18 (12 days, 32 s;
untouched outputs in `docs/live-run-2026-09-18/`):

```
$ python -m xmetrics.cli --db out/x.db diff --out out
changes since 20260906T031921Z-playwright → 20260918T020745Z-playwright: 2 flagged | 0 new | 0 dropped | 1 unchanged of 3
  @realFatCat1          views_up
  @merrittblack         went_silent
```

| handle | flags | followers | engagement | views/followers | last post (days) |
|---|---|---|---|---|---|
| @realFatCat1 | views_up | 3,143 → 3,259 (+3.7%) | 0.75% → 0.84% (+0.10 pp) | 41.6% → 54.1% (+30.1%) | 2 → 0 |
| @merrittblack | went_silent | 18,900 → 19,000 (+0.5%) | 0.26% → 0.27% (+0.00 pp) | 23.8% → 24.3% (+1.9%) | 5 → 17 |
| @ProbableChris | — | 7,681 → 7,757 (+1.0%) | 0.70% → 0.59% (−0.11 pp) | 60.1% → 51.7% (−14%) | within thresholds |

Read it the way a client would: one account's reach jumped a third while its follower count barely moved
(worth a look — a post took off), one account stopped posting (17 days; drop it from an outreach list until it
is back), and one drifted down on every rate but not enough to act on. `@realFatCat1`'s +3.7 % followers is
reported but *not* flagged — under the 5 % line on purpose.

Two rules keep the flags honest. A relative move on a tiny base is not a flag: engagement has to move by at
least 0.2 percentage points *and* 25 % — `0.30 % → 0.42 %` is +40 % and still not flagged. And a tier crossing
is a fact, not a threshold: it stays flagged however loose you set the numbers. `tests/test_diff.py` puts a
row on each side of every line.

## Live run (2026-09-06, Windows, logged-in session) — evidence in `docs/live-run-2026-09-06/`

| | |
|---|---|
| ![collector](docs/live-run-2026-09-06/01_collector_logged_in_timeline.png) | ![terminal](docs/live-run-2026-09-06/02_terminal_collect_progress.png) |
| collector's own browser, logged-in timeline being read | 3 accounts in 36 s, 20 original posts each |

![run and dashboard](docs/live-run-2026-09-06/03_terminal_run_and_dashboard.png)

Files in that folder are the untouched outputs of that run: `terminal.log`, `influencers.csv`, `dashboard.html`,
`validation_report.md`, `agent_audit.json`. Against the sheet measured by hand the day before:

| account | pipeline | hand (09-05) |
|---|---|---|
| @realFatCat1 | 3,143 · ER 0.75% · VR 41.6% | 3,142 · 0.70% · 39.4% |
| @merrittblack | 18,900 · 0.26% · 23.8% | 18,900 · 0.27% · 28.7% |
| @ProbableChris | 7,681 · 0.70% · 60.1% | 7,682 · 0.72% · 58.7% |

Differences are one day of new posts shifting the 20-post window, plus the per-post-median definition (Design notes).
Classification of those 3 accounts with bio + 20 post texts (Claude): 2 accepted, 1 rejected by `evidence` — the case above. Validation: 0 errors.

![collector reading a logged-in timeline](docs/collect.gif)

The GIF is that run, recorded by the collector itself (`collect --record docs/recording …` → `python tools/webm_to_gif.py docs/recording`).

## Proof on real data

`fixtures/` holds the hand-measured deliverable (JSONL of medians for 148 accounts, 53 screened out with reasons, and the CSV the client received).

```
$ python -m pytest -q
53 passed
$ python -m xmetrics.cli --db out/x.db import-legacy fixtures/legacy_m2_results.jsonl
imported: 95 accounts (53 screened out)
$ python -m xmetrics.cli --db out/x.db validate --csv fixtures/legacy_master.csv
rows checked: 95 | errors: 0 | warnings: 7
```

Every followers count, engagement rate, views ratio and post count in the delivered CSV matches the recomputation (tolerance = the sheet's rounding). The 7 warnings: 5 rows measured before the JSONL existed, 2 accounts sharing one bio URL (same firm, both legitimate).

## Run it

```bash
pip install -e .[dev]
python -m playwright install chromium

# 1. one-time session — either log in in the collector's own browser window…
python -m xmetrics.cli login --profile .xmetrics-profile
#    …or reuse a session from a browser you are already logged in to (DevTools > Application > Cookies > x.com)
python -m xmetrics.cli import-cookies --profile .xmetrics-profile

# 2. measure
python -m xmetrics.cli --db out/x.db collect --profile .xmetrics-profile realFatCat1 merrittblack ...
#    Ctrl-C any time; re-run the same command to resume.

# 3. classify (Claude if ANTHROPIC_API_KEY is set, else the offline rule classifier)
python -m xmetrics.cli --db out/x.db classify --claude

# 4. validate + export
python -m xmetrics.cli --db out/x.db run --out out --strict
# -> out/influencers.csv  out/dashboard.html  out/validation_report.md  out/agent_audit.json

# 5. next week, after another collect: what moved?
python -m xmetrics.cli --db out/x.db diff --out out            # -> out/changes.md  out/changes.json
```

## Design notes

- **Measure, don't estimate.** Nothing here calls a third-party "influencer score" API. Numbers come from the timeline a person would see, and the raw strings are kept.
- **Sum of medians vs median of sums.** The hand-made sheet used Σmedian(likes, replies, reposts)/followers. The pipeline computes median(likes+replies+reposts) per post — the statistically honest one. Legacy rows keep their definition so validation compares like with like; the difference is documented in `metrics.py`.
- **Fail loudly.** Markup drift raises; validation errors exclude rows and (with `--strict`) fail CI. A tool that quietly gives wrong numbers is worse than no tool.
- **Boring things done right.** Retries with backoff, jittered rate limiting, idempotent writes, resumable runs, audit table, evidence stored beside every number.

## In detail

For readers who want the whole answer rather than the summary.

### The rejected answer, in full

`claude-haiku-4-5-20251001`, @ProbableChris, bio + 20 posts:

```json
{"niche": "Day trading (stocks)", "is_spam": false, "confidence": 0.92,
 "evidence": [
   "I trade $NQ using statistical models, probabilistic frameworks, and custom tools built based on my market perspective.",
   "1:1 scalps after the NY equity open have been doing ok, because that is the bulk of opportunity that exists in these tight open ranges.",
   "Short from imbalance, post prior day high sweep. Scales at VWAP, -50% OR extension... Net +2R"],
 "fit_note": "Experienced NQ day trader sharing genuine setups, trade analysis, and statistical frameworks."}
```

The first two quotes are verbatim. The third is two ends of one post joined by an ellipsis, with the middle — where the trade went against him — removed. `evidence` rejected the answer.

**There is a second error in there, and the guardrail does not catch it.** `$NQ` is Nasdaq futures, not stocks, so `"Day trading (stocks)"` is the wrong label. That error passes every check: the label is inside the closed set, and the model's own confidence was 0.92. It happened to be discarded because the same answer failed on evidence. A check the model does not participate in is the only reason either problem stopped here.

Re-running the same account reproduces the same stitched quote, so this is a repeatable failure mode rather than a one-off.

### When two signals disagree

All 7 `rule_conflict` rows in the 95-account run point the same way: the model called the account a signal-seller, the keyword screen did not. All 7 arrived at confidence 0.85–0.95, so no confidence threshold would have caught them.

The model is not simply right here. Some calls are sound — `"Free + Premium Discord in Bio"`, `"SPX/SPY timing signals"`, `"Subscribe to my real time alerts"` are all things the five patterns miss, because they look for `join my discord` and `dm for signals`. Others overreach: one account was flagged for having a website in its bio, another for linking a prop-firm affiliate code.

So neither side wins automatically. All 7 go to a human queue, and the deliverable says nothing about them until someone looks. The point is not that the model was right. It is that **two independent signals disagreeing is information the model's own confidence does not contain.**

### Why the keyword screen stays small

It is five regexes, and that is deliberate. Its job is not to detect spam well; its job is to be an opinion the model cannot influence. Grow it toward the model's judgement and it stops being independent, which is the only property that makes `rule_conflict` mean anything.

## Layout

```
xmetrics/   parse · metrics · store · legacy · validate · agent · export · diff · collect · cli
tests/      53 tests (parser formats, metrics, legacy cross-check, guardrails, diff thresholds)
fixtures/   captured aria-labels + the real 2026-09 deliverable
out/        generated: CSV, dashboard, validation report, agent audit
mcp_server.py  ·  n8n/  ·  .github/workflows/weekly.yml
```

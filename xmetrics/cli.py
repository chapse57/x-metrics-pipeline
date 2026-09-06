"""Command line.

  xmetrics import-legacy  fixtures/legacy_m2_results.jsonl --db out/xmetrics.db
  xmetrics collect        --db out/xmetrics.db --profile ~/.xmetrics-profile [handles...]
  xmetrics classify       --db out/xmetrics.db [--claude]        # default: offline rule classifier
  xmetrics validate       --db out/xmetrics.db [--csv master.csv]
  xmetrics export         --db out/xmetrics.db --out out/
  xmetrics run            --db out/xmetrics.db --out out/          # validate + export (the weekly job)
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import agent as ag
from .export import rows_for_export, write_csv, write_dashboard
from .legacy import import_jsonl
from .store import Store
from .validate import report, run_all


def _store(args) -> Store:
    return Store(args.db)


def cmd_import_legacy(args) -> int:
    st = _store(args)
    run_id = import_jsonl(st, args.jsonl, screened_csv=args.screened)
    n = len(st.latest_measurements())
    so = st.conn.execute("SELECT COUNT(*) FROM accounts WHERE status='screened_out'").fetchone()[0]
    print(f"imported run {run_id}: {n} accounts ({so} screened out)")
    return 0


def cmd_login(args) -> int:
    """Open the collector's own browser profile so you can log in to X once. No credentials are stored by this tool;
    the browser keeps its own session cookies in --profile, exactly like a normal Chrome profile."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(str(Path(args.profile)), headless=False, locale="en-US", viewport={"width": 1200, "height": 900})
        page = ctx.new_page()
        page.goto("https://x.com/login")
        print("A browser window is open. Log in to X there, wait until your home timeline shows, then close the window.")
        try:
            page.wait_for_event("close", timeout=0)
        except Exception:
            pass
        ctx.close()
    print(f"profile saved: {args.profile}")
    return 0


def cmd_import_cookies(args) -> int:
    """Reuse an existing X session without a password: paste the two session cookies from a browser
    where you are already logged in (DevTools > Application > Cookies > https://x.com > auth_token, ct0).
    Values are read with getpass (not echoed, not in shell history) and stored only inside --profile."""
    import getpass
    from playwright.sync_api import sync_playwright
    from .collect import COOKIE_FILE, is_logged_in
    auth = getpass.getpass("auth_token (hidden): ").strip()
    ct0 = getpass.getpass("ct0 (hidden): ").strip()
    if len(auth) < 20 or len(ct0) < 20:
        print("values look too short — copy the full cookie values"); return 2
    cookies = [
        {"name": "auth_token", "value": auth, "domain": ".x.com", "path": "/", "httpOnly": True, "secure": True, "sameSite": "None"},
        {"name": "ct0", "value": ct0, "domain": ".x.com", "path": "/", "secure": True, "sameSite": "Lax"},
    ]
    profile = Path(args.profile)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(str(profile), headless=False, locale="en-US", viewport={"width": 1200, "height": 900})
        ctx.add_cookies(cookies)
        page = ctx.new_page()
        page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=45_000)
        ok = is_logged_in(page)
        ctx.close()
    if ok:
        profile.mkdir(parents=True, exist_ok=True)
        (profile / COOKIE_FILE).write_text(json.dumps(cookies), encoding="utf-8")
        print(f"session OK — saved to {profile / COOKIE_FILE} (re-applied on every collect)")
        return 0
    print("X did not accept the session (wrong/expired values?) — nothing saved")
    return 1


def cmd_status(args) -> int:
    st = _store(args)
    rows = st.conn.execute("SELECT status, COUNT(*) n FROM accounts GROUP BY status").fetchall()
    print({r["status"]: r["n"] for r in rows})
    for r in st.conn.execute("SELECT display, status, screen_reason FROM accounts WHERE status IN ('error','pending') ORDER BY updated_at DESC LIMIT 20"):
        print(f"  @{r['display']:<20} {r['status']:<8} {r['screen_reason'] or ''}")
    return 0


def cmd_collect(args) -> int:
    from .collect import Collector
    st = _store(args)
    run_id = Collector(st, args.profile, headless=args.headless, posts_per_account=args.posts,
                       record_dir=args.record).run(args.handles, note=args.note)
    if args.record and run_id:
        print(f"video(s) written to {args.record}/ — convert with: python tools/webm_to_gif.py {args.record}")
    print(f"run {run_id}: {len(st.latest_measurements())} measured, {len(st.pending_handles())} pending")
    return 0


def cmd_classify(args) -> int:
    st = _store(args)
    clf = ag.ClaudeClassifier(model=args.model) if args.claude else ag.RuleClassifier()
    rows = st.latest_measurements()
    counts = {"accepted": 0, "review": 0, "rejected": 0}
    for r in rows:
        if r["niche"] and r["niche_source"] in ("human", "legacy") and not (args.force or args.dry_run):
            continue
        texts = [t["text"] for t in st.conn.execute(
            "SELECT text FROM post_texts WHERE handle=? AND run_id=? ORDER BY idx", (r["handle"], r["run_id"])
        ).fetchall()] if _has_texts(st) else []
        inp = ag.AgentInput(r["handle"], r["bio"] or "", texts)
        v = ag.classify_with_guardrails(clf, inp, threshold=args.threshold)
        st.log_agent(r["handle"], clf.name, v.raw, v.parsed, v.status, v.failed)
        counts[v.status] += 1
        if v.status == "accepted" and not args.dry_run:
            st.upsert_account(r["handle"], niche=v.parsed["niche"], niche_source="agent",
                              fit_note=v.parsed.get("fit_note") or r["fit_note"])
            if v.parsed["is_spam"]:
                st.set_status(r["handle"], "screened_out", "agent: spam pattern (accepted with evidence)")
    print(json.dumps(counts))
    return 0


def _has_texts(st: Store) -> bool:
    return bool(st.conn.execute("SELECT 1 FROM sqlite_master WHERE name='post_texts'").fetchone())


def cmd_validate(args) -> int:
    st = _store(args)
    issues = run_all(st, csv_path=args.csv)
    txt = report(issues, len(st.latest_measurements()))
    out = Path(args.out) / "validation_report.md" if args.out else None
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(txt, encoding="utf-8")
    print(txt)
    return 1 if any(i.severity == "error" for i in issues) and args.strict else 0


def cmd_export(args) -> int:
    st = _store(args)
    issues = run_all(st)
    rows, blocked = rows_for_export(st, issues)
    out = Path(args.out)
    csv_path = write_csv(rows, out / "influencers.csv")
    dash = write_dashboard(rows, out / "dashboard.html", title=args.title,
                           subtitle=f"{len(rows)} accounts · median of last N original posts · {blocked and f'{len(blocked)} excluded by validation' or 'all rows passed validation'}")
    (out / "validation_report.md").write_text(report(issues, len(rows) + len(blocked)), encoding="utf-8")
    (out / "agent_audit.json").write_text(json.dumps(st.agent_audit_summary(), indent=2), encoding="utf-8")
    print(f"wrote {csv_path}\nwrote {dash}\nexcluded: {blocked}")
    return 0


def cmd_run(args) -> int:
    rc = cmd_validate(args)
    return rc or cmd_export(args)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(prog="xmetrics")
    p.add_argument("--db", default="out/xmetrics.db")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("import-legacy"); s.add_argument("jsonl"); s.add_argument("--screened"); s.set_defaults(fn=cmd_import_legacy)

    s = sub.add_parser("login"); s.add_argument("--profile", default=".xmetrics-profile"); s.set_defaults(fn=cmd_login)
    s = sub.add_parser("import-cookies", help="reuse an existing browser session (auth_token + ct0) instead of logging in")
    s.add_argument("--profile", default=".xmetrics-profile"); s.set_defaults(fn=cmd_import_cookies)

    s = sub.add_parser("status"); s.set_defaults(fn=cmd_status)

    s = sub.add_parser("collect"); s.add_argument("handles", nargs="*"); s.add_argument("--profile", default=".xmetrics-profile")
    s.add_argument("--headless", action="store_true"); s.add_argument("--posts", type=int, default=20); s.add_argument("--note")
    s.add_argument("--record", metavar="DIR", help="record the browser session as .webm into DIR (evidence / demo GIF)")
    s.set_defaults(fn=cmd_collect)

    s = sub.add_parser("classify"); s.add_argument("--claude", action="store_true"); s.add_argument("--model", default="claude-sonnet-4-5")
    s.add_argument("--threshold", type=float, default=0.7); s.add_argument("--force", action="store_true")
    s.add_argument("--dry-run", action="store_true", help="log to agent_audit only; never overwrite niches"); s.set_defaults(fn=cmd_classify)

    for name, fn in (("validate", cmd_validate), ("export", cmd_export), ("run", cmd_run)):
        s = sub.add_parser(name); s.add_argument("--out", default="out"); s.add_argument("--csv")
        s.add_argument("--strict", action="store_true"); s.add_argument("--title", default="X influencer metrics"); s.set_defaults(fn=fn)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())

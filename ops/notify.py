"""The one message the pipeline sends: a Slack incoming-webhook post when an attempt fails.

Success is silent. A message that arrives every week whether or not anything went wrong is
a message nobody reads; the only notification worth having is the one that means "act".

    XMETRICS_SLACK_WEBHOOK=https://hooks.slack.com/services/T.../B.../...
    python -m ops.notify "test message"        # prove the webhook works before you rely on it

No third-party HTTP library: urllib is enough for one POST, and one fewer dependency on the
box that runs at night.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.error
import urllib.request
from typing import Protocol

log = logging.getLogger("ops.notify")

WEBHOOK_ENV = "XMETRICS_SLACK_WEBHOOK"


class Notifier(Protocol):
    def send(self, text: str) -> bool: ...


class NullNotifier:
    """No webhook configured: log the message so it still lands somewhere."""
    def send(self, text: str) -> bool:
        log.warning("no %s set — alert not sent:\n%s", WEBHOOK_ENV, text)
        return False


class SlackNotifier:
    def __init__(self, webhook: str, timeout: float = 10.0):
        self.webhook, self.timeout = webhook, timeout

    def send(self, text: str) -> bool:
        body = json.dumps({"text": text}).encode("utf-8")
        req = urllib.request.Request(self.webhook, data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                ok = 200 <= resp.status < 300
        except (urllib.error.URLError, TimeoutError) as e:
            log.error("slack webhook failed: %s", e)
            return False
        if not ok:
            log.error("slack webhook answered %s", resp.status)
        return ok


def from_env() -> Notifier:
    hook = os.environ.get(WEBHOOK_ENV)
    return SlackNotifier(hook) if hook else NullNotifier()


def failure_message(attempt_id: int, step: str, error: str, host: str | None, retry_cmd: str) -> str:
    first = error.strip().splitlines()[0] if error.strip() else "(no error text)"
    return (f":red_circle: x-metrics pipeline #{attempt_id} failed at *{step}*"
            f"{f' on {host}' if host else ''}\n"
            f"> {first}\n"
            f"The dashboard keeps the previous data and its age card keeps counting. "
            f"Re-run only this attempt: `{retry_cmd}`")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="send one message to the configured Slack webhook")
    p.add_argument("text")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    n = from_env()
    if isinstance(n, NullNotifier):
        print(f"set {WEBHOOK_ENV} first"); return 2
    ok = n.send(args.text)
    print("sent" if ok else "not sent")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

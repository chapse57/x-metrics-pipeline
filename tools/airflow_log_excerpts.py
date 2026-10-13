"""Print the lines that matter from Airflow's task logs (airflow/logs is git-ignored).

    python tools/airflow_log_excerpts.py > docs/airflow-log-excerpts-2026-10-13.txt

One block per DAG run, one sub-block per task attempt, start-up noise left out. The log files
themselves stay on the machine that ran Airflow; this is the copy that can be committed.
"""
import glob
import json
import os
import re
import sys
from pathlib import Path

LOGS = Path(__file__).resolve().parents[1] / "airflow" / "logs"
NOISE = re.compile(r"^(::|DAG bundles|Filling up the DagBag|Worker startup|Pushing xcom|Found credentials"
                   r"|Task instance is in running state|Executing |Marking task|Task exited|Tmp dir)")


def events(path: str):
    for line in open(path, encoding="utf-8", errors="replace"):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        ev = str(d.get("event", "")).strip()
        if not ev or NOISE.match(ev):
            continue
        err = d.get("error_detail") or []
        if err and isinstance(err, list) and isinstance(err[0], dict):
            ev += f"  [{err[0].get('exc_type')}: {err[0].get('exc_value')}]"
        yield str(d.get("timestamp", ""))[:19].replace("T", " "), d.get("level", ""), ev.replace("\n", " | ")


def main() -> int:
    os.chdir(LOGS)
    print("# Airflow task log excerpts — airflow/logs (git-ignored), printed by tools/airflow_log_excerpts.py")
    print("# times are UTC; 'error' level on progress lines is Airflow tagging stderr, not a failure\n")
    for dag in sorted(p.split("=", 1)[1] for p in glob.glob("dag_id=*") if "xmetrics_af_check" not in p):
        print(f"######## DAG {dag}")
        for run in sorted(glob.glob(f"dag_id={dag}/run_id=*")):
            print(f"\n=== {run.split('run_id=')[1]}")
            for task in sorted(glob.glob(f"{run}/task_id=*")):
                for attempt in sorted(glob.glob(f"{task}/attempt=*.log")):
                    print(f"--- {task.split('task_id=')[1]} {os.path.basename(attempt)[:-4]}")
                    for ts, lv, ev in events(attempt):
                        print(f"  {ts} {lv:5} {ev[:600]}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())

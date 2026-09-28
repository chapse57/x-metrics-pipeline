@echo off
rem Copy to ops\env.cmd (git-ignored) and fill in. run_weekly.cmd calls it before the pipeline.
rem Owner login: the pipeline is the one process that writes (load + ops.pipeline_runs).
set XMETRICS_PG_DSN=postgresql://xmetrics:xmetrics@localhost:5432/xmetrics
rem Slack incoming webhook. Leave empty to log failures instead of sending them.
set XMETRICS_SLACK_WEBHOOK=

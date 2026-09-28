@echo off
rem The scheduled job. Task Scheduler runs this every Sunday 22:00 (ops\schedule_windows.ps1).
rem One log file per run under out\logs; the pipeline's own ledger is ops.pipeline_runs.
setlocal
cd /d "%~dp0.."
if not exist ops\env.cmd (
  echo ops\env.cmd missing - copy ops\env.example.cmd and fill it in
  exit /b 2
)
call ops\env.cmd
call .venv\Scripts\activate.bat
if not exist out\logs mkdir out\logs
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmm"') do set STAMP=%%i
python -m ops.pipeline --trigger scheduled --db out\x.db --out out --profile .xmetrics-profile > "out\logs\pipeline-%STAMP%.log" 2>&1
set RC=%ERRORLEVEL%
type "out\logs\pipeline-%STAMP%.log"
exit /b %RC%

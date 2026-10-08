@echo off
rem The scheduled job. Task Scheduler starts it every Sunday 22:00 and 5 minutes after each
rem logon (ops\schedule_windows.ps1); --if-due makes it collect only when the last successful
rem run is 6 or more days old, so a run that was missed or cut off is caught up at the next logon.
rem One log file per start under out\logs; the pipeline's own record is ops.pipeline_runs.
setlocal
title x-metrics pipeline - closes by itself when done
cd /d "%~dp0.."
if not exist ops\env.cmd (
  echo ops\env.cmd missing - copy ops\env.example.cmd and fill it in
  exit /b 2
)
call ops\env.cmd
call .venv\Scripts\activate.bat
rem Write log lines as they happen: a process that is killed keeps what it had already said.
set PYTHONUNBUFFERED=1
rem UTF-8 for the log file. Redirected output otherwise uses the console code page (cp949 on a
rem Korean Windows), which cannot encode the em dash in the pipeline's last line: a run that
rem succeeded would have crashed on its own summary and reported exit 1.
set PYTHONUTF8=1
if not exist out\logs mkdir out\logs
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmm"') do set STAMP=%%i
set LOG=out\logs\pipeline-%STAMP%.log
echo %date% %time% start >> "%LOG%"

rem The database first. After a reboot Docker Desktop is up but a container may not be, and a
rem connection to it can wait without end (2026-10-04). compose restarts it on its own now;
rem this is the second line of defence. Up to 2 minutes: crash recovery after a hard power-off
rem took about one.
docker compose up -d postgres >> "%LOG%" 2>&1
set /a TRIES=0
:wait_db
docker compose exec -T postgres pg_isready -q -U xmetrics >nul 2>&1 && goto db_ready
set /a TRIES+=1
if %TRIES% geq 24 (
  echo %date% %time% postgres not ready after 2 minutes - the pipeline will report it >> "%LOG%"
  goto db_ready
)
ping -n 6 127.0.0.1 >nul
goto wait_db
:db_ready

python -m ops.pipeline --trigger scheduled --if-due 6 --db out\x.db --out out --profile .xmetrics-profile >> "%LOG%" 2>&1
set RC=%ERRORLEVEL%
type "%LOG%"
exit /b %RC%

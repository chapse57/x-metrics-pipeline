# Windows에서 라이브 수집 (PowerShell)

```powershell
cd C:\Users\PC\Desktop\Upwork\claude-work\x-metrics-pipeline
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .[dev]
python -m playwright install chromium

# 1) X 로그인 1회 — 전용 브라우저 창이 뜸. 로그인 후 홈 타임라인 보이면 창 닫기
python -m xmetrics.cli login

# 2) 시험 수집 (3계정) — Ctrl+C 해도 다음 실행 때 이어서 함
python -m xmetrics.cli --db out\x.db collect realFatCat1 merrittblack ProbableChris

# 3) 검증 + 내보내기
python -m xmetrics.cli --db out\x.db run --out out
#    -> out\influencers.csv, out\dashboard.html, out\validation_report.md
```

잘 되면 4)로:
```powershell
# 4) Claude 분류 (키 있을 때) — 없으면 --claude 빼면 규칙 분류기
$env:ANTHROPIC_API_KEY="sk-ant-..."
python -m xmetrics.cli --db out\x.db classify --claude
python -m xmetrics.cli --db out\x.db run --out out
```

막히면 그대로 에러 메시지 복사해서 알려주세요. 

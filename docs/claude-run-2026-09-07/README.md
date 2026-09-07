# Claude run — 2026-09-07

Untouched outputs of the classification runs quoted in the main README.
Model: `claude-haiku-4-5-20251001`. Threshold 0.7.

| file | what it is |
|---|---|
| `audit_95_bio_only.json` | verdict + guardrail counts, 95 accounts, bio as the only input |
| `audit_3_with_posts.json` | same, 3 accounts with bio + 20 original post texts each |
| `guardrail_catches_95accounts.md` | every answer the guardrail did not accept, and what failed |
| `guardrail_catches_3accounts.md` | same, including the raw model output for the rejected one |
| `influencers.csv` | the deliverable produced from the 95-account run |
| `validation_report.md` | every validation check that ran against it |

Reproduce:

```bash
python -m xmetrics.cli --db out/x.db import-legacy fixtures/legacy_m2_results.jsonl --screened fixtures/legacy_screened_out.csv
python -m xmetrics.cli --db out/x.db classify --claude --model claude-haiku-4-5-20251001
python tools/audit_table.py --db out/x.db --raw 3
```

Counts will not match exactly — the model is sampled, not deterministic. The checks are.

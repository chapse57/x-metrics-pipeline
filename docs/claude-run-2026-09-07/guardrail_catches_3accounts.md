```json
{
 "attempts": 3,
 "by_verdict": {
  "accepted": 2,
  "rejected": 1
 },
 "guardrail_fired": {
  "evidence": 1
 },
 "classifiers": {
  "claude:claude-haiku-4-5-20251001": 3
 }
}
```

## What the model got wrong (1 of 3 answers refused)

| account | verdict | check | what the model said |
|---|---|---|---|
| @probablechris | rejected | `evidence` | quote not in source: "Short from imbalance, post prior day high sweep. Scales at VWAP, -50% OR extension... Net +2R" |

| check | catches |
|---|---|
| `evidence` (1) | evidence quote is not a verbatim substring of the input |

### raw output — @probablechris (rejected: evidence)

```
```json
{
  "niche": "Day trading (stocks)",
  "is_spam": false,
  "confidence": 0.92,
  "evidence": [
    "I trade $NQ using statistical models, probabilistic frameworks, and custom tools built based on my market perspective.",
    "1:1 scalps after the NY equity open have been doing ok, because that is the bulk of opportunity that exists in these tight open ranges.",
    "Short from imbalance, post prior day high sweep. Scales at VWAP, -50% OR extension... Net +2R"
  ],
  "fit_note": "Experienced NQ day trader sharing genuine setups, trade analysis, and statistical frameworks; legitimate educational content without signal-selling."
}
```
```

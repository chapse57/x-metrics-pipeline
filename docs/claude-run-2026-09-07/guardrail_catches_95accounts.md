```json
{
 "attempts": 95,
 "by_verdict": {
  "accepted": 72,
  "review": 23
 },
 "guardrail_fired": {
  "confidence": 16,
  "rule_conflict": 7
 },
 "classifiers": {
  "claude:claude-haiku-4-5-20251001": 95
 }
}
```

## What the model got wrong (23 of 95 answers refused)

| account | verdict | check | what the model said |
|---|---|---|---|
| @_amtrades | review | `confidence` | confidence = 0.3 |
| @frankof369 | review | `confidence` | confidence = 0.6 |
| @thetastowe42 | review | `rule_conflict` | model is_spam = True, rule screen disagrees |
| @brianleetrades | review | `confidence` | confidence = 0.65 |
| @triggertrades | review | `rule_conflict` | model is_spam = True, rule screen disagrees |
| @greckothe1 | review | `confidence` | confidence = 0.65 |
| @masterpandawu | review | `rule_conflict` | model is_spam = True, rule screen disagrees |
| @tradingthomas3 | review | `rule_conflict` | model is_spam = True, rule screen disagrees |
| @arjoio | review | `confidence` | confidence = 0.6 |
| @walterdeemer | review | `confidence` | confidence = 0.6 |
| @eliteoptions2 | review | `rule_conflict` | model is_spam = True, rule screen disagrees |
| @davetradesopts | review | `confidence` | confidence = 0.65 |
| @stoicta | review | `confidence` | confidence = 0.6 |
| @braczyy | review | `confidence` | confidence = 0.3 |
| @trader_dante | review | `confidence` | confidence = 0.65 |
| @yuriymatso | review | `confidence` | confidence = 0.6 |
| @profplum99 | review | `confidence` | confidence = 0.4 |
| @wolfryan | review | `confidence` | confidence = 0.65 |
| @i_am_the_algo | review | `confidence` | confidence = 0.65 |
| @casper_smc | review | `confidence` | confidence = 0.6 |
| @shinobbs | review | `rule_conflict` | model is_spam = True, rule screen disagrees |
| @jake__wujastyk | review | `rule_conflict` | model is_spam = True, rule screen disagrees |
| @theshortbear | review | `confidence` | confidence = 0.6 |

| check | catches |
|---|---|
| `confidence` (16) | confidence below threshold |
| `rule_conflict` (7) | disagrees with the rule-based spam screen |

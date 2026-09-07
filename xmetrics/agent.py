"""LLM classification with guardrails.

The model (Claude) reads an account's bio + recent post texts and proposes:
  niche      one of NICHES (closed set)
  is_spam    pump / squatting / bot pattern
  confidence 0..1
  evidence   short quotes copied from the input that justify the label
  fit_note   one sentence for the client sheet

The model's answer is never written to the deliverable directly. It goes through
`guard()`, which is deterministic and knows nothing about the model:

  schema        parses as JSON with exactly the expected keys/types
  label_set     niche is in the closed set (no invented categories)
  evidence      every evidence quote is a verbatim substring of the input,
                ignoring whitespace (catches hallucinated justifications, and
                quotes stitched together across an ellipsis)
  confidence    >= threshold, else -> review queue (a human decides)
  rule_conflict the five-pattern keyword screen disagrees on spam -> review queue
                (kept deliberately small: its value is being an opinion the model
                 cannot influence, not being a good spam detector)

Every attempt is logged to agent_audit with the verdict and the checks that failed,
which is where the README's "what the agent got wrong" table comes from.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Protocol

NICHES = (
    "Futures / order flow",
    "Prop trading",
    "Day trading (stocks)",
    "Swing trading (stocks)",
    "Options / 0DTE",
    "Trading psychology / education",
    "Macro commentary",
    "Crypto",
    "Other",
)

SPAM_PATTERNS = (
    r"\bjoin (my|our) (telegram|discord|whatsapp)\b",
    r"\b(guaranteed|100%|risk[- ]free) (profits?|returns?|wins?)\b",
    r"\bsignals?\b.*\b(vip|premium|paid)\b",
    r"\bdm (me )?for (signals|access|alerts)\b",
    r"\bpump\b",
)


@dataclass(frozen=True)
class AgentInput:
    handle: str
    bio: str
    posts: list[str]  # recent post texts (may be empty for legacy rows)

    def corpus(self) -> str:
        return "\n".join([self.bio, *self.posts])


@dataclass
class Verdict:
    status: str                      # accepted | review | rejected
    parsed: dict | None
    failed: list[str] = field(default_factory=list)
    raw: str | None = None


class Classifier(Protocol):
    name: str
    def classify(self, inp: AgentInput) -> str: ...  # returns raw text (expected JSON)


# ---------------------------------------------------------------- rules --
def rule_spam(text: str) -> bool:
    t = text.lower()
    return any(re.search(p, t) for p in SPAM_PATTERNS)


_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("Options / 0DTE", ("0dte", "options", "theta", "gamma", "spx", "premium selling")),
    ("Futures / order flow", ("futures", "order flow", "orderflow", "es ", "nq ", "emini", "e-mini", "footprint", "volume profile")),
    ("Prop trading", ("prop firm", "prop trader", "prop desk", "funded", "apex", "topstep")),
    ("Crypto", ("crypto", "bitcoin", "btc", "eth ", "altcoin", "perps")),
    ("Macro commentary", ("macro", "fed", "cpi", "yields", "strategist")),
    ("Swing trading (stocks)", ("swing", "breakout", "momentum", "canslim")),
    ("Day trading (stocks)", ("day trad", "daytrad", "scalp", "small cap", "penny")),
    ("Trading psychology / education", ("psychology", "mindset", "coach", "mentor", "educat", "journal")),
]


class RuleClassifier:
    """Offline fallback (no API key / no network). Deliberately simple; its job is
    to keep the pipeline runnable and to give the LLM something to disagree with."""
    name = "rule"

    def classify(self, inp: AgentInput) -> str:
        text = inp.corpus().lower()
        niche, hits = "Other", 0
        for label, kws in _KEYWORDS:
            n = sum(text.count(k) for k in kws)
            if n > hits:
                niche, hits = label, n
        ev = [k for _, kws in _KEYWORDS for k in kws if k in text][:3]
        # evidence must be verbatim substrings of the *original* text, so look them up case-insensitively
        quotes = []
        for k in ev:
            i = text.find(k)
            if i >= 0:
                quotes.append(inp.corpus()[i:i + len(k)])
        return json.dumps({
            "niche": niche, "is_spam": rule_spam(text), "confidence": min(0.5 + 0.1 * hits, 0.9) if hits else 0.3,
            "evidence": quotes, "fit_note": "",
        })


# --------------------------------------------------------------- claude --
_SYSTEM = """You classify X (Twitter) trading accounts for an influencer outreach list.
Return ONLY a JSON object with keys: niche, is_spam, confidence, evidence, fit_note.
- niche: exactly one of: %s
- is_spam: true if the account sells signals, pumps, or squats a handle; false otherwise
- confidence: number 0..1
- evidence: 1-3 short quotes COPIED VERBATIM from the input text (do not paraphrase)
- fit_note: one sentence (<=30 words) saying why the account fits or does not fit an outreach list for a day-trading platform
No prose outside the JSON.""" % ", ".join(f'"{n}"' for n in NICHES)


class ClaudeClassifier:
    def __init__(self, model: str = "claude-haiku-4-5-20251001", api_key: str | None = None):
        import anthropic  # imported lazily so the rest of the pipeline works without the SDK
        self._client = anthropic.Anthropic(api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"))
        self.model = model
        self.name = f"claude:{model}"

    def classify(self, inp: AgentInput) -> str:
        user = f"handle: @{inp.handle}\nbio: {inp.bio}\nrecent posts:\n" + "\n".join(f"- {p}" for p in inp.posts)
        msg = self._client.messages.create(model=self.model, max_tokens=400, system=_SYSTEM,
                                           messages=[{"role": "user", "content": user}])
        return "".join(getattr(b, "text", "") for b in msg.content)


# ------------------------------------------------------------- guardrail --
_REQUIRED = {"niche": str, "is_spam": bool, "confidence": (int, float), "evidence": list, "fit_note": str}


def _extract_json(raw: str) -> dict | None:
    raw = raw.strip()
    m = re.search(r"\{.*\}", raw, re.S)  # tolerate ```json fences / stray prose
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _norm(s: str) -> str:
    """Comparison form for the evidence check: whitespace removed, case folded.

    Whitespace carries no meaning here and is not reliably reproducible — the DOM
    puts inline links (@mentions, hashtags, t.co) on their own text nodes, so a bio
    that reads `for @Foo. Ex Head of ...` is stored with newlines around the mention.
    Ignoring whitespace keeps the check on what it is actually for: the quote's
    characters, in order, must exist in the text the model was shown. Paraphrases,
    invented quotes, and quotes stitched together across an ellipsis still fail.
    """
    return re.sub(r"\s+", "", s).lower()


def guard(raw: str, inp: AgentInput, *, rule_says_spam: bool | None = None, threshold: float = 0.7) -> Verdict:
    failed: list[str] = []
    parsed = _extract_json(raw)
    if parsed is None:
        return Verdict("rejected", None, ["schema"], raw)
    for k, t in _REQUIRED.items():
        if k not in parsed or not isinstance(parsed[k], t) or (k == "confidence" and isinstance(parsed[k], bool)):
            failed.append("schema")
            break
    if "schema" in failed:
        return Verdict("rejected", parsed, failed, raw)

    if parsed["niche"] not in NICHES:
        failed.append("label_set")

    corpus = _norm(inp.corpus())
    quotes = [q for q in parsed["evidence"] if isinstance(q, str) and q.strip()]
    if not quotes or any(_norm(q) not in corpus for q in quotes):
        failed.append("evidence")

    if not (0 <= parsed["confidence"] <= 1):
        failed.append("schema")
    elif parsed["confidence"] < threshold:
        failed.append("confidence")

    if rule_says_spam is not None and bool(parsed["is_spam"]) != rule_says_spam:
        failed.append("rule_conflict")

    if "label_set" in failed or "evidence" in failed or "schema" in failed:
        return Verdict("rejected", parsed, failed, raw)
    if failed:  # confidence / rule_conflict -> a human decides
        return Verdict("review", parsed, failed, raw)
    return Verdict("accepted", parsed, [], raw)


def classify_with_guardrails(clf: Classifier, inp: AgentInput, *, threshold: float = 0.7) -> Verdict:
    raw = clf.classify(inp)
    return guard(raw, inp, rule_says_spam=rule_spam(inp.corpus()), threshold=threshold)

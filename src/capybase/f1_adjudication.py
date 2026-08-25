"""F1 tier-2 (sprint-23): LLM subsumption adjudication for symmetric conflicts.

When both sides changed significantly (> 15 lines — tier-1 declined),
ask the model: does one side's version account for the other's intent,
or must both changes weave? The corpus prior is ~95% side-choice
(123:6 side:weave among passing churn>15 cases), and the failure-path
gate means the adjudicator only sees weaves that already failed.

Prompt design reuses the existing whole-side adjudication's structure
(keep/weave/delete) but asks the F1 triage explicitly (SIDE vs WEAVE,
not CURRENT vs REPLAYED between two compiling candidates).
"""
from __future__ import annotations

import json
import re


def f1_tier2_prompt(
    path: str,
    base_text: str,
    current: str,
    replayed: str,
    max_chars: int = 6000,
) -> str:
    """The F1 subsumption adjudication prompt."""
    def clip(t: str) -> str:
        return t[:max_chars] if len(t) > max_chars else t

    return f"""You are deciding the resolution of a git rebase conflict for `{path}`.

The merge has FAILED validation and deterministic repairs could not fix it.
Both sides changed significantly from the base. Answer: should one side's
version be taken WHOLESALE, or must both sides' changes be woven together?

- Choose a SIDE (current or replayed) when one side's version already
  accounts for the other side's intent — it includes the equivalent change,
  refines/replaces it, or the other side's change is cosmetic/obsolete
  relative to it.
- Choose WEAVE when both sides made genuinely independent changes that both
  need to survive.

BASE (common ancestor):
```
{clip(base_text)}
```

CURRENT (upstream, the branch being rebased onto):
```
{clip(current)}
```

REPLAYED (the commit being replayed on top of current):
```
{clip(replayed)}
```

Respond with JSON only: {{"decision": "current" | "replayed" | "weave",
"confidence": 0.0-1.0, "reason": "one sentence"}}
"""


def parse_f1_tier2_response(raw: str) -> tuple[str, float, str] | None:
    """Parse the adjudication response. Returns (choice, conf, reason) or None."""
    m = re.search(r"\{[^{}]*\}", raw or "", re.S)
    if not m:
        return None
    try:
        parsed = json.loads(m.group(0))
    except (json.JSONDecodeError, ValueError):
        return None
    choice = str(parsed.get("decision", "")).strip().lower()
    conf = float(parsed.get("confidence", 0.0) or 0.0)
    reason = str(parsed.get("reason", ""))[:200]
    if choice in ("current", "replayed", "weave"):
        return (choice, conf, reason)
    return None

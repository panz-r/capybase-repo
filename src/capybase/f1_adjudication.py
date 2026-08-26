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
    """The F1 subsumption adjudication prompt (sprint-24 diff-centered).

    Sprint-23's version clipped the full sides to 6000 chars, which hid
    the actual differences for large files (protobuf-0051: the model saw
    "identical" snippets and defaulted to the wrong side). This version
    shows ONLY the changed regions (unified diff hunks with context),
    which is small regardless of file size and carries 100% of the
    semantic signal needed for the subsumption judgment.
    """
    import difflib
    import re

    # Compute the diff between current and replayed
    diff_lines = list(difflib.unified_diff(
        current.splitlines(), replayed.splitlines(),
        fromfile="CURRENT", tofile="REPLAYED", lineterm=""))

    # If the diff is small enough, show it in full
    diff_text = "\n".join(diff_lines)
    if len(diff_text) > max_chars:
        # Show only the first N hunks (each hunk is a changed region)
        hunks = []
        current_hunk = []
        for line in diff_lines:
            if line.startswith("@@"):
                if current_hunk:
                    hunks.append("\n".join(current_hunk))
                current_hunk = [line]
            elif current_hunk:
                current_hunk.append(line)
        if current_hunk:
            hunks.append("\n".join(current_hunk))
        # Take hunks until we hit the char limit
        result = []
        total = 0
        for hunk in hunks:
            if total + len(hunk) > max_chars:
                break
            result.append(hunk)
            total += len(hunk)
        diff_text = "\n".join(result)
        if len(hunks) > len(result):
            diff_text += f"\n... ({len(hunks) - len(result)} more hunks)"

    # Also compute what each side changed vs base (for context)
    cur_changes = _changed_regions(base_text, current, "CURRENT")
    rep_changes = _changed_regions(base_text, replayed, "REPLAYED")

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

WHAT CURRENT CHANGED (vs the common ancestor):
```
{cur_changes}
```

WHAT REPLAYED CHANGED (vs the common ancestor):
```
{rep_changes}
```

DIFF BETWEEN CURRENT AND REPLAYED (the actual conflict):
```
{diff_text}
```

Respond with JSON only: {{"decision": "current" | "replayed" | "weave",
"confidence": 0.0-1.0, "reason": "one sentence"}}
"""


def _changed_regions(base_text: str, side_text: str, label: str,
                     max_chars: int = 2000) -> str:
    """Summarize what a side changed vs the base (compact diff hunks)."""
    import difflib
    diff = list(difflib.unified_diff(
        base_text.splitlines(), side_text.splitlines(),
        fromfile="BASE", tofile=label, lineterm=""))
    # Filter out the header lines (---, +++) and empty diffs
    hunks = []
    current_hunk = []
    for line in diff:
        if line.startswith("@@"):
            if current_hunk:
                hunks.append("\n".join(current_hunk))
            current_hunk = [line]
        elif current_hunk and not line.startswith(("---", "+++")):
            current_hunk.append(line)
    if current_hunk:
        hunks.append("\n".join(current_hunk))

    if not hunks:
        return "(no changes from base)"
    result = "\n".join(hunks)
    if len(result) > max_chars:
        result = result[:max_chars] + "\n... (truncated)"
    return result


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

"""R5 (sprint-23): alternate-presentation retry ladder.

The retry budget currently buys nothing (measured prompt similarity
0.85-0.95 across retries — the only variation is the error feedback
block; the presentation is static). R5 rotates the PRESENTATION along
the calibrated prompt-factor axes on each retry, simultaneously with
the accumulated feedback (CEGIS × presentation).

The ladder reuses the existing PromptProfile machinery — no new
renderers. Each attempt gets a profile variant; the axes rotate:
  attempt 1: side_ordering flipped (CURRENT_FIRST ↔ BASE_FIRST)
  attempt 2: output_layout changed (JSON_V6 ↔ MARKDOWN_CODE)
  attempt 3: instruction_position changed (BOTTOM ↔ TOP_HEAVY)
"""
from __future__ import annotations

import dataclasses

from capybase.prompt_profile import (
    InstructionPosition,
    OutputLayout,
    PromptProfile,
    SideOrdering,
)


def retry_profile_variant(base_profile: PromptProfile, attempt: int) -> PromptProfile:
    """Return a profile variant for the given retry attempt.

    attempt 0 returns the calibrated default (no change).
    attempt 1+ rotates one orthogonal axis per attempt, drawing from
    the same palette the calibration DOE explored (every point is
    known-parseable — the parser handles both levels of each axis)."""
    if attempt <= 0:
        return base_profile

    if attempt == 1:
        # Flip side ordering — anchoring effects are real for small models
        new_so = (SideOrdering.BASE_FIRST
                  if base_profile.side_ordering == SideOrdering.CURRENT_FIRST
                  else SideOrdering.CURRENT_FIRST)
        return dataclasses.replace(base_profile, side_ordering=new_so)
    elif attempt == 2:
        # Change output layout (json ↔ markdown-code)
        new_ol = (OutputLayout.MARKDOWN_CODE
                  if base_profile.output_layout == OutputLayout.JSON_V6
                  else OutputLayout.JSON_V6)
        return dataclasses.replace(base_profile, output_layout=new_ol)
    elif attempt >= 3:
        # Change instruction position (bottom ↔ top-heavy)
        new_ip = (InstructionPosition.TOP_HEAVY
                  if base_profile.instruction_position == InstructionPosition.BOTTOM
                  else InstructionPosition.BOTTOM)
        return dataclasses.replace(base_profile, instruction_position=new_ip)

    return base_profile

"""Instrumentation helper pattern (sprint-23 cycle A).

The prompt-assembly instrumentation bug (32 test failures): the event
was emitted inside an if/elif branch where its target variable wasn't
yet assigned. This module provides a pattern that guarantees
instrumentation code runs AFTER a block completes, with the block's
outputs available — eliminating the placement-bug class.

Usage:
    @instrumented("prompt_composition")
    def build_prompt(...):
        ...branching...
        return prompt

    # The decorator emits the event after build_prompt returns,
    # reading the result and any named capture variables.
"""
from __future__ import annotations

import functools
from typing import Any, Callable


def instrumented(
    event_name: str,
    *,
    extract: Callable[..., dict[str, Any]] | None = None,
) -> Callable:
    """Decorator that emits a journal event after the function returns.

    The `extract` callable receives the function's return value and
    keyword arguments, returning a dict of fields for the event. If
    not provided, a minimal {result_type, result_len} is emitted.

    The journal is looked up via `self.journal` on the first argument
    (the method's host object). If no journal is present, the
    instrumentation is a no-op — never breaks the primary path.
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            result = func(*args, **kwargs)
            try:
                host = args[0] if args else None
                journal = getattr(host, "journal", None)
                if journal is not None:
                    fields = (
                        extract(result, **kwargs)
                        if extract is not None
                        else {
                            "result_type": type(result).__name__,
                            "result_len": (
                                len(result) if hasattr(result, "__len__")
                                else None),
                        }
                    )
                    journal.emit(event_name, fields)
            except Exception:  # noqa: BLE001 — never break the primary path
                pass
            return result
        return wrapper
    return decorator

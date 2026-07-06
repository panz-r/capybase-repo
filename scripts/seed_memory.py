#!/usr/bin/env python3
"""Seed the shared experience store for live eval (embeddings round).

Populates the corpus at $CAPYBASE_MEMORY_DIR/experiences.jsonl with accepted
resolutions matching the live-eval scenarios, so RAG retrieval (repair path,
entity matching) has examples to surface from the first run. Without this, each
scenario starts cold and only accumulates after the orchestrator records its own
outcomes — too late to exercise the retrieval path within a single eval run.

Each seed record is a realistic (base/current/replayed/resolved) triple for one
scenario, marked ``outcome="accepted"`` with a low retry_count (the quality
filter for repair retrieval drops high-retry examples). Run before live_eval:

    .venv/bin/python scripts/seed_memory.py
    CAPYBASE_EMBED=1 .venv/bin/python scripts/live_eval.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from capybase.conflict_model import HistoricalExample
from capybase.memory.store import Experience, ExperienceStore


def _exp(*, summary, path, language, base, current, replayed, resolved,
         retry_count=1, region_kind="function"):
    return Experience(
        example=HistoricalExample(
            summary=summary, base=base, current=current, replayed=replayed,
            resolved=resolved, source="seed",
        ),
        outcome="accepted",
        language=language,
        path=path,
        retry_count=retry_count,
        region_kind=region_kind,
    )


def main() -> int:
    seed_dir = Path(os.environ.get("CAPYBASE_MEMORY_DIR", "/tmp/capybase-live/memory"))
    seed_dir.mkdir(parents=True, exist_ok=True)
    store_path = seed_dir / "experiences.jsonl"

    # A representative accepted resolution per scenario shape. These mirror the
    # live-eval conflicts so retrieval surfaces a relevant few-shot example.
    seeds: list[Experience] = [
        # py_simple: a value-resolution conflict (return 'hi' vs 'howdy').
        _exp(
            summary="seed:app.py:py_simple", path="app.py", language="python",
            base="def greet():\n    return 'hello'\n",
            current="def greet():\n    return 'hi'\n",
            replayed="def greet():\n    return 'howdy'\n",
            resolved="def greet():\n    return 'hi'\n",
        ),
        # py_multi_unit: distinct additions on both sides (scheduler + reloader,
        # cache + metrics both 'on').
        _exp(
            summary="seed:cfg.py:py_multi_unit", path="cfg.py", language="python",
            base=(
                'ENABLED_SERVICES = ["core", "cli"]\n\n\n'
                'class ServiceConfig:\n    name = "capybase"\n\n\n'
                'FEATURE_FLAGS = {\n    "cache": "off",\n    "metrics": "off",\n}\n'
            ),
            current=(
                'ENABLED_SERVICES = ["core", "cli", "scheduler"]\n\n\n'
                'class ServiceConfig:\n    name = "capybase"\n\n\n'
                'FEATURE_FLAGS = {\n    "cache": "off",\n    "metrics": "on",\n}\n'
            ),
            replayed=(
                'ENABLED_SERVICES = ["core", "cli", "reloader"]\n\n\n'
                'class ServiceConfig:\n    name = "capybase"\n\n\n'
                'FEATURE_FLAGS = {\n    "cache": "on",\n    "metrics": "off",\n}\n'
            ),
            resolved=(
                'ENABLED_SERVICES = ["core", "cli", "scheduler", "reloader"]\n\n\n'
                'class ServiceConfig:\n    name = "capybase"\n\n\n'
                'FEATURE_FLAGS = {\n    "cache": "on",\n    "metrics": "on",\n}\n'
            ),
        ),
        # rust_impl: field + constant additions on both sides.
        _exp(
            summary="seed:config.rs:rust_impl", path="src/config.rs", language="rust",
            base=(
                "pub struct Config {\n    pub name: String,\n    pub max_retries: u32,\n}\n\n"
                "impl Config {\n    pub fn new() -> Self {\n        Config {\n"
                '            name: "capybase".to_string(),\n            max_retries: 3,\n        }\n    }\n\n'
                '    pub fn label(&self) -> String {\n'
                '        format!("{} (retries={})", self.name, self.max_retries)\n    }\n}\n'
            ),
            current=(
                "pub struct Config {\n    pub name: String,\n    pub max_retries: u32,\n}\n\n"
                "impl Config {\n    pub fn new() -> Self {\n        Config {\n"
                '            name: "capybase".to_string(),\n            max_retries: 5,\n        }\n    }\n\n'
                '    pub fn label(&self) -> String {\n'
                '        format!("[{}] retries={}", self.name, self.max_retries)\n    }\n}\n'
            ),
            replayed=(
                "pub struct Config {\n    pub name: String,\n    pub max_retries: u32,\n    pub timeout_ms: u32,\n}\n\n"
                "impl Config {\n    pub fn new() -> Self {\n        Config {\n"
                '            name: "capybase".to_string(),\n            max_retries: 3,\n            timeout_ms: 10000,\n        }\n    }\n\n'
                '    pub fn label(&self) -> String {\n'
                '        format!("{} (retries={}, timeout={})", self.name, self.max_retries, self.timeout_ms)\n    }\n}\n'
            ),
            resolved=(
                "pub struct Config {\n    pub name: String,\n    pub max_retries: u32,\n    pub timeout_ms: u32,\n}\n\n"
                "impl Config {\n    pub fn new() -> Self {\n        Config {\n"
                '            name: "capybase".to_string(),\n            max_retries: 5,\n            timeout_ms: 10000,\n        }\n    }\n\n'
                '    pub fn label(&self) -> String {\n'
                '        format!("[{}] retries={}, timeout={}", self.name, self.max_retries, self.timeout_ms)\n    }\n}\n'
            ),
        ),
        # rust_port_test: a port value conflict (test asserts 9090).
        _exp(
            summary="seed:config.rs:rust_port_test", path="src/config.rs", language="rust",
            base=(
                "pub struct Config {\n    pub port: u16,\n}\n"
                "impl Config {\n    pub fn new() -> Self { Config { port: 8080 } }\n}\n"
            ),
            current=(
                "pub struct Config {\n    pub port: u16,\n}\n"
                "impl Config {\n    pub fn new() -> Self { Config { port: 9090 } }\n}\n"
            ),
            replayed=(
                "pub struct Config {\n    pub port: u16,\n}\n"
                "impl Config {\n    pub fn new() -> Self { Config { port: 7070 } }\n}\n"
            ),
            resolved=(
                "pub struct Config {\n    pub port: u16,\n}\n"
                "impl Config {\n    pub fn new() -> Self { Config { port: 9090 } }\n}\n"
            ),
        ),
    ]

    store = ExperienceStore(store_path)
    for exp in seeds:
        store.append(exp)
    print(f"Seeded {len(seeds)} accepted resolutions → {store_path}", flush=True)
    print(f"Corpus now holds {len(store)} experience(s) "
          f"({len(store.accepted())} accepted).", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

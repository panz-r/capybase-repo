"""Similarity-probe corpus for embeddings calibration.

Distinct from :mod:`capybase.calibration_corpus` (which calibrates the LLM
*resolver* against blessed merges). This corpus calibrates the *embedding
retriever's* similarity floor: each entry is a ``(query, related, unrelated)``
triple where a good embedding model should rank the ``related`` text close to
the ``query`` and the ``unrelated`` text far from it.

``calibrate-embeddings`` embeds every text, measures the cosine-similarity
distributions of the related and unrelated pairs, and derives a ``min_similarity``
threshold from the gap between them. The corpus is self-contained and
hand-authored so calibration is deterministic and needs no external data; it can
be swapped for a richer/real-data corpus later without changing the calibrator.

Conventions:
- ``query`` mirrors the retriever's real query format: the conflict signature
  (base + current + replayed concatenated), exactly what
  :meth:`EmbeddingRetriever.retrieve` embeds.
- ``related`` is a genuinely-similar past conflict — a plausible "should match"
  signature (same domain, overlapping identifiers/intent).
- ``unrelated`` is a topically-different conflict (different domain, disjoint
  vocabulary) — a "should NOT match" signature.
- Pairs span conflict shapes (config tweaks, function edits, imports, list/dict
  combines) and languages (python, rust) so the measured distribution is
  representative, not biased to one shape.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SimilarityProbe:
    """One ``(query, related, unrelated)`` triple for embeddings calibration.

    ``label`` is a short tag for the report (what conflict/domain this exercises).
    ``language`` is advisory (the calibrator measures across all pairs; language
    isn't used as a filter, but recorded for corpus-coverage reporting).
    """

    label: str
    language: str
    query: str
    related: str
    unrelated: str


def _py(base: str, current: str, replayed: str) -> str:
    """Build a conflict-signature query (the retriever's real format)."""
    return " ".join([base, current, replayed])


SIMILARITY_PROBES: list[SimilarityProbe] = [
    # 1. Config flag tweak — related is the same flag with a different value;
    #    unrelated is a Rust struct definition.
    SimilarityProbe(
        label="config-flag-tweak",
        language="python",
        query=_py("DEBUG = False", "DEBUG = True", "DEBUG = False"),
        related=_py("VERBOSE = False", "VERBOSE = True", "VERBOSE = False"),
        unrelated=_py(
            "struct Service {\n    name: String,\n}",
            "struct Service {\n    name: String,\n    port: u16,\n}",
            "struct Service {\n    name: String,\n}",
        ),
    ),
    # 2. Function return-value edit — related is a different function's return
    #    tweak; unrelated is a markdown list combine.
    SimilarityProbe(
        label="function-return-edit",
        language="python",
        query=_py(
            "def compute(x):\n    return x * 2",
            "def compute(x):\n    return x * 3",
            "def compute(x):\n    return x * 2",
        ),
        related=_py(
            "def transform(y):\n    return y + 1",
            "def transform(y):\n    return y + 2",
            "def transform(y):\n    return y + 1",
        ),
        unrelated=_py(
            "- apple\n- banana",
            "- apple\n- banana\n- cherry",
            "- apple\n- cherry",
        ),
    ),
    # 3. Import addition — related adds a different import; unrelated is a dict
    #    combine.
    SimilarityProbe(
        label="import-addition",
        language="python",
        query=_py(
            "import os\nimport sys",
            "import os\nimport sys\nimport json",
            "import os\nimport sys",
        ),
        related=_py(
            "import os\nimport sys",
            "import os\nimport sys\nimport logging",
            "import os\nimport sys",
        ),
        unrelated=_py(
            "config = {'a': 1}",
            "config = {'a': 1, 'b': 2}",
            "config = {'a': 1, 'c': 3}",
        ),
    ),
    # 4. List element combine — related is a list with different elements;
    #    unrelated is a Rust impl method.
    SimilarityProbe(
        label="list-combine",
        language="python",
        query=_py(
            "services = ['auth']",
            "services = ['auth', 'login']",
            "services = ['auth', 'profile']",
        ),
        related=_py(
            "endpoints = ['/health']",
            "endpoints = ['/health', '/metrics']",
            "endpoints = ['/health', '/ready']",
        ),
        unrelated=_py(
            "impl Handler {\n    fn handle(&self) {}\n}",
            "impl Handler {\n    fn handle(&self) {}\n    fn validate(&self) {}\n}",
            "impl Handler {\n    fn handle(&self) {}\n}",
        ),
    ),
    # 5. Rust constant bump — related bumps a different constant; unrelated is a
    #    Python guard-clause edit.
    SimilarityProbe(
        label="rust-const-bump",
        language="rust",
        query=_py(
            "const MAX_RETRIES: u32 = 3;",
            "const MAX_RETRIES: u32 = 5;",
            "const MAX_RETRIES: u32 = 3;",
        ),
        related=_py(
            "const TIMEOUT_MS: u32 = 1000;",
            "const TIMEOUT_MS: u32 = 2000;",
            "const TIMEOUT_MS: u32 = 1000;",
        ),
        unrelated=_py(
            "def process(data):\n    if not data:\n        return\n    handle(data)",
            "def process(data):\n    if not data:\n        return None\n    handle(data)",
            "def process(data):\n    if not data:\n        return\n    handle(data)",
        ),
    ),
    # 6. Dict flag combine — related combines different dict flags; unrelated is
    #    a Rust enum variant addition.
    SimilarityProbe(
        label="dict-flag-combine",
        language="python",
        query=_py(
            "flags = {'cache': 'on'}",
            "flags = {'cache': 'on', 'metrics': 'on'}",
            "flags = {'cache': 'on', 'logging': 'on'}",
        ),
        related=_py(
            "settings = {'debug': 'off'}",
            "settings = {'debug': 'off', 'verbose': 'on'}",
            "settings = {'debug': 'off', 'trace': 'on'}",
        ),
        unrelated=_py(
            "enum Color {\n    Red,\n    Green,\n}",
            "enum Color {\n    Red,\n    Green,\n    Blue,\n}",
            "enum Color {\n    Red,\n    Green,\n}",
        ),
    ),
    # 7. Guard-clause edit (same domain as #5's unrelated — tests that
    #    cross-domain unrelateds are consistently far).
    SimilarityProbe(
        label="guard-clause-edit",
        language="python",
        query=_py(
            "def process(data):\n    if not data:\n        return\n    handle(data)",
            "def process(data):\n    if not data:\n        return\n    handle(data)",
            "def process(data):\n    if data:\n        handle(data)",
        ),
        related=_py(
            "def validate(input):\n    if not input:\n        raise ValueError\n    use(input)",
            "def validate(input):\n    if not input:\n        raise ValueError\n    use(input)",
            "def validate(input):\n    if input:\n        use(input)",
        ),
        unrelated=_py(
            "MAX_RETRIES = 3",
            "MAX_RETRIES = 5",
            "MAX_TIMEOUT = 3",
        ),
    ),
    # 8. Rust method addition — related adds a different method; unrelated is a
    #    Python import addition.
    SimilarityProbe(
        label="rust-method-add",
        language="rust",
        query=_py(
            "impl Store {\n    fn get(&self) {}\n}",
            "impl Store {\n    fn get(&self) {}\n    fn set(&self) {}\n}",
            "impl Store {\n    fn get(&self) {}\n}",
        ),
        related=_py(
            "impl Cache {\n    fn read(&self) {}\n}",
            "impl Cache {\n    fn read(&self) {}\n    fn write(&self) {}\n}",
            "impl Cache {\n    fn read(&self) {}\n}",
        ),
        unrelated=_py(
            "import os",
            "import os\nimport sys",
            "import os",
        ),
    ),
]


def probes() -> list[SimilarityProbe]:
    """The similarity-probe corpus (the accessor mirrors ``calibration_corpus``)."""
    return list(SIMILARITY_PROBES)

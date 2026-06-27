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
    # 9. Class method addition — related adds a method to a different class;
    #    unrelated is a Rust const bump.
    SimilarityProbe(
        label="class-method-add",
        language="python",
        query=_py(
            "class User:\n    def name(self):\n        pass",
            "class User:\n    def name(self):\n        pass\n    def email(self):\n        pass",
            "class User:\n    def name(self):\n        pass",
        ),
        related=_py(
            "class Account:\n    def id(self):\n        pass",
            "class Account:\n    def id(self):\n        pass\n    def status(self):\n        pass",
            "class Account:\n    def id(self):\n        pass",
        ),
        unrelated=_py(
            "const LIMIT: u32 = 10;",
            "const LIMIT: u32 = 25;",
            "const LIMIT: u32 = 10;",
        ),
    ),
    # 10. Decorator add/remove — related decorates a different function;
    #     unrelated is a list combine.
    SimilarityProbe(
        label="decorator-add",
        language="python",
        query=_py(
            "def view():\n    return data",
            "@app.route('/')\ndef view():\n    return data",
            "def view():\n    return data",
        ),
        related=_py(
            "def submit():\n    return ok",
            "@app.route('/submit')\ndef submit():\n    return ok",
            "def submit():\n    return ok",
        ),
        unrelated=_py(
            "xs = [1]",
            "xs = [1, 2]",
            "xs = [1, 3]",
        ),
    ),
    # 11. Type annotation edit — related annotates a different function's arg;
    #     unrelated is a dict flag combine.
    SimilarityProbe(
        label="type-annotation",
        language="python",
        query=_py(
            "def parse(raw):\n    return raw",
            "def parse(raw: str):\n    return raw",
            "def parse(raw):\n    return raw",
        ),
        related=_py(
            "def load(path):\n    return path",
            "def load(path: Path):\n    return path",
            "def load(path):\n    return path",
        ),
        unrelated=_py(
            "opts = {'fast': True}",
            "opts = {'fast': True, 'safe': True}",
            "opts = {'fast': True, 'loud': True}",
        ),
    ),
    # 12. Docstring / return-value edit — related edits a different function's
    #     return line; unrelated is a Rust impl method add.
    SimilarityProbe(
        label="return-edit",
        language="python",
        query=_py(
            "def to_str(self):\n    return self.name",
            "def to_str(self):\n    return self.full_name",
            "def to_str(self):\n    return self.name",
        ),
        related=_py(
            "def label(self):\n    return self.title",
            "def label(self):\n    return self.heading",
            "def label(self):\n    return self.title",
        ),
        unrelated=_py(
            "impl Node {\n    fn leaf(&self) {}\n}",
            "impl Node {\n    fn leaf(&self) {}\n    fn branch(&self) {}\n}",
            "impl Node {\n    fn leaf(&self) {}\n}",
        ),
    ),
    # 13. async/await edit — related toggles a different coroutine; unrelated is
    #     an import addition.
    SimilarityProbe(
        label="async-toggle",
        language="python",
        query=_py(
            "def fetch(url):\n    return get(url)",
            "async def fetch(url):\n    return await get(url)",
            "def fetch(url):\n    return get(url)",
        ),
        related=_py(
            "def stream(path):\n    return read(path)",
            "async def stream(path):\n    return await read(path)",
            "def stream(path):\n    return read(path)",
        ),
        unrelated=_py(
            "import os",
            "import os\nimport re",
            "import os",
        ),
    ),
    # 14. Error-handling wrapper add — related wraps a different call in
    #     try/except; unrelated is a config flag tweak.
    SimilarityProbe(
        label="try-except-wrapper",
        language="python",
        query=_py(
            "def save(rec):\n    write(rec)",
            "def save(rec):\n    try:\n        write(rec)\n    except IOError:\n        log()",
            "def save(rec):\n    write(rec)",
        ),
        related=_py(
            "def flush(rec):\n    write(rec)",
            "def flush(rec):\n    try:\n        write(rec)\n    except IOError:\n        log()",
            "def flush(rec):\n    write(rec)",
        ),
        unrelated=_py(
            "DEBUG = False",
            "DEBUG = True",
            "DEBUG = False",
        ),
    ),
    # 15. Format-string edit — related edits a different f-string; unrelated is a
    #     Rust enum variant add.
    SimilarityProbe(
        label="format-string",
        language="python",
        query=_py(
            "msg = f'hi {name}'",
            "msg = f'hi {name}!'",
            "msg = f'hi {name}'",
        ),
        related=_py(
            "line = f'{key}={val}'",
            "line = f'{key}: {val}'",
            "line = f'{key}={val}'",
        ),
        unrelated=_py(
            "enum Shape {\n    Circle,\n}",
            "enum Shape {\n    Circle,\n    Square,\n}",
            "enum Shape {\n    Circle,\n}",
        ),
    ),
    # 16. Rust lifetime annotation edit — related edits a different lifetime;
    #     unrelated is a Python guard-clause edit.
    SimilarityProbe(
        label="rust-lifetime",
        language="rust",
        query=_py(
            "fn first<'a>(s: &'a str) -> &str {\n    s\n}",
            "fn first<'a, 'b>(s: &'a str) -> &'b str {\n    s\n}",
            "fn first<'a>(s: &'a str) -> &str {\n    s\n}",
        ),
        related=_py(
            "fn last<'a>(s: &'a str) -> &str {\n    s\n}",
            "fn last<'a, 'b>(s: &'a str) -> &'b str {\n    s\n}",
            "fn last<'a>(s: &'a str) -> &str {\n    s\n}",
        ),
        unrelated=_py(
            "def go(items):\n    if not items:\n        return\n    take(items)",
            "def go(items):\n    if not items:\n        return\n    take(items)",
            "def go(items):\n    if items:\n        take(items)",
        ),
    ),
    # 17. Rust trait bound / generic edit — related edits a different bound;
    #     unrelated is a dict combine.
    SimilarityProbe(
        label="rust-trait-bound",
        language="rust",
        query=_py(
            "fn sum<T>(xs: Vec<T>) {\n}",
            "fn sum<T: Add>(xs: Vec<T>) {\n}",
            "fn sum<T>(xs: Vec<T>) {\n}",
        ),
        related=_py(
            "fn mul<T>(xs: Vec<T>) {\n}",
            "fn mul<T: Mul>(xs: Vec<T>) {\n}",
            "fn mul<T>(xs: Vec<T>) {\n}",
        ),
        unrelated=_py(
            "m = {'k': 1}",
            "m = {'k': 1, 'j': 2}",
            "m = {'k': 1, 'l': 3}",
        ),
    ),
    # 18. Numeric literal bump (Python) — related bumps a different number;
    #     unrelated is a Rust struct field add.
    SimilarityProbe(
        label="py-numeric-bump",
        language="python",
        query=_py(
            "BATCH = 32",
            "BATCH = 64",
            "BATCH = 32",
        ),
        related=_py(
            "WORKERS = 4",
            "WORKERS = 8",
            "WORKERS = 4",
        ),
        unrelated=_py(
            "struct Point {\n    x: i32,\n}",
            "struct Point {\n    x: i32,\n    y: i32,\n}",
            "struct Point {\n    x: i32,\n}",
        ),
    ),
    # 19. String literal edit — related edits a different string; unrelated is a
    #     function return edit.
    SimilarityProbe(
        label="string-literal",
        language="python",
        query=_py(
            "MODE = 'prod'",
            "MODE = 'production'",
            "MODE = 'prod'",
        ),
        related=_py(
            "ENV = 'dev'",
            "ENV = 'development'",
            "ENV = 'dev'",
        ),
        unrelated=_py(
            "def calc(x):\n    return x * 2",
            "def calc(x):\n    return x * 5",
            "def calc(x):\n    return x * 2",
        ),
    ),
    # 20. Conditional operator edit — related edits a different condition;
    #     unrelated is a list combine.
    SimilarityProbe(
        label="condition-edit",
        language="python",
        query=_py(
            "if count > 0:\n    act()",
            "if count >= 1:\n    act()",
            "if count > 0:\n    act()",
        ),
        related=_py(
            "if size < 10:\n    act()",
            "if size <= 9:\n    act()",
            "if size < 10:\n    act()",
        ),
        unrelated=_py(
            "cols = ['a']",
            "cols = ['a', 'b']",
            "cols = ['a', 'c']",
        ),
    ),
    # 21. Rust match arm add — related adds an arm to a different match;
    #     unrelated is a Python import addition.
    SimilarityProbe(
        label="rust-match-arm",
        language="rust",
        query=_py(
            "match n {\n    0 => {}\n}",
            "match n {\n    0 => {}\n    1 => {}\n}",
            "match n {\n    0 => {}\n}",
        ),
        related=_py(
            "match c {\n    'a' => {}\n}",
            "match c {\n    'a' => {}\n    'b' => {}\n}",
            "match c {\n    'a' => {}\n}",
        ),
        unrelated=_py(
            "import os\nimport re",
            "import os\nimport re\nimport json",
            "import os\nimport re",
        ),
    ),
    # 22. Default argument edit — related edits a different default; unrelated is
    #     a Rust const bump.
    SimilarityProbe(
        label="default-arg",
        language="python",
        query=_py(
            "def run(n=10):\n    pass",
            "def run(n=20):\n    pass",
            "def run(n=10):\n    pass",
        ),
        related=_py(
            "def step(n=1):\n    pass",
            "def step(n=5):\n    pass",
            "def step(n=1):\n    pass",
        ),
        unrelated=_py(
            "const SIZE: u32 = 8;",
            "const SIZE: u32 = 16;",
            "const SIZE: u32 = 8;",
        ),
    ),
    # 23. Comment/docstring add — related adds a docstring to a different
    #     function; unrelated is a dict flag combine.
    SimilarityProbe(
        label="docstring-add",
        language="python",
        query=_py(
            "def init():\n    setup()",
            "def init():\n    \"\"\"Boot the app.\"\"\"\n    setup()",
            "def init():\n    setup()",
        ),
        related=_py(
            "def close():\n    teardown()",
            "def close():\n    \"\"\"Shut down.\"\"\"\n    teardown()",
            "def close():\n    teardown()",
        ),
        unrelated=_py(
            "flags = {'dry': True}",
            "flags = {'dry': True, 'fast': True}",
            "flags = {'dry': True, 'slow': True}",
        ),
    ),
    # 24. Rust field rename — related renames a field in a different struct;
    #     unrelated is a Python list combine.
    SimilarityProbe(
        label="rust-field-rename",
        language="rust",
        query=_py(
            "struct Config {\n    name: String,\n}",
            "struct Config {\n    label: String,\n}",
            "struct Config {\n    name: String,\n}",
        ),
        related=_py(
            "struct Task {\n    work: String,\n}",
            "struct Task {\n    job: String,\n}",
            "struct Task {\n    work: String,\n}",
        ),
        unrelated=_py(
            "qs = ['x']",
            "qs = ['x', 'y']",
            "qs = ['x', 'z']",
        ),
    ),
]


def probes() -> list[SimilarityProbe]:
    """The similarity-probe corpus (the accessor mirrors ``calibration_corpus``)."""
    return list(SIMILARITY_PROBES)

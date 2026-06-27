#!/usr/bin/env python3
#
# fetch_mergeconflict_datasets.py — download open merge-conflict datasets and
# process them into usable capybase test cases.
#
# Method D (real-world conflict harvesting) infrastructure. This is the first
# dependency on external data: it downloads open datasets (Zenodo, etc.), walks
# each dataset's conflict representation, classifies conflicts by language,
# filters to Rust, and emits one JSON test case per Rust conflict under
# extracted-testdata/realworld/. tests/realworld_loader.py reads those JSON
# cases; the parametrized tests/test_realworld_conflicts.py runs them through
# the verifier (skipping cleanly when no data has been downloaded).
#
# Everything this script writes (downloads/, external-datasets/,
# extracted-testdata/) is gitignored — regenerated on demand, never committed.
# The 325MB+ archives are too large for the repo and their licenses require
# attribution rather than redistribution, so a fresh clone has empty data dirs
# until someone runs this.
#
# Design: a DATASETS registry maps a dataset id -> (url, md5, extractor). Adding
# the next dataset is one registry entry + an extractor function. The first
# consumer is zenodo-hdiff (DOI 10.5281/zenodo.3751038, CC-BY 4.0): GitHub merge
# conflicts as per-folder A/O/B/M tuples (base / side-A / side-B / merged).
#
# The A/O/B/M -> capybase mapping:
#   O (base)           -> BASE
#   A                  -> CURRENT_UPSTREAM_SIDE
#   B                  -> REPLAYED_COMMIT_SIDE   (A/B direction is arbitrary for
#                                                a 3-way merge; documented here)
#   M (human merge)    -> the known-correct resolution (expected_resolved oracle)
# Authentic conflict markers are regenerated via `git merge-file` over A/O/B
# (NOT read from M), matching the synthetic catalog's build_markers approach.
#
# Usage:
#   .venv/bin/python scripts/fetch_mergeconflict_datasets.py                 # all
#   .venv/bin/python scripts/fetch_mergeconflict_datasets.py --dataset zenodo-hdiff
#   .venv/bin/python scripts/fetch_mergeconflict_datasets.py --list          # registry
#   DATA_ROOT=/path .venv/bin/python scripts/fetch_mergeconflict_datasets.py # custom root
#
# Idempotent + resumable: skips an already-downloaded (md5-verified) archive and
# an already-extracted tree, so re-running only re-processes when needed.

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths (env-overridable). Defaults assume the script is at <repo>/scripts/.
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = Path(__import__("os").environ.get("DATA_ROOT", REPO_ROOT))
DOWNLOADS = DATA_ROOT / "downloads"
EXTERNAL = DATA_ROOT / "external-datasets"
TESTDATA = DATA_ROOT / "extracted-testdata" / "realworld"


# ---------------------------------------------------------------------------
# Dataset registry. Each entry: id -> Dataset(url, md5, archive_name,
# extract_subdir, extractor_name, license, source_url).
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Dataset:
    id: str
    url: str
    md5: str
    archive_name: str
    extract_subdir: str  # dir under external-datasets/ after extraction
    extractor: str       # name of the extractor function in EXTRACTORS
    license: str
    source_url: str


DATASETS: dict[str, Dataset] = {
    "zenodo-hdiff": Dataset(
        id="zenodo-hdiff",
        url="https://zenodo.org/records/3751038/files/dataset-hdiff.tar.gz",
        md5="da8436fb47726c5d5a93c040183fbb84",
        archive_name="dataset-hdiff.tar.gz",
        extract_subdir="hdiff",
        extractor="hdiff_aobm",
        license="CC-BY-4.0",
        source_url="https://zenodo.org/records/3751038",
    ),
}


# ---------------------------------------------------------------------------
# Download + extract (shared by all datasets).
# ---------------------------------------------------------------------------


def md5_of(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(dataset: Dataset) -> Path:
    """Download the archive to downloads/ (md5-verified), skipping if present."""
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    dest = DOWNLOADS / dataset.archive_name
    if dest.exists() and md5_of(dest) == dataset.md5:
        print(f"  [skip] {dest.name} already downloaded (md5 ok)")
        return dest
    print(f"  [download] {dataset.url}")
    print(f"             -> {dest} ({_human_size(dataset)} ...)")
    # Stream to a temp file then rename, so a partial download isn't mistaken
    # for a complete one on re-run.
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(dataset.url, timeout=120) as resp, tmp.open("wb") as out:
        shutil.copyfileobj(resp, out, length=1 << 20)
    actual_md5 = md5_of(tmp)
    if actual_md5 != dataset.md5:
        tmp.unlink(missing_ok=True)
        raise SystemExit(
            f"md5 mismatch for {dataset.archive_name}: expected {dataset.md5}, "
            f"got {actual_md5}. The archive may have been updated on the server."
        )
    tmp.rename(dest)
    print(f"  [done] {dest.name} ({dest.stat().st_size // (1 << 20)} MB)")
    return dest


def extract(dataset: Dataset) -> Path:
    """Extract the archive to external-datasets/<extract_subdir>/."""
    archive = DOWNLOADS / dataset.archive_name
    dest = EXTERNAL / dataset.extract_subdir
    if dest.exists() and any(dest.iterdir()):
        print(f"  [skip] {dest} already extracted")
        return dest
    EXTERNAL.mkdir(parents=True, exist_ok=True)
    print(f"  [extract] {archive.name} -> {dest}")
    with tempfile.TemporaryDirectory(dir=EXTERNAL) as td:
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(td)  # noqa: S202 (trusted open dataset)
        # The tarball's top-level dir may differ from extract_subdir; rename.
        extracted_top = [p for p in Path(td).iterdir() if p.is_dir()]
        if len(extracted_top) == 1 and not dest.exists():
            extracted_top[0].rename(dest)
        else:
            dest.mkdir(parents=True, exist_ok=True)
            for p in Path(td).iterdir():
                shutil.move(str(p), str(dest / p.name))
    print(f"  [done] extracted to {dest}")
    return dest


def _human_size(dataset: Dataset) -> str:
    # We don't know the size without a HEAD; hint from known datasets.
    return "~325MB" if "hdiff" in dataset.archive_name else "(unknown size)"


# ---------------------------------------------------------------------------
# Language classification (shared). Tries tree-sitter, falls back to heuristics.
# ---------------------------------------------------------------------------

_EXT_LANG = {
    ".rs": "rust", ".py": "python", ".hs": "haskell", ".java": "java",
    ".js": "javascript", ".ts": "typescript", ".go": "go", ".c": "c",
    ".cpp": "cpp", ".h": "c", ".hpp": "cpp", ".cs": "csharp", ".rb": "ruby",
    ".scala": "scala", ".kt": "kotlin", ".swift": "swift", ".m": "objc",
}


def classify_language(text: str, hint_path: str = "") -> str | None:
    """Best-effort language classification of a source snippet.

    Priority: (1) extension of hint_path, (2) content heuristics. Returns a
    language string or None. The dataset's A/O/B/M files carry no extension, so
    content heuristics are the primary signal — but the heuristic keys off
    strong syntactic markers (fn/let-mut/pub for Rust, def/import for Python,
    module/:: for Haskell) rather than weak keyword overlap.
    """
    if hint_path:
        from pathlib import PurePosixPath

        ext = PurePosixPath(hint_path).suffix.lower()
        if ext in _EXT_LANG:
            return _EXT_LANG[ext]
    # Content heuristics (ordered by distinctiveness).
    if any(
        pat in text
        for pat in ("fn main(", "fn ", "pub fn ", "let mut ", "impl ", "use std::", "-> u32", "extern crate")
    ) and "def " not in text[:200]:
        return "rust"
    if "def " in text or "import " in text and "self." in text:
        return "python"
    if "module " in text and (":: " in text or "::\n" in text) or "data " in text and "where" in text:
        return "haskell"
    if "public class " in text or "System.out" in text:
        return "java"
    if "func " in text and "package " in text:
        return "go"
    return None


# ---------------------------------------------------------------------------
# Extractor: zenodo-hdiff (A/O/B/M folder tuples).
# ---------------------------------------------------------------------------


@dataclass
class ConflictTuple:
    """One A/O/B/M conflict from the hdiff dataset."""

    folder: Path
    base: str      # O
    current: str   # A
    replayed: str  # B
    merged: str    # M (human resolution)
    language: str | None  # classified from folder/extension/content


def _match_aobm(folder: Path) -> tuple[Path, Path, Path, Path] | None:
    """Find the A/O/B/M files in ``folder`` (extension-bearing: A.py, M.rs...).

    The hdiff dataset names them ``A.<ext>``, ``O.<ext>``, ``B.<ext>``,
    ``M.<ext>`` (NOT bare A/O/B/M). Returns the four paths or None if any is
    missing.
    """
    def find(prefix: str) -> Path | None:
        hits = [p for p in folder.iterdir() if p.is_file() and p.name.split(".")[0] == prefix]
        return hits[0] if hits else None

    a, o, b, m = find("A"), find("O"), find("B"), find("M")
    if not (a and o and b and m):
        return None
    return (a, o, b, m)


def iter_hdiff_conflicts(root: Path):
    """Yield ConflictTuples from the hdiff tree.

    Layout: ``<root>/conflicts-<lang>/<repo>-<sha>-<sha>/{A,O,B,M}.<ext>`` (plus
    ``parse-error-<lang>/`` variants). We walk every directory that holds all
    four A/O/B/M files. Language is inferred from (1) the file extension, then
    (2) the ``conflicts-<lang>``/``parse-error-<lang>`` ancestor dir, then (3)
    content heuristics. The two top-level category dirs (``conflicts-*`` vs
    ``parse-error-*``) distinguish real conflicts from un-parseable merges; we
    process only ``conflicts-*``.
    """
    # Collect conflict folders: any dir containing A.*/O.*/B.*/M.* under a
    # `conflicts-<lang>` ancestor (skip `parse-error-*`).
    for d in sorted(root.rglob("A.*")):
        if not d.is_file():
            continue
        folder = d.parent
        parts = {p.name for p in folder.parents} | {folder.name}
        # Only the conflicts-* category (real conflicts), not parse-error-*.
        if not any(p.startswith("conflicts-") for p in parts):
            continue
        tup = _match_aobm(folder)
        if tup is None:
            continue
        a, o, b, m = tup
        # Language: FILE EXTENSION first (authoritative — the dataset names files
        # A.<ext>), then the conflicts-<lang> ancestor dir, then content
        # heuristics. Content heuristics alone misclassify (Clojure `(defn ...)`
        # contains the substring `fn `, tripping the Rust rule), so they're the
        # last resort, never the first.
        ext = m.suffix.lower().lstrip(".")
        lang = _EXT_LANG.get(f".{ext}") if ext else None
        if lang is None:
            for anc in [folder, *folder.parents]:
                if anc.name.startswith("conflicts-"):
                    lang = _LANG_FROM_CATEGORY.get(anc.name[len("conflicts-"):])
                    break
        if lang is None:
            lang = classify_language(m.read_text(encoding="utf-8", errors="replace"))
        try:
            yield ConflictTuple(
                folder=folder,
                base=o.read_text(encoding="utf-8", errors="replace"),
                current=a.read_text(encoding="utf-8", errors="replace"),
                replayed=b.read_text(encoding="utf-8", errors="replace"),
                merged=m.read_text(encoding="utf-8", errors="replace"),
                language=lang,
            )
        except OSError:
            continue


# Map the hdiff category-dir language suffix to a canonical language string.
# The dataset uses these six; a future Rust dataset would add "rs" -> "rust".
_LANG_FROM_CATEGORY = {
    "py": "python", "js": "javascript", "java": "java", "clj": "clojure",
    "lua": "lua", "sh": "shell", "rs": "rust", "go": "go", "rb": "ruby",
    "hs": "haskell", "c": "c", "cpp": "cpp",
}


EXTRACTORS = {
    "hdiff_aobm": iter_hdiff_conflicts,
}


# ---------------------------------------------------------------------------
# Authentic marker generation (mirrors tests/rust_conflict_catalog.build_markers).
# ---------------------------------------------------------------------------


def build_markers(base: str, current: str, replayed: str) -> str | None:
    """Run ``git merge-file`` over the three sides; return the marker-marked
    text, or None if they merge cleanly (no conflict).

    A real-world conflict tuple SHOULD conflict (that's why it's in the
    dataset), but a clean merge is possible if the dataset records a tuple where
    A and B touched disjoint regions. We skip those — there's no conflict to
    resolve.
    """
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        subprocess.run(["git", "init", "-q"], cwd=d, check=True, capture_output=True)
        (d / "O").write_text(base)
        (d / "B").write_text(replayed)
        cur = d / "A"
        cur.write_text(current)
        proc = subprocess.run(
            ["git", "merge-file", "-p", "A", "O", "B"],
            cwd=d, capture_output=True, text=True,
        )
        if proc.returncode == 0:
            return None  # clean merge, no conflict
        return proc.stdout


# ---------------------------------------------------------------------------
# Process one dataset -> Rust JSON cases + language histogram.
# ---------------------------------------------------------------------------


def process(dataset: Dataset, *, rust_only: bool = True) -> int:
    """Walk the extracted dataset, emit Rust (or all-language) JSON cases.

    Prints a language histogram so we can see what the dataset contains even
    when it has no Rust (the expected outcome for hdiff). Returns the count of
    JSON cases written.
    """
    root = EXTERNAL / dataset.extract_subdir
    if not root.exists():
        print(f"  [error] {root} not extracted; run --dataset {dataset.id} first")
        return 0
    extractor = EXTRACTORS[dataset.extractor]

    lang_hist: Counter[str] = Counter()
    rust_cases: list[dict] = []
    n = 0
    for ct in extractor(root):
        n += 1
        # The extractor already classified the language (extension → category-dir
        # → content heuristics). Fall back to "unknown" only if it couldn't.
        lang = ct.language or "unknown"
        lang_hist[lang] += 1
        if rust_only and lang != "rust":
            continue
        # Regenerate authentic markers from A/O/B. Skip clean merges.
        marker_original = build_markers(ct.base, ct.current, ct.replayed)
        if marker_original is None:
            continue
        ext = "rs" if lang == "rust" else "txt"
        rust_cases.append({
            "id": f"{dataset.id}-{len(rust_cases) + 1:04d}",
            "dataset": dataset.id,
            "path": f"conflict_{len(rust_cases) + 1:04d}.rs" if lang == "rust" else f"conflict_{len(rust_cases) + 1:04d}.txt",
            "language": lang,
            "base": ct.base,
            "current": ct.current,
            "replayed": ct.replayed,
            "expected_resolved": ct.merged,
            "marker_original": marker_original,
            "license": dataset.license,
            "source_url": dataset.source_url,
        })

    # Histogram report (the key diagnostic: does this dataset have Rust?).
    print(f"  [scan] {dataset.id}: {n} conflict tuples")
    print("  [histogram] language distribution:")
    for lang, count in lang_hist.most_common():
        marker = "  <-- RUST" if lang == "rust" else ""
        print(f"    {lang:12s} {count:6d}{marker}")

    if not rust_cases:
        print(f"  [result] no {'Rust ' if rust_only else ''}conflicts found in {dataset.id}")
        if rust_only and "rust" not in lang_hist:
            print("           (this dataset has no Rust content; the registry is")
            print("            ready for the next dataset — add a DATASETS entry)")
        return 0

    # Write JSON cases.
    TESTDATA.mkdir(parents=True, exist_ok=True)
    # Clear prior cases for this dataset so re-runs don't accumulate stale ones.
    for old in TESTDATA.glob(f"{dataset.id}-*.json"):
        old.unlink()
    for case in rust_cases:
        (TESTDATA / f"{case['id']}.json").write_text(
            json.dumps(case, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    print(f"  [wrote] {len(rust_cases)} Rust case(s) to {TESTDATA}")
    return len(rust_cases)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Download open merge-conflict datasets and process them into "
            "usable capybase test cases (Method D real-world harvesting)."
        )
    )
    ap.add_argument(
        "--dataset", choices=list(DATASETS) + ["all"], default="all",
        help="which dataset to fetch+process (default: all)",
    )
    ap.add_argument("--list", action="store_true", help="list the dataset registry and exit")
    ap.add_argument(
        "--all-languages", action="store_true",
        help="process all languages, not just Rust (for surveying a dataset)",
    )
    args = ap.parse_args(argv)

    if args.list:
        print("Dataset registry:")
        for ds in DATASETS.values():
            print(f"  {ds.id:20s} {ds.license:10s} {ds.source_url}")
        return 0

    selected = list(DATASETS.values()) if args.dataset == "all" else [DATASETS[args.dataset]]
    total_cases = 0
    for ds in selected:
        print(f"==> dataset: {ds.id} ({ds.license})")
        download(ds)
        extract(ds)
        total_cases += process(ds, rust_only=not args.all_languages)
        print()
    print(f"==> done. {total_cases} case(s) written to {TESTDATA}")
    if total_cases == 0:
        print("    (no test data generated; tests/test_realworld_conflicts.py will skip)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

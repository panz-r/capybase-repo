"""Artifact-level packaging guard: build the wheel AND the sdist, verify
their contents.

test_packaging_metadata.py pins the *config* (fast tomllib reads); this
module pins the *artifacts* — the License-Expression metadata and the
bundled LICENSE/NOTICE files that package consumers and license scanners
actually see (the original MIT/Apache contradiction shipped exactly
there, in the built distribution's metadata).

One build per pytest session, fcntl-lock protected so the documented
`pytest tests/ -n 6` gate does not race six setuptools builds over the
shared in-tree build/ directory — the other workers reuse the first
worker's artifacts. Skipped when the `build` module or the backend
requirements are unavailable (the dev extra ships both).

Run:  .venv/bin/python -m pytest tests/test_packaging_build.py -q
"""

from __future__ import annotations

import contextlib
import tarfile
import zipfile
from pathlib import Path

import pytest
import tomllib

_REPO = Path(__file__).resolve().parent.parent


def _headers(meta: str) -> dict[str, list[str]]:
    """Parse the RFC-822 header block of METADATA/PKG-INFO."""
    out: dict[str, list[str]] = {}
    for line in meta.split("\n\n", 1)[0].splitlines():
        if ": " in line:
            key, value = line.split(": ", 1)
            out.setdefault(key, []).append(value)
    return out


def _license_expression(h: dict[str, list[str]]) -> list[str]:
    # PEP 639 backends emit License-Expression; accept the legacy
    # License: field so an old backend fails on the VALUE, not the key.
    return h.get("License-Expression") or h.get("License") or []


@contextlib.contextmanager
def _cross_process_lock(lock_path: Path):
    fh = lock_path.open("a+")
    try:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    except ImportError:  # pragma: no cover — non-POSIX dev box: best effort
        pass
    try:
        yield
    finally:
        try:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except ImportError:  # pragma: no cover
            pass
        finally:
            fh.close()


def _shared_run_dir(tmp_path_factory) -> Path:
    """A directory shared by all workers of this pytest run.

    xdist workers get per-worker basetemps (.../pytest-<n>/popen-gw<i>),
    so basetemp itself is NOT shared — but the run-level parent is. The
    setuptools backend writes egg-info/build into the source tree, so
    concurrent builds race unless the lock (and the pay-once cache) live
    somewhere every worker can see.
    """
    base = tmp_path_factory.getbasetemp()
    return base.parent if base.name.startswith("popen-gw") else base


@pytest.fixture(scope="session")
def built_artifacts(tmp_path_factory) -> dict[str, Path]:
    """Build sdist+wheel once per run; reuse across xdist workers."""
    try:
        from build import ProjectBuilder
    except ModuleNotFoundError:
        pytest.skip("the `build` module is not installed (dev extra provides it)")

    cache = _shared_run_dir(tmp_path_factory) / "packaging-build"
    cache.mkdir(parents=True, exist_ok=True)
    done = cache / ".done"

    with _cross_process_lock(cache / ".lock"):
        if done.exists():
            return {
                key: Path(value)
                for key, value in (
                    line.split(":", 1)
                    for line in done.read_text().splitlines() if line)
            }

        builder = ProjectBuilder(_REPO)
        missing = sorted(
            builder.check_dependencies(distribution="sdist")
            | builder.check_dependencies(distribution="wheel"))
        if missing:
            pytest.skip(f"build backend requirements missing: {missing}")

        artifacts = {
            "sdist": builder.build("sdist", str(cache)),
            "wheel": builder.build("wheel", str(cache)),
        }
        done.write_text(
            "\n".join(f"{k}:{v}" for k, v in artifacts.items()) + "\n")
        return artifacts


def _pyproject_license() -> tuple[str, list[str]]:
    with (_REPO / "pyproject.toml").open("rb") as f:
        proj = tomllib.load(f)["project"]
    return proj["license"], proj["license-files"]


def test_wheel_license_metadata_and_bundled_files(built_artifacts):
    declared, declared_files = _pyproject_license()
    with zipfile.ZipFile(built_artifacts["wheel"]) as z:
        names = z.namelist()
        meta = z.read(
            next(n for n in names if n.endswith(".dist-info/METADATA"))
        ).decode()
        bundled = {
            n.rsplit("/", 1)[-1]: z.read(n)
            for n in names if "/licenses/" in n
        }

    h = _headers(meta)
    expr = _license_expression(h)
    assert expr == [declared], (
        f"wheel metadata license {expr!r} disagrees with the pyproject "
        f"source of truth {declared!r} (LICENSE/README are {declared})")
    assert sorted(h.get("License-File", [])) == sorted(declared_files), (
        f"wheel License-File entries {h.get('License-File')!r} != declared "
        f"{declared_files!r} — a declared license file is not bundled")
    assert not [c for c in h.get("Classifier", [])
                if c.startswith("License ::")], (
        "PEP 639: `License ::` classifiers must not accompany "
        "License-Expression (setuptools >=77 rejects the combination)")

    for name in declared_files:
        assert bundled[name] == (_REPO / name).read_bytes(), (
            f"{name} is bundled in the wheel but its bytes differ from the repo")


def test_sdist_carries_license_files_and_pkg_info(built_artifacts):
    declared, declared_files = _pyproject_license()
    with tarfile.open(built_artifacts["sdist"]) as t:
        names = t.getnames()
        base = names[0].split("/")[0]

        def member(rel: str) -> bytes:
            return t.extractfile(f"{base}/{rel}").read()

        h = _headers(member("PKG-INFO").decode())
        expr = _license_expression(h)
        member_bytes = {name: member(name) for name in declared_files}
        present = {
            rel: f"{base}/{rel}" in names
            for rel in ("pyproject.toml", "setup.py", "README.md",
                        "src/capybase/__init__.py")
        }

    assert expr == [declared], (
        f"sdist PKG-INFO license {expr!r} disagrees with the pyproject "
        f"source of truth {declared!r}")
    for name in declared_files:
        assert member_bytes[name] == (_REPO / name).read_bytes(), (
            f"sdist {name} missing or differs from the repo copy")

    # The sdist must remain a buildable source tree, not a metadata husk.
    for rel, there in present.items():
        assert there, f"sdist is missing {rel}"

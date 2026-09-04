"""Rust dep vendoring helpers (live_eval_realworld) — manifest surgery.

Covers the two defect classes found in the s26-era-dead investigation
(ledger EXTEND-67): the duplicate [patch.crates-io] append (sea-orm-0003's
`error: duplicate key`) and the rewrites' scoping (the `^0.17.1` fragment
must match only the trees it was validated on).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "_live_eval_realworld",
    Path(__file__).resolve().parent.parent / "scripts" / "live_eval_realworld.py")
_ler = importlib.util.module_from_spec(_SPEC)
sys.modules.setdefault("_live_eval_realworld", _ler)
_SPEC.loader.exec_module(_ler)


class TestMergePatchEntries:

    def test_appends_section_when_absent(self):
        out = _ler._merge_patch_entries(
            "[dependencies]\nfoo = \"1\"\n",
            ['sea-query = { git = "x", tag = "0.18.2" }'])
        assert out.count("[patch.crates-io]") == 1
        assert 'sea-query = { git = "x", tag = "0.18.2" }' in out
        assert "[dependencies]" in out

    def test_merges_into_existing_section(self):
        # sea-orm-0003's shape: the tree's own patch table + our entries
        # must become ONE table or cargo dies with `duplicate key`.
        manifest = (
            '[dependencies]\nsea-query = { version = "^0.27" }\n\n'
            '[patch.crates-io]\n'
            'sea-query = { git = "https://github.com/SeaQL/sea-query", '
            'rev = "890e22c" }\n'
            'sea-query-binder = { git = "https://github.com/SeaQL/sea-query", '
            'rev = "890e22c" }\n\n'
            '[features]\nfoo = []\n')
        out = _ler._merge_patch_entries(
            manifest, ['sea-query-derive = { git = "y", tag = "0.18.2" }'])
        assert out.count("[patch.crates-io]") == 1
        assert 'sea-query-derive = { git = "y", tag = "0.18.2" }' in out
        # entries land inside the section, not after [features]
        assert out.index("sea-query-derive") < out.index("[features]")
        import tomllib
        tomllib.loads(out)  # parses: no duplicate table

    def test_existing_section_at_eof(self):
        manifest = ('[dependencies]\nfoo = "1"\n\n[patch.crates-io]\nbar = "2"')
        out = _ler._merge_patch_entries(manifest, ['baz = "3"'])
        assert out.count("[patch.crates-io]") == 1
        assert 'baz = "3"' in out

    def test_entries_for_already_pinned_packages_are_skipped(self):
        # 0003's own table pins sea-query (rev) and sea-query-binder;
        # merging the sea-orm patch block must add ONLY sea-query-derive
        # — a second `sea-query = ...` key is the duplicate-key error one
        # level below the section header (found in the A7 live rerun).
        manifest = (
            '[patch.crates-io]\n'
            'sea-query = { git = "https://github.com/SeaQL/sea-query", '
            'rev = "890e22c" }\n'
            'sea-query-binder = { git = "https://github.com/SeaQL/sea-query", '
            'rev = "890e22c" }\n')
        entries = [
            'sea-query = { git = "https://github.com/SeaQL/sea-query.git", tag = "0.18.2" }',
            'sea-query-derive = { git = "https://github.com/SeaQL/sea-query.git", tag = "0.18.2" }',
        ]
        out = _ler._merge_patch_entries(manifest, entries)
        import tomllib
        parsed = tomllib.loads(out)
        patch = parsed["patch"]["crates-io"]
        assert set(patch) == {"sea-query", "sea-query-binder", "sea-query-derive"}
        # the tree's own rev pin survives, not our tag
        assert "rev" in patch["sea-query"] and "tag" not in patch["sea-query"]


class TestSeaOrmRewrites:

    def test_0171_fragment_scoped_to_the_five_targets(self):
        # The survey (EXTEND-67 offline validation): exactly 0015-0019
        # carry `^0.17.1`; the rewrite must not touch the neighbors
        # (`^0.17.0` on 0020, `^0.16.x`, `^0.18.0`, `^0.21`).
        rw = dict(_ler.RUST_DEP_REWRITES["sea-orm-history"])
        for neighbor in (
            'sea-query = { version = "^0.17.0", features = ["thread-safe"] }',
            'sea-query = { version = "^0.16.5", features = ["thread-safe"] }',
            'sea-query = { version = "^0.18.0", features = ["thread-safe"] }',
            'sea-query = { version = "^0.21.0", features = ["thread-safe"] }',
        ):
            out = neighbor
            for old, new in rw.items():
                out = out.replace(old, new)
            assert out == neighbor, f"rewrite touched a neighbor: {neighbor}"

    def test_0171_fragment_rewrites_the_targets(self):
        rw = dict(_ler.RUST_DEP_REWRITES["sea-orm-history"])
        line = 'sea-query = { version = "^0.17.1", features = ["thread-safe"] }'
        out = line
        for old, new in rw.items():
            out = out.replace(old, new)
        assert out == ('sea-query = { version = "0.18.2", '
                       'features = ["thread-safe"] }')

    def test_dead_branch_retag_matches_both_users(self):
        # 0003's patch table and 0002's dep line both carry the dead
        # branch; the retag is textual and covers both.
        rw = dict(_ler.RUST_DEP_REWRITES["sea-orm-history"])
        for carrier in (
            'sea-query = { git = "https://github.com/SeaQL/sea-query", '
            'branch = "sqlite-bind-decimals" }',
            'sea-query = { version = "^0.27", '
            'git = "https://github.com/SeaQL/sea-query", '
            'branch = "sqlite-bind-decimals" }',
        ):
            out = carrier
            for old, new in rw.items():
                out = out.replace(old, new)
            assert 'rev = "890e22c39b86a5f1ee65fb1e454270b813da505e"' in out
            assert 'branch = "sqlite-bind-decimals"' not in out

    def test_path_dep_fragment_is_0029_shaped(self):
        rw = dict(_ler.RUST_DEP_REWRITES["sea-orm-history"])
        line = 'sea-query = { path = "../sea-query", version = "^0.11" }'
        out = line
        for old, new in rw.items():
            out = out.replace(old, new)
        assert out == 'sea-query = { version = "0.12.0" }'

    def test_patch_table_carries_both_sea_query_entries(self):
        blk = "".join(_ler.RUST_DEP_PATCHES["sea-orm-history"])
        assert "sea-query = { git =" in blk
        assert "sea-query-derive = { git =" in blk
        import tomllib
        tomllib.loads(blk)  # the block alone is a valid table


class TestVendorRustDepsRestore:

    def test_exception_path_restores_manifest(self, tmp_path, monkeypatch):
        # The timeout/exception path used to return WITHOUT restoring —
        # the poisoned manifest then failed every era probe identically
        # (the duplicate-key class). Restore must run on exceptions too.
        repo = tmp_path / "r"
        repo.mkdir()
        original = b'[dependencies]\nsea-query = { version = "^0.17.1" }\n'
        (repo / "Cargo.toml").write_bytes(original)

        def exploding_vendor(*a, **kw):
            raise TimeoutError("vendor timeout")

        monkeypatch.setattr(
            "subprocess.run", exploding_vendor, raising=False)
        # _vendor_rust_deps imports subprocess lazily inside — patch the
        # module attribute it will find.
        import subprocess
        monkeypatch.setattr(subprocess, "run", exploding_vendor)
        assert _ler._vendor_rust_deps(repo, "sea-orm-history") is False
        assert (repo / "Cargo.toml").read_bytes() == original

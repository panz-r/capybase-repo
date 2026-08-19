"""Build subprocess hygiene — orphaned-tree reaping + ccache wiring.

Two production defects found while investigating a load-92 box after the
sprint-19 D7 live legs (2026-08-19):

1. ``subprocess.run(shell=True, timeout=...)`` kills only the direct
   child. Every timed-out full build leaked its whole make/libtool/ccache
   tree into a worktree the harness then deleted, where it compiled
   against nothing for hours (six+ leaked generations between 02:38 and
   17:11). ``_run_shell_tree`` runs the child in its own session and
   SIGKILLs the process group on timeout.

2. ``_ccache_env`` set CC/CXX=``ccache gcc``/``ccache g++`` AND put a
   PATH shim in front whose script exec'ed ``ccache gcc`` by bare name.
   ccache resolves the compiler through PATH, found its own shim, marked
   the call uncacheable ("Result: disabled" — 995/995 calls), then
   "fell back" to executing the shim — re-entering ccache in an infinite
   loop. A one-line TU failed to compile within 90s. The shims now exec
   ``ccache <absolute-compiler>`` and CC/CXX are left alone.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time

import pytest

from capybase.verification import _ccache_env, _run_shell_tree


def _proc_cmdline(pid: int) -> str:
    with open(f"/proc/{pid}/cmdline", "rb") as f:
        return f.read().decode("utf-8", errors="replace").replace("\0", " ")


class TestRunShellTree:
    def test_returns_completed_process_contract(self, tmp_path):
        proc = _run_shell_tree("echo hi; exit 3", cwd=str(tmp_path), timeout=30)
        assert proc.returncode == 3
        assert proc.stdout.strip() == "hi"

    def test_timeout_kills_entire_descendant_tree(self, tmp_path):
        # Two sleeps (one backgrounded): a bare kill of the shell leaves
        # both alive — only a process-group kill reaps them.
        marker = "sleep 3999"  # unique on the box for the scan below
        with pytest.raises(subprocess.TimeoutExpired):
            _run_shell_tree(f"{marker} & {marker}", cwd=str(tmp_path), timeout=1.0)
        time.sleep(0.5)  # let SIGKILLs land
        leaked = []
        for pid_dir in os.listdir("/proc"):
            if not pid_dir.isdigit():
                continue
            try:
                if marker in _proc_cmdline(int(pid_dir)):
                    leaked.append(pid_dir)
            except OSError:
                continue
        assert not leaked, f"descendants survived the timeout: {leaked}"


class TestCcacheEnv:
    def test_shim_references_absolute_compiler(self, tmp_path, monkeypatch):
        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        for name in ("gcc", "g++"):
            compiler = fake_bin / name
            compiler.write_text("#!/bin/sh\nexit 0\n")
            compiler.chmod(0o755)
        monkeypatch.setattr("capybase.verification._ccache_available", True)
        monkeypatch.setenv("PATH", str(fake_bin))

        # A stale self-referential shim must be rewritten, not reused.
        shim_dir = tmp_path / "shim"
        shim_dir.mkdir()
        (shim_dir / "gcc").write_text('#!/bin/sh\nexec ccache gcc "$@"\n')
        (shim_dir / "g++").write_text('#!/bin/sh\nexec ccache g++ "$@"\n')

        env = _ccache_env(shim_dir=shim_dir)

        for name in ("gcc", "g++"):
            content = (shim_dir / name).read_text()
            assert f'exec ccache "{fake_bin / name}"' in content, content
            # The old self-referential form is exactly the recursion bug.
            assert f"exec ccache {name} " not in content, content
        assert env["PATH"].startswith(f"{shim_dir}:")
        # The double wrap re-triggers the loop; the shim alone must carry it.
        assert env.get("CC") != "ccache gcc"
        assert env.get("CXX") != "ccache g++"

    def test_no_ccache_leaves_env_untouched(self, tmp_path, monkeypatch):
        monkeypatch.setattr("capybase.verification._ccache_available", False)
        shim_dir = tmp_path / "shim"
        env = _ccache_env(shim_dir=shim_dir)
        assert not shim_dir.exists()
        assert env == os.environ.copy()

    def test_cross_worktree_keys_present(self, tmp_path, monkeypatch):
        # The eval re-materializes a fresh /var/tmp/capy-rw-* worktree per
        # case and per majority repeat; without NOHASHDIR the compilation
        # directory poisons the hash and every worktree misses (verified
        # live). Temp must also leave the 6G /run/user tmpfs.
        monkeypatch.setattr("capybase.verification._ccache_available", True)
        env = _ccache_env(shim_dir=tmp_path / "shim")
        assert env.get("CCACHE_NOHASHDIR") == "1"
        assert env.get("CCACHE_BASEDIR") == "/var/tmp"
        assert str(env.get("CCACHE_TEMPDIR", "")).startswith("/var/tmp")

    @pytest.mark.skipif(
        shutil.which("ccache") is None or shutil.which("g++") is None,
        reason="needs ccache and g++ on PATH",
    )
    def test_cross_worktree_cache_hit(self, tmp_path, monkeypatch):
        """Same content, two worktree dirs, -g build: the second must HIT.

        This is the property the eval's economics depend on (fresh
        worktree per case/repeat). Fails without CCACHE_NOHASHDIR.
        """
        monkeypatch.setenv("CCACHE_DIR", str(tmp_path / "ccache"))
        env = _ccache_env(shim_dir=tmp_path / "shim")
        env["CCACHE_BASEDIR"] = str(tmp_path)  # cover this test's trees
        for wt in ("wt-a", "wt-b"):
            src = tmp_path / wt / "src"
            src.mkdir(parents=True)
            (src / "t.cc").write_text("int f() { return 41; }\n")
        first = subprocess.run(
            ["g++", "-g", "-O2", "-c", "t.cc", "-o", "t.o"],
            cwd=tmp_path / "wt-a" / "src", env=env,
            capture_output=True, text=True, timeout=120,
        )
        assert first.returncode == 0, first.stderr
        env["CCACHE_LOGFILE"] = str(tmp_path / "second.log")
        env["CCACHE_DEBUG"] = "1"
        second = subprocess.run(
            ["g++", "-g", "-O2", "-c", "t.cc", "-o", "t.o"],
            cwd=tmp_path / "wt-b" / "src", env=env,
            capture_output=True, text=True, timeout=120,
        )
        assert second.returncode == 0, second.stderr
        log = (tmp_path / "second.log").read_text()
        assert "Succeeded getting cached result" in log, log[-2000:]

    @pytest.mark.skipif(
        shutil.which("ccache") is None or shutil.which("g++") is None,
        reason="needs ccache and g++ on PATH",
    )
    def test_compile_completes_and_hits_cache(self, tmp_path, monkeypatch):
        """The live-fire regression: pre-fix, this compile livelocks.

        A 1-line TU through the shim PATH failed to finish in 90s (ccache
        re-entering itself forever). Post-fix it completes, and a second
        identical compile is served from the cache.
        """
        monkeypatch.setenv("CCACHE_DIR", str(tmp_path / "ccache"))
        (tmp_path / "t.cc").write_text("int main() { return 0; }\n")
        env = _ccache_env(shim_dir=tmp_path / "shim")

        # Compile exactly as a Makefile would: bare g++ resolved via PATH.
        first = subprocess.run(
            ["g++", "-c", "t.cc", "-o", "t.o"], cwd=tmp_path, env=env,
            capture_output=True, text=True, timeout=120,
        )
        assert first.returncode == 0, first.stderr
        assert (tmp_path / "t.o").exists()

        (tmp_path / "t.o").unlink()
        # File logging needs debug level to record cache events.
        env["CCACHE_LOGFILE"] = str(tmp_path / "second.log")
        env["CCACHE_DEBUG"] = "1"
        second = subprocess.run(
            ["g++", "-c", "t.cc", "-o", "t.o"], cwd=tmp_path, env=env,
            capture_output=True, text=True, timeout=120,
        )
        assert second.returncode == 0, second.stderr
        log = (tmp_path / "second.log").read_text()
        assert "Succeeded getting cached result" in log, log[-2000:]
        # The recursion signature must never reappear.
        assert "Result: disabled" not in log, log[-2000:]

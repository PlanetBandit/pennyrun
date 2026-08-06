"""Regression guard for the direct-invocation ImportError.

`python3 tools/sweep.py ...` puts tools/ on sys.path, not the repo root, so
`from tools import hdclient` can't resolve without a bootstrap -- Python
never adds the parent of the script's own directory on its own. Every
shipped caller (the GitHub workflow, both systemd units, the README, muscle
memory) runs the script this way, not as `python3 -m tools.sweep`, so this
has to work.

An unknown stage name is used for sweep.py instead of a real one (like
`probe`) so the test stays hermetic and fast: the import bootstrap happens
unconditionally at module load, before argv is even inspected, so any
invocation exercises the identical import path -- and `unknown stage` fails
fast without touching the network, where `probe` would try to reach Home
Depot. The assertion looks specifically for "No module named 'tools'"
rather than any ModuleNotFoundError, so it isn't confused by an unrelated
missing dependency (e.g. curl_cffi not installed) in some other
environment -- that's a different failure this test is not about.
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TOOLS_IMPORT_ERROR = "No module named 'tools'"


def _run(*args, timeout=15):
    return subprocess.run([sys.executable] + list(args), cwd=ROOT,
                          capture_output=True, text=True, timeout=timeout)


def test_sweep_direct_invocation_resolves_its_imports():
    r = _run("tools/sweep.py", "not-a-real-stage")
    assert _TOOLS_IMPORT_ERROR not in r.stderr, r.stderr
    assert "unknown stage" in r.stderr
    assert r.returncode == 1


def test_sweep_module_invocation_still_works():
    """The -m form must keep working too -- the bootstrap must not break it."""
    r = _run("-m", "tools.sweep", "not-a-real-stage")
    assert _TOOLS_IMPORT_ERROR not in r.stderr, r.stderr
    assert "unknown stage" in r.stderr
    assert r.returncode == 1


def test_checkhost_direct_invocation_resolves_its_imports():
    """checkhost.py has no argument that skips its network probe, so this
    only proves the import bootstrap works, not that Home Depot answered --
    a live probe result (any exit code) or a timeout while attempting one
    both prove the import resolved; only a ModuleNotFoundError disproves it."""
    try:
        r = _run("tools/checkhost.py", timeout=45)
    except subprocess.TimeoutExpired:
        return  # reached the network attempt -- imports were never the problem
    assert _TOOLS_IMPORT_ERROR not in r.stderr, r.stderr

"""The bench-setup entry point (bench-suite#1)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from bench_setup import __version__
from bench_setup.cli import main

SRC = Path(__file__).resolve().parents[1] / "src"


def test_version(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"bench-setup {__version__}"


def test_help_names_the_services(capsys) -> None:
    with pytest.raises(SystemExit):
        main(["--help"])
    out = capsys.readouterr().out
    for name in ("benchhud-intake", "bench-parts", "bench-recon", "benchhud"):
        assert name in out


def test_no_arguments_points_at_the_services_command(capsys) -> None:
    assert main([]) == 0
    assert "sudo bench-setup services" in capsys.readouterr().out


def test_runs_as_a_module_on_the_standard_library_alone() -> None:
    """It has to run on a fresh host, before anything is pip-installed."""
    # -I -S: no env vars, no site-packages, no sitecustomize. Only the stdlib
    # and our own source are importable, the way a bare host's python3 sees it.
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]); sys.argv = ['bench-setup', '--version']\n"
        "import bench_setup.services, bench_setup.host\n"
        "try:\n    import bench_setup.__main__\nexcept SystemExit as e:\n    assert e.code == 0\n"
        "third = {m.split('.')[0] for m in sys.modules} - set(sys.stdlib_module_names)\n"
        "third -= {'bench_setup', '__main__'}\n"
        "assert not {m for m in third if not m.startswith('_')}, third\n"
    )
    subprocess.run([sys.executable, "-I", "-S", "-c", code, str(SRC)], check=True)

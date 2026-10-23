"""Tests that the scripts run from any cwd (sys.path fix)."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ["probe_readonly", "probe_orders", "probe_llm", "report", "reset_paper"]


def _run(args, cwd):
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    return subprocess.run([sys.executable, *args], cwd=str(cwd), capture_output=True,
                          text=True, env=env, timeout=60)


@pytest.mark.parametrize("name", SCRIPTS)
def test_script_help_from_other_cwd(tmp_path, name):
    script = PROJECT_ROOT / "scripts" / f"{name}.py"
    result = _run([str(script), "--help"], tmp_path)
    assert result.returncode == 0, result.stderr
    assert "ModuleNotFoundError" not in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("name", SCRIPTS)
def test_script_help_via_module_flag(name):
    result = _run(["-m", f"scripts.{name}", "--help"], PROJECT_ROOT)
    assert result.returncode == 0, result.stderr
    assert "ModuleNotFoundError" not in result.stderr
    assert "Traceback" not in result.stderr

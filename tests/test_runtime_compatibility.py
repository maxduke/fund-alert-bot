from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_installed_dependencies_and_polling_lifecycle() -> None:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, str(root / "tests" / "runtime_smoke.py")],
        cwd=root,
        env={
            **os.environ,
            "PYTHONPATH": str(root / "src"),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert '"events": ["started", "job", "stopped"]' in result.stdout

"""Run a project regression script and expose failures in GitHub annotations."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FAILURE_LINE = re.compile(r"^\s*\[(?:FAIL|ERR\s*)\]\s*.*$", re.MULTILINE)


def _escape_workflow_command(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python .github/scripts/run_regression.py tests/test_<suite>.py", file=sys.stderr)
        return 2

    test_path = (ROOT / sys.argv[1]).resolve()
    if not test_path.is_relative_to(ROOT) or not test_path.is_file():
        print(f"Regression script not found inside repository: {sys.argv[1]}", file=sys.stderr)
        return 2

    child_env = os.environ.copy()
    child_env["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run(
        [sys.executable, str(test_path)],
        cwd=ROOT,
        env=child_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)

    if result.returncode and os.environ.get("GITHUB_ACTIONS") == "true":
        combined_output = result.stdout + "\n" + result.stderr
        details = FAILURE_LINE.findall(combined_output)
        if not details:
            details = combined_output.splitlines()[-20:]
        if not details:
            details = [f"{test_path.name} exited with code {result.returncode}"]
        for detail in details[:20]:
            message = _escape_workflow_command(detail[:2000])
            print(f"::error title=Failure in {test_path.stem}::{message}")

    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())

"""
E2E tests for bsagent.

Each test:
  1. Copies the fixture_repo to a fresh tmp_path (clean state every run)
  2. Runs the agent one-shot against that directory
  3. Asserts on filesystem state — not on exact LLM text output

Model used: AGENT_MODEL env var (defaults to codex-mini-latest for speed/cost).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# ─── Paths ────────────────────────────────────────────────────────────────────

BSAGENT_ROOT = Path(__file__).parent.parent
FIXTURE_REPO = Path(__file__).parent / "fixture_repo"
AGENT_PY = BSAGENT_ROOT / "agent.py"
PYTHON = sys.executable  # use whatever python is running pytest

# ─── Helpers ──────────────────────────────────────────────────────────────────


def fresh_repo(tmp_path: Path) -> Path:
    """Copy fixture_repo to tmp_path/repo and return the path."""
    dest = tmp_path / "repo"
    shutil.copytree(FIXTURE_REPO, dest)
    return dest


def run_agent(prompt: str, cwd: Path, timeout: int = 90) -> subprocess.CompletedProcess:
    """Run bsagent one-shot against `cwd` and return the completed process."""
    env = {
        **os.environ,
        "AGENT_WORKDIR": str(cwd),
        "AGENT_MODEL": os.environ.get("AGENT_MODEL", "codex-mini-latest"),
    }
    return subprocess.run(
        [PYTHON, str(AGENT_PY), prompt],
        cwd=str(BSAGENT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


# ─── Tests ────────────────────────────────────────────────────────────────────


def test_creates_new_file(tmp_path: Path) -> None:
    """Agent should be able to create a new file using apply_patch or shell_command."""
    repo = fresh_repo(tmp_path)

    result = run_agent(
        'Create a file called hello.txt containing exactly the text "hello world"',
        cwd=repo,
    )

    assert result.returncode == 0, f"agent exited {result.returncode}:\n{result.stderr}"
    target = repo / "hello.txt"
    assert target.exists(), f"hello.txt was not created\nstderr:\n{result.stderr}"
    assert "hello world" in target.read_text(), "hello.txt content mismatch"


def test_reads_and_reports_file_contents(tmp_path: Path) -> None:
    """Agent should be able to read a file and surface its contents in the response."""
    repo = fresh_repo(tmp_path)

    result = run_agent(
        "What functions are defined in string_utils.py? List them by name only.",
        cwd=repo,
    )

    assert result.returncode == 0, f"agent exited {result.returncode}:\n{result.stderr}"
    combined = result.stdout + result.stderr
    # All five function names should appear somewhere in agent output
    for fn in ["reverse_string", "count_vowels", "is_palindrome", "truncate", "title_case"]:
        assert fn in combined, f"function '{fn}' not mentioned in output:\n{combined[:500]}"


def test_fixes_factorial_bug(tmp_path: Path) -> None:
    """Agent should fix the off-by-one bug in factorial() so tests pass."""
    repo = fresh_repo(tmp_path)

    result = run_agent(
        (
            "The factorial() function in math_utils.py has an off-by-one bug — "
            "the range should be range(1, n + 1) not range(1, n). "
            "Fix it using apply_patch."
        ),
        cwd=repo,
    )

    assert result.returncode == 0, f"agent exited {result.returncode}:\n{result.stderr}"
    source = (repo / "math_utils.py").read_text()
    assert "range(1, n + 1)" in source, (
        f"Bug not fixed — range(1, n + 1) not found in math_utils.py:\n{source}"
    )


def test_runs_failing_tests_and_reports(tmp_path: Path) -> None:
    """Agent should run pytest, observe failures, and report them."""
    repo = fresh_repo(tmp_path)

    result = run_agent(
        "Run the tests with pytest and tell me which tests are failing and why.",
        cwd=repo,
    )

    assert result.returncode == 0, f"agent exited {result.returncode}:\n{result.stderr}"
    combined = result.stdout + result.stderr
    # The agent should have run pytest (shell_command) and surfaced failure info
    assert any(
        kw in combined for kw in ["FAILED", "failed", "factorial", "zero"]
    ), f"No test failure info in output:\n{combined[:800]}"


def test_adds_docstrings(tmp_path: Path) -> None:
    """Agent should add docstrings to functions in string_utils.py."""
    repo = fresh_repo(tmp_path)

    result = run_agent(
        "Add a one-line docstring to every function in string_utils.py.",
        cwd=repo,
    )

    assert result.returncode == 0, f"agent exited {result.returncode}:\n{result.stderr}"
    source = (repo / "string_utils.py").read_text()
    # At least 3 of 5 functions should now have triple-quoted docstrings
    docstring_count = source.count('"""')
    assert docstring_count >= 6, (  # 3 functions × opening+closing quotes
        f"Expected at least 3 docstrings (6 triple-quote markers), "
        f"found {docstring_count // 2}:\n{source}"
    )


def test_shell_command_list_files(tmp_path: Path) -> None:
    """Agent should be able to list files in the repo via shell_command."""
    repo = fresh_repo(tmp_path)

    result = run_agent("List all Python files in this directory.", cwd=repo)

    assert result.returncode == 0, f"agent exited {result.returncode}:\n{result.stderr}"
    combined = result.stdout + result.stderr
    for fname in ["math_utils.py", "string_utils.py", "test_math.py"]:
        assert fname in combined, f"{fname} not listed in output:\n{combined[:500]}"

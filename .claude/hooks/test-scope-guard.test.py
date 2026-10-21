#!/usr/bin/env python3
"""Tests for test-scope-guard.py.

Runs the hook as a subprocess with crafted PreToolUse payloads and asserts
the deny/allow decision. Run: `python3 .claude/hooks/test-scope-guard.test.py`.
"""

import json
import subprocess
import sys
from pathlib import Path

HOOK = str(Path(__file__).with_name("test-scope-guard.py"))


def decision(command):
    """Return 'deny' if the hook blocks the command, else 'allow'."""
    out = subprocess.run(
        [sys.executable, HOOK],
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command}}),
        capture_output=True, text=True,
    ).stdout.strip()
    if not out:
        return "allow"
    return json.loads(out)["hookSpecificOutput"]["permissionDecision"]


CASES = [
    # Whole backend suite, every spelling.
    ("pytest", "deny"),
    ("cd backend && pytest -q", "deny"),
    ("pytest tests/", "deny"),
    ("pytest tests -x", "deny"),
    ("python -m pytest -q", "deny"),
    ("/usr/bin/python3 -m pytest tests", "deny"),
    ("PYTHONPATH=/x /usr/bin/python3 -m pytest -q -p no:cacheprovider", "deny"),
    ("backend/.venv/bin/pytest --splits 8 --group 1", "deny"),
    ("pytest -q 2>&1 | tail -20", "deny"),
    # Scoped backend runs.
    ("pytest tests/test_auth.py", "allow"),
    ("pytest tests/test_auth.py::test_login -q", "allow"),
    ("cd backend && python -m pytest -q tests/test_a.py tests/test_b.py", "allow"),
    ("PYTHONPATH=/x /usr/bin/python3 -m pytest -q -p no:cacheprovider tests/test_x.py", "allow"),
    ("pytest -k password", "allow"),
    ("pytest --collect-only -q", "allow"),
    ("pytest --co", "allow"),
    ("pytest --version", "allow"),
    ("pytest --timeout 60 tests/test_x.py", "allow"),
    # Whole Playwright suite.
    ("pnpm exec playwright test", "deny"),
    ("cd frontend && npx playwright test --config=tests-e2e/playwright.config.ts", "deny"),
    ("npx playwright test --config tests-e2e/playwright.config.ts --workers 4", "deny"),
    ("pnpm -C frontend test:e2e", "deny"),
    # Scoped Playwright runs.
    ("npx playwright test --config=tests-e2e/playwright.config.ts invoices/review.spec.ts", "allow"),
    ("pnpm exec playwright test erp/", "allow"),
    ("npx playwright test --list", "allow"),
    ("npx playwright test --grep @smoke", "allow"),
    ("pnpm -C frontend test:e2e tests-e2e/vendors/verify.spec.ts", "allow"),
    # Root pnpm whole-suite scripts.
    ("pnpm test", "deny"),
    ("pnpm run test", "deny"),
    ("pnpm test:backend", "deny"),
    ("pnpm test:frontend", "deny"),
    ("pnpm test:scim", "allow"),
    ("pnpm test:icons", "allow"),
    # Override.
    ("FEOH_FULL_SUITE=1 pytest", "allow"),
    ("cd backend && FEOH_FULL_SUITE=1 python -m pytest -q", "allow"),
    ("env FEOH_FULL_SUITE=1 pnpm test:backend", "allow"),
    # Mentions that are not runs.
    ('git commit -m "pytest passes" -- a.py', "allow"),
    ("grep -rn pytest backend/pyproject.toml", "allow"),
    ("echo 'run pytest later'", "allow"),
    ("pip install pytest", "allow"),
]


def main():
    failures = [(c, e, decision(c)) for c, e in CASES if decision(c) != e]
    for cmd, expected, got in failures:
        print(f"FAIL {cmd!r}: expected {expected}, got {got}")
    print(f"{len(CASES) - len(failures)}/{len(CASES)} passed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""PreToolUse Bash guard: no whole-suite test runs from a Claude session.

The backend suite is ~10,000 tests (~1 h serial on a laptop) and the Playwright
suite is 14 CI shards' worth. CI runs both, sharded, on every PR, so a local
whole-suite run buys a slower copy of an answer the PR gets for free, and it
blocks the session (or a subagent the operator is waiting on) for an hour.
Root CLAUDE.md § "Every change must update docs and tests" item 3 asks for the
test files covering the diff instead; this hook makes that the default for the
main session and every subagent alike.

Denied (each shell segment is checked on its own):
  pytest / python -m pytest with no test path, or with only `tests` / `.`
  playwright test with no spec path (npx / pnpm exec / pnpm test:e2e alike)
  pnpm [run] test, test:backend, test:frontend, test:e2e (bare)

Allowed: any run naming a file, `file::test`, or a sub-directory; `-k` / `-m`
/ `--grep` filters; `--collect-only` / `--co` / `--list`; `--version` / `-h`.

Override for the rare change with no bounded caller set (a conftest, a base
model, a cross-cutting serializer), and say so when you do:
  FEOH_FULL_SUITE=1 pytest
"""

import json
import os
import re
import shlex
import sys

OVERRIDE = "FEOH_FULL_SUITE=1"

# Option flags whose NEXT token is a value, not a test path.
PYTEST_VALUE_OPTS = {
    "-c", "-o", "-p", "--rootdir", "--confcutdir", "--basetemp", "--splits",
    "--group", "--splitting-algorithm", "--durations-path", "--timeout",
    "--ignore", "--ignore-glob", "--deselect", "--maxfail", "--tb",
    "--import-mode", "-W", "--log-level", "--junitxml", "--durations",
}
PYTEST_FILTERS = {"-k", "-m", "--lf", "--last-failed", "--sw", "--stepwise"}
PYTEST_HARMLESS = {"--collect-only", "--co", "--version", "-h", "--help", "--fixtures",
                   "--markers"}

PW_VALUE_OPTS = {
    "--config", "-c", "--project", "--workers", "-j", "--shard", "--reporter",
    "--retries", "--timeout", "--output", "--max-failures", "--repeat-each",
    "--grep-invert", "--trace",
}
PW_FILTERS = {"--grep", "-g", "--last-failed", "--only-changed"}
PW_HARMLESS = {"--list", "--help", "-h", "--version"}

WHOLE_TREE = {"tests", "tests/", ".", "./", "tests-e2e", "tests-e2e/"}

PNPM_WHOLE_SUITE = {"test", "test:backend", "test:frontend", "test:e2e", "test:mobile"}

ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _deny(reason):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(0)


def _segments(command):
    """Split a command line into simple-command token lists on ; && || | and newlines."""
    # Drop redirections first (`2>&1`, `> out.log`, `< in`): their targets are not
    # test paths, and `&` inside `2>&1` would otherwise read as a separator.
    command = re.sub(r"\d*[<>]+&?\s*[^\s;&|]+", " ", command.replace("\n", " ; "))
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
    lexer.whitespace_split = True
    segments, current = [], []
    try:
        for tok in lexer:
            if tok and set(tok) <= set(";&|"):
                if current:
                    segments.append(current)
                current = []
            else:
                current.append(tok)
    except ValueError:
        return []  # unbalanced quotes: not ours to judge
    if current:
        segments.append(current)
    return segments


def _strip_env(tokens):
    """Drop leading VAR=value assignments; report whether the override was among them."""
    override = False
    i = 0
    while i < len(tokens) and ENV_ASSIGN.match(tokens[i]):
        if tokens[i] == OVERRIDE:
            override = True
        i += 1
    if i < len(tokens) and tokens[i] == "env":  # `env A=1 pytest`
        i += 1
        while i < len(tokens) and ENV_ASSIGN.match(tokens[i]):
            if tokens[i] == OVERRIDE:
                override = True
            i += 1
    return tokens[i:], override


def _is_scoped(args, value_opts, filters, harmless):
    """True when the args name a test path below the suite root, a filter, or a no-run flag."""
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        name = arg.split("=", 1)[0]
        if name in harmless or name in filters:
            return True
        if name in value_opts:
            skip_next = "=" not in arg
            continue
        if arg.startswith("-"):
            continue
        if arg.rstrip("/") + "/" in {w.rstrip("/") + "/" for w in WHOLE_TREE}:
            continue
        return True  # a file, file::test, or sub-directory
    return False


def _pytest_args(tokens):
    """Args after the pytest entry point, or None if this segment is not a pytest run."""
    if not tokens:
        return None
    head = os.path.basename(tokens[0])
    if head in {"pytest", "py.test"}:
        return tokens[1:]
    if re.fullmatch(r"python(3(\.\d+)?)?", head):
        rest = tokens[1:]
        while rest and rest[0].startswith("-") and rest[0] != "-m":
            rest = rest[1:]
        if len(rest) >= 2 and rest[0] == "-m" and rest[1] == "pytest":
            return rest[2:]
    return None


def _playwright_args(tokens):
    """Args after `playwright test`, or None if this segment is not a Playwright run."""
    for i, tok in enumerate(tokens):
        if os.path.basename(tok) == "playwright" and i + 1 < len(tokens) and tokens[i + 1] == "test":
            return tokens[i + 2:]
    return None


def _pnpm_script(tokens):
    """(script, extra args) for a pnpm script invocation, else None."""
    if not tokens or os.path.basename(tokens[0]) != "pnpm":
        return None
    rest = tokens[1:]
    while rest and rest[0] in {"-C", "--dir", "--filter", "-F"}:
        rest = rest[2:]
    if rest and rest[0] == "run":
        rest = rest[1:]
    if not rest:
        return None
    return rest[0], rest[1:]


REASON = (
    "Whole-suite test run blocked: {what}. CI runs the full suite sharded on every PR "
    "(~7 min/shard), so a local whole-suite run is an hour-long slower copy of that. "
    "Run the test files your diff touches instead (e.g. `pytest tests/test_x.py`, "
    "`playwright test path/to/spec.ts`), plus a grep sweep of the callers of anything "
    "shared you changed. If the change genuinely has no bounded caller set (a conftest, "
    "a base model, a cross-cutting serializer), prefix the command with {override} and "
    "say why. See .claude/hooks/test-scope-guard.py."
)


def check(command):
    """Return a deny reason for the command, or None to allow it."""
    for seg in _segments(command):
        tokens, override = _strip_env(seg)
        if override:
            continue
        args = _pytest_args(tokens)
        if args is not None and not _is_scoped(args, PYTEST_VALUE_OPTS, PYTEST_FILTERS,
                                               PYTEST_HARMLESS):
            return "pytest with no test file named"
        args = _playwright_args(tokens)
        if args is not None and not _is_scoped(args, PW_VALUE_OPTS, PW_FILTERS, PW_HARMLESS):
            return "playwright test with no spec named"
        script = _pnpm_script(tokens)
        if script is not None:
            name, extra = script
            if name in PNPM_WHOLE_SUITE and not _is_scoped(
                extra, PW_VALUE_OPTS | PYTEST_VALUE_OPTS, PW_FILTERS | PYTEST_FILTERS,
                PW_HARMLESS | PYTEST_HARMLESS,
            ):
                return f"`pnpm {name}` runs a whole suite"
    return None


def main():
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        sys.exit(0)
    if payload.get("tool_name") != "Bash":
        sys.exit(0)
    command = (payload.get("tool_input") or {}).get("command") or ""
    what = check(command)
    if what:
        _deny(REASON.format(what=what, override=OVERRIDE))
    sys.exit(0)


if __name__ == "__main__":
    main()

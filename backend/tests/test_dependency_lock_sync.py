"""Guard the hash-pinned locks against drifting from their manifests.

Dependabot's pip ecosystem updates `backend/pyproject.toml`, but it has no
idea `requirements.lock` / `requirements-dev.lock` exist — its pip-compile
support only recognises a lockfile whose name ends in `.txt` and matches an
`.in` file's basename, which these deliberately don't (they're compiled from
pyproject.toml, not from a `.in`). So a merged Dependabot PR can raise a
floor in pyproject.toml while the locks keep installing the old version, and
the production image quietly ships pins nobody declared. That failure is
silent: `--require-hashes` still succeeds, because the stale lock is
internally consistent.

These tests make it loud. They compare *declared constraints* against
*locked versions* — no re-resolution, no network — so they only fail when
someone actually changed a manifest without regenerating its lock, never
because a new version appeared on PyPI overnight. Regenerate with the
commands in backend/CLAUDE.md → Dependency lock.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

BACKEND = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND.parent

# `name==version` at the start of a logical line. The locks are uv-generated
# with `--generate-hashes`, so every pin is followed by continuation lines of
# `--hash=...`, which this deliberately ignores.
_PIN = re.compile(r"^(?P<name>[A-Za-z0-9._-]+)==(?P<version>[^\s;\\]+)")


def _locked_versions(lockfile: Path) -> dict[str, Version]:
    pins: dict[str, Version] = {}
    for line in lockfile.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "--hash")):
            continue
        match = _PIN.match(stripped)
        if match:
            pins[canonicalize_name(match.group("name"))] = Version(match.group("version"))
    return pins


def _declared(requirement_strings: list[str]) -> list[Requirement]:
    """Parse requirement strings, dropping ones excluded by a marker.

    A marker like `python_version < "3.12"` describes an environment the
    locks aren't compiled for, so an absent pin is correct, not drift.
    """
    parsed = []
    for raw in requirement_strings:
        req = Requirement(raw)
        if req.marker is not None and not req.marker.evaluate():
            continue
        parsed.append(req)
    return parsed


def _pyproject_requirements(extra: str | None = None) -> list[Requirement]:
    data = tomllib.loads((BACKEND / "pyproject.toml").read_text())
    project = data["project"]
    if extra is None:
        return _declared(project["dependencies"])
    return _declared(project["optional-dependencies"][extra])


def _requirements_in(path: Path) -> list[Requirement]:
    lines = [
        line.strip()
        for line in path.read_text().splitlines()
        if line.strip() and not line.strip().startswith(("#", "-"))
    ]
    return _declared(lines)


def _assert_satisfied(
    declared: list[Requirement], locked: dict[str, Version], lock_name: str
) -> None:
    for req in declared:
        name = canonicalize_name(req.name)
        assert name in locked, (
            f"{req.name} is declared but missing from {lock_name} — "
            f"regenerate the lock (backend/CLAUDE.md → Dependency lock)."
        )
        version = locked[name]
        assert req.specifier.contains(version, prereleases=True), (
            f"{lock_name} pins {req.name}=={version}, which does not satisfy the "
            f"declared '{req}'. A manifest bump landed without regenerating the "
            f"lock, so the image installs a version nobody declared."
        )


def test_runtime_lock_satisfies_pyproject() -> None:
    """`requirements.lock` is what the production image installs."""
    _assert_satisfied(
        _pyproject_requirements(),
        _locked_versions(BACKEND / "requirements.lock"),
        "requirements.lock",
    )


def test_dev_lock_satisfies_pyproject_base_and_dev_extra() -> None:
    """`requirements-dev.lock` is what CI installs to run these tests."""
    locked = _locked_versions(BACKEND / "requirements-dev.lock")
    _assert_satisfied(_pyproject_requirements(), locked, "requirements-dev.lock")
    _assert_satisfied(_pyproject_requirements("dev"), locked, "requirements-dev.lock")


def test_dev_lock_satisfies_requirements_dev_in() -> None:
    """The extra pins folded in alongside the [dev] extra — notably pip itself."""
    _assert_satisfied(
        _requirements_in(BACKEND / "requirements-dev.in"),
        _locked_versions(BACKEND / "requirements-dev.lock"),
        "requirements-dev.lock",
    )


@pytest.mark.parametrize("fixture", ["fake-erp", "fake-commerce"])
def test_fake_fixture_lock_satisfies_its_requirements_in(fixture: str) -> None:
    """Same guard for the fixture images, whose pairs Dependabot does manage.

    Dependabot regenerates `requirements.txt` itself here, so this mostly
    catches a hand-edited `requirements.in` that never got recompiled.
    """
    root = REPO_ROOT / "tools" / fixture
    _assert_satisfied(
        _requirements_in(root / "requirements.in"),
        _locked_versions(root / "requirements.txt"),
        f"tools/{fixture}/requirements.txt",
    )


@pytest.mark.parametrize(
    "lockfile",
    [
        BACKEND / "requirements.lock",
        BACKEND / "requirements-dev.lock",
        REPO_ROOT / "tools" / "fake-erp" / "requirements.txt",
        REPO_ROOT / "tools" / "fake-commerce" / "requirements.txt",
    ],
    ids=lambda p: str(p.relative_to(REPO_ROOT)),
)
def test_every_locked_package_carries_a_hash(lockfile: Path) -> None:
    """A pin without a hash defeats `--require-hashes` for that package."""
    text = lockfile.read_text()
    locked = _locked_versions(lockfile)
    assert locked, f"{lockfile} parsed to zero pins — did the format change?"
    # uv emits `name==version \` then indented `--hash=sha256:...` lines, so a
    # hash count below the pin count means some package was left unhashed.
    hash_count = len(re.findall(r"--hash=sha256:[0-9a-f]{64}", text))
    assert hash_count >= len(locked), (
        f"{lockfile.name}: {len(locked)} pinned packages but only {hash_count} "
        "hashes — regenerate with --generate-hashes."
    )

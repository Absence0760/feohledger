"""Every Postgres readiness probe in the repo checks over TCP, never the socket.

On a fresh data volume the Postgres image's entrypoint runs a temporary
socket-only server for initdb and its init scripts, then shuts it down and
starts the real one. `pg_isready` with no `-h` asks over the Unix socket, so it
reports that temporary server as ready: the container goes healthy, and the
first client (migrations, the seed, a test job) meets "the database system is
shutting down". The real server is the only one listening on TCP, so
`-h 127.0.0.1` is what makes "healthy" mean "will accept a connection".

This was reproduced against `deploy/compose.prod.yml` first and fixed there;
the dev compose file (which the day-one stack layers over, so it inherits the
healthcheck) and CI's service containers carried the socket form until the same
fix reached them (backend/docs/docker.md § Health Checks). The scan is textual
over every compose file and workflow, so a new service or job that copies an
old snippet fails here rather than as a first-boot flake.

Pure filesystem reads: no Docker, no database.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

SCANNED = sorted(
    {
        *(REPO_ROOT / ".github" / "workflows").glob("*.y*ml"),
        *(REPO_ROOT / "backend").glob("docker-compose*.y*ml"),
        *(REPO_ROOT / "deploy").glob("compose*.y*ml"),
        *(REPO_ROOT / "scripts").rglob("compose*.y*ml"),
        *(REPO_ROOT / "deploy").glob("*.sh"),
        *(REPO_ROOT / "scripts").rglob("*.sh"),
    }
)

# A `pg_isready` invocation up to the end of its command (closing quote,
# bracket or line end). Comment lines are skipped before matching.
_INVOCATION = re.compile(r"pg_isready\b[^\"'\]\n]*")
_TCP_HOST = re.compile(r"(?:^|\s)(?:-h\s*|--host[=\s])(?:127\.0\.0\.1|localhost)\b")


def _invocations(path: Path) -> list[tuple[int, str]]:
    found = []
    for lineno, line in enumerate(path.read_text().splitlines(), start=1):
        if line.lstrip().startswith("#"):
            continue
        found.extend((lineno, m.group(0)) for m in _INVOCATION.finditer(line))
    return found


def test_the_scan_reaches_the_files_that_probe_postgres() -> None:
    """Guard against a vacuous pass: the known probes must be found."""
    probed = {path.relative_to(REPO_ROOT).as_posix() for path in SCANNED if _invocations(path)}
    assert {
        "backend/docker-compose.yml",
        "deploy/compose.prod.yml",
        ".github/workflows/ci.yml",
        ".github/workflows/sso-e2e.yml",
    } <= probed, probed


@pytest.mark.parametrize("path", SCANNED, ids=lambda p: p.relative_to(REPO_ROOT).as_posix())
def test_pg_isready_checks_over_tcp(path: Path) -> None:
    socket_form = [
        f"{path.relative_to(REPO_ROOT)}:{lineno}: {cmd.strip()}"
        for lineno, cmd in _invocations(path)
        if not _TCP_HOST.search(cmd)
    ]
    assert not socket_form, (
        "pg_isready without `-h 127.0.0.1` asks over the socket and reports the "
        "image's temporary initdb server as ready:\n" + "\n".join(socket_form)
    )


def test_the_matcher_tells_the_two_forms_apart() -> None:
    assert not _TCP_HOST.search("pg_isready -U postgres")
    assert _TCP_HOST.search("pg_isready -U postgres -h 127.0.0.1")
    assert _TCP_HOST.search("pg_isready -h localhost -U postgres")

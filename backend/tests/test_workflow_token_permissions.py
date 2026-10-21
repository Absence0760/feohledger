"""`security-events: write` stays on the jobs that upload SARIF, never the workflow.

OpenSSF Scorecard's Token-Permissions check scores a workflow 0 for a
top-level `security-events: write`, because a top-level grant reaches every
job in the file. At job level it accepts the grant, but only in a workflow
that runs an action it knows uploads SARIF (`codeql-action/analyze`,
`codeql-action/upload-sarif`, `ossf/scorecard-action`, …). security.yml
carried the grant at the top, so it was flagged when ci.yml began calling it.

The rule pinned here:

- No workflow grants `security-events: write` at the top level, except the
  ones in ``TOP_LEVEL_EXCEPTIONS``, each with its reason.
- A job that grants it either runs a SARIF-uploading step itself, or calls a
  local reusable workflow whose own jobs pass this same rule. The second case
  is ci.yml's `security` job: a called workflow's token is capped by its
  caller's, so the caller must hold the grant. Scorecard cannot see through a
  `uses:` job and reports it anyway (see the comment on that job).
"""

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

# Actions that upload SARIF to code scanning, as Scorecard's allowlist names them.
SARIF_ACTIONS = {
    "github/codeql-action/analyze",
    "github/codeql-action/upload-sarif",
    "ossf/scorecard-action",
}

TOP_LEVEL_EXCEPTIONS = {
    # One job, and gitleaks-action uploads its own SARIF. A job-level grant
    # would be the same token on the same job, and Scorecard does not
    # allowlist gitleaks-action, so moving it changes nothing.
    "gitleaks.yml",
}


def _load(name: str, root: Path = WORKFLOWS) -> dict:
    return yaml.safe_load((root / name).read_text())


def _grants_security_events(permissions) -> bool:
    if permissions == "write-all":
        return True
    return isinstance(permissions, dict) and permissions.get("security-events") == "write"


def _uploads_sarif(job: dict) -> bool:
    for step in job.get("steps", []):
        uses = step.get("uses", "")
        if uses.split("@")[0] in SARIF_ACTIONS:
            return True
    return False


def _job_grant_problems(
    workflow: str, root: Path = WORKFLOWS, seen: frozenset[str] = frozenset()
) -> list[str]:
    problems = []
    doc = _load(workflow, root)
    if workflow not in TOP_LEVEL_EXCEPTIONS and _grants_security_events(doc.get("permissions")):
        problems.append(f"{workflow}: top-level security-events: write")
    for name, job in doc["jobs"].items():
        if not _grants_security_events(job.get("permissions")):
            continue
        called = job.get("uses", "")
        if called.startswith("./.github/workflows/"):
            target = called.removeprefix("./.github/workflows/")
            if target in seen:
                continue
            problems.extend(_job_grant_problems(target, root, seen | {workflow}))
        elif not _uploads_sarif(job):
            problems.append(
                f"{workflow}: job {name!r} grants security-events: write but uploads no SARIF"
            )
    return problems


WORKFLOW_FILES = sorted(p.name for p in WORKFLOWS.glob("*.yml"))


@pytest.mark.parametrize("workflow", WORKFLOW_FILES)
def test_security_events_write_is_scoped_to_sarif_jobs(workflow):
    assert _job_grant_problems(workflow) == []


def test_security_yml_is_read_only_at_the_top():
    # The file ci.yml calls on every PR. Its top-level grant is what Scorecard flagged.
    assert _load("security.yml")["permissions"] == {"contents": "read"}


def test_guard_catches_a_top_level_grant(tmp_path):
    # The guard must fail on the shape it exists to stop, not just pass on today's files.
    (tmp_path / "bad.yml").write_text(
        yaml.safe_dump(
            {
                "permissions": {"contents": "read", "security-events": "write"},
                "jobs": {"build": {"runs-on": "ubuntu-latest", "steps": [{"run": "true"}]}},
            }
        )
    )
    assert _job_grant_problems("bad.yml", tmp_path) == ["bad.yml: top-level security-events: write"]


def test_guard_catches_a_job_grant_without_sarif(tmp_path):
    (tmp_path / "bad.yml").write_text(
        yaml.safe_dump(
            {
                "permissions": {"contents": "read"},
                "jobs": {
                    "build": {
                        "permissions": {"security-events": "write"},
                        "runs-on": "ubuntu-latest",
                        "steps": [{"run": "true"}],
                    }
                },
            }
        )
    )
    assert _job_grant_problems("bad.yml", tmp_path) == [
        "bad.yml: job 'build' grants security-events: write but uploads no SARIF"
    ]

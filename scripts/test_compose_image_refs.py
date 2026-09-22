#!/usr/bin/env python3
"""Prove scripts/compose_image_refs.sh refuses what it must (docs/decisions.md §203).

CI's `services:` images and its MinIO `docker run` are whatever this script
prints, so the failure that matters is the quiet one: GitHub starts NO service
container when `services.<id>.image` is an empty string, and does not fail the
job for it. Each test below hands the script a compose file that is wrong in
exactly one way and requires a non-zero exit with nothing usable on stdout.
The last one runs it over the real backend/docker-compose.yml.

Standard library only; needs `docker compose` and `jq` on PATH, as CI's
`compose-images` job has. Run it with
  python3 -m unittest scripts/test_compose_image_refs.py
"""

import os
import re
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SCRIPT = os.path.join(HERE, "compose_image_refs.sh")

DIGEST = "sha256:" + "ab" * 32
PINNED = f"redis:7.4.11-alpine@{DIGEST}"


def _run(compose_text, *services):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "compose.yml")
        with open(path, "w") as fh:
            fh.write(compose_text)
        return subprocess.run(
            [SCRIPT, path, *services], capture_output=True, text=True, check=False
        )


def _compose(**images):
    lines = ["services:"]
    for name, image in images.items():
        lines.append(f"  {name}:")
        if image is None:
            lines.append("    build: .")
        else:
            lines.append(f"    image: {image!r}")
    return "\n".join(lines) + "\n"


class RefusesWhatCiMustNotRun(unittest.TestCase):
    def assertRefused(self, result, service):
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertNotIn(f"{service}=", result.stdout)

    def test_floating_tag(self):
        result = _run(_compose(cache="redis:7-alpine"), "cache")
        self.assertRefused(result, "cache")
        self.assertIn("'cache' is not pinned", result.stderr)

    def test_tag_without_digest(self):
        self.assertRefused(_run(_compose(cache="redis:7.4.11-alpine"), "cache"), "cache")

    def test_digest_without_tag(self):
        # The tag is what a human and Dependabot read the release from.
        self.assertRefused(_run(_compose(cache=f"redis@{DIGEST}"), "cache"), "cache")

    def test_short_digest(self):
        result = _run(_compose(cache="redis:7.4.11-alpine@sha256:abc"), "cache")
        self.assertRefused(result, "cache")

    def test_build_only_service_has_no_image_to_read(self):
        result = _run(_compose(app=None), "app")
        self.assertRefused(result, "app")
        self.assertIn("image: ''", result.stderr)

    def test_service_the_file_does_not_define(self):
        self.assertRefused(_run(_compose(cache=PINNED), "cache", "db"), "db")

    def test_no_service_named(self):
        self.assertEqual(_run(_compose(cache=PINNED)).returncode, 2)


class ReadsWhatTheFileSays(unittest.TestCase):
    def test_prints_name_equals_ref_in_argument_order(self):
        other = f"quay.io/minio/minio:RELEASE.2025-09-07T16-13-09Z@{'sha256:' + 'cd' * 32}"
        result = _run(_compose(cache=PINNED, store=other), "store", "cache")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"store={other}\ncache={PINNED}\n")

    def test_names_a_service_behind_a_profile(self):
        text = f"services:\n  idp:\n    image: {PINNED!r}\n    profiles: [idp]\n"
        result = _run(text, "idp")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"idp={PINNED}\n")

    def test_the_real_backend_compose_file(self):
        """The call ci.yml and sso-e2e.yml make, against the file they make it on."""
        compose = os.path.join(REPO, "backend", "docker-compose.yml")
        result = subprocess.run(
            [SCRIPT, compose, "postgres", "redis", "minio"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        names = [line.split("=", 1)[0] for line in result.stdout.splitlines()]
        self.assertEqual(names, ["postgres", "redis", "minio"])
        with open(compose) as fh:
            written = set(re.findall(r"^\s+image:\s*(\S+)\s*$", fh.read(), re.MULTILINE))
        for line in result.stdout.splitlines():
            self.assertIn(line.split("=", 1)[1], written)


if __name__ == "__main__":
    unittest.main()

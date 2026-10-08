# Base image pinned by digest so a re-tagged python:3.14-slim can't
# silently change what we ship. Scorecard's PinnedDependencies query
# treats unpinned container images the same as unpinned actions — both
# leave a supply-chain pivot point. Bump the digest alongside the tag
# when Renovate / Dependabot opens a PR (the trailing comment is what
# the docker ecosystem reads as the human-version reference).
FROM python:3.14-slim@sha256:cea0e6040540fb2b965b6e7fb5ffa00871e632eef63719f0ea54bca189ce14a6

# Pull Debian security backports on top of the pinned base layer. The
# digest above only changes when the upstream python:3.14-slim manifest
# rebuilds, which lags Debian's trixie-security stream by days-to-weeks.
# Trivy + GitHub code scanning flag CVEs in libc6 / libcap2 / libsystemd0
# / libudev1 / sed every time that gap opens; this step closes it without
# waiting for an upstream rebuild. `--no-install-recommends` keeps the
# image lean; the apt list cleanup keeps the layer small.
RUN apt-get update \
    && apt-get -y --no-install-recommends upgrade \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install uv for fast dependency management. Pinned to the 0.5 release
# series by digest — `:latest` would re-resolve on every rebuild, which
# Scorecard flags and which can quietly swap the uv binary under us.
COPY --from=ghcr.io/astral-sh/uv:0.5@sha256:7bff3c3776ec467fc1437960f2c469d8beb30f536a6465a3350c647ccd260ec2 /uv /usr/local/bin/uv

# Runtime deps install from a fully hash-pinned lock so a compromised or
# republished PyPI artifact can't slip into the image — same Scorecard
# PinnedDependencies posture as CI. `requirements.lock` is the runtime-only
# (no [dev]/[ocr] extras) sibling of requirements-dev.lock; regenerate both
# from pyproject.toml with `uv pip compile … --generate-hashes` — see
# backend/CLAUDE.md → Dependency lock. The app itself runs from the copied
# source (no editable install), so only its dependencies are locked here.
# `--no-cache` because uv otherwise leaves its download cache (~300 MB of
# wheels and unpacked archives) in /root/.cache/uv in the final layer.
# Nothing at runtime reads it, and Trivy scans the stale package metadata
# inside it as if those copies were installed.
COPY pyproject.toml requirements.lock .

# The same layer then removes pip. It is the ONLY package `python:3.14-slim`
# ships in site-packages, nothing in this image installs with it (uv does that,
# and the app runs from the copied source), and no runtime path imports it — so
# all it contributes is a dist-info for Trivy to match CVEs against. It has
# already produced one (CVE-2026-13346, pip < 26.2.0, "arbitrary file
# installation via malicious package indexes"), reported against an installer
# this image never invokes. Deleting it retires that finding and every future
# pip CVE with it, rather than chasing the base-image digest each time one
# lands. To install something into a running container for debugging,
# `uv pip install --system` is still there.
RUN uv pip install --system --no-cache --require-hashes -r requirements.lock \
    && python -m pip uninstall -y pip

COPY . .

# Postgres TLS trust anchor for Amazon RDS. `COPY . .` above ships the committed
# AWS RDS global CA bundle (certs/rds-global-bundle.pem — a public file, pinned
# by sha256 in tests/test_container_supply_chain.py, so a bump is a reviewed
# diff rather than a build-time download). asyncpg and libpq read PGSSLROOTCERT
# only when PGSSLMODE is require or stricter, so naming it here changes nothing
# for the local Postgres container (compose.prod.yml defaults PGSSLMODE to
# `prefer`); against RDS, `PGSSLMODE=verify-full` checks the server's chain and
# hostname against it. Deliberately NOT added to the system trust store: these
# CAs are for the database connection, not for every HTTPS call the app makes.
# docs/minimal-deployment.md § Database TLS.
ENV PGSSLROOTCERT=/app/certs/rds-global-bundle.pem

EXPOSE 8000

# Default entrypoint is the ECS/Fargate API (uvicorn). This same image is
# ALSO deployed as the async-worker Lambdas (extraction / ERP / audit): on
# Lambda the function's ImageConfig overrides the entrypoint to
# `python -m awslambdaric` + its handler, so no Lambda-specific image is
# built. The awslambdaric runtime client is bundled via requirements.lock —
# see pyproject.toml + docs/production-deployment.md → Lambda workers.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

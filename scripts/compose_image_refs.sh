#!/usr/bin/env bash
#
# Print `<service>=<image ref>` for each named service of a compose file, one
# per line: the `name=value` shape `$GITHUB_OUTPUT` takes.
#
# CI's service containers (`services:` in ci.yml and sso-e2e.yml) and its MinIO
# `docker run` take their images from this, so the compose file is the only
# place a ref is written, and a Dependabot `docker-compose` bump is what that
# PR's own CI run tests (docs/decisions.md §203, backend/docs/docker.md
# § Image pinning).
#
# It fails closed. Every ref must be `repo:tag@sha256:<64 hex>`: an EMPTY
# `services.<id>.image` does not fail a GitHub job (the service silently does
# not start), and a floating tag would un-pin CI behind the compose file's back.
# A service the file does not define is refused by compose itself.
#
# Usage: scripts/compose_image_refs.sh <compose-file> <service>...
# Needs: docker compose (v2 plugin, for `config --format json`) and jq.
# Its tests: python3 -m unittest scripts/test_compose_image_refs.py
set -euo pipefail

if [ "$#" -lt 2 ]; then
  echo "usage: $0 <compose-file> <service>..." >&2
  exit 2
fi
compose_file=$1
shift

# JSON rather than `config --images`, which prints bare refs in no stable order
# with nothing tying a line to its service. Naming the services also activates
# any profile one sits behind.
config=$(docker compose -f "$compose_file" config --format json "$@")

pinned='^[^[:space:]@]+:[^[:space:]@]+@sha256:[0-9a-f]{64}$'
for service in "$@"; do
  ref=$(jq -r --arg s "$service" '.services[$s].image // ""' <<<"$config")
  if ! [[ $ref =~ $pinned ]]; then
    echo "::error file=${compose_file}::service '${service}' is not pinned as repo:tag@sha256:<digest> (image: '${ref}')" >&2
    exit 1
  fi
  printf '%s=%s\n' "$service" "$ref"
done

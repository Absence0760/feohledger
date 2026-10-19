#!/bin/sh
# The deploy's database step: build an empty control plane, migrate it to head,
# then migrate every tenant database. Every deploy runs exactly this file, from
# the api image's working directory (/app), before the new code serves traffic:
#
#   deploy/deploy.sh                    compose run --rm api sh scripts/deploy_migrate.sh
#   .github/workflows/aws-deploy.yml    a one-off Fargate task: sh scripts/deploy_migrate.sh
#   CI's day-one job (scripts/day-one)  docker run <the api image> sh scripts/deploy_migrate.sh
#
# It is a file, not a command line restated in each of those places, so the job
# that rehearses a deploy from an empty database cannot drift from what
# production runs (#575).
#
# - init_control_plane.py builds an EMPTY control plane (tables, system roles,
#   Alembic's version row, one transaction) and is a no-op on an initialised
#   one; Alembic's chain has no step that creates it
#   (app/services/control_plane_bootstrap.py). Exit 2 = a database Alembic
#   stamped while empty, with the recovery steps on stderr.
# - migrate_all_tenants.py attempts every tenant and exits non-zero if any
#   failed, so a deploy never rolls code onto a schema it does not match.
#
# A second run against a migrated stack changes nothing; CI's day-one job
# asserts that.
set -eu
cd "$(dirname "$0")/.."
python scripts/init_control_plane.py
alembic upgrade head
python scripts/migrate_all_tenants.py

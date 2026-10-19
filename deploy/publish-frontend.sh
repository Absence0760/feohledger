#!/usr/bin/env bash
# Publish a built SPA to the frontend bucket and make it live on CloudFront
# (infra/frontend.tf, docs/decisions.md §278). Run by .github/workflows/
# vm-deploy.yml after the VM deploy has shipped the backend, so a new UI never
# calls an API that is not there yet. Not run on the VM.
#
#   FRONTEND_BUCKET=<bucket> CLOUDFRONT_DISTRIBUTION_ID=<id> \
#     deploy/publish-frontend.sh frontend/build
#
# The order is what keeps a page someone already has open working:
#
#   1. hashed assets (_app/immutable/*) first, never deleting anything — an
#      open page lazy-loads chunks from the build it started on;
#   2. the other static files (icons, manifest, marketing/);
#   3. the entry documents LAST — index.html, then _app/version.json, which
#      SvelteKit polls to notice a new deploy — both no-cache, so nothing
#      points at an asset before it exists;
#   4. invalidate /* and wait for it, so the run reports success only once the
#      edge serves the new build;
#   5. prune hashed assets that are in neither this build nor uploaded within
#      FRONTEND_PRUNE_DAYS (default 30). The bucket is versioned, so a pruned
#      file is still restorable for 90 days (the lifecycle rule).
#
# Rollback is publishing an older build the same way (deploy/README.md §
# Frontend rollback).
set -euo pipefail

die() {
	echo "publish-frontend: $*" >&2
	exit 1
}

[ $# -eq 1 ] || die "usage: $0 <build-dir>"
build=${1%/}
bucket=${FRONTEND_BUCKET:-}
distribution=${CLOUDFRONT_DISTRIBUTION_ID:-}
prune_days=${FRONTEND_PRUNE_DAYS:-30}

[ -n "$bucket" ] || die "FRONTEND_BUCKET is not set (terraform output frontend_bucket)"
[ -n "$distribution" ] || die "CLOUDFRONT_DISTRIBUTION_ID is not set (terraform output frontend_distribution_id)"
[[ $prune_days =~ ^[1-9][0-9]*$ ]] || die "FRONTEND_PRUNE_DAYS must be a whole number of days, at least 1"

# A partial build must never become the live one.
for f in index.html _app/version.json; do
	[ -s "$build/$f" ] || die "$build/$f is missing or empty — not a complete adapter-static build"
done
[ -d "$build/_app/immutable" ] || die "$build/_app/immutable is missing — not a complete adapter-static build"

dest="s3://${bucket}"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

echo "==> 1/5 hashed assets"
# Content-hashed names: an existing key already holds these bytes, so
# --size-only skips it instead of re-uploading (and re-versioning) every file.
aws s3 sync "$build/_app/immutable" "$dest/_app/immutable" \
	--size-only \
	--cache-control "public, max-age=31536000, immutable" \
	--only-show-errors

echo "==> 2/5 static files"
aws s3 sync "$build" "$dest" \
	--exclude "_app/immutable/*" \
	--exclude "index.html" \
	--exclude "_app/version.json" \
	--exclude "*.webmanifest" \
	--cache-control "public, max-age=300" \
	--only-show-errors
# The CLI guesses Content-Type from the runner's MIME table, which may not know
# .webmanifest; a manifest served as application/octet-stream is ignored by
# browsers, so it is named explicitly.
while IFS= read -r -d '' manifest; do
	rel=${manifest#"$build"/}
	aws s3 cp "$manifest" "$dest/$rel" \
		--cache-control "public, max-age=300" \
		--content-type "application/manifest+json" \
		--only-show-errors
done < <(find "$build" -name '*.webmanifest' -not -path "$build/_app/*" -print0)

echo "==> 3/5 entry documents"
aws s3 cp "$build/index.html" "$dest/index.html" \
	--cache-control "no-cache" \
	--content-type "text/html; charset=utf-8" \
	--only-show-errors
aws s3 cp "$build/_app/version.json" "$dest/_app/version.json" \
	--cache-control "no-cache" \
	--content-type "application/json" \
	--only-show-errors

echo "==> 4/5 invalidating"
invalidation=$(aws cloudfront create-invalidation \
	--distribution-id "$distribution" \
	--paths "/*" \
	--query Invalidation.Id --output text)
aws cloudfront wait invalidation-completed \
	--distribution-id "$distribution" \
	--id "$invalidation"
echo "    invalidation ${invalidation} completed"

echo "==> 5/5 pruning hashed assets older than ${prune_days} days that this build does not use"
cutoff=$(date -u -d "-${prune_days} days" +%Y-%m-%dT%H:%M:%S)
(cd "$build" && find _app/immutable -type f) | sort >"$work/keep"
aws s3api list-objects-v2 \
	--bucket "$bucket" \
	--prefix "_app/immutable/" \
	--query "Contents[].[Key,LastModified]" \
	--output text >"$work/listing"
# ISO-8601 timestamps in UTC compare correctly as strings. An empty prefix
# prints "None".
awk -F '\t' -v cutoff="$cutoff" 'NF == 2 && $2 < cutoff { print $1 }' "$work/listing" |
	sort | comm -23 - "$work/keep" >"$work/prune"

count=$(wc -l <"$work/prune")
if [ "$count" -eq 0 ]; then
	echo "    nothing to prune"
else
	# delete-objects takes at most 1000 keys per call.
	split -l 1000 "$work/prune" "$work/batch."
	for batch in "$work"/batch.*; do
		jq -R -s '{Objects: (split("\n") | map(select(length > 0)) | map({Key: .})), Quiet: true}' \
			"$batch" >"$batch.json"
		errors=$(aws s3api delete-objects \
			--bucket "$bucket" \
			--delete "file://$batch.json" \
			--query "Errors[].Key" --output text)
		if [ -n "$errors" ] && [ "$errors" != "None" ]; then
			die "could not delete: $errors"
		fi
	done
	echo "    pruned ${count} file(s)"
fi

echo "==> published $(cat "$build/_app/version.json")"

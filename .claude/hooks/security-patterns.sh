#!/usr/bin/env bash
# .claude/hooks/security-patterns.sh
#
# Cheap, fast pattern checks for security regressions in this AP
# system. Designed to run as a PostToolUse hook on Edit / Write /
# MultiEdit so violations surface to Claude before the next turn.
#
# Philosophy
# ----------
# Greps catch the bug classes that have a stable textual shape. Each
# rule answers: "What does the violation look like in code, and what
# is the safer replacement?" The rules below are tuned to:
#   1. Flag the specific regressions this codebase has been bitten by
#      (bcrypt truncation, missing HMAC, raw filename in S3 key, etc.)
#   2. Flag *broader* shapes that future bugs of the same class will
#      take — `Float` near a money-named column, `logger.* %s ... exc`,
#      `jwt.decode` outside the central helper, naive `datetime.now()`.
#
# Adding a new rule is one block. Rules MUST:
#   - Have a unique RULE_NAME
#   - State WHY they exist (so a maintainer can decide whether to
#     bypass with `# noqa: <rule>` or fix)
#   - State the safer alternative
#
# Bypass
# ------
# A reviewer who concludes a rule is wrong in a specific spot can
# append `# noqa: <rule_name>` to the line. The hook honours that
# token. Project-wide opt-out: comment the rule out of `RULES` below.
#
# Exit codes
# ----------
#   0 — no findings (or only on files outside the scope)
#   2 — at least one finding; stderr is shown to Claude as a
#       system-reminder so the next turn can address it
#
# Invocation
# ----------
# Wired in `.claude/settings.json` as a PostToolUse hook against
# Edit | Write | MultiEdit. The Claude Code runtime passes a JSON
# event on stdin; we extract the modified file path(s) from it.

set -euo pipefail

# ---------------------------------------------------------------------------
# Read the tool event from stdin and pull out the file paths.
# Claude Code passes `tool_input.file_path` for single-file tools and
# `tool_input.edits[].file_path` style structures for some others.
# Be forgiving — if we can't parse, fall back to scanning everything
# in `backend/` and `frontend/` that's been touched recently is too
# expensive; better to do nothing and let the agents catch it.
# ---------------------------------------------------------------------------
INPUT="$(cat || true)"

# Single-file edit: tool_input.file_path
FILE="$(printf '%s' "$INPUT" | jq -r '.tool_input.file_path // empty' 2>/dev/null || true)"

# Multi-edit (MultiEdit): tool_input.file_path (same key — MultiEdit
# touches one file too). If empty, give up gracefully.
if [[ -z "$FILE" || "$FILE" == "null" ]]; then
  exit 0
fi

# Only scan files this project actually owns. Skip vendored / generated.
case "$FILE" in
  */node_modules/*|*/.venv/*|*/dist/*|*/build/*|*/.next/*|*/__pycache__/*) exit 0 ;;
  */alembic/versions/*) exit 0 ;;     # migrations have their own review path
  */tests/*|*/tests-e2e/*) ;;          # tests get scanned too — they can leak
  *.py|*.svelte|*.ts) ;;               # supported types
  *) exit 0 ;;
esac

# File may have been deleted (e.g., Edit that wipes a file). Bail.
[[ -f "$FILE" ]] || exit 0

# Accumulate findings into a buffer so we can emit them all at once.
FINDINGS=""

# Helper: register a finding. `$1` rule_name, `$2` line:col-ish, `$3` why,
# `$4` fix. Skips when the offending line carries a matching `noqa: rule`.
register() {
  local rule="$1" line="$2" why="$3" fix="$4"
  # If the offending line includes `# noqa: <rule>` or `// noqa: <rule>`
  # treat as opted-out. Use sed to fetch the matched line.
  local lineno="${line%%:*}"
  if [[ -n "$lineno" && "$lineno" =~ ^[0-9]+$ ]]; then
    local content
    content="$(sed -n "${lineno}p" "$FILE" 2>/dev/null || true)"
    if grep -qE "noqa:\s*${rule}\b" <<<"$content"; then
      return
    fi
  fi
  FINDINGS+="  [${rule}] ${FILE}:${line}
    why: ${why}
    fix: ${fix}

"
}

# Helper: grep -nE wrapper. Echoes line numbers (or nothing).
hits() {
  grep -nE "$1" "$FILE" 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# Rules
# Each rule is scoped to the file types where the pattern is meaningful.
# ---------------------------------------------------------------------------

# ===== Python-only rules ===================================================
if [[ "$FILE" == *.py ]]; then

# ----- RULE: bcrypt without sha256 prehash --------------------------------
# Why: bcrypt truncates input at 72 bytes. Two long passwords sharing the
# first 72 chars hash equal — an attacker who guesses the prefix only has
# to brute-force the suffix. The repo standard is bcrypt_sha256 (which
# pre-hashes with SHA-256 before bcrypt). Bug class: any new hash-context
# instantiation that picks the wrong scheme.
#
# Two shapes, because the hasher no longer goes through passlib: a fresh
# passlib CryptContext on the raw scheme (should passlib ever come back), and
# a direct `bcrypt.hashpw` / `bcrypt.checkpw` anywhere but the one module that
# owns the algorithm — which is now the likelier way to reintroduce the bug.
while IFS= read -r m; do
  ln="${m%%:*}"
  register "bcrypt-truncation" "$ln" \
    "CryptContext using raw 'bcrypt' silently truncates inputs at 72 bytes" \
    "Use 'from app.utils.passwords import pwd_context' instead of building a fresh CryptContext"
done < <(hits 'CryptContext\(.*schemes=\["bcrypt"\]' | grep -v 'bcrypt_sha256')

if [[ "$FILE" != */app/utils/passwords.py ]]; then
  while IFS= read -r m; do
    ln="${m%%:*}"
    register "bcrypt-truncation" "$ln" \
      "raw bcrypt.hashpw/checkpw truncates the password at 72 bytes" \
      "Use 'from app.utils.passwords import pwd_context' (or the awaitable hash_password / verify_password) — app/utils/passwords.py is the only module that may call bcrypt directly"
  done < <(hits 'bcrypt\.(hashpw|checkpw|kdf)\(')
fi

# ----- RULE: exception interpolated into log message ----------------------
# Why: SDKs sometimes raise with the partial PAN, masked tax id, or
# account number in the exception message. Interpolating `exc` itself
# (vs `exc.__class__.__name__`) pushes that string into the log sink.
# Project invariant #7. Bug class: any new logger call that surfaces a
# raw exception object.
while IFS= read -r m; do
  ln="${m%%:*}"
  register "exception-in-log" "$ln" \
    "logger.* %s …, exc — raw exception messages can include PII / PAN" \
    "Log exc.__class__.__name__ instead; let audit dispatch capture the rest"
done < <(hits 'logger\.(info|warning|error)\(.*%s.*,\s*exc\)')

# ----- RULE: deprecated/naive datetime call -------------------------------
# Why: `datetime.now()` without tz is naive — comparing to a UTC value
# silently fails ordering. `datetime.utcnow()` is deprecated as of 3.12.
# The repo uses `datetime.now(UTC)` everywhere. Bug class: anything
# building a tz-naive timestamp and persisting / comparing it.
while IFS= read -r m; do
  ln="${m%%:*}"
  register "naive-datetime" "$ln" \
    "datetime.utcnow() is deprecated and produces naive timestamps" \
    "Use datetime.now(UTC) — already imported in most modules"
done < <(hits 'datetime\.utcnow\(\)')

# ----- RULE: jwt.decode outside the central decoder -----------------------
# Why: `app.api.deps.decode_token` pins `algorithms=["HS256"]` and turns
# JWTError into a 401. A direct `jwt.decode` call elsewhere is liable
# to skip those guards — or worse, accept `algorithms=["HS256","none"]`.
# Bug class: anyone shortcutting around the decoder.
if [[ "$FILE" != *"app/api/deps.py" && "$FILE" != *"app/services/mfa.py" && "$FILE" != *"app/services/sso.py" ]]; then
  while IFS= read -r m; do
    ln="${m%%:*}"
    register "bypass-decode-token" "$ln" \
      "jwt.decode() called outside the central decode_token helper" \
      "from app.api.deps import decode_token — same shape, also enforces algorithm + 401-on-fail"
  done < <(hits '\bjwt\.decode\(')
fi

# ----- RULE: Float column on money-named field ----------------------------
# Why: project invariant #1. Float drifts on round-trip; you can't sum
# Floats and trust the cents. Bug class: any future model adding a
# money-named column with Float.
if [[ "$FILE" == */app/models/*.py ]]; then
  while IFS= read -r m; do
    ln="${m%%:*}"
    register "float-on-money" "$ln" \
      "Float column for currency drifts on round-trip and breaks sums" \
      "Use Numeric(15, 2) — see test_money_invariants.py for the contract"
  done < <(hits 'mapped_column\(Float' | grep -iE 'amount|total|price|subtotal|tax|rebate')
fi

# ----- RULE: tenant DB URL assembled by interpolation ---------------------
# Why: project invariant #4. A tenant session is only as isolated as the
# URL its engine was built from. `create_async_engine(f"{base}/feoh_{slug}")`
# reaches a tenant database with no resolved Organization row behind it, so
# the `get_tenant` JWT org-claim cross-check never runs at all — every guard
# in it sits upstream of a call that never happens. Bug class: any new
# module that builds its own tenant URL instead of asking for one.
#
# Line-based by design (like every rule here), so it catches the URL built
# inline at the call — positionally or as `url=`. A URL assembled on an
# earlier line and passed by name is beyond a grep;
# tests/test_tenant_engine_construction.py resolves that case and is the
# real backstop.
if [[ "$FILE" == */app/*.py && "$FILE" != */app/database.py ]]; then
  while IFS= read -r m; do
    ln="${m%%:*}"
    register "tenant-url-interpolation" "$ln" \
      "create_async_engine() URL built by interpolation / concatenation — no Organization row, no get_tenant cross-check" \
      "import make_tenant_url from app.tenant_url (dependency-free, safe on a dotenv-less Lambda path) or _make_tenant_url from app.database in the app — pass it org.db_name off a resolved row; see tests/test_tenant_engine_construction.py"
  done < <(hits "create_async_engine\(\s*(f[\"']|[^)]*(url\s*=\s*f[\"']|\+|%|\.format\())")
fi

# ----- RULE: hardcoded tenant database name -------------------------------
# Why: `Don't hardcode tenant DB names` (root CLAUDE.md). A literal like
# `feoh_acme` points a query at one specific tenant from code that has no
# business knowing which tenant it runs for, and it survives every runtime
# guard because it never asks a question. `app/config.py` owns the prefix.
# Bug class: a debugging shortcut or a copy-pasted fixture that ships.
if [[ "$FILE" == */app/*.py && "$FILE" != */app/config.py ]]; then
  # The public-API key brand (`feoh_live_<43 chars>`) shares the product prefix
  # with the tenant DB names but names no database. Exempted only in the two
  # modules that own it — a blanket filter would also swallow a real tenant
  # literal whose slug starts `live` (`feoh_livecorp`).
  DB_NAME_HITS="$(hits "[\"']feoh_[a-z0-9]+[a-z0-9_]*[\"']")"
  case "$FILE" in
    */app/services/api_keys.py|*/app/models/api_key.py)
      DB_NAME_HITS="$(printf '%s' "$DB_NAME_HITS" | grep -vE "[\"']feoh_live(_[A-Za-z0-9]+)?[\"']" || true)" ;;
  esac
  while IFS= read -r m; do
    [[ -n "$m" ]] || continue
    ln="${m%%:*}"
    register "hardcoded-tenant-db-name" "$ln" \
      "Tenant database name hardcoded as a literal — pins this code to one tenant" \
      "Resolve it from the Organization row (org.db_name); the feoh_ prefix lives in settings.tenant_db_prefix"
  done <<<"$DB_NAME_HITS"
fi

# ----- RULE: raw user input interpolated into S3 key / file path ----------
# Why: caught a real bug — `f"{org_id}/{invoice_id}/{file.filename}"`
# let a vendor land their upload under another tenant's prefix. Bug
# class: any new storage helper that interpolates a request-supplied
# filename without going through `_safe_filename`.
while IFS= read -r m; do
  ln="${m%%:*}"
  register "raw-filename-in-key" "$ln" \
    "Interpolating file.filename / request.filename into a path or S3 key is path-traversal-prone" \
    "Sanitise via storage._safe_filename() or strip path separators explicitly"
done < <(hits 'f"[^"]*\{[^}]*\.?filename[^}]*\}' | grep -v _safe_filename)

# ----- RULE: webhook endpoint without HMAC verification -------------------
# Why: project invariant #9. Every inbound webhook must verify the
# provider's signature. The shared helper is
# `services/webhook_security.verify_hmac_sha256`. Bug class: any new
# webhook handler that goes straight to body parsing.
if grep -qE '@router\.(post|put|patch)\([^)]*webhook' "$FILE"; then
  if ! grep -qE 'verify_hmac_sha256|parse_webhook' "$FILE"; then
    register "webhook-no-hmac" "1" \
      "Webhook endpoint defined but no verify_hmac_sha256 / parse_webhook call in the file" \
      "Import verify_hmac_sha256 from app.services.webhook_security and verify against the per-tenant signing secret"
  fi
  if ! grep -qE 'is_event_already_processed|provider_payment_id' "$FILE"; then
    register "webhook-no-dedup" "1" \
      "Webhook endpoint defined but no dedup-by-event-id check" \
      "Call is_event_already_processed(provider, event_id) before applying side effects"
  fi
fi

# ----- RULE: direct status assignment that bypasses transition_invoice ----
# Why: any handler that assigns `invoice.status = X` directly skips the
# audit dispatch in `transition_invoice`. SOC 2 evidence trail breaks.
# Project invariant #3. Bug class: future PRs that "just need to set
# the status" without realising the audit row matters.
if [[ "$FILE" == */app/api/*.py || "$FILE" == */app/services/*.py ]] \
   && [[ "$FILE" != */workflow_engine.py && "$FILE" != */payment_erp_sync.py && "$FILE" != */test_*.py ]]; then
  while IFS= read -r m; do
    ln="${m%%:*}"
    # Allow assignments that are in the same file as a transition_invoice call
    if grep -q 'transition_invoice' "$FILE"; then continue; fi
    register "direct-status-assignment" "$ln" \
      "Direct .status = … assignment skips the audit dispatch wired into transition_invoice" \
      "Use transition_invoice(...) from app.services.workflow_engine — it validates the transition AND writes the audit row"
  done < <(hits '\binvoice\.status\s*=\s*InvoiceStatus\.')
fi

# ----- RULE: response schema field named like a secret --------------------
# Why: PII / token leak via response shape. Project invariant #7. Bug
# class: a new schema that exposes hashed_password, secret (outside MFA
# enroll), private_key, signing_key, etc.
if [[ "$FILE" == */app/schemas/*.py ]]; then
  while IFS= read -r m; do
    ln="${m%%:*}"
    register "secret-in-response-schema" "$ln" \
      "Response schema field name suggests a credential or secret" \
      "Exclude from the response, or — if intentional (e.g. MFAEnrollStartResponse.secret) — add # noqa: secret-in-response-schema with rationale"
  done < <(hits '^\s+(hashed_password|signing_key|private_key|api_key|webhook_secret):' | grep -v noqa)
fi

# ----- RULE: hardcoded default for a long-lived secret --------------------
# Why: caught defaults like `secret_key = "change-me-in-production"`
# are intentional in dev config, but a `or "fallback"` chain in a
# secret-reading call site is a backdoor in prod. Bug class: any
# new `os.environ.get("X", "...") or "..."` style fallback for
# credentials.
while IFS= read -r m; do
  ln="${m%%:*}"
  register "hardcoded-secret-fallback" "$ln" \
    "os.environ.get with a non-empty fallback for a secret-shaped name is a backdoor" \
    "Pull from app.config.settings (sops-encrypted env) or raise — never fall back to a literal"
done < <(hits 'os\.environ\.get\(.*(secret|key|token|password).*,\s*"[^"]+' | grep -iE 'secret|key|token|password')

fi  # end Python-only rules

# ===== TypeScript-only rules ==============================================
if [[ "$FILE" == *.ts || "$FILE" == *.svelte ]]; then

# ----- RULE: raw fetch() instead of the api client ------------------------
# Why: frontend/src/lib/api.ts auto-adds the Authorization header and
# X-Tenant-Slug, plus catches 401 → /login. A direct fetch() in any
# component skips all of that. Bug class: future fetches that don't
# include tenant scoping or token rotation.
# `portalApi.ts` is the supplier-portal's parallel API client (separate token
# key) — it owns the Bearer + X-Tenant-Slug + 401-bounce just like api.ts, so
# it's excluded for the same reason api.ts is.
if [[ "$FILE" == */src/* && "$FILE" != */api.ts && "$FILE" != */portalApi.ts ]]; then
  while IFS= read -r m; do
    ln="${m%%:*}"
    register "raw-fetch-in-component" "$ln" \
      "Direct fetch() bypasses api.ts — no Bearer header, no X-Tenant-Slug, no 401-bounce" \
      "Use api.get/post/patch/delete from #lib/api"
  done < <(grep -nE '\bfetch\(' "$FILE" | grep -v 'api\.ts')
fi

# ----- RULE: console.log in committed source -----------------------------
# Why: client-side logs can include PII when the line interpolates an
# object. Bug class: forgotten debug logs that ship to prod.
while IFS= read -r m; do
  ln="${m%%:*}"
  register "console-log-debug" "$ln" \
    "console.log in committed source — prod console output can leak PII" \
    "Remove or replace with the project's logger if one exists; tests use vitest's expect"
done < <(grep -nE 'console\.log\(' "$FILE")

fi  # end TS-only rules

# ===== Universal rules ====================================================

# ----- RULE: TODO / FIXME without owner ----------------------------------
# Why: anonymous TODOs rot. The repo style is "TODO(owner): why".
# Bug class: low priority but accumulates.
while IFS= read -r m; do
  ln="${m%%:*}"
  register "todo-no-owner" "$ln" \
    "TODO / FIXME without an owner attribution" \
    "Format as TODO(jared): <reason> so the comment can be triaged later"
done < <(grep -nE '\b(TODO|FIXME|XXX)\b' "$FILE" | grep -vE 'TODO\(|FIXME\(|XXX\(')

# ---------------------------------------------------------------------------
# Emit findings (if any) and signal to Claude
# ---------------------------------------------------------------------------
if [[ -n "$FINDINGS" ]]; then
  {
    echo "Security-pattern hook flagged the change to ${FILE}:"
    echo
    printf '%s' "$FINDINGS"
    echo "Each rule has a 'why' (the bug class it prevents) and a 'fix' (the project's approved alternative)."
    echo "If the rule is wrong in context, append '# noqa: <rule_name>' to the line with a short rationale."
  } >&2
  # Exit 2 surfaces stderr to Claude as a system-reminder for the next turn.
  exit 2
fi

exit 0

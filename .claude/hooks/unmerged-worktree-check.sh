#!/usr/bin/env bash
# SessionStart hook: surface work that lives off `main`.
#
# With per-session git worktrees (`claude --worktree <name>`), each session
# commits on its own branch. Git won't let a worktree check out `main`, so that
# work only reaches `main` via an explicit merge from the primary checkout. If a
# worktree branch is forgotten, its commits sit stranded off `main`.
#
# This hook lists every local branch holding work not yet on `main` and prints a
# warning to stdout — which Claude Code injects as session context, so the next
# turn can surface it and offer to merge. Silent when everything is on `main`.
# Fail-open: any error exits 0 with no output, never wedging startup.
#
# ## Why `git branch --no-merged` is not enough
#
# That test is ancestry-based, and **this repo squash-merges every PR**. A
# squash rewrites the branch's commits into one new commit with a different
# SHA, so the branch tip never becomes an ancestor of `main` and the branch
# stays "unmerged" forever — even though every line of it is on `main`.
#
# Run bare, the hook therefore reported three branches (`feat/followups-round-17`,
# `feat/round-18-batch`, `feat/round-24-backlog-batch`) as 99 stranded commits.
# All three had merged months earlier as #361, #362 and #376. That is worse than
# useless: a warning that fires every session for work that is already landed
# trains the reader to dismiss it, and the one genuinely stranded branch goes
# with it.
#
# So each candidate gets a second, content-based test. `commit-tree` builds a
# throwaway commit holding the branch's whole tree parented on its merge-base —
# i.e. the branch's cumulative diff as a single patch, the same shape a squash
# merge produces — and `git cherry` asks whether `main` already contains an
# equivalent patch. `-` means yes (squash-merged; stay quiet), `+` means no
# (genuinely unmerged; report). The temporary commit is unreferenced and is
# collected by the next `git gc`.
set -u

repo="$(git rev-parse --show-toplevel 2>/dev/null)" || exit 0
[ -n "$repo" ] || exit 0

# Does `main` exist? If not, this repo doesn't use the convention — stay quiet.
git -C "$repo" rev-parse --verify --quiet main >/dev/null 2>&1 || exit 0

# Branches with commits not contained in `main` (main itself never lists here).
branches="$(git -C "$repo" branch --no-merged main --format='%(refname:short)' 2>/dev/null)" || exit 0
[ -n "$branches" ] || exit 0

current="$(git -C "$repo" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '')"

# True when `main` already holds an equivalent patch for the branch's cumulative
# diff — the squash-merge case ancestry cannot see.
squash_merged() {
	local br="$1" base squashed cherry
	base="$(git -C "$repo" merge-base main "$br" 2>/dev/null)" || return 1
	[ -n "$base" ] || return 1
	squashed="$(git -C "$repo" commit-tree "$br^{tree}" -p "$base" -m _ 2>/dev/null)" || return 1
	[ -n "$squashed" ] || return 1
	cherry="$(git -C "$repo" cherry main "$squashed" 2>/dev/null)" || return 1
	case "$cherry" in
		-*) return 0 ;;
		*) return 1 ;;
	esac
}

report=""
while IFS= read -r br; do
	[ -n "$br" ] || continue
	squash_merged "$br" && continue
	count="$(git -C "$repo" rev-list --count "main..$br" 2>/dev/null || echo '?')"
	wt="$(git -C "$repo" worktree list --porcelain 2>/dev/null \
		| awk -v b="refs/heads/$br" '
			/^worktree /{path=substr($0,10)}
			$0=="branch "b{print path}')"
	suffix=""
	[ "$br" = "$current" ] && suffix=" — the branch this session is ON, so probably in flight"
	if [ -n "$wt" ]; then
		report="${report}  - ${br} (${count} commit(s) off main) — worktree: ${wt}${suffix}
"
	else
		report="${report}  - ${br} (${count} commit(s) off main)${suffix}
"
	fi
done <<EOF
$branches
EOF

[ -n "$report" ] || exit 0

cat <<EOF
[unmerged-worktree-check] Work exists that is NOT on \`main\`:
${report}
Branches already squash-merged into \`main\` are filtered out, so everything
listed above really is absent from \`main\`.

To land it on main, from the PRIMARY checkout (the one with \`main\`):
  git merge <branch>        # fast-forwards if main hasn't moved; else a merge commit
  # or, for linear history: cd into the worktree, \`git rebase main\`, then \`git merge --ff-only <branch>\`
Surface this to the user and offer to consolidate it before it's forgotten.
EOF
exit 0

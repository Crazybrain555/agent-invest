#!/usr/bin/env bash
# Per-task agent worktrees for the handoff protocol (root AGENTS.md §3).
#
#   spawn <task-key>   create ../agent-invest-worktrees/<task-key> on branch task/<task-key>,
#                      copying gitignored files listed in .worktreeinclude (never docs/agent/)
#   list               show every worktree with its branch, dirty count, handoffs, and parked records
#   reap <task-key>    remove a worktree whose tree is clean, handoffs closed/absent, parked records
#                      gone, and runtime claims released
#
# Override the worktree root with AGENT_WT_ROOT.
set -euo pipefail

die() { echo "agent_worktree: $*" >&2; exit 1; }

PRIMARY="$(git worktree list --porcelain | awk 'NR==1 && $1=="worktree" {print substr($0, 10)}')"
[[ -n "$PRIMARY" ]] || die "not inside a git repository"
WT_ROOT="${AGENT_WT_ROOT:-$(dirname "$PRIMARY")/agent-invest-worktrees}"
KEY_RE='^[a-z0-9][a-z0-9._-]*$'
CLOSED_RE='^- [Ss]tate: `?(closed|completed)`?[.。]?[[:space:]]*$'

copy_worktreeinclude() {
  local dest=$1 pat f
  [[ -f "$PRIMARY/.worktreeinclude" ]] || return 0
  while IFS= read -r pat; do
    [[ -z "$pat" || "$pat" == \#* ]] && continue
    (
      cd "$PRIMARY"
      for f in $pat; do
        [[ -e "$f" ]] || continue
        case "$f" in docs/agent|docs/agent/*|*/docs/agent|*/docs/agent/*)
          echo "skip (handoff state is worktree-local): $f" >&2; continue;;
        esac
        if [[ -d "$f" ]]; then
          mkdir -p "$dest/$f"
          cp -R "$f/." "$dest/$f/"
        else
          mkdir -p "$dest/$(dirname "$f")"
          cp "$f" "$dest/$f"
        fi
      done
    )
  done < "$PRIMARY/.worktreeinclude"
}

show_parked() {
  local root=$1 indent=$2 d p any=0
  for d in "$root/docs/agent/parked" "$root"/services/*/docs/agent/parked \
           "$root"/packages/*/docs/agent/parked; do
    [[ -d "$d" ]] || continue
    for p in "$d"/*.md; do
      [[ -f "$p" ]] || continue
      any=1
      echo "${indent}parked: ${p#"$root"/} $(grep -m1 -iE '^- State:' "$p" | sed -E 's/^- [Ss]tate:[[:space:]]*//' || true)"
    done
  done
  return 0
}

show_task_state() {
  local wt=$1 h state state_line found=0
  for h in "$wt/docs/agent/HANDOFF.md" "$wt"/services/*/docs/agent/HANDOFF.md \
           "$wt"/packages/*/docs/agent/HANDOFF.md; do
    [[ -f "$h" ]] || continue
    found=1
    state_line="$(grep -m1 -iE '^- State:' "$h" || true)"
    state="$(printf '%s' "$state_line" | sed -E 's/^- [Ss]tate:[[:space:]]*//')"
    if [[ -z "$state" ]]; then
      echo "  handoff: ${h#"$wt"/} (no parseable State line — treated as gate-holding)"
    elif printf '%s\n' "$state_line" | grep -qiE "$CLOSED_RE"; then
      echo "  handoff: ${h#"$wt"/} closed ($state)"
    else
      echo "  handoff: ${h#"$wt"/} (gate) $state"
      grep -m1 -iE '^- Writer:' "$h" | sed 's/^/    /' || true
    fi
  done
  show_parked "$wt" "  "
  if [[ $found -eq 0 ]]; then echo "  (no handoff)"; fi
}

cmd="${1:-}"; shift || true
case "$cmd" in
  spawn)
    key="${1:?usage: agent_worktree.sh spawn <task-key>}"
    [[ "$key" =~ $KEY_RE ]] || die "task key must be kebab-case ([a-z0-9._-])"
    path="$WT_ROOT/$key"
    branch="task/$key"
    [[ -e "$path" ]] && die "worktree path already exists: $path"
    git -C "$PRIMARY" show-ref --verify --quiet "refs/heads/$branch" \
      && die "branch $branch already exists; pick a new task key or delete the branch first"
    mkdir -p "$WT_ROOT"
    git -C "$PRIMARY" worktree add -b "$branch" "$path" HEAD
    copy_worktreeinclude "$path"
    mkdir -p "$path/docs/agent"
    echo "worktree ready: $path (branch $branch, base $(git -C "$PRIMARY" rev-parse --short HEAD))"
    echo "primary checkout: $PRIMARY (runtime claims: docs/agent/RUNTIME.md there)"
    echo "primary parked guards — their premises stay protected in every worktree:"
    show_parked "$PRIMARY" "  " | grep . || echo "  (none)"
    echo "next: create $path/docs/agent/HANDOFF.md when a root AGENTS.md §3 trigger applies,"
    echo "      before the first task mutation"
    ;;
  list)
    git -C "$PRIMARY" worktree list --porcelain | awk '$1=="worktree" {print substr($0, 10)}' \
    | while IFS= read -r wt; do
        if [[ ! -d "$wt" ]] || ! git -C "$wt" rev-parse --git-dir >/dev/null 2>&1; then
          echo "== $wt (missing/prunable or unreadable; no live task state readable)"
          continue
        fi
        branch="$(git -C "$wt" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
        dirty="$(git -C "$wt" status --porcelain | wc -l | tr -d ' ')"
        echo "== $wt (branch $branch, $dirty dirty paths)"
        show_task_state "$wt"
      done
    ;;
  reap)
    key="${1:?usage: agent_worktree.sh reap <task-key>}"
    [[ "$key" =~ $KEY_RE ]] || die "task key must be kebab-case ([a-z0-9._-])"
    path="$WT_ROOT/$key"
    [[ -d "$path" ]] || die "no worktree at $path"
    [[ -z "$(git -C "$path" status --porcelain)" ]] || die "working tree dirty: $path — commit or clean first"
    for h in "$path/docs/agent/HANDOFF.md" "$path"/services/*/docs/agent/HANDOFF.md \
             "$path"/packages/*/docs/agent/HANDOFF.md; do
      [[ -f "$h" ]] || continue
      grep -m1 -iE '^- State:' "$h" | grep -qiE "$CLOSED_RE" \
        || die "unclosed handoff: $h — close it or transfer its gates first"
    done
    for d in "$path/docs/agent/parked" "$path"/services/*/docs/agent/parked \
             "$path"/packages/*/docs/agent/parked; do
      [[ -d "$d" ]] || continue
      find "$d" -name '*.md' | grep -q . \
        && die "parked records remain under $d — resolve or transfer them first"
    done
    runtime="$PRIMARY/docs/agent/RUNTIME.md"
    if [[ -f "$runtime" ]] && grep -qF -e "$path" -e "task/$key" -e "worktrees/$key" "$runtime"; then
      die "primary RUNTIME.md still references this worktree or task/$key — release runtime claims first"
    fi
    branch="$(git -C "$path" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "task/$key")"
    if ! git -C "$PRIMARY" merge-base --is-ancestor "$branch" HEAD 2>/dev/null; then
      echo "note: branch $branch is not merged into the primary HEAD; the branch is kept" >&2
    fi
    git -C "$PRIMARY" worktree remove "$path"
    echo "removed worktree $path (branch $branch kept; delete it manually after merge)"
    ;;
  *)
    sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
    ;;
esac

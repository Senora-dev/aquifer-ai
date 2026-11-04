#!/usr/bin/env bash
# Create private GitHub test repositories from the synthetic demo data (idempotent).
#
#   deploy/aws/scripts/seed-github-repos.sh [owner] [prefix]
#
# Creates <prefix>-payments, <prefix>-platform and <prefix>-legacy-billing under <owner>
# (default: the gh CLI user, prefix "aquifer-test"), pushes the demo files, and opens real
# issues/PRs matching examples/demo/repos/*/.aquifer/*.json (one PR merged, one left open).
# Prints the repository list as JSON on stdout. Delete them later with:
#   gh repo delete <owner>/<prefix>-payments --yes   (needs the delete_repo scope)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
OWNER="${1:-$(gh api user -q .login)}"
PREFIX="${2:-aquifer-test}"
DEMO="$ROOT/examples/demo/repos"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

log() { echo "[seed] $*" >&2; }

seed_repo() {
  local name="$1" full="$OWNER/$PREFIX-$1"
  if gh repo view "$full" >/dev/null 2>&1; then
    log "$full exists — leaving it unchanged"
    return
  fi
  log "creating private repo $full"
  gh repo create "$full" --private --description "Aquifer test data (synthetic)" >/dev/null
  local dir="$WORK/$name"
  mkdir -p "$dir"
  # Everything except the local-mode fixture metadata (real GitHub provides revisions/URLs).
  (cd "$DEMO/$name" && tar --exclude ./.aquifer -cf - .) | (cd "$dir" && tar -xf -)
  git -C "$dir" init -q -b main
  git -C "$dir" add -A
  git -C "$dir" -c user.name="aquifer-seed" -c user.email="aquifer-seed@users.noreply.github.com" \
    commit -qm "Synthetic Aquifer test data"
  git -C "$dir" remote add origin "https://github.com/$full.git"
  git -C "$dir" push -q -u origin main

  # Issues from the fixture file.
  local issues="$DEMO/$name/.aquifer/issues.json"
  if [[ -f "$issues" ]]; then
    python3 - "$issues" <<'PY' | while IFS=$'\t' read -r title body; do
import json, sys
for it in json.load(open(sys.argv[1])):
    print(it["title"] + "\t" + it["body"].replace("\n", "\\n"))
PY
      gh issue create -R "$full" --title "$title" --body "$(printf '%b' "$body")" >/dev/null
      log "  issue: $title"
    done
  fi

  # Pull requests from the fixture file: state "merged" is merged, others stay open.
  local pulls="$DEMO/$name/.aquifer/pulls.json"
  if [[ -f "$pulls" ]]; then
    python3 - "$pulls" <<'PY' | while IFS=$'\t' read -r num state title body; do
import json, sys
for it in json.load(open(sys.argv[1])):
    print(f'{it["number"]}\t{it["state"]}\t{it["title"]}\t' + it["body"].replace("\n", "\\n"))
PY
      local branch="change-$num"
      git -C "$dir" checkout -q -b "$branch" main
      mkdir -p "$dir/docs/changes"
      printf '# %s\n\n%b\n' "$title" "$body" > "$dir/docs/changes/$num.md"
      git -C "$dir" add -A
      git -C "$dir" -c user.name="aquifer-seed" -c user.email="aquifer-seed@users.noreply.github.com" \
        commit -qm "$title"
      git -C "$dir" push -q -u origin "$branch"
      gh pr create -R "$full" --base main --head "$branch" --title "$title" \
        --body "$(printf '%b' "$body")" >/dev/null
      if [[ "$state" == "merged" ]]; then
        gh pr merge -R "$full" "$branch" --squash --delete-branch >/dev/null
        log "  PR (merged): $title"
      else
        log "  PR (open): $title"
      fi
      git -C "$dir" checkout -q main
    done
  fi
}

for name in payments platform legacy-billing; do
  seed_repo "$name"
done

python3 -c "import json,sys; print(json.dumps([f'{sys.argv[1]}/{sys.argv[2]}-{n}' for n in ('payments','platform','legacy-billing')]))" "$OWNER" "$PREFIX"

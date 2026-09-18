#!/usr/bin/env bash
set -euo pipefail
[[ "$(git branch --show-current)" == main ]] || { echo "Run from main"; exit 1; }
[[ -z "$(git status --porcelain)" ]] || { echo "Working tree must be clean"; exit 1; }
commit=$(git rev-parse HEAD)
failed=0
for remote in origin github; do
  if git push "$remote" main:main; then
    observed=$(git ls-remote "$remote" refs/heads/main | awk '{print $1}')
    if [[ "$observed" == "$commit" ]]; then echo "PASS $remote $commit"; else echo "FAIL $remote verification"; failed=1; fi
  else
    echo "FAIL $remote push; successful remote is unchanged"; failed=1
  fi
done
exit "$failed"

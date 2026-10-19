#!/usr/bin/env bash
# Push local commits made by brain jobs, with rebase retries.
# Event files have unique paths and never conflict. Generated files can only
# conflict if two writers rebuilt them; "-X theirs" during a rebase keeps the
# commits being replayed; the rollup that follows rebuilds them from all events.
set -euo pipefail
branch="$(git rev-parse --abbrev-ref HEAD)"
git fetch -q origin "$branch"
if [ -z "$(git log "origin/$branch..HEAD" --oneline)" ]; then
  echo "nothing to push"; exit 0
fi
for i in 1 2 3 4 5; do
  if git push -q origin "HEAD:$branch"; then echo "pushed"; exit 0; fi
  echo "push rejected; rebasing (attempt $i)"
  git pull -q --rebase -X theirs origin "$branch"
  # Rebuild generated files so they include events that arrived from the remote.
  if command -v brain >/dev/null 2>&1; then brain rollup >/dev/null; fi
  sleep $((i * 3))
done
echo "push failed after retries" >&2
exit 1

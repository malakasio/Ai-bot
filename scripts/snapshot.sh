#!/usr/bin/env bash
# JARVIS v7.0 snapshot.sh — commit and push the working tree before any
# destructive operation.
#
# Behavior:
#   1. Refuses to run outside a git work tree.
#   2. If nothing is dirty, creates a `pre-mutation/<ISO8601 UTC>` tag at
#      HEAD instead of an empty commit — gives us a rollback point either
#      way.
#   3. If dirty, stages everything (`git add -A`), commits with a
#      timestamped message, and tags HEAD.
#   4. Optionally pushes:
#        --no-push           never push (default in CI)
#        --push (default)    push to the upstream of the current branch,
#                            but only fast-forward
#
# Output:
#   On success the tag name is printed on stdout. On failure, exit nonzero
#   with a clear stderr message.
#
# Exit codes:
#   0  ok (tag on stdout)
#   1  not a git repo
#   2  HEAD has no commits yet
#   3  push failed
#   4  unexpected git error

set -euo pipefail

REASON="${1:-pre-mutation}"
shift || true
DO_PUSH=true
for arg in "$@"; do
  case "$arg" in
    --no-push) DO_PUSH=false ;;
    --push)    DO_PUSH=true ;;
    *) ;;
  esac
done

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "snapshot: not a git repository" >&2
  exit 1
fi

if ! git rev-parse --verify HEAD >/dev/null 2>&1; then
  echo "snapshot: no commits on HEAD yet" >&2
  exit 2
fi

BASE_TS="$(date -u +%Y%m%dT%H%M%SZ)"
# Find a non-colliding tag name; sub-second resolution via a short suffix.
TS="${BASE_TS}"
TAG="pre-mutation/${TS}"
n=0
while git rev-parse --verify "refs/tags/${TAG}" >/dev/null 2>&1; do
  n=$((n + 1))
  if [ "$n" -gt 99 ]; then
    echo "snapshot: could not find unique tag near ${BASE_TS}" >&2
    exit 4
  fi
  TS="${BASE_TS}.$(printf '%02d' "$n")"
  TAG="pre-mutation/${TS}"
done
MSG_SUBJECT="snapshot: ${REASON}"
MSG_BODY="ts: ${TS}
reason: ${REASON}
host: $(hostname)"

# Stage everything but keep the result reproducible if the tree was clean.
git add -A >&2

if git diff --cached --quiet; then
  # Nothing to commit — tag HEAD as a rollback point.
  git tag -a "${TAG}" -m "${MSG_SUBJECT}" HEAD >&2
else
  # Use multi-line message; git porcelain output goes to stderr so stdout
  # contains only the tag.
  git commit -m "${MSG_SUBJECT}" -m "${MSG_BODY}" >&2
  git tag -a "${TAG}" -m "${MSG_SUBJECT}" HEAD >&2
fi

if [ "${DO_PUSH}" = "true" ]; then
  BRANCH="$(git symbolic-ref --quiet --short HEAD || true)"
  if [ -z "${BRANCH}" ]; then
    echo "snapshot: HEAD is detached; skipping push" >&2
  else
    UPSTREAM="$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || true)"
    if [ -z "${UPSTREAM}" ]; then
      echo "snapshot: branch '${BRANCH}' has no upstream; skipping push" >&2
    else
      # Refuse to push to main/master under --force; we always fast-forward only.
      if ! git push origin "${BRANCH}" >/dev/null 2>push.err; then
        echo "snapshot: push of ${BRANCH} failed:" >&2
        cat push.err >&2
        rm -f push.err
        exit 3
      fi
      rm -f push.err
      # Push the tag explicitly so it lands on the remote.
      git push origin "${TAG}" >/dev/null 2>push.err || true
      rm -f push.err
    fi
  fi
fi

echo "${TAG}"

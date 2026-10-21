#!/usr/bin/env bash
# Picks the functions the pipeline builds, pushes and releases.
#
#   scripts/changed-functions.sh [--all] [--environment dev|prod] [--output FILE]
#
# Writes one function name per line to FILE (default functions.txt) and
# prints a table with the decision for every function.
#
# A function is released when its code differs from what is live, not when
# the push happened to touch it. The pipeline tags every image it pushes
# with the commit it was built from, so the image behind the "live" alias
# says which commit is live, and git says whether functions/<name>/,
# shared/ or requirements.txt differ between that commit and HEAD. That
# also covers a push whose release failed, a redeploy of an older commit
# and a configuration release from the infrastructure repo (the image keeps
# its commit tag in ECR).
#
# Released regardless (always the safe side):
#   - --all or RELEASE_ALL=true: the "all" input of a manual run, and every
#     pull request build (nothing is released there, but every image must
#     still build)
#   - the live image carries no commit tag: the placeholder the
#     infrastructure pipeline pushes to a new repository, or an image pushed
#     by hand
#   - the live commit is not in this clone (shallow clone, rewritten history)
#   - the function, its alias or its image cannot be read
#
# Needs git with the full history (actions/checkout fetch-depth: 0), jq, and
# AWS credentials for the environment (lambda:GetFunction on the alias,
# ecr:DescribeImages). Environment: ENVIRONMENT (dev|prod), RELEASE_ALL
# (true|false), ALIAS (default live), PARALLEL (lookups at a time, 8).
set -euo pipefail

ENVIRONMENT="${ENVIRONMENT:-}"
RELEASE_ALL="${RELEASE_ALL:-false}"
OUTPUT="functions.txt"
ALIAS="${ALIAS:-live}"
PARALLEL="${PARALLEL:-8}"
# Paths outside functions/<name>/ that every image contains: a change here
# releases every function.
SHARED_PATHS="shared requirements.txt"

while [ $# -gt 0 ]; do
  case "$1" in
    --all) RELEASE_ALL=true; shift ;;
    --environment) ENVIRONMENT="$2"; shift 2 ;;
    --output) OUTPUT="$2"; shift 2 ;;
    -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
case "$RELEASE_ALL" in true|false) ;; *) echo "RELEASE_ALL must be true or false" >&2; exit 2 ;; esac
[ -d functions ] || { echo "run from the repository root (no functions/ here)" >&2; exit 2; }
if [ "$RELEASE_ALL" = false ]; then
  [ -n "$ENVIRONMENT" ] || { echo "missing --environment (or ENVIRONMENT)" >&2; exit 2; }
  git rev-parse --verify HEAD >/dev/null 2>&1 || { echo "not a git checkout" >&2; exit 2; }
fi

functions=()
for dir in functions/*/; do
  [ -f "$dir/Dockerfile" ] && functions+=("$(basename "$dir")")
done
[ "${#functions[@]}" -gt 0 ] || { echo "no function folders with a Dockerfile under functions/" >&2; exit 2; }

# One line per function: "<name> release|skip <live version> <reason>".
decide() {
  local fn="$1" resource live version image resolved tag base="" repo digest tags t rc
  if [ "$ENVIRONMENT" = prod ]; then resource="$fn"; else resource="$ENVIRONMENT-$fn"; fi
  if ! live=$(aws lambda get-function --function-name "$resource" --qualifier "$ALIAS" --output json 2>/dev/null); then
    echo "$fn release - no-live-alias"
    return 0
  fi
  version=$(jq -r '.Configuration.Version // "?"' <<<"$live")
  image=$(jq -r '.Code.ImageUri // ""' <<<"$live")
  resolved=$(jq -r '.Code.ResolvedImageUri // ""' <<<"$live")
  # The pipeline sets $LATEST to repo:<commit>, so a version it published
  # carries the commit as the tag. A version published by the infrastructure
  # pipeline (configuration release) or Terraform points at repo:latest: look
  # the digest up in ECR, where the same image also carries its commit tag.
  tag="${image##*:}"
  [[ "$tag" =~ ^[0-9a-f]{40}$ ]] && base="$tag"
  if [ -z "$base" ] && [[ "$resolved" == *@sha256:* ]]; then
    repo="${resolved%@*}"; repo="${repo#*/}"; digest="${resolved#*@}"
    tags=$(aws ecr describe-images --repository-name "$repo" --image-ids "imageDigest=$digest" \
      --query 'imageDetails[0].imageTags' --output text 2>/dev/null) || tags=""
    for t in $tags; do
      if [[ "$t" =~ ^[0-9a-f]{40}$ ]]; then base="$t"; break; fi
    done
  fi
  if [ -z "$base" ]; then
    echo "$fn release v$version live-image-has-no-commit-tag"
    return 0
  fi
  if ! git cat-file -e "$base^{commit}" 2>/dev/null; then
    echo "$fn release v$version live-commit-${base:0:7}-not-in-clone"
    return 0
  fi
  rc=0
  # shellcheck disable=SC2086
  git diff --quiet "$base" HEAD -- "functions/$fn" $SHARED_PATHS || rc=$?
  case "$rc" in
    0) echo "$fn skip v$version same-as-${base:0:7}" ;;
    1) echo "$fn release v$version changed-since-${base:0:7}" ;;
    *) echo "$fn release v$version git-diff-failed" ;;
  esac
}

decisions=$(mktemp)
trap 'rm -f "$decisions"' EXIT
if [ "$RELEASE_ALL" = true ]; then
  for fn in "${functions[@]}"; do echo "$fn release - all-requested"; done > "$decisions"
else
  export -f decide
  export ENVIRONMENT ALIAS SHARED_PATHS
  printf '%s\n' "${functions[@]}" \
    | xargs -r -P "$PARALLEL" -I{} bash -c 'set -uo pipefail; decide "$1"' _ {} > "$decisions"
fi
sort -o "$decisions" "$decisions"

awk '$2 == "release" { print $1 }' "$decisions" > "$OUTPUT"
table=$( { printf '%-28s %-8s %-6s %s\n' FUNCTION DECISION LIVE REASON
           awk '{ printf "%-28s %-8s %-6s %s\n", $1, $2, $3, $4 }' "$decisions"; } )
echo "$table"
released=$(wc -l < "$OUTPUT" | tr -d ' ')
echo "$released of ${#functions[@]} functions to build and release -> $OUTPUT"
if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  { echo "### Functions to release: $released of ${#functions[@]}"; echo; echo '```'; echo "$table"; echo '```'; } >> "$GITHUB_STEP_SUMMARY"
fi

#!/usr/bin/env bash
# Tests scripts/changed-functions.sh against a throwaway git repository and
# a fake aws command (no AWS access needed):
#
#   bash scripts/changed-functions-test.sh
set -euo pipefail

SCRIPT="$(cd "$(dirname "$0")" && pwd)/changed-functions.sh"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# --- a fake aws that answers from files: lambda/<function>.json, ecr/<repo>.txt
FAKE="$TMP/fake-aws"
mkdir -p "$FAKE/bin" "$FAKE/lambda" "$FAKE/ecr"
cat > "$FAKE/bin/aws" <<'FAKE_AWS'
#!/usr/bin/env bash
svc="$1"; op="$2"; shift 2
echo "$svc $op" >> "$FAKE_AWS/calls.log"
name=""; repo=""
while [ $# -gt 0 ]; do
  case "$1" in
    --function-name) name="$2"; shift 2 ;;
    --repository-name) repo="$2"; shift 2 ;;
    *) shift ;;
  esac
done
case "$svc $op" in
  "lambda get-function")
    [ -f "$FAKE_AWS/lambda/$name.json" ] && cat "$FAKE_AWS/lambda/$name.json" \
      || { echo "ResourceNotFoundException: $name" >&2; exit 254; } ;;
  "ecr describe-images")
    [ -f "$FAKE_AWS/ecr/$repo.txt" ] && cat "$FAKE_AWS/ecr/$repo.txt" \
      || { echo "ImageNotFoundException: $repo" >&2; exit 254; } ;;
  *) echo "fake aws: unexpected call: $svc $op" >&2; exit 1 ;;
esac
FAKE_AWS
chmod +x "$FAKE/bin/aws"
export PATH="$FAKE/bin:$PATH" FAKE_AWS="$FAKE"

REG="123456789012.dkr.ecr.eu-north-1.amazonaws.com"
live() { # <resource> <tag> <digest> <version>: what "live" points at
  printf '{"Configuration":{"Version":"%s"},"Code":{"ImageUri":"%s/%s:%s","ResolvedImageUri":"%s/%s@sha256:%s"}}\n' \
    "$4" "$REG" "$1" "$2" "$REG" "$1" "$3" > "$FAKE/lambda/$1.json"
}

# --- a repository with six functions and three commits
REPO="$TMP/repo"
mkdir -p "$REPO"
cd "$REPO"
export GIT_AUTHOR_NAME=test GIT_AUTHOR_EMAIL=test@example.com GIT_COMMITTER_NAME=test GIT_COMMITTER_EMAIL=test@example.com
git init -q
git checkout -q -b main 2>/dev/null || true
commit() { git add -A && git commit -q -m "$1" && git rev-parse HEAD; }
for fn in alpha bravo charlie delta echo foxtrot; do
  mkdir -p "functions/$fn"
  echo "FROM base" > "functions/$fn/Dockerfile"
  echo "v1" > "functions/$fn/app.py"
done
mkdir -p functions/not-a-function shared tests   # no Dockerfile: ignored
echo "v1" > shared/x.py
echo "boto3" > requirements.txt
echo "t1" > tests/test_x.py
C1=$(commit "first")
echo "v2" > functions/alpha/app.py
commit "change alpha" >/dev/null
echo "t2" > tests/test_x.py
C3=$(commit "tests only")

# --- helpers
failures=0
run() { # <environment> [RELEASE_ALL] [extra args...]
  local env="$1" all="${2:-false}"; shift; shift || true
  : > "$FAKE/calls.log"
  if ! ENVIRONMENT="$env" RELEASE_ALL="$all" bash "$SCRIPT" --output "$TMP/out.txt" "$@" > "$TMP/run.log" 2>&1; then
    echo "FAIL: script exited with $? for environment $env"; cat "$TMP/run.log"; exit 1
  fi
}
expect_list() { # <test name> <expected names, space separated>
  local got; got=$(tr '\n' ' ' < "$TMP/out.txt" | sed 's/ $//')
  if [ "$got" = "$2" ]; then echo "ok    $1"; else echo "FAIL  $1: expected [$2], got [$got]"; cat "$TMP/run.log"; failures=$((failures + 1)); fi
}
expect_line() { # <test name> <regex that one table line must match>
  if grep -Eq "$2" "$TMP/run.log"; then echo "ok    $1"; else echo "FAIL  $1: no line matches /$2/"; cat "$TMP/run.log"; failures=$((failures + 1)); fi
}

# --- production: live state per function
live alpha   "$C1"    aaaa 7            # changed since C1 -> release
live bravo   "$C1"    bbbb 7            # same code -> skip
live charlie latest   cccc 8            # configuration release: tag via ECR -> skip
printf 'latest\t%s\n' "$C1" > "$FAKE/ecr/charlie.txt"
live delta   latest   dddd 1            # placeholder image, no commit tag -> release
echo "latest" > "$FAKE/ecr/delta.txt"
                                        # echo: no function in Lambda -> release
live foxtrot 0000000000000000000000000000000000000000 ffff 3   # commit not in clone -> release

run prod
expect_list "prod: changed, placeholder, missing and unknown-commit functions" "alpha delta echo foxtrot"
expect_line "prod: alpha changed since the live commit"  "^alpha +release +v7 +changed-since-${C1:0:7}"
expect_line "prod: bravo unchanged"                      "^bravo +skip +v7 +same-as-${C1:0:7}"
expect_line "prod: charlie resolved through ECR tags"    "^charlie +skip +v8 +same-as-${C1:0:7}"
expect_line "prod: delta is the placeholder"             "^delta +release +v1 +live-image-has-no-commit-tag"
expect_line "prod: echo has no live alias"               "^echo +release +- +no-live-alias"
expect_line "prod: foxtrot's commit is unknown"          "^foxtrot +release +v3 +live-commit-0000000-not-in-clone"
expect_line "prod: summary line"                         "^4 of 6 functions to build and release"
if grep -q "not-a-function" "$TMP/run.log"; then echo "FAIL  folders without a Dockerfile are ignored"; failures=$((failures + 1)); else echo "ok    folders without a Dockerfile are ignored"; fi

# --- dev: resources are prefixed dev-; only alpha and bravo exist there
live dev-alpha "$C3" 1111 2
live dev-bravo "$C1" 2222 2
run dev
expect_list "dev: dev- prefixed lookups" "charlie delta echo foxtrot"
expect_line "dev: alpha live at HEAD"   "^alpha +skip +v2 +same-as-${C3:0:7}"
expect_line "dev: bravo unchanged"      "^bravo +skip +v2 +same-as-${C1:0:7}"

# --- a change under shared/ releases every function
echo "v2" > shared/x.py
commit "change shared" >/dev/null
run prod
expect_list "prod: shared/ change releases everything" "alpha bravo charlie delta echo foxtrot"
git revert --no-edit HEAD >/dev/null

# --- so does requirements.txt
echo "boto3==1" > requirements.txt
commit "change requirements" >/dev/null
run prod
expect_list "prod: requirements.txt change releases everything" "alpha bravo charlie delta echo foxtrot"
git revert --no-edit HEAD >/dev/null

# --- a new function folder is released (nothing live to compare with)
mkdir -p functions/golf && echo "FROM base" > functions/golf/Dockerfile
commit "add golf" >/dev/null
run prod
expect_list "prod: new function folder" "alpha delta echo foxtrot golf"

# --- --all and RELEASE_ALL=true release everything without asking AWS
run "" true
expect_list "RELEASE_ALL=true releases everything" "alpha bravo charlie delta echo foxtrot golf"
if [ -s "$FAKE/calls.log" ]; then echo "FAIL  RELEASE_ALL=true made AWS calls"; failures=$((failures + 1)); else echo "ok    RELEASE_ALL=true makes no AWS calls"; fi
run prod false --all
expect_list "--all releases everything" "alpha bravo charlie delta echo foxtrot golf"
expect_line "--all reason" "^bravo +release +- +all-requested"

# --- argument errors
if ENVIRONMENT="" RELEASE_ALL=false bash "$SCRIPT" >/dev/null 2>&1; then echo "FAIL  missing environment is an error"; failures=$((failures + 1)); else echo "ok    missing environment is an error"; fi
if ENVIRONMENT=prod RELEASE_ALL=maybe bash "$SCRIPT" >/dev/null 2>&1; then echo "FAIL  bad RELEASE_ALL is an error"; failures=$((failures + 1)); else echo "ok    bad RELEASE_ALL is an error"; fi

echo
if [ "$failures" = 0 ]; then echo "all changed-functions tests passed"; else echo "$failures test(s) FAILED"; exit 1; fi

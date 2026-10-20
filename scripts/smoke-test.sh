#!/usr/bin/env bash
# Post-deploy smoke test: is the environment answering right after a release?
#
#   API_BASE_URL=https://xxx.execute-api.eu-north-1.amazonaws.com \
#   SMOKE_HOST=test-restaurang-2.dev.booqy.se \
#   SMOKE_SITES="customer=https://... admin=https://... console=https://..." \
#   ALERT_TOPIC_ARN=arn:aws:sns:... ENVIRONMENT=dev SOURCE=application@1a2b3c4 \
#   bash ci/smoke-test.sh
#
# Checks (each retried for up to RETRY_SECONDS, so a CloudFront invalidation
# that is still propagating does not fail a good release):
#   - GET $API_BASE_URL/site-config?host=$SMOKE_HOST answers 200 with a
#     tenantId. With SMOKE_HOST empty (no permanent test restaurant yet) an
#     unknown host must answer the handler's 404 instead - that response is
#     only produced after the DynamoDB lookup, so it still proves API
#     Gateway -> Lambda -> table end to end.
#   - every site in SMOKE_SITES answers 200 at / with the app shell
#     (id="root") AND the script bundle index.html references is served -
#     an index.html whose hashed assets a bad deploy deleted fails here.
#
# Failure: the job fails and, when ALERT_TOPIC_ARN is set, one email goes to
# the alerts topic with every failed check. Success sends nothing.
# Exit code: 0 = all checks passed, 1 = at least one failed.
#
# Source of truth: infrastructure/ci/smoke-test.sh. The app repos carry a
# copy in scripts/ - keep them in sync.
set -uo pipefail

: "${API_BASE_URL:?set API_BASE_URL}"
: "${ENVIRONMENT:?set ENVIRONMENT}"
SMOKE_HOST="${SMOKE_HOST:-}"
SMOKE_SITES="${SMOKE_SITES:-}"
ALERT_TOPIC_ARN="${ALERT_TOPIC_ARN:-}"
SOURCE="${SOURCE:-smoke test}"
RETRY_SECONDS="${RETRY_SECONDS:-90}"
RETRY_INTERVAL="${RETRY_INTERVAL:-15}"
CURL=(curl -sS -L --max-time 20 -A "booqy-smoke-test/1" -H "Accept: */*")

failures=() report=()
body=$(mktemp)
trap 'rm -f "$body"' EXIT

# get URL -> prints the status (curl prints 000 when the request itself failed), body in $body
get() {
  "${CURL[@]}" -o "$body" -w "%{http_code}" "$1" 2>/dev/null
}

# check NAME URL EXPECTED_STATUS EXPECTED_TEXT - retries until it passes or
# RETRY_SECONDS are up. Returns 0 when it passed.
check() {
  local name="$1" url="$2" want="$3" text="$4" status="" waited=0
  while :; do
    status=$(get "$url")
    if [ "$status" = "$want" ] && grep -q -F -- "$text" "$body"; then
      report+=("OK      $name: $status $url")
      return 0
    fi
    if [ "$waited" -ge "$RETRY_SECONDS" ]; then break; fi
    sleep "$RETRY_INTERVAL"; waited=$((waited + RETRY_INTERVAL))
  done
  local why="got $status"
  [ "$status" = "$want" ] && why="$status but '$text' is not in the response"
  failures+=("$name: expected $want with '$text' - $why ($url)")
  report+=("FAILED  $name: $why $url")
  return 1
}

# --- API: site-config ---------------------------------------------------------
if [ -n "$SMOKE_HOST" ]; then
  check "site-config" "${API_BASE_URL%/}/site-config?host=$SMOKE_HOST" 200 '"tenantId"'
else
  echo "SMOKE_HOST is empty - checking that an unknown host gets the handler's 404"
  check "site-config" "${API_BASE_URL%/}/site-config?host=smoke-test.invalid" 404 'not found'
fi

# --- sites: shell + script bundle --------------------------------------------
for site in $SMOKE_SITES; do
  name="${site%%=*}" url="${site#*=}"
  check "$name" "${url%/}/" 200 'id="root"' || continue
  # first <script src="..."> in index.html (Vite: /assets/index-<hash>.js)
  src=$(grep -o -E '<script[^>]+src="[^"]+"' "$body" | head -1 | sed -E 's/.*src="([^"]+)".*/\1/')
  if [ -z "$src" ]; then
    failures+=("$name: index.html has no <script src> ($url)")
    report+=("FAILED  $name: index.html has no script bundle")
    continue
  fi
  case "$src" in
    http*) bundle="$src" ;;
    /*) bundle="${url%/}$src" ;;
    *) bundle="${url%/}/$src" ;;
  esac
  check "$name bundle" "$bundle" 200 '' || true
done

printf '%s\n' "${report[@]}"

if [ "${#failures[@]}" -eq 0 ]; then
  echo "smoke test passed ($ENVIRONMENT)"
  exit 0
fi

for f in "${failures[@]}"; do echo "::error::smoke test failed - $f"; done
if [ -n "$ALERT_TOPIC_ARN" ]; then
  run="${GITHUB_SERVER_URL:-}/${GITHUB_REPOSITORY:-}/actions/runs/${GITHUB_RUN_ID:-}"
  message=$(printf 'The smoke test after a release FAILED - the environment is not answering as expected. Roll back (docs/RELEASES.md in the infrastructure repo) or fix forward.\n\nEnvironment: %s\nSource:      %s\nRun:         %s\n\n%s\n' \
    "$ENVIRONMENT" "$SOURCE" "$run" "$(printf '%s\n' "${failures[@]}")")
  aws sns publish --topic-arn "$ALERT_TOPIC_ARN" \
    --subject "[$ENVIRONMENT] SMOKE TEST FAILED after $SOURCE" --message "$message" >/dev/null \
    || echo "::warning::could not send the smoke test email - does this pipeline's deploy role have sns:Publish on $ALERT_TOPIC_ARN?"
fi
exit 1

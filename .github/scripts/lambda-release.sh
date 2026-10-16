#!/usr/bin/env bash
# Blue/green release of ONE container-image Lambda through CodeDeploy.
#
#   ci/lambda-release.sh --function NAME --app CODEDEPLOY_APP --topic ALERT_TOPIC_ARN \
#                        --environment dev|prod [--image REPO_URI:TAG] [--source TEXT]
#                        [--notify always|failure]
#                        [--to-version N]
#
#   --image   new image to release (app pipelines). Omit to release whatever
#             is on $LATEST now (this repo's pipeline, after an apply changed
#             a function's configuration).
#   --notify  always (default): email every result. failure: email only
#             rollbacks/failures - for pipelines releasing many functions at
#             once, which send one summary email themselves.
#   --source  free text recorded on the version and in the email,
#             e.g. "application@1a2b3c4".
#   --to-version
#             manual rollback: point "live" straight at an existing version N
#             (no CodeDeploy - its alarms are usually firing when you roll
#             back and would undo it), email the result, and put N's image
#             back on :latest and $LATEST.
#
# Steps: put the image on $LATEST -> publish version N (green) -> CodeDeploy
# shifts the "live" alias from blue to N (canary in prod, all-at-once in
# dev), rolling back on its own if an alarm fires -> email the result.
# On failure the previous image is also put back on :latest and $LATEST, so
# nothing that invokes the function unqualified, and no later Terraform
# apply, picks up the broken image.
#
# Exit code: 0 = released (or nothing to release), 1 = failed / rolled back.
#
# COPY of infrastructure/ci/lambda-release.sh - change it there first, then
# copy it here unchanged.
set -euo pipefail

FN="" APP="" TOPIC="" ENVIRONMENT="" IMAGE="" SOURCE="" TO_VERSION="" NOTIFY="always"
ALIAS="live"
TIMEOUT_SECONDS="${RELEASE_TIMEOUT_SECONDS:-1800}"

while [ $# -gt 0 ]; do
  case "$1" in
    --function) FN="$2"; shift 2 ;;
    --app) APP="$2"; shift 2 ;;
    --topic) TOPIC="$2"; shift 2 ;;
    --environment) ENVIRONMENT="$2"; shift 2 ;;
    --image) IMAGE="$2"; shift 2 ;;
    --source) SOURCE="$2"; shift 2 ;;
    --to-version) TO_VERSION="$2"; shift 2 ;;
    --notify) NOTIFY="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[ -n "$FN" ] || { echo "missing --function" >&2; exit 2; }
[ -n "$APP" ] || { echo "missing --app" >&2; exit 2; }
[ -n "$TOPIC" ] || { echo "missing --topic" >&2; exit 2; }
[ -n "$ENVIRONMENT" ] || { echo "missing --environment" >&2; exit 2; }
case "$NOTIFY" in always|failure) ;; *) echo "--notify must be always or failure" >&2; exit 2 ;; esac

log() { echo "[$FN] $*"; }

notify() { # subject, body
  aws sns publish --topic-arn "$TOPIC" --subject "$1" --message "$2" >/dev/null \
    || log "WARNING: could not send the result email"
}

# Point :latest and $LATEST at an image that is already in the repository
# (by digest), so unqualified invokes and later Terraform applies use it.
restore_image() { # repo@sha256:digest
  local image="$1" repo_uri repo_name digest manifest
  repo_uri="${image%@*}"; repo_name="${repo_uri#*/}"; digest="${image#*@}"
  manifest=$(aws ecr batch-get-image --repository-name "$repo_name" --image-ids imageDigest="$digest" \
    --query 'images[0].imageManifest' --output text) || return 1
  if ! aws ecr put-image --repository-name "$repo_name" --image-tag latest --image-manifest "$manifest" >/dev/null 2>&1; then
    # already tagged :latest is fine; anything else is a real failure
    [ "$(aws ecr describe-images --repository-name "$repo_name" --image-ids imageTag=latest \
          --query 'imageDetails[0].imageDigest' --output text)" = "$digest" ] || return 1
  fi
  aws lambda update-function-code --function-name "$FN" --image-uri "$repo_uri:latest" >/dev/null || return 1
  aws lambda wait function-updated-v2 --function-name "$FN"
}

blue=$(aws lambda get-alias --function-name "$FN" --name "$ALIAS" --query FunctionVersion --output text)
blue_image=$(aws lambda get-function --function-name "$FN" --qualifier "$blue" --query Code.ResolvedImageUri --output text)
log "live -> version $blue ($blue_image)"

if [ -n "$TO_VERSION" ]; then
  green="$TO_VERSION"
  if [ "$green" = "$blue" ]; then
    log "live is already on version $green - nothing to roll back"
    exit 0
  fi
  green_image=$(aws lambda get-function --function-name "$FN" --qualifier "$green" --query Code.ResolvedImageUri --output text)
  log "rolling back: live $blue -> $green ($green_image)"
  aws lambda update-alias --function-name "$FN" --name "$ALIAS" --function-version "$green" \
    --routing-config '{"AdditionalVersionWeights": {}}' >/dev/null
  if restore_image "$green_image"; then restored="yes"; else restored="NO - put $green_image on :latest by hand"; fi
  log "ROLLED BACK - live -> version $green (image restored: $restored)"
  notify "[$ENVIRONMENT] ROLLBACK DONE: $FN is on v$green" \
"$FN ($ENVIRONMENT): the manual rollback finished - 100% of traffic is on version $green.

Live version: $green (was $blue)
Image:        $green_image
Restored:     $restored (:latest and \$LATEST)
Requested by: ${SOURCE:-n/a}"
  exit 0
else
  # 1. new code onto $LATEST
  if [ -n "$IMAGE" ]; then
    log "updating \$LATEST to $IMAGE"
    aws lambda update-function-code --function-name "$FN" --image-uri "$IMAGE" >/dev/null
    aws lambda wait function-updated-v2 --function-name "$FN"
  fi

  # 2. publish green. Lambda returns the existing latest version instead of
  #    a new one when nothing changed since it was published.
  green=$(aws lambda publish-version --function-name "$FN" \
    --description "${SOURCE:-release} $(date -u +%Y-%m-%dT%H:%MZ)" \
    --query Version --output text)
  aws lambda wait published-version-active --function-name "$FN" --qualifier "$green" 2>/dev/null || true

  if [ "$green" = "$blue" ]; then
    log "nothing to release - live is already on version $green"
    exit 0
  fi
fi
log "releasing version $blue -> $green"

# 3. CodeDeploy traffic shift
# (built with printf: jq 1.6 would print the required "version": 0.0 as 0)
appspec=$(printf '{"version":0.0,"Resources":[{"target":{"Type":"AWS::Lambda::Function","Properties":{"Name":"%s","Alias":"%s","CurrentVersion":"%s","TargetVersion":"%s"}}}]}' \
  "$FN" "$ALIAS" "$blue" "$green")
revision=$(jq -nc --arg c "$appspec" '{revisionType: "AppSpecContent", appSpecContent: {content: $c}}')

status="" reason="" waited=0 console="n/a"
if deployment=$(aws deploy create-deployment --application-name "$APP" --deployment-group-name "$FN" \
      --revision "$revision" --description "${SOURCE:-release}: v$blue -> v$green" \
      --query deploymentId --output text); then
  console="https://console.aws.amazon.com/codesuite/codedeploy/deployments/$deployment?region=${AWS_REGION:-eu-north-1}"
  log "deployment $deployment - $console"
  while :; do
    # a transient API error must not end the script before the email
    status=$(aws deploy get-deployment --deployment-id "$deployment" --query deploymentInfo.status --output text) \
      || status="Unknown"
    case "$status" in
      Succeeded|Failed|Stopped) break ;;
    esac
    if [ "$waited" -ge "$TIMEOUT_SECONDS" ]; then
      log "still $status after ${TIMEOUT_SECONDS}s - stopping it (CodeDeploy rolls back)"
      aws deploy stop-deployment --deployment-id "$deployment" --auto-rollback-enabled >/dev/null || true
      status="TimedOut"
      sleep 20 # let the automatic rollback move the alias back
      break
    fi
    sleep 15
    waited=$((waited + 15))
  done
  if [ "$status" != "Succeeded" ]; then
    reason=$(aws deploy get-deployment --deployment-id "$deployment" \
      --query 'deploymentInfo.errorInformation.message' --output text 2>/dev/null) || reason="n/a"
  fi
else
  # e.g. another release of this function is still running
  status="NotStarted"
  reason="CodeDeploy refused to start the deployment (is another release of this function still running?)"
fi

if [ "$status" = "Succeeded" ]; then
  log "SUCCESS - live -> version $green"
  [ "$NOTIFY" = always ] && \
  notify "[$ENVIRONMENT] SUCCESS: $FN released (v$green)" \
"$FN ($ENVIRONMENT): the release finished - 100% of traffic is on the new version.

Live version: $green (was $blue)
Image:        ${IMAGE:-unchanged - configuration release}
Source:       ${SOURCE:-n/a}
Deployment:   $console"
  exit 0
fi

# 4. failed: CodeDeploy has moved live back to blue (or never moved it).
#    Make $LATEST and the :latest tag match blue again too.
live_now=$(aws lambda get-alias --function-name "$FN" --name "$ALIAS" --query FunctionVersion --output text)
log "FAILED ($status): $reason - live is on version $live_now"

restored="not needed (no new image)"
if [ -n "$IMAGE" ]; then
  if restore_image "$blue_image"; then
    restored="yes - :latest and \$LATEST point at the previous image again"
  else
    restored="NO - put the previous image back by hand: $blue_image"
  fi
  log "restore previous image: $restored"
fi

notify "[$ENVIRONMENT] ROLLED BACK: $FN release failed" \
"$FN ($ENVIRONMENT): the release did NOT go live and traffic is back on the previous version.

Result:       $status
Reason:       $reason
Live version: $live_now (the new version was $green)
Image:        ${IMAGE:-unchanged - configuration release}
Restored:     $restored
Source:       ${SOURCE:-n/a}
Deployment:   $console"
exit 1

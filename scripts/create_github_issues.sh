#!/usr/bin/env bash
# Bulk-creates one GitHub issue per Lambda function via the gh CLI.
# Requires: `gh auth login` already done, and run from inside a clone
# of this repo (or pass --repo owner/name to every `gh issue create`
# call below if running from elsewhere).
set -euo pipefail

gh issue create \
  --title "Implement Lambda: manage-auth" \
  --label "lambda,trigger:api-gateway,auth:none" \
  --body-file "issue-bodies/manage-auth.md"

gh issue create \
  --title "Implement Lambda: create-location" \
  --label "lambda,trigger:api-gateway,auth:jwt" \
  --body-file "issue-bodies/create-location.md"

gh issue create \
  --title "Implement Lambda: get-location" \
  --label "lambda,trigger:api-gateway,auth:jwt" \
  --body-file "issue-bodies/get-location.md"

gh issue create \
  --title "Implement Lambda: manage-user" \
  --label "lambda,trigger:api-gateway,auth:jwt" \
  --body-file "issue-bodies/manage-user.md"

gh issue create \
  --title "Implement Lambda: manage-menu" \
  --label "lambda,trigger:api-gateway,auth:jwt" \
  --body-file "issue-bodies/manage-menu.md"

gh issue create \
  --title "Implement Lambda: get-menu" \
  --label "lambda,trigger:api-gateway,auth:none" \
  --body-file "issue-bodies/get-menu.md"

gh issue create \
  --title "Implement Lambda: pre-signed-url" \
  --label "lambda,trigger:api-gateway,auth:jwt" \
  --body-file "issue-bodies/pre-signed-url.md"

gh issue create \
  --title "Implement Lambda: manage-layout-element" \
  --label "lambda,trigger:api-gateway,auth:jwt" \
  --body-file "issue-bodies/manage-layout-element.md"

gh issue create \
  --title "Implement Lambda: publish-layout" \
  --label "lambda,trigger:api-gateway,auth:jwt" \
  --body-file "issue-bodies/publish-layout.md"

gh issue create \
  --title "Implement Lambda: list-layout-version" \
  --label "lambda,trigger:api-gateway,auth:jwt" \
  --body-file "issue-bodies/list-layout-version.md"

gh issue create \
  --title "Implement Lambda: activate-layout-version" \
  --label "lambda,trigger:api-gateway,auth:jwt" \
  --body-file "issue-bodies/activate-layout-version.md"

gh issue create \
  --title "Implement Lambda: expire-layout-version" \
  --label "lambda,trigger:eventbridge-scheduler,auth:n-a" \
  --body-file "issue-bodies/expire-layout-version.md"

gh issue create \
  --title "Implement Lambda: block-table" \
  --label "lambda,trigger:api-gateway,auth:jwt" \
  --body-file "issue-bodies/block-table.md"

gh issue create \
  --title "Implement Lambda: get-availability" \
  --label "lambda,trigger:api-gateway,auth:none" \
  --body-file "issue-bodies/get-availability.md"

gh issue create \
  --title "Implement Lambda: create-pending-reservation" \
  --label "lambda,trigger:api-gateway,auth:none" \
  --body-file "issue-bodies/create-pending-reservation.md"

gh issue create \
  --title "Implement Lambda: get-reservation" \
  --label "lambda,trigger:api-gateway,auth:jwt" \
  --body-file "issue-bodies/get-reservation.md"

gh issue create \
  --title "Implement Lambda: stripe-webhook" \
  --label "lambda,trigger:function-url,auth:stripe-signature" \
  --body-file "issue-bodies/stripe-webhook.md"

gh issue create \
  --title "Implement Lambda: no-show-check" \
  --label "lambda,trigger:eventbridge-scheduler,auth:n-a" \
  --body-file "issue-bodies/no-show-check.md"

gh issue create \
  --title "Implement Lambda: mark-arrived" \
  --label "lambda,trigger:api-gateway,auth:jwt" \
  --body-file "issue-bodies/mark-arrived.md"

gh issue create \
  --title "Implement Lambda: cancel-reservation" \
  --label "lambda,trigger:api-gateway,auth:none" \
  --body-file "issue-bodies/cancel-reservation.md"

gh issue create \
  --title "Implement Lambda: notification" \
  --label "lambda,trigger:dynamodb-stream,auth:n-a" \
  --body-file "issue-bodies/notification.md"

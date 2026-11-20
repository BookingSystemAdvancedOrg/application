# Amazon Connect A2A audio line

The AWS phone line for the voice gateway: Amazon Connect Customer hands a voice contact to the gateway as an
external agent-to-agent (A2A) collaborator with audio streaming. Connect owns the number, the PSTN leg, recording
and the flow; the gateway gets the caller's PCM over one secure WebSocket per contact and streams its own Polly
audio back. AWS-billed only (Connect, Lex, Fargate, ALB, Transcribe, Polly, Bedrock); no third-party telephony.

This replaces the Chime SDK Voice Connector idea in DESIGN.md 10: since the A2A audio feature, Connect can stream
audio both ways to an external agent.

Code: `voice_gateway/audio/connect_a2a.py` (the adapter, the endpoint, the wss server),
`python -m voice_gateway.server connect` (the entry point), tests in `tests/test_vg_audio_connect_a2a.py`.

Sources (read 2026-11-20; follow them, not this summary, when they change): Connect admin guide pages
`a2a-collaboration`, `a2a-how-it-works`, `a2a-external-server` (the `a2a-voice` link lands there),
`a2a-setup-external`, `a2a-quotas`, `a2a-observability`, `a2a-trace-enforcement`, `agentic-self-service`;
developer guide `a2a-developer-guide` (the protocol contract, A2A v1.0) and `a2a-sample-events`; Lex V2
`built-in-intent-qinconnect`.

## 1. What the adapter does on the wire

| Connect sends / situation | The gateway |
|---|---|
| WebSocket upgrade with `Authorization: Bearer <apiKey>` | constant-time compare with the key; 401 when missing, 403 when wrong; 503 while draining or above `max_calls` (Connect retries 503 once) |
| `A2A-Extensions: https://docs.aws.amazon.com/connect/a2a/ext/v1` | echoed in the upgrade response (activation) |
| `traceparent`, `tracestate` on the upgrade | traceId and parent span adopted for every span of the connection |
| `INIT_SESSION` (JSON-RPC `SendMessage`) | `INIT_SESSION_RESPONSE` with the same id, `statusCode "200"`; a direction proposed at 8 or 24 kHz is counter-proposed at 16 kHz (the gateway's rate, so Connect resamples); no audio config (chat, text streaming) or a format outside LINEAR_PCM 16-bit mono 8/16/24 kHz: JSON-RPC error with `google.rpc.ErrorInfo`, close 1008 |
| `CHANNEL_STATE READY` | output allowed from now on (nothing is sent before it; frames meanwhile are dropped and counted) |
| caller audio (`raw` part, `audio/lpcm`, one request per slice) | cut into 20 ms canonical frames on the call clock; gaps over 100 ms filled with silence (the adapter timing contract) |
| gateway speech (`play`) | `artifactUpdate` `AUDIO_RESPONSE_CHUNK`, one artifact per spoken response on its own task, never two open; first chunk `append: false`, then `append: true`; a gap inside a response is bridged with silence for up to 400 ms, then the response ends: `lastChunk: true`, the turn's one `TRACING_SPAN` artifact, `statusUpdate TASK_STATE_COMPLETED` |
| barge-in (`clear`) | `INTERRUPTION USER_AUDIO_INPUT` first (Connect flushes its buffer), no `lastChunk` for the cut artifact, then its trace and terminal status; the next response opens a fresh task and artifact |
| Connect's `INTERRUPTION` | the open response is closed as cut (our own VAD handles the caller's speech) |
| `DTMF_INPUT_COMPLETE`, text input (`<EMPTY_USER_INPUT>`), informational `ERROR` | logged (type and code only), otherwise ignored |
| `FRAGMENT` | reassembled by `fragmentId` in `partNumber` order, base64 decoded, dispatched; our own oversized trace frames are fragmented, audio never |
| unknown method | JSON-RPC error -32601 with ErrorInfo |
| orchestrator `hangup(reason)` | waits for the queued audio to play (at most 4 s), then `FINISH COMPLETE` (reason `error`: `ESCALATE`), on the open task's terminal status or as a message; nothing after it; socket closed by us 2 s later if Connect has not |
| orchestrator `transfer(number)` | after the TRANSFERRING sentence: `FINISH ESCALATE` with reason `transfer_to_restaurant` (no number in it); the flow's Escalate path connects the restaurant. Returns answered (the gateway cannot see staff answer). When Connect does not list `ESCALATE` in `supportedFinishTypes`: not answered, so the agent says TRANSFER_UNANSWERED |
| socket closes with 1005, 1006, 1012, 1013, 1014 or no close frame | the call keeps running for up to 8 s (silence in, output dropped); Connect's one reconnect (new INIT_SESSION, same `contextId`) re-attaches it and waits for READY again; a second drop or no reconnect ends the call |
| socket closes with any other code (1000 when the caller hangs up, 1011) | the call ends at once (the orchestrator sees `closed`) |
| SIGTERM (deployment) | new upgrades get 503, running calls get up to 110 s, the rest are closed with 1012 |

Traces: one `TRACING_SPAN` per task (A.10b shape): the root `invoke_agent booqy-voice-gateway` (SERVER) with the
caller's turn text as the input event (once per caller turn, on the first response that carries speech) and the
spoken sentences as the output event; a `chat sentence-<n>` span per played item (latency per segment) and an
`execute_tool <name>` span per agent tool call. The text goes to Connect only (Connect requires it and keeps it as
the contact's conversation history); it is never logged. `subscribeToTracingEvents: false` turns traces off.

Booking behaviour is unchanged: the same orchestrator, brain, guards, greeting and owner wording run over this
line as over LocalAdapter. The Connect flow must not play a greeting of its own: the gateway's greeting carries the
disclosure.

## 2. Setup (in this order; API only, every call SigV4 signed)

`REGION` is the Connect instance's Region (see the support case, 4.1), `ENV` dev or prod. Values in capitals are
placeholders. Nothing here is run by hand in prod: it becomes Terraform plus CI (section 3).

1. **Connect Customer tier.** The instance must be on the Connect Customer tier (A2A is not on Customer Basic).
   Change the instance's plan in the console or via the Connect pricing change; check the Swedish number is
   claimed on this instance (the open +46 cases).
2. **Connect assistant (AI agents).** Enable Connect AI agents for the instance (admin guide "Initial set-up for
   AI agents"): a qconnect assistant associated with the instance. Note `ASSISTANT_ID` and its ARN.
3. **API key secret.**
   1. A customer managed KMS key whose policy adds (next to the account's admin statements):
      `{"Sid": "AllowConnectToDecryptSecret", "Effect": "Allow", "Principal": {"Service": "connect.amazonaws.com"},
      "Action": "kms:Decrypt", "Resource": "*", "Condition": {"StringEquals": {"kms:ViaService":
      "secretsmanager.REGION.amazonaws.com"}}}`. The default `aws/secretsmanager` key does not work.
   2. A secret encrypted with that key, value: the API key (a long random string the owner generates; never in
      chat, a repo or Terraform state).
   3. Resource policy on the secret: `connect.amazonaws.com` may `secretsmanager:GetSecretValue` with
      `aws:SourceAccount = ACCOUNT_ID` and `aws:SourceArn = arn:aws:connect:REGION:ACCOUNT_ID:instance/INSTANCE_ID`.
4. **Register the gateway** as an AppIntegrations application (type cannot change later):
   ```
   aws appintegrations create-application --name booqy-voice-gateway-ENV --namespace booqy-vg-ENV \
     --application-source-config '{"ExternalUrlConfig":{"AccessUrl":"wss://voice-a2a.ENV.booqy.se"}}' \
     --application-type A2A_SERVER \
     --auth-config '{"AuthType":"API_KEY","CredentialProviderIdentifier":"SECRET_ARN"}' --region REGION
   ```
   Note `APPLICATION_ID` and `APPLICATION_ARN`. The AccessUrl must be public and TLS (`wss://`).
5. **Associate it with the instance:**
   `aws connect create-integration-association --instance-id INSTANCE_ID --integration-type APPLICATION
   --integration-arn APPLICATION_ARN --region REGION`.
6. **The orchestrating AI agent** (`aws qconnect create-ai-agent --cli-input-json file://create-ai-agent.json`):
   ```
   {
     "assistantId": "ASSISTANT_ID",
     "name": "booqy-voice-ENV",
     "type": "ORCHESTRATION",
     "visibilityStatus": "PUBLISHED",
     "configuration": {
       "orchestrationAIAgentConfiguration": {
         "connectInstanceArn": "arn:aws:connect:REGION:ACCOUNT_ID:instance/INSTANCE_ID",
         "multiAgentConfigurations": [{
           "handoffAgentConfiguration": {
             "agentTarget": {"applicationId": "APPLICATION_ID"},
             "instruction": {"instruction": "Hand every call to the Booqy voice gateway. It greets the caller, books, moves or cancels tables and returns COMPLETE when the call is done or ESCALATE when the caller should talk to the restaurant."},
             "audioStreamingEnabled": true,
             "immediateHandoff": true
           }
         }]
       }
     }
   }
   ```
   Rules the API validates: no `orchestrationAIPromptId`, no guardrail, the gateway is the only collaborator, and
   the only tools are the reserved Return to Control tools **Complete** (required) and **Escalate** (admin guide
   "Tools for orchestrator AI agents"; the default Complete and Escalate tools of the SelfServiceOrchestrator are
   the shape to copy).
7. **Security profile for the AI agent:**
   `aws connect update-security-profile --instance-id INSTANCE_ID --security-profile-id SP_ID
   --allowed-ai-agents '[{"Arn":"APPLICATION_ARN","Type":"THIRD_PARTY"}]'`, and the **Access Contact Control
   Panel** permission on the same profile. That permission is required for Booqy: without it Connect sends no
   `contactContext`, the gateway gets no dialed number, cannot find the restaurant and hangs up
   (`not_a_booqy_number`). Then `aws connect associate-security-profiles --entity-type AI_AGENT
   --entity-arn AGENT_ARN --security-profiles Id=SP_ID` twice: with the unversioned and the versioned agent ARN.
8. **Publish:** `aws qconnect create-ai-agent-version --assistant-id ASSISTANT_ID --ai-agent-id AGENT_ID`, and make
   it the assistant's self-service orchestration agent (Default AI Agent Configurations, Self Service row, or the
   matching qconnect API).
9. **Lex V2 bot** (same Region as the assistant): one locale using **Amazon Nova Sonic speech-to-speech** with a
   single `AMAZON.QInConnectIntent` pointing at the assistant ARN; no other intents. Nova Sonic has no Swedish, so
   the locale is a placeholder (for example en_US or en_GB): with audio streaming the bot does no speech for the
   collaborator, the gateway hears and speaks Swedish itself. Build a version and an alias, then associate the
   alias with the instance (`aws connect associate-bot`).
10. **Flow change** (the restaurant number's inbound flow):
    1. Set logging and recording as today; no greeting prompt (the gateway greets).
    2. **Get customer input** with the Lex bot alias, the placeholder locale, the Connect AI agent intent.
    3. After its Default output, **Check contact attributes** on Lex session attribute `Tool`:
       `Complete` -> Disconnect; `Escalate` -> a Lambda (or the existing phone agent config lookup) that finds the
       restaurant's transfer number by the dialed number, then **Transfer to phone number**; on no answer or
       failure, a short Swedish prompt in the owner's wording plus the callback, then Disconnect.
    4. The block's **Error** branch (the collaboration failed: no connection, 2 drops, READY timeout) -> today's
       Stage 1 phone agent flow, so a gateway outage never drops a guest.
11. **The wss endpoint** (Fargate behind an ALB):
    - ECS Fargate ARM64 service from the existing image, command
      `python -m voice_gateway.server connect --port 8443 --health-port 8080`, secret `VG_A2A_API_KEY` injected
      from the step 3 secret (or `VG_A2A_API_KEY_SECRET_ARN` read at start), `stopTimeout` 120 s (the server
      drains 110 s on SIGTERM), environment as the gateway today (`PHONE_AGENT_MODEL_ID`, `VG_CONFIG_JSON` from
      SSM, `VG_APP_ROOT`).
    - Internet-facing ALB (Connect reaches a public host), HTTPS listener 443 with an ACM certificate for
      `voice-a2a.ENV.booqy.se`, target group HTTP to port 8443 (`target_type ip`), health check `GET /health`
      on the traffic port (503 while draining), idle timeout 300 s, deregistration delay 120 s. TLS ends at the
      ALB; ALB to task is plain ws inside the VPC.
    - Security groups: ALB 443 from the internet (Connect publishes no source ranges, see 4.1); task 8443 and
      8080 from the ALB only. Optional AWS WAF on the ALB that rejects requests without an `Authorization`
      header (cheap noise filter; the key check stays in the gateway).

## 3. IAM and Terraform (list only; nothing is written yet)

IAM call-out (owner decision, no OIDC or deploy-role change in this step):

- **Setup principal** (CI Terraform, so the deploy role would need it: a deploy-role change, the owner's call):
  `secretsmanager:CreateSecret`, `secretsmanager:PutResourcePolicy`, `kms:CreateKey`, `kms:CreateAlias`,
  `kms:PutKeyPolicy`, `app-integrations:CreateApplication`, `connect:CreateIntegrationAssociation`,
  `connect:UpdateSecurityProfile`, `connect:AssociateSecurityProfiles`, `connect:AssociateBot`,
  `wisdom:CreateAIAgent`, `wisdom:UpdateAIAgent`, `wisdom:CreateAIAgentVersion` (Connect AI agents use the
  `wisdom` prefix), the Lex V2 bot actions, `connect` flow actions, plus ECS, ECR, ELB, ACM, Route 53 for the
  endpoint.
- **Resource policies:** the KMS key statement for `connect.amazonaws.com` (`kms:Decrypt` via Secrets Manager) and
  the secret policy for `connect.amazonaws.com` (`secretsmanager:GetSecretValue`, SourceAccount and SourceArn).
- **Task execution role:** `secretsmanager:GetSecretValue` on the API key secret and `kms:Decrypt` on its key (ECS
  injects `VG_A2A_API_KEY`), ECR pull, CloudWatch Logs.
- **Task role:** as DESIGN.md 10 (Transcribe and Polly in eu-central-1, Bedrock, the tool Lambdas, the PhoneAgent and
  tenant tables, SSM). The adapter itself needs no AWS permission (Connect calls the gateway). With
  `VG_A2A_API_KEY_SECRET_ARN` instead of injection: `secretsmanager:GetSecretValue` and `kms:Decrypt` here too.
- **Lex bot role:** the service-linked role gets `wisdom:CreateSession`, `GetAssistant`, `SendMessage`,
  `GetNextMessage` on the assistant when the intent is added (custom role: add them by hand).

Terraform resources (infrastructure repo):

- `aws_kms_key`, `aws_kms_alias` (key policy with the Connect statement)
- `aws_secretsmanager_secret` (kms_key_id), `aws_secretsmanager_secret_policy`; the value is put by the owner
  out of band (`put-secret-value`), never in state
- AppIntegrations `A2A_SERVER` application and the Connect `APPLICATION` integration association (check the AWS
  provider; otherwise the `awscc` provider through Cloud Control or a CLI step in CI)
- qconnect: assistant and its association, the orchestration AI agent and its version (`awscc_wisdom_*`, or CLI in
  CI)
- `aws_connect_security_profile` changes (allowed AI agents, Access Contact Control Panel) and the AI agent
  association (CLI in CI if the provider lacks it)
- Lex V2: `aws_lexv2models_bot`, `_bot_locale` (Nova Sonic placeholder locale), `_intent` (parent
  `AMAZON.QInConnectIntent`), `_bot_version`, a bot alias (`awscc_lex_bot_alias`), `aws_connect_bot_association`
- `aws_connect_contact_flow` (the flow of 2.10)
- `aws_ecr_repository`, `aws_ecs_cluster`, `aws_ecs_task_definition` (ARM64, 1 vCPU, 2 GB, ports 8443 and 8080,
  secrets, stopTimeout 120), `aws_ecs_service`, `aws_iam_role` x2 with policies, `aws_cloudwatch_log_group`
  (30 days), `aws_ssm_parameter` (config JSON)
- `aws_lb` (internet-facing, idle_timeout 300), `aws_lb_listener` (443, TLS policy), `aws_lb_target_group`
  (HTTP 8443, ip, health `/health`, deregistration_delay 120), `aws_acm_certificate` and validation,
  `aws_route53_record` (`voice-a2a.ENV.booqy.se`), `aws_security_group` x2, optional `aws_wafv2_web_acl` and
  association

## 4. Open questions for the AWS support case

1. **Region.** Is A2A collaboration with audio streaming available in eu-central-1 (and which other EU Regions)?
   Are Nova Sonic speech-to-speech Lex locales available there? Same Region needed for Connect, the assistant and
   the bot. Does Connect publish source IP ranges for A2A WebSocket connections (to narrow the ALB security group)?
2. **Swedish with a placeholder locale.** With `audioStreamingEnabled` the bot does no speech for the
   collaborator; is a Nova Sonic en_US or en_GB locale fine for a Swedish caller end to end? Does the locale
   affect Connect's silence detection, the interruption detection it sends, the contact transcript, or Contact
   Lens on this contact? Does a Swedish +46 number on the Customer tier work with this flow?
3. **Price.** What does a minute of an A2A audio collaboration cost (Connect Customer tier per minute, the A2A
   charge per mode, Lex and Nova Sonic charges while the bot only hands off), on top of telephony? Is there a
   charge per collaboration or per trace?
4. **Latency.** How much latency does Connect add each way (caller to our socket, our chunk to the caller), how
   large is its playout buffer, and what gap in an open audio artifact counts as an underrun? (We bridge gaps with
   silence for up to 400 ms and close the artifact after.) What Connect frame budget applies to one WebSocket
   frame?
5. **Protocol points the docs leave open:**
   - Should the agent answer each caller audio `SendMessage` (and CHANNEL_STATE, INTERRUPTION) with a JSON-RPC
     response echoing its id? We answer only INIT_SESSION, as the samples show.
   - Is an input-rate counter-proposal honoured (the sample only shows the output one)?
   - The exact `contactContext` field names for the customer and system addresses (we read
     `customerAddress`/`systemAddress` or `...Endpoint`, as a string or an object with `address`).
   - Is a terminal status without `lastChunk` correct after our INTERRUPTION?
   - Trace completeness: is an output event with empty content acceptable for a response that is only a short
     acknowledgement ("mm") or a filler? Is a tool span with the tool name and result status (not the arguments,
     which the gateway's brain does not expose) enough, or will the endpoint be disabled?
   - Does Connect still play audio already sent before FINISH, or should FINISH wait for playout (we wait)?
   - Is the FINISH reason available to the flow (Lex session attribute) or only to the orchestrating agent? Does an
     orchestrator with no prompt always call Escalate after FINISH ESCALATE and Complete after COMPLETE?
   - Quota: 5 concurrent outbound third-party connections per instance (adjustable). Booqy needs more (per Region,
     all restaurants together): request an increase together with this case.
   - After a reconnect lands on another task (deployment), is there any way to resume (history in the new
     INIT_SESSION)? Today the new task starts the call afresh.

## 5. Test plan

1. **Unit (done, CI):** `tests/test_vg_audio_connect_a2a.py` with a fake Connect client: auth 401/403/503, the
   extension echo, traceparent adoption, INIT_SESSION response and 16 kHz counter-proposal, refusals (no audio, bad
   format, wrong instance), READY gating and timeout (1011), caller audio to continuous 20 ms frames with silence
   concealment, 8 kHz resampling (numpy), one artifact per response with bridging and lastChunk, trace before the
   terminal status, barge-in (INTERRUPTION first, no lastChunk, fresh artifact after), FINISH COMPLETE and
   ESCALATE (nothing after it, socket closed later), transfer as ESCALATE and refused when not supported, one
   reconnect re-attaching the running call and a second drop ending it, normal close and reconnect timeout,
   FRAGMENT both ways, JSON-RPC errors, trace content (input once, output, tool and segment spans, Connect's
   parent), tracing off, a whole call through the orchestrator (greeting, turn, reply, FINISH), the real WebSocket
   server (auth, echo, health) and `server connect` drain. A protocol checker asserts Connect's rules on every frame
   we send.
2. **Local:** `VG_A2A_API_KEY=local-test python -m voice_gateway.server connect --fake --port 8443` and the fake
   client from the tests against it; then without `--fake` on the `booqy-dev` profile (real Transcribe, Polly,
   Bedrock, tool Lambdas faked as in the replay harness).
3. **Dev (after Terraform and the support answers):** `curl https://voice-a2a.dev.booqy.se/health`; an upgrade
   without and with a wrong key gives 401 and 403; then calls to the dev number:
   - greeting with the disclosure, first audio time after the caller stops (CloudWatch `EosToFirstAudio`, compare
     with REPLAY-RESULTS.md), no Connect greeting before ours;
   - a booking with read-back, the fee rule before a guarantee booking, a clear yes, the stored time equal to the
     read-back, one live hold;
   - barge-in over a long reply: the line goes quiet within 300 ms (`BargeInStopMs`), the read-back counts as not
     heard;
   - "Jag vill prata med restaurangen": TRANSFERRING, FINISH ESCALATE, the flow transfers; no answer: the flow's
     prompt and callback;
   - the caller hangs up mid-reply (call ends, call row finished as hangup), silence until STILL_THERE and the end;
   - a deployment during a call (ECS rolling update): drain, then 1012 and Connect's reconnect;
   - the gateway stopped: the flow's Error branch takes the call to the Stage 1 agent;
   - Connect's AI agent traces show input, output and spans per turn and the endpoint is not disabled after a day;
   - 5 concurrent calls (the quota), then the raised quota.
   Delete the test calls, transcripts and holds right after (dev test data rule).
4. **Prod:** only after dev passes and the owner decides; one restaurant first, Stage 1 stays as the Error branch.

---
name: gateway-restart-troubleshooting
description: Use when Hermes Agent gateway restarts, Telegram/Google Chat bots go silent, typing indicators get stuck, or a reply disappears after /restart or systemd restart. Diagnose logs first, preserve the final reply, and prefer externally scheduled delayed restarts over self-restarts.
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [hermes, gateway, telegram, systemd, troubleshooting, restart, reliability]
    related_skills: [hermes-agent, systematic-debugging]
---

# Hermes gateway restart troubleshooting

## Overview

Hermes gateway runs the messaging adapters that connect Hermes Agent to Telegram, Google Chat, Discord, Slack, and other platforms. Restarting it from inside the same chat that is currently being served is a special failure mode: the agent may finish the work, then kill the process before the final message is delivered, or leave a platform-level typing indicator running while the gateway is already in a half-restarted state.

The safest pattern is: diagnose from logs and systemd first; if a restart is needed, schedule it from outside the current gateway process with a short delay, let the current response send, then verify the gateway comes back and answers a safe test prompt.

## When to use

Use this skill when any of these happen:

- Telegram shows "typing..." after Hermes already finished the task.
- A gateway bot receives a message but sends no reply.
- The user asks to restart Hermes gateway, a profile gateway, Telegram bot, or Google Chat bot.
- `/restart` or `hermes gateway restart` appears to interrupt the final answer.
- A profile-specific service such as `hermes-gateway-<profile>.service` is silent.
- systemd shows the gateway as active, but the platform chat has no response.
- Logs mention send failures, polling reconnects, cancelled tasks, interrupted sessions, or repeated startup/shutdown cycles.

Do not use this skill for normal CLI-only Hermes model/tool issues unless the gateway is involved.

## Mental model

There are three separate states to check:

1. Platform state: Telegram/Google Chat may still show typing or may have delivered the inbound message.
2. Gateway process state: systemd may say the service is active even if the current conversation worker stalled.
3. Agent session state: the latest session may contain tool results but no final assistant message because the process restarted or was interrupted before delivery.

A green systemd status alone is not enough. Always correlate inbound message time, session transcript, and outbound send log.

## Fast triage

Set variables first:

```bash
# Default gateway
SERVICE=hermes-gateway.service
HOME_DIR=/root/.hermes

# Profile gateway example
PROFILE=<profile>
SERVICE=hermes-gateway-${PROFILE}.service
HOME_DIR=/root/.hermes/profiles/${PROFILE}
```

Check service status:

```bash
systemctl --user status "$SERVICE" --no-pager --lines=50
journalctl --user -u "$SERVICE" --since "30 min ago" --no-pager | tail -120
```

Check gateway logs:

```bash
grep -i "error\|failed\|exception\|traceback\|send\|typing\|connected\|polling" "$HOME_DIR/logs/gateway.log" | tail -120
```

Check whether there was an inbound message with no later outbound response:

```bash
grep -i "received\|message\|response ready\|sent\|send" "$HOME_DIR/logs/gateway.log" | tail -160
```

If session files are available, inspect the newest one:

```bash
python3 - <<'PY'
from pathlib import Path
home = Path('/root/.hermes')  # change for a profile if needed
sessions = sorted((home / 'sessions').glob('**/*'), key=lambda p: p.stat().st_mtime if p.is_file() else 0)
for p in reversed(sessions):
    if p.is_file():
        print(p)
        break
PY
```

Look for a pattern like: user message -> tool calls -> tool results -> no final assistant content. That usually means the agent did work but delivery was interrupted or the run stalled near the end.

## Safe restart pattern

Avoid restarting the gateway synchronously from inside the chat that is about to send the final message. Use a delayed external systemd job:

```bash
systemd-run --user --on-active=3s --unit=restart-${SERVICE%.service}-$(date +%s) \
  systemctl --user restart "$SERVICE"
```

Then send the user a short final message before the restart fires. After the restart, verify:

```bash
systemctl --user is-active "$SERVICE"
journalctl --user -u "$SERVICE" --since "2 min ago" --no-pager | tail -80
```

For a profile bot, run a safe CLI test outside Telegram:

```bash
timeout 120 hermes --profile "$PROFILE" chat -q '只回复 OK' --toolsets safe --quiet
```

For the default gateway:

```bash
timeout 120 hermes chat -q '只回复 OK' --toolsets safe --quiet
```

## Direct restart decision tree

Use direct restart only when the current chat does not need a final reply, or when you are operating from a separate shell/SSH session:

```bash
systemctl --user restart hermes-gateway.service
# or
systemctl --user restart hermes-gateway-<profile>.service
```

Use delayed external restart when:

- The user is talking to the same gateway you are restarting.
- Telegram typing is stuck but you still need to report what you did.
- The previous run completed tools but failed to deliver the final message.
- You are not sure whether `/restart` will kill the response path.

## Common root causes

### 1. Self-restart race

The agent calls a restart command from inside the gateway process. The gateway exits before the final answer is delivered.

Fix: use `systemd-run --user --on-active=3s ... restart ...` and only then send the final status.

### 2. Active service, silent worker

systemd says the gateway is active, but a specific conversation run stalled. This can happen after a long tool call, network timeout, model provider failure, or a cancelled run.

Fix: inspect latest session and logs. Restart gateway if the latest inbound message has no later outbound send. Verify with a safe `hermes chat -q '只回复 OK'` test.

### 3. Missing fallback providers

Some new profiles have `fallback_providers: []`. If the primary provider fails, the gateway may return transient failures or go silent instead of trying another provider.

Fix: add at least one fallback in the profile config:

```yaml
fallback_providers:
  - provider: nous
    model: anthropic/claude-sonnet-4.6
```

Use the provider/model actually configured for the installation. Do not assume OpenRouter is available.

### 4. Telegram typing indicator linger

Telegram typing is a best-effort heartbeat. It can linger after the real task is finished if the stop signal is missed, the process is interrupted, or Telegram/network retry is slow.

Fix: confirm the final message was delivered and check send errors. If typing persists and annoys the user, restart the gateway. Do not permanently disable typing unless the user explicitly wants that; many users prefer typing while the agent is genuinely working.

### 5. Secondary log noise hiding the real issue

A corrupted Kanban DB, an unrelated plugin warning, or a platform reconnect can appear scary but not block replies.

Fix: correlate timestamps. Treat secondary errors as non-root-cause unless they occur exactly between inbound receipt and missing outbound send.

## Watchdog bot pattern

For production-ish personal deployments, add an independent watchdog outside the LLM loop. This can be a tiny "bot watchdog" service or timer that monitors every Hermes agent profile gateway and issues restart commands when a profile is clearly unhealthy.

The important design constraint: the watchdog must not depend on the same agent gateway it is supervising, and it must not call an LLM. If an agent profile gateway is wedged, the watchdog still has to run and restart it.

A good watchdog checks:

- `systemctl --user is-active hermes-gateway*.service`
- every profile under `~/.hermes/profiles/*`
- each profile's `logs/gateway.log`
- latest platform inbound time
- latest outbound send/response-ready time
- whether active-but-silent duration exceeds a conservative threshold, such as 20-30 minutes

It should restart only when there is clear evidence of silence. It should not ask an LLM whether to restart. If there is no problem, it should print nothing.

Recommended implementation shape:

```text
systemd timer / cron / external bot process
  -> deterministic Python script
  -> scan hermes-gateway.service + hermes-gateway-<profile>.service
  -> detect inactive or active-but-silent gateway
  -> systemctl --user restart <bad service>
  -> optionally notify a separate Telegram/admin channel
```

Example restart action from the watchdog:

```bash
systemctl --user restart hermes-gateway-<profile>.service
```

This direct restart is safe because it is not being executed from inside the failing agent's own conversation path. The watchdog is outside that path.

Minimum watchdog rules:

1. Restart immediately if a gateway service is inactive/failed.
2. Restart active-but-silent gateways only if there is an inbound message newer than the last outbound send and the gap is older than the threshold.
3. Do not keep treating an old unanswered inbound as stuck after the gateway has restarted/reconnected. Track `Starting Hermes Gateway`, `Connected to Telegram`, or `Gateway running with...`; if that timestamp is newer than the last unanswered inbound, consider the old message already handled by the restart and stay quiet until a new inbound arrives.
4. Do not call blocking `systemctl --user restart <unit>` from the watchdog. It can hang until the gateway's full stop timeout if MCP/tool child processes are slow to die, causing the watchdog itself to fail. Use `systemctl --user restart --no-block <unit>` or schedule a transient `systemd-run --user --on-active=... systemctl --user restart <unit>` job.
5. Catch `subprocess.TimeoutExpired`/restart command failures so one bad service does not crash the entire watchdog scan.
6. Rate-limit restarts per service, for example no more than once every 10 minutes.
7. Exclude the watchdog's own service from scans.
8. Keep the script quiet when nothing is wrong, so timer/cron output does not spam logs or chats.

## Long-task checkpoint pattern

Gateway restarts are survivable only if long-running work does not live solely in chat context. For any long task, write a compact checkpoint file before and during execution.

Use a durable shared path:

```text
/root/.hermes/task-checkpoints/YYYYMMDD-HHMM-<task-slug>.md
```

For very short restart windows, a temporary file is acceptable, but durable checkpoints are better:

```text
/tmp/hermes-<task-slug>.checkpoint.md          # short-lived scratch
/root/.hermes/task-checkpoints/...md          # durable cross-profile resume
```

A checkpoint should include:

- original user request
- status: `active`, `blocked`, `completed`, or `cancelled`
- completed steps
- next exact pending step
- important files/URLs/IDs
- safe resume command or instruction

Minimal template:

```markdown
# <Task title>

- task_id: <timestamp-slug>
- owner_profile: <profile>
- updated_at: <ISO timestamp>
- status: active
- resume_summary: <2-5 sentences>

## Artifacts
- <paths / URLs / IDs>

## Completed
- [x] <step>

## Pending
- [ ] <next exact step>

## Resume Instructions
1. Read this checkpoint.
2. Verify artifact paths still exist.
3. Continue from the first unchecked pending item.
4. Update this checkpoint before responding.
```

On agent restart, context compaction, or profile handoff, the next agent should read the checkpoint first and continue from the first unchecked pending item. Do not make the user re-explain the task.

## Verification checklist

- [ ] Identified the exact gateway service name.
- [ ] Checked systemd status and journal.
- [ ] Checked gateway log around the user’s message timestamp.
- [ ] Confirmed whether an outbound response was sent after the inbound message.
- [ ] Used delayed external restart if operating from the same chat.
- [ ] Verified the service is active after restart.
- [ ] Ran a safe CLI test prompt for the default/profile Hermes instance.
- [ ] Confirmed the platform bot replies again.
- [ ] Did not disable typing permanently unless explicitly requested.
- [ ] If a recurring failure was found, added or checked an LLM-free watchdog bot/timer.
- [ ] For long tasks, wrote or updated a checkpoint under `/root/.hermes/task-checkpoints/` before any restart.
- [ ] After restart, resumed from checkpoint instead of asking the user to restate context.

## User-facing incident summary template

Use this when reporting back after fixing the issue:

```text
Root cause: the gateway restart happened from inside the same messaging path that needed to deliver the final reply. The task had effectively finished, but the gateway process restarted before the response path fully completed, so Telegram looked stuck/silent.

Fix: I switched to an external delayed restart pattern. The restart is scheduled through systemd a few seconds later, which gives Hermes time to send the final message before the gateway exits.

Verification: systemd shows the gateway active again, logs show the platform connected, and a safe test prompt returns OK.

Prevention: use delayed external restarts for gateway/profile restarts, and keep an independent watchdog for active-but-silent gateways.
```

## GitHub publishing notes

This skill is safe to publish publicly if you remove environment-specific paths, chat IDs, bot tokens, and private profile names. Generic paths such as `~/.hermes`, `/root/.hermes`, `hermes-gateway.service`, and `hermes-gateway-<profile>.service` are fine.

Before pushing:

```bash
python3 - <<'PY'
from pathlib import Path
import re, yaml
p = Path('SKILL.md')
content = p.read_text()
assert content.startswith('---')
end = content.find('\n---\n', 3)
assert end != -1
fm = yaml.safe_load(content[3:end])
assert fm['name']
assert fm['description'] and len(fm['description']) <= 1024
assert len(content) <= 100000
print('OK')
PY
```

Do a quick secret scan before commit:

```bash
grep -RInE "(TELEGRAM_BOT_TOKEN|API_KEY|SECRET|chat_id|token|Bearer )" . || true
```

# Hermes Gateway Recovery

Recovery playbooks, watchdog patterns, and checkpoint templates for Hermes Agent messaging gateways.

This repo documents how to diagnose and recover Hermes Agent gateway failures, especially cases where Telegram/Google Chat shows typing forever, a bot goes silent, or a restart interrupts the final response.

## What this covers

- **Self-restart race:** the gateway is restarted from inside the same chat path before the final reply is delivered.
- **Active-but-silent gateways:** systemd says `active`, but an inbound platform message has no later outbound response.
- **Typing indicators stuck forever:** the task finished or failed, but the platform typing heartbeat did not stop cleanly.
- **Profile gateways:** profile-specific services such as `hermes-gateway-<profile>.service`.
- **LLM-free watchdogs:** deterministic timers/scripts that restart clearly unhealthy gateways without asking an agent.
- **Long-task checkpoints:** durable recovery notes so work can resume after gateway restarts or context compaction.

## Core rule

Do not restart the messaging gateway synchronously from the same chat path that still needs to send the final answer.

Prefer an externally scheduled delayed restart:

```bash
SERVICE=hermes-gateway.service
systemd-run --user --on-active=3s --unit=restart-${SERVICE%.service}-$(date +%s)   systemctl --user restart "$SERVICE"
```

That gives Hermes time to deliver the final response before the gateway exits.

## Quick triage

```bash
SERVICE=hermes-gateway.service
HOME_DIR="$HOME/.hermes"

systemctl --user status "$SERVICE" --no-pager --lines=50
journalctl --user -u "$SERVICE" --since "30 min ago" --no-pager | tail -120

grep -i "error\|failed\|exception\|traceback\|send\|typing\|connected\|polling"   "$HOME_DIR/logs/gateway.log" | tail -120

grep -i "received\|message\|response ready\|sent\|send"   "$HOME_DIR/logs/gateway.log" | tail -160
```

For profile gateways:

```bash
PROFILE=chopin
SERVICE=hermes-gateway-${PROFILE}.service
HOME_DIR="$HOME/.hermes/profiles/${PROFILE}"
```

## Repo layout

```text
SKILL.md                              # Hermes skill / runbook
scripts/hermes_gateway_watchdog.py    # deterministic LLM-free watchdog
systemd/hermes-gateway-watchdog.service
systemd/hermes-gateway-watchdog.timer
templates/task-checkpoint.md          # long-task checkpoint template
docs/delayed-restart.md               # safe restart pattern
docs/active-but-silent.md             # how to detect active-but-silent gateways
```

## Install the watchdog timer

Copy the script somewhere stable:

```bash
mkdir -p ~/.local/bin
cp scripts/hermes_gateway_watchdog.py ~/.local/bin/hermes_gateway_watchdog.py
chmod +x ~/.local/bin/hermes_gateway_watchdog.py
```

Install the user systemd units:

```bash
mkdir -p ~/.config/systemd/user
cp systemd/hermes-gateway-watchdog.service ~/.config/systemd/user/
cp systemd/hermes-gateway-watchdog.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now hermes-gateway-watchdog.timer
```

Dry-run once:

```bash
~/.local/bin/hermes_gateway_watchdog.py --dry-run --verbose
```

The watchdog is intentionally quiet when nothing is wrong.

## Long-task checkpoint path

Use a durable checkpoint before long-running tasks or risky gateway operations:

```text
~/.hermes/task-checkpoints/YYYYMMDD-HHMM-<task-slug>.md
```

See [`templates/task-checkpoint.md`](templates/task-checkpoint.md).

## Skill usage

`SKILL.md` can be copied into a Hermes skills directory, for example:

```bash
mkdir -p ~/.hermes/skills/devops/gateway-restart-troubleshooting
cp SKILL.md ~/.hermes/skills/devops/gateway-restart-troubleshooting/SKILL.md
```

## GitHub description

> Recovery playbooks, watchdog patterns, and checkpoint templates for Hermes Agent messaging gateways.

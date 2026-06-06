# Active-but-silent gateways

`systemctl --user is-active hermes-gateway.service` can return `active` while the bot is still silent.

Always correlate three signals:

1. Systemd state: service exists and is active.
2. Gateway logs: inbound message timestamp.
3. Gateway logs: outbound `sent` / `response ready` timestamp after that inbound.

If inbound is newer than outbound and the gap is older than a conservative threshold, such as 20-30 minutes, the gateway is probably wedged.

Quick log check:

```bash
HOME_DIR="$HOME/.hermes"
grep -i "received\|message\|response ready\|sent\|send" "$HOME_DIR/logs/gateway.log" | tail -160
```

The watchdog in `scripts/hermes_gateway_watchdog.py` implements a conservative version of this rule and rate-limits restarts.

Two important false-positive/operational guards:

- If the last unanswered inbound message is older than the most recent `Starting Hermes Gateway`, `Connected to Telegram`, or `Gateway running with...` log line, treat it as already handled by a restart/reconnect and stay quiet until a newer inbound arrives.
- Queue restarts non-blockingly (`systemctl --user restart --no-block`) or via a transient delayed `systemd-run` job. A blocking restart can hang until the gateway's full stop timeout if MCP/browser/tool child processes are slow to exit, making the watchdog itself fail.

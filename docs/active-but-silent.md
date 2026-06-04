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

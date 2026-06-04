# Delayed restart pattern

Restarting a Hermes gateway from inside the same chat path can kill the process before the final answer is delivered.

Use an external delayed systemd job instead:

```bash
SERVICE=hermes-gateway.service
systemd-run --user --on-active=3s --unit=restart-${SERVICE%.service}-$(date +%s)   systemctl --user restart "$SERVICE"
```

Then send the user a short final message before the restart fires.

Verify after restart:

```bash
systemctl --user is-active "$SERVICE"
journalctl --user -u "$SERVICE" --since "2 min ago" --no-pager | tail -80
timeout 120 hermes chat -q 'Reply only OK' --toolsets safe --quiet
```

Use a profile service when needed:

```bash
PROFILE=chopin
SERVICE=hermes-gateway-${PROFILE}.service
systemd-run --user --on-active=3s --unit=restart-${SERVICE%.service}-$(date +%s)   systemctl --user restart "$SERVICE"
timeout 120 hermes --profile "$PROFILE" chat -q 'Reply only OK' --toolsets safe --quiet
```

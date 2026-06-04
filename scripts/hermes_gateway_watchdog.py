#!/usr/bin/env python3
"""LLM-free watchdog for Hermes gateway services.

Restarts user systemd Hermes gateway services when they are inactive/failed, or
when logs indicate an inbound platform message has not had a later outbound send
for a conservative threshold.

This script intentionally does not call Hermes or any LLM. It is designed to run
from cron or a systemd timer outside the gateway process it supervises.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

INBOUND_RE = re.compile(r"\b(received|incoming|inbound|message received|update received)\b", re.I)
OUTBOUND_RE = re.compile(r"\b(sent|send ok|response ready|delivered|outbound|message sent)\b", re.I)


@dataclass
class Gateway:
    service: str
    home: Path


def run(cmd: list[str], *, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=check)


def discover_gateways(base_home: Path) -> list[Gateway]:
    gateways = [Gateway("hermes-gateway.service", base_home)]
    profiles = base_home / "profiles"
    if profiles.exists():
        for p in sorted(profiles.iterdir()):
            if p.is_dir():
                gateways.append(Gateway(f"hermes-gateway-{p.name}.service", p))
    # Add any installed gateway services that may not map to profile dirs.
    proc = run(["systemctl", "--user", "list-units", "--all", "--type=service", "--no-legend", "hermes-gateway*.service"])
    for line in proc.stdout.splitlines():
        service = line.split(None, 1)[0] if line.split() else ""
        if not service or "watchdog" in service:
            continue
        if service not in {g.service for g in gateways}:
            gateways.append(Gateway(service, base_home))
    return gateways


def service_state(service: str) -> str:
    proc = run(["systemctl", "--user", "is-active", service])
    return proc.stdout.strip() or proc.stderr.strip() or "unknown"


def service_exists(service: str) -> bool:
    proc = run(["systemctl", "--user", "status", service])
    return proc.returncode in (0, 3)  # active=0, inactive/failed often=3


def parse_log_times(log_path: Path) -> tuple[float | None, float | None]:
    if not log_path.exists():
        return None, None
    latest_in = None
    latest_out = None
    # Use file mtime as fallback line timestamp. This is conservative: it only
    # detects recent active-but-silent logs, not historical precise timings.
    try:
        lines = log_path.read_text(errors="replace").splitlines()[-500:]
    except OSError:
        return None, None
    mtime = log_path.stat().st_mtime
    for line in lines:
        if INBOUND_RE.search(line):
            latest_in = mtime
        if OUTBOUND_RE.search(line):
            latest_out = mtime
    return latest_in, latest_out


def state_file_path(base_home: Path) -> Path:
    return base_home / "watchdog" / "gateway-watchdog-state.json"


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(path)


def should_rate_limit(state: dict, service: str, now: float, min_gap: int) -> bool:
    last = state.get(service, {}).get("last_restart_at", 0)
    return bool(last and now - float(last) < min_gap)


def restart(service: str, *, dry_run: bool) -> None:
    if dry_run:
        print(f"DRY-RUN restart {service}")
        return
    run(["systemctl", "--user", "restart", service], check=False)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", default=os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
    ap.add_argument("--silent-threshold-minutes", type=int, default=30)
    ap.add_argument("--min-restart-gap-minutes", type=int, default=10)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    base_home = Path(args.home).expanduser()
    now = time.time()
    threshold = args.silent_threshold_minutes * 60
    min_gap = args.min_restart_gap_minutes * 60
    state_path = state_file_path(base_home)
    state = load_state(state_path)

    for gw in discover_gateways(base_home):
        if not service_exists(gw.service):
            continue
        active = service_state(gw.service)
        reason = None
        if active in {"inactive", "failed", "deactivating", "activating"}:
            reason = f"service state is {active}"
        elif active == "active":
            latest_in, latest_out = parse_log_times(gw.home / "logs" / "gateway.log")
            if latest_in and (not latest_out or latest_in > latest_out) and now - latest_in > threshold:
                reason = "active but inbound appears newer than outbound beyond threshold"
            elif args.verbose:
                print(f"OK {gw.service}: active")
        elif args.verbose:
            print(f"SKIP {gw.service}: state={active}")

        if not reason:
            continue
        if should_rate_limit(state, gw.service, now, min_gap):
            if args.verbose:
                print(f"RATE-LIMIT {gw.service}: {reason}")
            continue
        print(f"RESTART {gw.service}: {reason}")
        restart(gw.service, dry_run=args.dry_run)
        state.setdefault(gw.service, {})["last_restart_at"] = now
        state[gw.service]["last_reason"] = reason

    if not args.dry_run:
        save_state(state_path, state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

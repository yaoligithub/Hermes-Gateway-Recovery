#!/usr/bin/env python3
"""Hermes gateway watchdog.

Layer 1: if a Hermes gateway systemd user service is not active/running,
schedule an external delayed restart and print an alert.

Layer 2: if a gateway is active but appears stuck after receiving a Telegram
message (recent inbound message with no later response-ready/send log), schedule
an external delayed restart and print an alert.

Normal healthy runs are silent; cron with no_agent=True only delivers non-empty
stdout.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

HOME = Path.home()
STATE_DIR = HOME / ".hermes" / "watchdog"
STATE_FILE = STATE_DIR / "hermes_gateway_watchdog_state.json"
HELPER = Path("/usr/local/bin/restart-hermes-gateway.sh")
COOLDOWN_SECONDS = 10 * 60
STUCK_AFTER_SECONDS = 25 * 60
UNIT_PATTERN = "hermes-gateway*.service"
DIRECT_RESTART = os.getenv("HERMES_WATCHDOG_DIRECT_RESTART", "0").lower() in {"1", "true", "yes", "on"}
NOTIFY_TELEGRAM = os.getenv("HERMES_WATCHDOG_NOTIFY_TELEGRAM", "0").lower() in {"1", "true", "yes", "on"}
TELEGRAM_CHAT_ID = os.getenv("HERMES_WATCHDOG_TELEGRAM_CHAT_ID", "")
TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d{3}")
INBOUND_RE = re.compile(r"inbound message: .*msg='(.*)'$")
RESPONSE_MARKERS = ("response ready:", "Sending response")


def run(cmd: list[str], timeout: int = 20) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        stdout = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        stderr = e.stderr.decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
        return subprocess.CompletedProcess(cmd, 124, stdout, stderr or f"timed out after {timeout}s")


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(STATE_FILE)


def list_gateway_units() -> list[str]:
    cp = run(["systemctl", "--user", "list-unit-files", UNIT_PATTERN, "--no-legend", "--no-pager"])
    units: list[str] = []
    if cp.returncode == 0:
        for line in cp.stdout.splitlines():
            parts = line.split()
            if parts and parts[0].startswith("hermes-gateway") and parts[0].endswith(".service") and "watchdog" not in parts[0]:
                units.append(parts[0])
    if not units:
        cp = run(["systemctl", "--user", "list-units", "--all", UNIT_PATTERN, "--no-legend", "--no-pager"])
        if cp.returncode == 0:
            for line in cp.stdout.splitlines():
                parts = line.split()
                if parts and parts[0].startswith("hermes-gateway") and parts[0].endswith(".service"):
                    units.append(parts[0])
    return sorted(set(units))


def show_unit(unit: str) -> dict[str, str]:
    cp = run([
        "systemctl", "--user", "show", unit,
        "-p", "LoadState", "-p", "ActiveState", "-p", "SubState",
        "-p", "Result", "-p", "MainPID", "--no-pager",
    ])
    props: dict[str, str] = {}
    if cp.returncode != 0:
        props["error"] = (cp.stderr or cp.stdout).strip()
        return props
    for line in cp.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            props[k] = v
    return props


def profile_from_unit(unit: str) -> str:
    if unit == "hermes-gateway.service":
        return "default"
    return unit.removeprefix("hermes-gateway-").removesuffix(".service")


def log_path_for_unit(unit: str) -> Path:
    profile = profile_from_unit(unit)
    if profile == "default":
        return HOME / ".hermes" / "logs" / "gateway.log"
    return HOME / ".hermes" / "profiles" / profile / "logs" / "gateway.log"


def read_dotenv_value(key: str) -> str:
    env_path = HOME / ".hermes" / ".env"
    if not env_path.exists():
        return ""
    for line in env_path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k == key:
            return v.strip().strip('"').strip("'")
    return ""


def notify_telegram(text: str) -> None:
    if not NOTIFY_TELEGRAM:
        return
    token = os.getenv("TELEGRAM_BOT_TOKEN") or read_dotenv_value("TELEGRAM_BOT_TOKEN")
    if not token or not TELEGRAM_CHAT_ID:
        return
    data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": text[:3500]}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
    try:
        urllib.request.urlopen(req, timeout=15).read()
    except Exception:
        # Do not fail watchdog/restart just because notification failed.
        pass


def schedule_restart(unit: str) -> tuple[bool, str]:
    if DIRECT_RESTART:
        # Queue the restart and return immediately. A plain `systemctl restart`
        # can block for the gateway's full TimeoutStopUSec when child MCP/tool
        # processes are slow to exit; that made the watchdog itself fail before
        # systemd finished the restart.
        cp = run(["systemctl", "--user", "restart", "--no-block", unit], timeout=10)
        detail = (cp.stdout or cp.stderr).strip() or "queued direct systemctl --user restart --no-block from watchdog timer"
        return cp.returncode == 0, detail

    profile = profile_from_unit(unit)
    if HELPER.exists() and os.access(HELPER, os.X_OK):
        cp = run([str(HELPER), profile], timeout=30)
        detail = (cp.stdout or cp.stderr).strip()
        return cp.returncode == 0, detail
    transient = f"restart-{unit}-{int(time.time())}"
    cp = run([
        "systemd-run", "--user", "--on-active=3s", "--unit", transient,
        "/usr/bin/systemctl", "--user", "restart", unit,
    ], timeout=30)
    detail = (cp.stdout or cp.stderr).strip()
    return cp.returncode == 0, detail


def parse_ts(line: str) -> datetime | None:
    m = TS_RE.match(line)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def tail_lines(path: Path, max_bytes: int = 300_000) -> list[str]:
    if not path.exists():
        return []
    with path.open("rb") as f:
        try:
            f.seek(-max_bytes, os.SEEK_END)
        except OSError:
            f.seek(0)
        data = f.read().decode(errors="replace")
    return data.splitlines()


def stuck_after_inbound(unit: str, now_dt: datetime) -> tuple[bool, str]:
    """Detect active-but-silent gateway after an inbound message.

    We intentionally use a conservative 25-minute threshold so legitimate long
    research/tool turns are not killed.
    """
    path = log_path_for_unit(unit)
    lines = tail_lines(path)
    if not lines:
        return False, f"no log file at {path}"

    last_inbound_ts: datetime | None = None
    last_inbound_msg = ""
    last_response_ts: datetime | None = None
    last_response_kind = ""
    last_gateway_start_ts: datetime | None = None

    for line in lines:
        ts = parse_ts(line)
        if ts is None:
            continue
        if "Starting Hermes Gateway" in line or "Connected to Telegram" in line or "Gateway running with" in line:
            last_gateway_start_ts = ts
        inbound = INBOUND_RE.search(line)
        if inbound:
            last_inbound_ts = ts
            last_inbound_msg = inbound.group(1)
        if any(marker in line for marker in RESPONSE_MARKERS):
            last_response_ts = ts
            last_response_kind = "response ready" if "response ready:" in line else "Sending response"

    if last_inbound_ts is None:
        return False, "no inbound seen in log tail"
    if last_response_ts is not None and last_response_ts >= last_inbound_ts:
        return False, f"last inbound answered by {last_response_kind} at {last_response_ts}"
    if last_gateway_start_ts is not None and last_gateway_start_ts >= last_inbound_ts:
        return False, f"last unanswered inbound predates gateway restart/reconnect at {last_gateway_start_ts}"

    age = (now_dt - last_inbound_ts).total_seconds()
    if age < STUCK_AFTER_SECONDS:
        return False, f"pending inbound age {int(age)}s < {STUCK_AFTER_SECONDS}s"

    msg_preview = last_inbound_msg[:120].replace("\n", " ")
    return True, f"last inbound at {last_inbound_ts} has no later response for {int(age//60)} min; msg='{msg_preview}'"


def alert_restart(alerts: list[str], state: dict, unit: str, key: str, title: str, detail: str, now: int) -> None:
    last = int(state.get(key, 0))
    if now - last < COOLDOWN_SECONDS:
        return
    state[key] = now
    ok, restart_detail = schedule_restart(unit)
    mode = "直接 systemctl 重启" if DIRECT_RESTART else "外部延迟重启"
    if ok:
        msg = f"🛠️ Hermes watchdog: {title}，已执行{mode}。\n{unit}: {detail}"
    else:
        msg = f"🚨 Hermes watchdog: {title}，但{mode}失败。\n{unit}: {detail}"
    if restart_detail:
        msg += f"\n{restart_detail[:500]}"
    alerts.append(msg)
    notify_telegram(msg)


def main() -> int:
    now = int(time.time())
    now_dt = datetime.now()
    state = load_state()
    units = list_gateway_units()
    alerts: list[str] = []

    if not units:
        key = "__no_units__"
        if now - int(state.get(key, 0)) >= COOLDOWN_SECONDS:
            state[key] = now
            alerts.append("⚠️ Hermes watchdog: 没找到任何 hermes-gateway*.service，请检查 systemd user services。")
        save_state(state)
        if alerts:
            print("\n".join(alerts))
        return 0

    for unit in units:
        props = show_unit(unit)
        load = props.get("LoadState", "unknown")
        active = props.get("ActiveState", "unknown")
        sub = props.get("SubState", "unknown")
        result = props.get("Result", "")
        pid = props.get("MainPID", "")

        if load == "not-found":
            continue
        healthy = (load == "loaded" and active == "active" and sub == "running")
        if not healthy:
            detail = f"LoadState={load}, ActiveState={active}, SubState={sub}, Result={result}, MainPID={pid}"
            alert_restart(alerts, state, unit, f"service:{unit}", "服务状态异常", detail, now)
            continue

        stuck, reason = stuck_after_inbound(unit, now_dt)
        if stuck:
            alert_restart(alerts, state, unit, f"stuck:{unit}", "活着但疑似卡住", reason, now)

    save_state(state)
    if alerts:
        print("\n\n".join(alerts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

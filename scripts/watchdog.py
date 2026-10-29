"""AIVA server watchdog: email a warning before the server falls over.

Every run checks three things:

  * disk    each path in WATCHDOG_DISK_PATHS is under WATCHDOG_DISK_PERCENT full
  * oracle  the backend's own DB user can log in and run a query
  * app     the backend answers GET /health

It runs from cron, outside the backend, on purpose. On 2026-09-12 the disk
filled up, Oracle refused every normal login (ORA-00257) and the backend died
at startup, so the backend's own error alerts - which also look their
recipients up in that same database - could not fire. This script needs
neither: recipients come from WATCHDOG_ALERT_EMAILS, and mail goes out through
the backend's normal SMTP / Zoho settings.

Each problem is emailed once when it starts, again every WATCHDOG_REPEAT_HOURS
while it lasts, and followed by an all-clear once it is fixed. The oracle and
app checks must fail two runs in a row first, so a restart does not page anyone.

    python scripts/watchdog.py               # one run - what cron calls
    python scripts/watchdog.py --check       # print results only: no email, no state
    python scripts/watchdog.py --test-email  # send a test email to prove delivery

Install on the server (every 5 minutes; flock stops runs piling up if one hangs):

    crontab -e
    */5 * * * * cd /path/to/AIVA-V2 && flock -n /tmp/aiva-watchdog.lock /path/to/python scripts/watchdog.py >> $HOME/aiva-watchdog.log 2>&1

Settings, read from the same .env files as the backend:

    WATCHDOG_ALERT_EMAILS   comma-separated recipients (required)
    WATCHDOG_DISK_PERCENT   alert at or above this % full (default 80)
    WATCHDOG_DISK_PATHS     comma-separated mount points (default /)
    WATCHDOG_HEALTH_URL     default http://127.0.0.1:<BACKEND_PORT>/health; "off" skips it
    WATCHDOG_REPEAT_HOURS   reminder interval while a problem lasts (default 6)
    WATCHDOG_STATE_FILE     default /dev/shm/aiva-watchdog.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

_AIVA_V2 = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_AIVA_V2))

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from backend.config import Settings, get_settings
from backend.services.email import EmailMessage, get_mail_sender
from backend.services.email.templates import build_message

_log = logging.getLogger("aiva.watchdog")

# The oracle and app checks must fail this many runs in a row before alerting,
# so a restart or a deploy does not send an email.
_CONFIRM_RUNS = 2


def _default_state_file() -> str:
    # /dev/shm lives in RAM, so the state still saves when the disk itself is
    # full - otherwise a full disk would re-send its alert on every run. It is
    # lost on reboot, which costs at most one repeated email.
    shm = Path("/dev/shm")
    base = shm if shm.is_dir() else Path(tempfile.gettempdir())
    return str(base / "aiva-watchdog.json")


class WatchdogSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="WATCHDOG_",
        env_file=(_AIVA_V2 / ".env", _AIVA_V2 / "backend" / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    alert_emails: str = ""
    disk_percent: float = 80.0
    disk_paths: str = "/"
    health_url: str = ""
    repeat_hours: float = 6.0
    state_file: str = Field(default_factory=_default_state_file)

    @property
    def recipients(self) -> list[str]:
        return [e.strip() for e in self.alert_emails.split(",") if e.strip()]


@dataclass
class CheckResult:
    key: str  # stable id in the state file
    label: str  # short name, used in the subject line
    ok: bool
    summary: str
    target: str = ""  # what was checked (path, DSN, URL)
    hint: str = ""  # first thing to try when it fails
    confirm_runs: int = 1


def _first_line(exc: BaseException) -> str:
    lines = str(exc).strip().splitlines()
    return (lines[0] if lines else type(exc).__name__)[:300]


def _gb(n: int) -> str:
    return f"{n / 1024**3:.1f} GB"


def _when(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


# ---- checks ----


def check_disk(path: str, limit: float) -> CheckResult:
    key, label = f"disk:{path}", f"Disk {path}"
    hint = (
        "Find what grew: sudo du -xh -d1 / 2>/dev/null | sort -h | tail\n"
        "Free space before it reaches 100% - at 100% Oracle refuses every login "
        "and AIVA goes down."
    )
    try:
        du = shutil.disk_usage(path)
    except OSError as exc:
        return CheckResult(key, label, False, f"could not read usage: {exc}", path, hint)
    # Same formula as `df`: blocks reserved for root count as neither used nor free.
    pct = 100 * du.used / (du.used + du.free) if du.used + du.free else 0.0
    summary = f"{pct:.1f}% full, {_gb(du.free)} free of {_gb(du.total)}"
    return CheckResult(key, label, pct < limit, summary, path, hint)


def check_oracle(settings: Settings, timeout: float = 20.0) -> CheckResult:
    """Log in as the backend's own DB user and run a query.

    Deliberately not a SYSDBA login: SYSDBA keeps working through ORA-00257,
    so it would report healthy during exactly the outage this exists to catch.
    """
    import oracledb

    params: dict[str, object] = {
        "user": settings.oracle_user,
        "password": settings.oracle_password,
        "dsn": settings.oracle_dsn,
        "tcp_connect_timeout": 10,
    }
    if settings.oracle_wallet_dir:
        params["config_dir"] = settings.oracle_wallet_dir
        params["wallet_location"] = settings.oracle_wallet_dir
        if settings.oracle_wallet_password:
            params["wallet_password"] = settings.oracle_wallet_password

    outcome: dict[str, object] = {}

    def attempt() -> None:
        try:
            with oracledb.connect(**params) as conn:
                conn.call_timeout = int(timeout * 1000)
                with conn.cursor() as cur:
                    cur.execute("SELECT 1 FROM dual")
                    cur.fetchone()
            outcome["ok"] = True
        except Exception as exc:
            outcome["error"] = exc

    # A wedged database can stall inside the login itself, so bound the whole
    # attempt here rather than trusting the driver's timeouts alone. Daemon
    # thread: if it is still stuck, it dies with the script.
    start = time.perf_counter()
    worker = threading.Thread(target=attempt, daemon=True)
    worker.start()
    worker.join(timeout)
    ms = round((time.perf_counter() - start) * 1000)

    label, target = "Oracle database", settings.oracle_dsn
    hint = (
        "Check the database container and its log:\n"
        "  sudo docker ps\n"
        "  sudo docker logs --tail 50 oracle23ai\n"
        "ORA-00257 or ORA-19502 means the disk is full."
    )
    if worker.is_alive():
        summary = f"no answer within {timeout:.0f}s"
    elif "error" in outcome:
        summary = _first_line(outcome["error"])  # type: ignore[arg-type]
    else:
        return CheckResult("oracle", label, True, f"answered in {ms} ms", target, hint, _CONFIRM_RUNS)
    return CheckResult("oracle", label, False, summary, target, hint, _CONFIRM_RUNS)


def check_app(url: str, timeout: float = 10.0) -> CheckResult:
    label = "AIVA app"
    hint = (
        "AIVA is not answering at this address. Check its tmux session (tmux attach -t aiva): "
        "the backend - and the web UI, if this is the UI's address - must be running."
    )
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            code = resp.status
    except urllib.error.HTTPError as exc:
        code = exc.code
    except Exception as exc:
        return CheckResult("app", label, False, f"not reachable: {_first_line(exc)}", url, hint, _CONFIRM_RUNS)
    return CheckResult("app", label, code < 400, f"HTTP {code}", url, hint, _CONFIRM_RUNS)


def _health_url(ws: WatchdogSettings, settings: Settings) -> str | None:
    url = ws.health_url.strip()
    if url.lower() == "off":
        return None
    return url or f"http://127.0.0.1:{settings.backend_port}/health"


def run_checks(ws: WatchdogSettings, settings: Settings) -> list[CheckResult]:
    results = [check_disk(p.strip(), ws.disk_percent) for p in ws.disk_paths.split(",") if p.strip()]
    results.append(check_oracle(settings))
    url = _health_url(ws, settings)
    if url:
        results.append(check_app(url))
    return results


# ---- alert decisions ----


def decide(
    results: list[CheckResult], state: dict, now: float, repeat_seconds: float
) -> tuple[list[CheckResult], list[CheckResult], dict]:
    """Work out what to email. Returns (problems due an email, recoveries, new state).

    Healthy checks keep no state. A failing check records how many runs in a
    row it has failed, since when, and when it was last emailed. ``settle``
    stamps ``alerted_at`` only once an email has actually gone out, so a failed
    send is retried on the next run.
    """
    alerts: list[CheckResult] = []
    recovered: list[CheckResult] = []
    new_state: dict = {}
    for r in results:
        prev = state.get(r.key) or {}
        if r.ok:
            if prev.get("alerted_at"):
                recovered.append(r)
                new_state[r.key] = prev  # kept until the all-clear is sent
            continue
        entry = {
            "fails": int(prev.get("fails", 0)) + 1,
            "since": prev.get("since", now),
            "alerted_at": prev.get("alerted_at"),
        }
        last = entry["alerted_at"]
        if entry["fails"] >= r.confirm_runs and (last is None or now - last >= repeat_seconds):
            alerts.append(r)
        new_state[r.key] = entry
    return alerts, recovered, new_state


def settle(
    new_state: dict, alerts: list[CheckResult], recovered: list[CheckResult], now: float, sent: bool
) -> dict:
    if sent:
        for r in alerts:
            new_state[r.key]["alerted_at"] = now
        for r in recovered:
            new_state.pop(r.key, None)
    return new_state


def _details(results: list[CheckResult], state: dict) -> list[tuple[str, str]]:
    rows = []
    for r in results:
        label = f"{r.label} ({r.target})" if r.target else r.label
        if r.ok:
            rows.append((label, f"OK: {r.summary}"))
        else:
            since = state.get(r.key, {}).get("since")
            rows.append((label, f"PROBLEM: {r.summary}" + (f" (since {_when(since)})" if since else "")))
    return rows


def compose(
    alerts: list[CheckResult],
    recovered: list[CheckResult],
    results: list[CheckResult],
    state: dict,
    ws: WatchdogSettings,
    host: str,
) -> EmailMessage:
    failing = [r for r in results if not r.ok]
    details = _details(results, state)
    if alerts:
        first = alerts[0]
        more = f" (+{len(alerts) - 1} more)" if len(alerts) > 1 else ""
        headline = f"{first.label}: {first.summary}{more}"
        subject = f"[AIVA ALERT] {host}: {headline}"
        hints = "\n\n".join(f"{r.label}\n{r.hint}" for r in failing if r.hint)
        content = {
            "title": "AIVA server needs attention",
            "intro": (
                f"The watchdog on {host} found a problem that can take AIVA down. "
                "Current readings are below, with a first step to try for each problem."
            ),
            "details": details,
            "block_label": "What to do" if hints else None,
            "block_text": hints or None,
            "note": (
                f"You will get a reminder every {ws.repeat_hours:g} hours while this "
                "lasts, and an all-clear once it is fixed."
            ),
        }
    else:
        names = ", ".join(r.label for r in recovered)
        headline = f"back to normal: {names}"
        subject = f"[AIVA OK] {host}: {headline}"
        intro = f"The problem the watchdog on {host} reported earlier has cleared ({names})."
        if failing:
            intro += " Other problems are still open - see below."
        content = {"title": "AIVA server is back to normal", "intro": intro, "details": details}
    return build_message(
        to=ws.recipients,
        subject=subject[:200],
        preheader=headline[:150],
        eyebrow="Server watchdog",
        **content,
    )


# ---- state + delivery ----


def load_state(path: str) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        _log.warning("Could not read state file %s; starting fresh", path, exc_info=True)
        return {}
    return data if isinstance(data, dict) else {}


def save_state(path: str, state: dict) -> None:
    target = Path(path)
    tmp = target.with_name(target.name + ".tmp")
    try:
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, target)
    except OSError:
        _log.warning("Could not save state file %s; the next run may repeat an email", path, exc_info=True)


async def send(msg: EmailMessage) -> bool:
    try:
        return await get_mail_sender().send(msg)
    except Exception:
        _log.exception("Sending the watchdog email failed")
        return False


# ---- entry points ----


async def run_once(ws: WatchdogSettings, settings: Settings) -> int:
    if not ws.recipients:
        _log.error("WATCHDOG_ALERT_EMAILS is empty, so there is nobody to alert. Set it in the backend .env.")
        return 2
    results = run_checks(ws, settings)
    for r in results:
        if not r.ok:
            _log.warning("%s: %s", r.label, r.summary)

    now = time.time()
    alerts, recovered, new_state = decide(results, load_state(ws.state_file), now, ws.repeat_hours * 3600)
    sent = False
    if alerts or recovered:
        msg = compose(alerts, recovered, results, new_state, ws, socket.gethostname())
        sent = await send(msg)
        if sent:
            _log.info("Emailed %s: %s", ", ".join(ws.recipients), msg.subject)
        else:
            _log.error("Could not send %r; will retry next run", msg.subject)
    save_state(ws.state_file, settle(new_state, alerts, recovered, now, sent))
    return 0


def print_check(ws: WatchdogSettings, settings: Settings) -> int:
    results = run_checks(ws, settings)
    for r in results:
        target = f"  [{r.target}]" if r.target else ""
        print(f"{'OK' if r.ok else 'PROBLEM':8} {r.label}: {r.summary}{target}")
    print(f"Alerts go to: {', '.join(ws.recipients) or '(nobody - set WATCHDOG_ALERT_EMAILS)'}")
    print(f"Email via: {type(get_mail_sender()).__name__}")
    print(f"State file: {ws.state_file}")
    return 0 if all(r.ok for r in results) else 1


async def send_test(ws: WatchdogSettings, settings: Settings) -> int:
    if not ws.recipients:
        print("WATCHDOG_ALERT_EMAILS is empty - set it in the backend .env first.")
        return 2
    host = socket.gethostname()
    msg = build_message(
        to=ws.recipients,
        subject=f"[AIVA] Watchdog test from {host}",
        preheader="If you can read this, watchdog alerts will reach you.",
        eyebrow="Server watchdog",
        title="Watchdog test email",
        intro=(
            f"This is a test from the AIVA watchdog on {host}. Real alerts will look "
            "like this and arrive at this address. Current readings:"
        ),
        details=_details(run_checks(ws, settings), {}),
    )
    ok = await send(msg)
    print(f"Test email {'sent' if ok else 'FAILED'} to {', '.join(ws.recipients)}")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="AIVA server watchdog - see the module docstring.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="print results only: no email, no state change")
    mode.add_argument("--test-email", action="store_true", help="send a test email and exit")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # smtplib has no timeout of its own; a stuck mail server must not hang cron.
    socket.setdefaulttimeout(30)

    ws, settings = WatchdogSettings(), get_settings()
    if args.check:
        return print_check(ws, settings)
    if args.test_email:
        return asyncio.run(send_test(ws, settings))
    return asyncio.run(run_once(ws, settings))


if __name__ == "__main__":
    sys.exit(main())

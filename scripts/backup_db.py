"""Weekly AIVA database backup: export the app schema and keep it on this server.

Runs as the backend's own DB user over the normal DB connection - no docker,
no sudo, no passwords on a command line:

  1. DBMS_DATAPUMP exports the schema, as of one moment in time, into
     DATA_PUMP_DIR inside the Oracle container
  2. the dump is streamed out through a BFILE, gzipped, into BACKUP_DIR on
     this host, and the copy inside the container is deleted
  3. only the newest BACKUP_KEEP backups are kept
  4. the result - success or failure - is emailed to WATCHDOG_ALERT_EMAILS, so
     a backup that quietly stops working gets noticed

The copies live on this server only. They protect against mistakes and
database damage, not against losing the disk itself.

    python scripts/backup_db.py             # one backup - what cron calls
    python scripts/backup_db.py --check     # can a backup run? (no export, no email)
    python scripts/backup_db.py --no-email  # back up without emailing the result

Install (Sundays at 03:00):

    crontab -e
    0 3 * * 0 cd /path/to/AIVA-V2 && flock -n /tmp/aiva-backup.lock /path/to/python scripts/backup_db.py >> $HOME/aiva-backup.log 2>&1

Settings, read from the same .env files as the backend:

    BACKUP_DIR              where backups are saved (default ~/aiva-backups)
    BACKUP_KEEP             how many to keep (default 8 - two months of weekly runs)
    WATCHDOG_ALERT_EMAILS   who gets the result

Restore - this REPLACES the live tables with the backup's contents:

    gunzip -k aiva_<stamp>.dmp.gz
    sudo docker cp aiva_<stamp>.dmp oracle23ai:<DATA_PUMP_DIR path, shown by --check>/
    sudo docker exec -u 0 oracle23ai chown oracle <that path>/aiva_<stamp>.dmp
    sudo docker exec -it oracle23ai impdp <ORACLE_USER>@localhost:1521/FREEPDB1 \\
        directory=DATA_PUMP_DIR dumpfile=aiva_<stamp>.dmp table_exists_action=replace
"""
from __future__ import annotations

import argparse
import asyncio
import gzip
import logging
import os
import shutil
import socket
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

_AIVA_V2 = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_AIVA_V2))

import oracledb
from pydantic_settings import BaseSettings, SettingsConfigDict

from backend.config import Settings, get_settings
from backend.services.email import get_mail_sender
from backend.services.email.templates import build_message

_log = logging.getLogger("aiva.backup")

_DIRECTORY = "DATA_PUMP_DIR"
_PREFIX = "aiva_"
_CHUNK = 4 * 1024 * 1024

_EXPORT = """
DECLARE
  h     NUMBER;
  state VARCHAR2(30);
BEGIN
  h := DBMS_DATAPUMP.OPEN(operation => 'EXPORT', job_mode => 'SCHEMA', job_name => :job);
  DBMS_DATAPUMP.ADD_FILE(h, :dumpfile, :dir);
  DBMS_DATAPUMP.ADD_FILE(h, :logfile, :dir, filetype => DBMS_DATAPUMP.KU$_FILE_TYPE_LOG_FILE);
  DBMS_DATAPUMP.METADATA_FILTER(h, 'SCHEMA_EXPR', '= ''' || USER || '''');
  -- Read every table as of one moment, so rows written during the export
  -- cannot leave the backup with broken references.
  DBMS_DATAPUMP.SET_PARAMETER(h, 'FLASHBACK_SCN', :scn);
  DBMS_DATAPUMP.START_JOB(h);
  DBMS_DATAPUMP.WAIT_FOR_JOB(h, state);
  :state := state;
EXCEPTION
  WHEN OTHERS THEN
    IF h IS NOT NULL THEN
      BEGIN
        DBMS_DATAPUMP.STOP_JOB(h, immediate => 1, keep_master => 0);
      EXCEPTION
        WHEN OTHERS THEN NULL;
      END;
    END IF;
    RAISE;
END;
"""


class BackupSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(_AIVA_V2 / ".env", _AIVA_V2 / "backend" / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    backup_dir: str = str(Path.home() / "aiva-backups")
    backup_keep: int = 8
    watchdog_alert_emails: str = ""

    @property
    def recipients(self) -> list[str]:
        return [e.strip() for e in self.watchdog_alert_emails.split(",") if e.strip()]


class BackupError(Exception):
    def __init__(self, message: str, log_text: str = "") -> None:
        super().__init__(message)
        self.log_text = log_text


@dataclass
class BackupResult:
    file: Path
    raw_bytes: int
    seconds: float
    kept: list[Path]


def _first_line(exc: BaseException) -> str:
    lines = str(exc).strip().splitlines()
    return (lines[0] if lines else type(exc).__name__)[:300]


def _size(n: float) -> str:
    if n >= 1024**3:
        return f"{n / 1024**3:.1f} GB"
    return f"{n / 1024**2:.1f} MB"


def _taken_at(backup: Path) -> str:
    try:
        stamp = backup.name[len(_PREFIX) : len(_PREFIX) + 15]
        return datetime.strptime(stamp, "%Y%m%d_%H%M%S").strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return backup.name


def _needed(schema_bytes: int) -> int:
    # The dump lands inside the container first (same disk here), then a gzipped
    # copy is written out - and a backup must never be what fills the disk.
    return 2 * schema_bytes + 1024**3


def _backups(dest_dir: Path) -> list[Path]:
    """Existing backups, oldest first (the timestamp in the name sorts by date)."""
    return sorted(dest_dir.glob(f"{_PREFIX}*.dmp.gz"))


def connect(settings: Settings) -> oracledb.Connection:
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
    return oracledb.connect(**params)


def _schema_bytes(cur: oracledb.Cursor) -> int:
    cur.execute("SELECT NVL(SUM(bytes), 0) FROM user_segments")
    return int(cur.fetchone()[0])


def _open_bfile(conn: oracledb.Connection, filename: str) -> oracledb.LOB:
    with conn.cursor() as cur:
        cur.execute("SELECT BFILENAME(:d, :f) FROM dual", d=_DIRECTORY, f=filename)
        (bfile,) = cur.fetchone()
    bfile.open()
    return bfile


def _read_text(conn: oracledb.Connection, filename: str) -> str:
    try:
        bfile = _open_bfile(conn, filename)
        try:
            return bfile.read().decode("utf-8", errors="replace")
        finally:
            bfile.close()
    except oracledb.Error as exc:
        _log.warning("Could not read %s: %s", filename, _first_line(exc))
        return ""


def _copy_out(conn: oracledb.Connection, filename: str, dest: Path) -> int:
    """Stream a file out of the Oracle directory into ``dest``, gzipped. Returns its raw size."""
    bfile = _open_bfile(conn, filename)
    try:
        size = bfile.size()
        copied = 0
        with gzip.open(dest, "wb", compresslevel=6) as out:
            while copied < size:
                chunk = bfile.read(copied + 1, _CHUNK)
                if not chunk:
                    break
                out.write(chunk)
                copied += len(chunk)
    finally:
        bfile.close()
    if copied != size:
        raise BackupError(f"Copied only {copied} of {size} bytes of {filename}")
    return size


def _remove(conn: oracledb.Connection, filename: str) -> None:
    try:
        with conn.cursor() as cur:
            cur.callproc("UTL_FILE.FREMOVE", [_DIRECTORY, filename])
    except oracledb.Error as exc:
        _log.warning("Could not delete %s from %s: %s", filename, _DIRECTORY, _first_line(exc))


def prune(dest_dir: Path, keep: int) -> list[Path]:
    """Delete all but the newest ``keep`` backups. Returns the ones kept."""
    backups = _backups(dest_dir)
    keep = max(keep, 1)
    for old in backups[:-keep]:
        old.unlink(missing_ok=True)
        old.with_name(old.name.removesuffix(".dmp.gz") + ".log").unlink(missing_ok=True)
    return backups[-keep:]


def run_backup(bs: BackupSettings, settings: Settings) -> BackupResult:
    dest_dir = Path(bs.backup_dir).expanduser()
    dest_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(dest_dir, 0o700)  # the dump holds every user's data and password hashes
    for stale in dest_dir.glob("*.part"):  # left by a run that was killed mid-copy
        stale.unlink(missing_ok=True)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dumpfile, logfile = f"{_PREFIX}{stamp}.dmp", f"{_PREFIX}{stamp}.log"
    final = dest_dir / f"{dumpfile}.gz"
    start = time.monotonic()

    with connect(settings) as conn:
        with conn.cursor() as cur:
            needed, free = _needed(_schema_bytes(cur)), shutil.disk_usage(dest_dir).free
            if free < needed:
                raise BackupError(f"Not enough free disk: {_size(free)} free, the backup needs about {_size(needed)}")
            cur.execute("SELECT TIMESTAMP_TO_SCN(SYSTIMESTAMP) FROM dual")
            (scn,) = cur.fetchone()
            state = cur.var(str)
            try:
                cur.execute(
                    _EXPORT,
                    job=f"AIVA_BACKUP_{stamp}",
                    dumpfile=dumpfile,
                    logfile=logfile,
                    dir=_DIRECTORY,
                    scn=scn,
                    state=state,
                )
            except oracledb.Error as exc:
                log_text = _read_text(conn, logfile)
                _remove(conn, dumpfile)
                _remove(conn, logfile)
                raise BackupError(f"Export failed: {_first_line(exc)}", log_text) from exc

        try:
            log_text = _read_text(conn, logfile)
            # "completed with N error(s)" also ends as COMPLETED, so read the log too.
            if state.getvalue() != "COMPLETED" or "successfully completed" not in log_text:
                raise BackupError(f"Export ended as {state.getvalue()}", log_text)
            part = final.with_name(final.name + ".part")
            try:
                raw_bytes = _copy_out(conn, dumpfile, part)
                os.chmod(part, 0o600)
                os.replace(part, final)
            except BaseException:
                part.unlink(missing_ok=True)
                raise
            (dest_dir / logfile).write_text(log_text, encoding="utf-8")
        finally:
            _remove(conn, dumpfile)
            _remove(conn, logfile)

    # Prune only after the new backup is safely on disk, so there is always one.
    kept = prune(dest_dir, bs.backup_keep)
    return BackupResult(final, raw_bytes, time.monotonic() - start, kept)


# ---- reporting ----


async def send_report(bs: BackupSettings, subject: str, **content) -> None:
    if not bs.recipients:
        _log.warning("WATCHDOG_ALERT_EMAILS is empty, so the result is not emailed")
        return
    # smtplib has no timeout of its own. Set only now: the database work is done,
    # and a long export must not trip a socket timeout.
    socket.setdefaulttimeout(30)
    msg = build_message(to=bs.recipients, subject=subject, preheader=subject, eyebrow="Database backup", **content)
    try:
        ok = await get_mail_sender().send(msg)
    except Exception:
        _log.exception("Sending the backup report failed")
        ok = False
    if not ok:
        _log.error("Could not email the backup result")


def success_report(bs: BackupSettings, result: BackupResult, host: str) -> tuple[str, dict]:
    size = result.file.stat().st_size
    free = shutil.disk_usage(result.file.parent).free
    content = {
        "title": "Weekly database backup completed",
        "intro": f"The AIVA database on {host} was backed up successfully.",
        "details": [
            ("File", str(result.file)),
            ("Size", f"{_size(size)} compressed ({_size(result.raw_bytes)} before compression)"),
            ("Took", f"{result.seconds:.0f} seconds"),
            ("Backups kept", f"{len(result.kept)}, oldest from {_taken_at(result.kept[0])}"),
            ("Free disk", _size(free)),
        ],
        "note": (
            "These copies are on the server only. They protect against mistakes and "
            "database damage, but not against losing the server's disk."
        ),
    }
    return f"[AIVA] Database backup OK ({_size(size)})", content


def failure_report(bs: BackupSettings, exc: BaseException, host: str) -> tuple[str, dict]:
    existing = _backups(Path(bs.backup_dir).expanduser()) if Path(bs.backup_dir).expanduser().is_dir() else []
    log_tail = "\n".join(getattr(exc, "log_text", "").strip().splitlines()[-30:])
    content = {
        "title": "Database backup FAILED",
        "intro": (
            f"The weekly AIVA database backup on {host} did not complete. Until it is "
            "fixed, the newest good backup keeps getting older."
        ),
        "details": [
            ("Error", _first_line(exc)),
            ("Newest good backup", _taken_at(existing[-1]) if existing else "none"),
        ],
        "block_label": "Export log (last lines)" if log_tail else None,
        "block_text": log_tail or None,
        "note": (
            "To see the full error, run on the server: cd ~/AIVA/AIVA-V2 && "
            "venv/bin/python scripts/backup_db.py --no-email"
        ),
    }
    return f"[AIVA ALERT] Database backup FAILED on {host}", content


# ---- entry points ----


def check(bs: BackupSettings, settings: Settings) -> int:
    ok = True

    def line(good: bool, text: str) -> None:
        nonlocal ok
        ok = ok and good
        print(f"{'OK' if good else 'PROBLEM':8} {text}")

    schema = 0
    try:
        with connect(settings) as conn, conn.cursor() as cur:
            cur.execute("SELECT directory_path FROM all_directories WHERE directory_name = :d", d=_DIRECTORY)
            row = cur.fetchone()
            line(row is not None, f"{_DIRECTORY}: {row[0] if row else 'not visible to ' + settings.oracle_user}")
            cur.execute("SELECT privilege FROM all_tab_privs WHERE table_name = :d", d=_DIRECTORY)
            privs = {r[0] for r in cur}
            line({"READ", "WRITE"} <= privs, f"access to {_DIRECTORY}: {', '.join(sorted(privs)) or 'none'}")
            cur.execute(
                "SELECT DISTINCT object_name FROM all_procedures "
                "WHERE owner = 'SYS' AND object_name IN ('DBMS_DATAPUMP', 'UTL_FILE')"
            )
            packages = {r[0] for r in cur}
            line(packages == {"DBMS_DATAPUMP", "UTL_FILE"}, f"can run: {', '.join(sorted(packages)) or 'none'}")
            cur.execute("SELECT TIMESTAMP_TO_SCN(SYSTIMESTAMP) FROM dual")
            line(cur.fetchone()[0] is not None, "consistent point-in-time export available")
            schema = _schema_bytes(cur)
    except oracledb.Error as exc:
        line(False, f"database: {_first_line(exc)}")

    dest = Path(bs.backup_dir).expanduser()
    probe = next(p for p in (dest, *dest.parents) if p.exists())
    free = shutil.disk_usage(probe).free
    line(free >= _needed(schema), f"free disk {_size(free)}; a backup needs about {_size(_needed(schema))}")
    print(f"Backups go to: {dest} (keeping the newest {bs.backup_keep})")
    print(f"Result emailed to: {', '.join(bs.recipients) or '(nobody - set WATCHDOG_ALERT_EMAILS)'}")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="AIVA database backup - see the module docstring.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="check a backup can run: no export, no email")
    mode.add_argument("--no-email", action="store_true", help="back up without emailing the result")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    bs, settings = BackupSettings(), get_settings()
    if args.check:
        return check(bs, settings)

    host = socket.gethostname()
    try:
        result = run_backup(bs, settings)
    except Exception as exc:
        _log.exception("Backup failed")
        if not args.no_email:
            subject, content = failure_report(bs, exc, host)
            asyncio.run(send_report(bs, subject, **content))
        return 1

    _log.info("Backup saved: %s (%s)", result.file, _size(result.file.stat().st_size))
    if not args.no_email:
        subject, content = success_report(bs, result, host)
        asyncio.run(send_report(bs, subject, **content))
    return 0


if __name__ == "__main__":
    sys.exit(main())

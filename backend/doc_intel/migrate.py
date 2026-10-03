"""Reviewed, explicit migrations for the document intelligence tables.

Nothing in this module runs at app startup. A human runs it:

    python -m backend.doc_intel.migrate verify   --version 001      # read-only checks
    python -m backend.doc_intel.migrate show     --version 001      # print the SQL + objects
    python -m backend.doc_intel.migrate apply    --version 001 --confirm-schema AI_ASSISTANT --yes
    python -m backend.doc_intel.migrate rollback --version 001 --confirm-schema AI_ASSISTANT --yes

``--file path/to/script.sql`` runs any script instead of a numbered version (its
rollback is the sibling ``*.rollback.sql``); used for the DI_TEST_* test fixtures.

Safety rules:
- ``verify`` and ``show`` never change anything.
- ``apply``/``rollback`` require ``--confirm-schema`` equal to the connected schema and ``--yes``.
- ``apply`` aborts if any object it would create already exists.
- ``rollback`` of V001 refuses while documents are still PUBLISHED (unpublish first) unless ``--force``.
- ``rollback`` of a version refuses while a later version is still applied (V002 before V001).
- Scripts may only CREATE/DROP objects they own; any other statement type is refused.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import re
import socket
import sys
from dataclasses import dataclass, field
from pathlib import Path

import oracledb

from backend.config import get_settings

_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_ROOT = Path(__file__).resolve().parent.parent.parent

# Existing tables the module only READS; verify reports who owns them.
_READ_ONLY_TABLES = (
    "KB_CORPUS",
    "KB_CHUNK",
    "AIVA_ACCOUNTS",
    "AIVA_ORGANIZATIONS",
    "AIVA_USERS",
    "AIVA_ROLES",
    "AIVA_USER_ROLES",
)
# (table, timestamp column) used only to show whether live traffic reaches this schema.
_ACTIVITY_PROBES = (
    ("AIVA_HTTP_REQUEST_LOGS", "created_at"),
    ("AIVA_CHAT_MESSAGES", "created_at"),
)
_ALLOWED_STATEMENT = re.compile(r"^\s*(CREATE\s+(UNIQUE\s+)?(TABLE|INDEX)|DROP\s+TABLE)\b", re.IGNORECASE)
_DROP_TABLE = re.compile(r"\s*DROP\s+TABLE\s+([A-Za-z][A-Za-z0-9_$#]*)(?:\s+PURGE)?\s*", re.IGNORECASE)
_FORBIDDEN_SCHEMAS = {"SYS", "SYSTEM"}


# ---- script parsing ------------------------------------------------------------------------


@dataclass
class Script:
    path: Path
    statements: list[str]
    tables: list[str] = field(default_factory=list)
    indexes: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    dropped_tables: list[str] = field(default_factory=list)

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.path.read_bytes()).hexdigest()


def _strip_comments(sql: str) -> str:
    out: list[str] = []
    in_quote = False
    i = 0
    while i < len(sql):
        ch = sql[i]
        if ch == "'":
            in_quote = not in_quote
            out.append(ch)
        elif not in_quote and sql.startswith("--", i):
            nl = sql.find("\n", i)
            i = len(sql) if nl == -1 else nl
            continue
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _split_statements(sql: str) -> list[str]:
    stmts: list[str] = []
    buf: list[str] = []
    in_quote = False
    for ch in _strip_comments(sql):
        if ch == "'":
            in_quote = not in_quote
        if ch == ";" and not in_quote:
            stmt = "".join(buf).strip()
            if stmt:
                stmts.append(stmt)
            buf = []
        else:
            buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        stmts.append(tail)
    return stmts


def parse_script(path: Path) -> Script:
    statements = _split_statements(path.read_text(encoding="utf-8"))
    script = Script(path=path, statements=statements)
    for stmt in statements:
        if not _ALLOWED_STATEMENT.match(stmt):
            raise SystemExit(f"Refusing {path.name}: only CREATE TABLE/INDEX and DROP TABLE are allowed:\n  {stmt[:120]}")
        m = re.match(r"\s*CREATE\s+TABLE\s+(\w+)", stmt, re.IGNORECASE)
        if m:
            script.tables.append(m.group(1).upper())
        m = re.match(r"\s*CREATE\s+(?:UNIQUE\s+)?INDEX\s+(\w+)", stmt, re.IGNORECASE)
        if m:
            script.indexes.append(m.group(1).upper())
        script.constraints.extend(c.upper() for c in re.findall(r"CONSTRAINT\s+(\w+)", stmt, re.IGNORECASE))
        if re.match(r"\s*DROP\b", stmt, re.IGNORECASE):
            # One plain, unqualified table name only: a quoted or schema-qualified name would
            # slip past the ownership check below (and past skip_missing_drops).
            m = _DROP_TABLE.fullmatch(stmt)
            if m is None:
                raise SystemExit(
                    f"Refusing {path.name}: DROP TABLE must name one unquoted table of this schema:\n  {stmt[:120]}"
                )
            script.dropped_tables.append(m.group(1).upper())
    return script


def check_drop_scope(fwd: Script, rb: Script) -> None:
    """Scripts may only drop tables that the forward script itself creates, never an existing one."""
    owned = set(fwd.tables)
    foreign = sorted({t for t in (*fwd.dropped_tables, *rb.dropped_tables) if t not in owned})
    if foreign:
        raise SystemExit(
            f"Refusing {fwd.path.name} / {rb.path.name}: they drop tables that {fwd.path.name} does not create: "
            + ", ".join(foreign)
        )


def resolve_scripts(version: str | None, file: str | None) -> tuple[Script, Script, str | None]:
    """(forward, rollback, version-or-None)."""
    if file:
        fwd = Path(file)
        if not fwd.is_absolute():
            fwd = (_ROOT / fwd).resolve()
        rb = fwd.with_name(fwd.name.replace(".sql", ".rollback.sql"))
        if not fwd.is_file() or not rb.is_file():
            raise SystemExit(f"Script or its rollback not found: {fwd} / {rb}")
        fwd_script, rb_script = parse_script(fwd), parse_script(rb)
        check_drop_scope(fwd_script, rb_script)
        return fwd_script, rb_script, None
    if not version:
        raise SystemExit("Pass --version NNN or --file PATH")
    v = version.zfill(3)
    fwd_matches = [p for p in _MIGRATIONS_DIR.glob(f"V{v}__*.sql") if not p.name.endswith(".rollback.sql")]
    if len(fwd_matches) != 1:
        raise SystemExit(f"Expected exactly one V{v}__*.sql in {_MIGRATIONS_DIR}, found {len(fwd_matches)}")
    fwd = fwd_matches[0]
    rb = fwd.with_name(fwd.name.replace(".sql", ".rollback.sql"))
    if not rb.is_file():
        raise SystemExit(f"Missing rollback script {rb.name}")
    fwd_script, rb_script = parse_script(fwd), parse_script(rb)
    check_drop_scope(fwd_script, rb_script)
    return fwd_script, rb_script, v


# ---- database helpers ----------------------------------------------------------------------


def connect() -> oracledb.Connection:
    s = get_settings()
    kwargs: dict = {"user": s.oracle_user, "password": s.oracle_password, "dsn": s.oracle_dsn}
    if s.oracle_wallet_dir:
        kwargs.update(config_dir=s.oracle_wallet_dir, wallet_location=s.oracle_wallet_dir)
        if s.oracle_wallet_password:
            kwargs["wallet_password"] = s.oracle_wallet_password
    return oracledb.connect(**kwargs)


def _scalar(conn: oracledb.Connection, sql: str, **binds):
    with conn.cursor() as cur:
        cur.execute(sql, binds)
        row = cur.fetchone()
        return row[0] if row else None


def _rows(conn: oracledb.Connection, sql: str, **binds) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(sql, binds)
        return list(cur.fetchall())


def _in_list(names: list[str]) -> tuple[str, dict]:
    binds = {f"n{i}": n for i, n in enumerate(names)}
    return ", ".join(f":{k}" for k in binds), binds


def existing_objects(conn: oracledb.Connection, names: list[str]) -> dict[str, str]:
    """{NAME: object_type} for names that exist in the connected schema (objects or constraints)."""
    if not names:
        return {}
    placeholders, binds = _in_list(names)
    found = {r[0]: r[1] for r in _rows(conn, f"SELECT object_name, object_type FROM user_objects WHERE object_name IN ({placeholders})", **binds)}
    for r in _rows(conn, f"SELECT constraint_name, constraint_type FROM user_constraints WHERE constraint_name IN ({placeholders})", **binds):
        found.setdefault(r[0], f"CONSTRAINT({r[1]})")
    return found


def session_info(conn: oracledb.Connection) -> dict[str, str]:
    info: dict[str, str] = {}
    row = _rows(
        conn,
        """
        SELECT USER,
               SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA'),
               SYS_CONTEXT('USERENV', 'CON_NAME'),
               SYS_CONTEXT('USERENV', 'DB_NAME'),
               SYS_CONTEXT('USERENV', 'SERVICE_NAME'),
               SYS_CONTEXT('USERENV', 'SERVER_HOST'),
               TO_CHAR(SYSTIMESTAMP, 'YYYY-MM-DD HH24:MI:SS TZH:TZM')
        FROM dual
        """,
    )[0]
    keys = ("session_user", "current_schema", "container", "db_name", "service", "server_host", "db_time")
    info.update({k: str(v) for k, v in zip(keys, row)})
    try:
        info["db_version"] = str(_scalar(conn, "SELECT version_full FROM product_component_version WHERE ROWNUM = 1"))
    except oracledb.DatabaseError:
        info["db_version"] = "n/a"
    return info


# ---- commands ------------------------------------------------------------------------------


def _print_objects(script: Script) -> None:
    for label, names in (("tables", script.tables), ("indexes", script.indexes), ("constraints", script.constraints)):
        if names:
            print(f"    {label:<11} {', '.join(names)}")
    if script.tables:
        print("    (plus the system-named identity sequences / LOB segments Oracle creates for these tables)")
    if script.dropped_tables:
        print(f"    drops       {', '.join(script.dropped_tables)}")


def cmd_show(fwd: Script, rb: Script) -> int:
    for script in (fwd, rb):
        print(f"===== {script.path.relative_to(_ROOT)}  (sha256 {script.checksum[:16]}…)")
        _print_objects(script)
        for i, stmt in enumerate(script.statements, 1):
            print(f"\n-- [{i}/{len(script.statements)}]\n{stmt};")
        print()
    return 0


def cmd_verify(conn: oracledb.Connection, fwd: Script, rb: Script, version: str | None) -> dict:
    """Read-only. Returns facts used by apply/rollback."""
    facts: dict = {}
    s = get_settings()
    print(f"Target DSN        : {s.oracle_dsn}   (user {s.oracle_user})")
    info = session_info(conn)
    facts["schema"] = info["current_schema"].upper()
    for k in ("session_user", "current_schema", "container", "db_name", "service", "server_host", "db_version", "db_time"):
        print(f"{k.replace('_', ' ').capitalize():<18}: {info[k]}")

    privs = {r[0] for r in _rows(conn, "SELECT privilege FROM session_privs")}
    can_create = bool({"CREATE TABLE", "CREATE ANY TABLE"} & privs)
    facts["can_create_table"] = can_create
    print(f"CREATE TABLE      : {'yes' if can_create else 'NO'}"
          f"{' (UNLIMITED TABLESPACE)' if 'UNLIMITED TABLESPACE' in privs else ''}")
    default_ts = _scalar(conn, "SELECT default_tablespace FROM user_users")
    quotas = _rows(conn, "SELECT tablespace_name, max_bytes FROM user_ts_quotas")
    quota_txt = ", ".join(f"{t}={'unlimited' if (m is not None and m < 0) else m}" for t, m in quotas) or "none listed"
    print(f"Default tablespace: {default_ts}   quotas: {quota_txt}")

    print("\nObjects this script creates (must NOT exist yet):")
    _print_objects(fwd)
    new_names = fwd.tables + fwd.indexes + fwd.constraints
    collisions = existing_objects(conn, new_names)
    facts["collisions"] = collisions
    if collisions:
        print("  ALREADY PRESENT: " + ", ".join(f"{n} ({t})" for n, t in sorted(collisions.items())))
    else:
        print("  none present — no name collisions")

    print("\nExisting tables the module only READS (ownership):")
    placeholders, binds = _in_list(list(_READ_ONLY_TABLES))
    owners: dict[str, list[str]] = {}
    for owner, table in _rows(conn, f"SELECT owner, table_name FROM all_tables WHERE table_name IN ({placeholders}) ORDER BY table_name, owner", **binds):
        owners.setdefault(table, []).append(owner)
    for table in _READ_ONLY_TABLES:
        who = owners.get(table)
        mark = "owned by connected schema" if who and facts["schema"] in who else "NOT in connected schema"
        print(f"  {table:<22} owners={','.join(who) if who else '-':<22} {mark}")
    facts["read_only_owners"] = owners

    print("\nLive-traffic indicator (read-only MAX timestamps):")
    for table, col in _ACTIVITY_PROBES:
        try:
            latest = _scalar(conn, f"SELECT MAX({col}) FROM {table}")
            print(f"  {table:<22} latest {col} = {latest}")
        except oracledb.DatabaseError as ex:
            print(f"  {table:<22} n/a ({str(ex).splitlines()[0][:80]})")

    ledger = existing_objects(conn, ["AIVA_DI_SCHEMA_VERSION"])
    if ledger:
        rows = _rows(conn, "SELECT version, applied_at, applied_by FROM AIVA_di_schema_version ORDER BY version")
        facts["applied_versions"] = {r[0] for r in rows}
        print("\nApplied doc-intel versions: " + (", ".join(f"{r[0]} @ {r[1]} by {r[2]}" for r in rows) or "none"))
    else:
        facts["applied_versions"] = set()
        print("\nApplied doc-intel versions: none (ledger table not created yet)")

    if existing_objects(conn, ["AIVA_KB_DOCUMENTS"]):
        facts["published_docs"] = int(_scalar(conn, "SELECT COUNT(*) FROM AIVA_kb_documents WHERE status = 'PUBLISHED'") or 0)
        print(f"Published documents: {facts['published_docs']}")
    else:
        facts["published_docs"] = 0

    if facts["schema"] in _FORBIDDEN_SCHEMAS:
        print(f"\nREFUSED: connected as {facts['schema']}; never run application migrations there.")
    return facts


def _require_confirmation(args: argparse.Namespace, facts: dict) -> None:
    if facts["schema"] in _FORBIDDEN_SCHEMAS:
        raise SystemExit(f"Refusing to run in schema {facts['schema']}.")
    if not args.confirm_schema or args.confirm_schema.upper() != facts["schema"]:
        raise SystemExit(f"--confirm-schema must equal the connected schema ({facts['schema']}); nothing executed.")
    if not args.yes:
        raise SystemExit("Add --yes to execute; nothing executed.")


def _run_statements(conn: oracledb.Connection, script: Script, *, skip_missing_drops: bool) -> list[str]:
    done: list[str] = []
    with conn.cursor() as cur:
        for i, stmt in enumerate(script.statements, 1):
            head = " ".join(stmt.split()[:4])
            m = re.match(r"\s*DROP\s+TABLE\s+(\w+)", stmt, re.IGNORECASE)
            if m and skip_missing_drops and not existing_objects(conn, [m.group(1).upper()]):
                print(f"  [{i}/{len(script.statements)}] skip (not present): {head}")
                continue
            try:
                cur.execute(stmt)
            except oracledb.DatabaseError as ex:
                print(f"  [{i}/{len(script.statements)}] FAILED: {head}\n      {str(ex).splitlines()[0]}")
                print("  Stopped. Completed so far: " + (", ".join(done) or "nothing"))
                raise SystemExit(2) from ex
            done.append(head)
            print(f"  [{i}/{len(script.statements)}] ok: {head}")
    return done


def cmd_apply(conn: oracledb.Connection, args: argparse.Namespace, fwd: Script, rb: Script, version: str | None) -> int:
    facts = cmd_verify(conn, fwd, rb, version)
    print()
    if version and version in facts["applied_versions"]:
        print(f"Version {version} is already applied; nothing to do.")
        return 0
    if facts["collisions"]:
        raise SystemExit("Refusing to apply: objects above already exist. Nothing executed.")
    if not facts["can_create_table"]:
        raise SystemExit("Refusing to apply: the connected user lacks CREATE TABLE. Nothing executed.")
    _require_confirmation(args, facts)
    print(f"Applying {fwd.path.name} to schema {facts['schema']} …")
    _run_statements(conn, fwd, skip_missing_drops=False)
    if version:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO AIVA_di_schema_version (version, description, checksum, applied_by) "
                "VALUES (:v, :d, :c, :b)",
                v=version,
                d=fwd.path.stem.split("__", 1)[-1][:256],
                c=fwd.checksum,
                b=f"{getpass.getuser()}@{socket.gethostname()}"[:128],
            )
        conn.commit()
    print("Done.")
    return 0


def cmd_rollback(conn: oracledb.Connection, args: argparse.Namespace, fwd: Script, rb: Script, version: str | None) -> int:
    facts = cmd_verify(conn, fwd, rb, version)
    print()
    later = sorted(v for v in facts.get("applied_versions", set()) if version and v > version)
    if later:
        # V001 holds the ledger: dropping it first would strand V002's tables (stored credentials,
        # CRM entities) without their ledger row (review finding F31). --force does not skip this.
        raise SystemExit(
            f"Refusing to roll back V{version}: V{', V'.join(later)} is still applied. "
            f"Roll back the later version(s) first, newest first."
        )
    if version == "001" and facts["published_docs"] and not args.force:
        raise SystemExit(
            f"Refusing to roll back: {facts['published_docs']} document(s) are PUBLISHED. "
            "Unpublish them first (their kbdoc-* verticals and chunks live in the KB), or pass --force."
        )
    _require_confirmation(args, facts)
    print(f"Rolling back with {rb.path.name} in schema {facts['schema']} …")
    if version and version != "001" and version in facts["applied_versions"]:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM AIVA_di_schema_version WHERE version = :v", v=version)
        conn.commit()
    _run_statements(conn, rb, skip_missing_drops=True)
    print("Done.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m backend.doc_intel.migrate", description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=("verify", "show", "apply", "rollback"))
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--version", help="migration number, e.g. 001")
    target.add_argument("--file", help="path to a *.sql script (rollback = sibling *.rollback.sql)")
    parser.add_argument("--confirm-schema", help="must equal the connected schema for apply/rollback")
    parser.add_argument("--yes", action="store_true", help="actually execute apply/rollback")
    parser.add_argument("--force", action="store_true", help="rollback even with PUBLISHED documents")
    args = parser.parse_args(argv)

    fwd, rb, version = resolve_scripts(args.version, args.file)
    if args.command == "show":
        return cmd_show(fwd, rb)
    conn = connect()
    try:
        if args.command == "verify":
            cmd_verify(conn, fwd, rb, version)
            return 0
        if args.command == "apply":
            return cmd_apply(conn, args, fwd, rb, version)
        return cmd_rollback(conn, args, fwd, rb, version)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())

"""The migration tool's script checks (no database: ``show`` and parsing never connect)."""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.doc_intel import migrate

_ROOT = Path(__file__).resolve().parents[3]
V001 = _ROOT / "backend" / "doc_intel" / "migrations" / "V001__flow1_monitoring.sql"
DI_TEST = _ROOT / "backend" / "tests" / "doc_intel" / "sql" / "DI_TEST_kb_tables.sql"

CREATE = "CREATE TABLE DI_OWN_A (id NUMBER);\nCREATE TABLE DI_OWN_B (id NUMBER);\n"


def _pair(tmp_path: Path, forward: str, rollback: str) -> Path:
    fwd = tmp_path / "X__probe.sql"
    fwd.write_text(forward, encoding="utf-8")
    (tmp_path / "X__probe.rollback.sql").write_text(rollback, encoding="utf-8")
    return fwd


def test_the_shipped_scripts_pass_the_drop_scope_check():
    fwd, rb, version = migrate.resolve_scripts("001", None)
    assert version == "001" and set(rb.dropped_tables) == set(fwd.tables)
    fwd, rb, version = migrate.resolve_scripts(None, str(DI_TEST))
    assert version is None and set(rb.dropped_tables) == {"DI_TEST_KB_CORPUS", "DI_TEST_KB_CHUNK"}


def test_the_shipped_v002_scripts_pass_the_drop_scope_check():
    fwd, rb, version = migrate.resolve_scripts("002", None)
    assert version == "002" and set(rb.dropped_tables) == set(fwd.tables) == {
        "AIVA_CRM_SOURCES", "AIVA_CRM_SYNC_RUNS", "AIVA_CRM_SOURCE_FILES", "AIVA_CRM_ENTITIES",
    }


class _NoDb:
    """A connection that must never be used (the refusal comes before any statement)."""

    def cursor(self):
        raise AssertionError("no statement may run")

    def commit(self):
        raise AssertionError("nothing may be committed")


def _rollback_args(**over):
    import argparse

    values = {"confirm_schema": "AI_ASSISTANT", "yes": True, "force": False}
    values.update(over)
    return argparse.Namespace(**values)


@pytest.mark.parametrize("force", [False, True])
def test_v001_is_never_rolled_back_while_v002_is_applied(monkeypatch, capsys, force):
    """F31: V001 holds the ledger; rolling it back first stranded V002's tables (credentials, CRM data)."""
    facts = {"schema": "AI_ASSISTANT", "applied_versions": {"001", "002"}, "published_docs": 0}
    monkeypatch.setattr(migrate, "cmd_verify", lambda *a, **k: facts)
    monkeypatch.setattr(migrate, "_run_statements", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no DDL")))
    fwd, rb, version = migrate.resolve_scripts("001", None)
    with pytest.raises(SystemExit, match="V002 is still applied"):
        migrate.cmd_rollback(_NoDb(), _rollback_args(force=force), fwd, rb, version)


def test_v002_rollback_is_not_blocked_by_the_earlier_version(monkeypatch):
    facts = {"schema": "AI_ASSISTANT", "applied_versions": {"001", "002"}, "published_docs": 3}
    ran: list[str] = []
    monkeypatch.setattr(migrate, "cmd_verify", lambda *a, **k: facts)
    monkeypatch.setattr(migrate, "_run_statements", lambda conn, script, **k: ran.append(script.path.name) or [])

    class Ledger:
        def __init__(self):
            self.deleted: list[str] = []

        def cursor(self):
            ledger = self

            class Cur:
                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False

                def execute(self, sql, **binds):
                    ledger.deleted.append(binds["v"])

            return Cur()

        def commit(self):
            pass

    conn = Ledger()
    fwd, rb, version = migrate.resolve_scripts("002", None)
    assert migrate.cmd_rollback(conn, _rollback_args(), fwd, rb, version) == 0
    assert ran == ["V002__sharepoint_crm.rollback.sql"] and conn.deleted == ["002"]


def test_a_rollback_that_drops_its_own_tables_is_accepted(tmp_path: Path):
    fwd = _pair(tmp_path, CREATE, "DROP TABLE DI_OWN_B;\ndrop table di_own_a purge;\n")
    f, r, _ = migrate.resolve_scripts(None, str(fwd))
    assert r.dropped_tables == ["DI_OWN_B", "DI_OWN_A"]


@pytest.mark.parametrize("statement", ["DROP TABLE AIVA_users", "DROP TABLE kb_chunk PURGE"])
def test_a_rollback_may_not_drop_an_existing_table(tmp_path: Path, statement: str):
    fwd = _pair(tmp_path, CREATE, f"DROP TABLE DI_OWN_A;\n{statement};\n")
    with pytest.raises(SystemExit, match="does not create"):
        migrate.resolve_scripts(None, str(fwd))


def test_a_forward_script_may_not_drop_an_existing_table(tmp_path: Path):
    fwd = _pair(tmp_path, CREATE + "DROP TABLE AIVA_accounts;\n", "DROP TABLE DI_OWN_A;\n")
    with pytest.raises(SystemExit, match="AIVA_ACCOUNTS"):
        migrate.resolve_scripts(None, str(fwd))


@pytest.mark.parametrize(
    "statement",
    ['DROP TABLE "AIVA_users"', "DROP TABLE AI_ASSISTANT.AIVA_users", "DROP TABLE DI_OWN_A CASCADE CONSTRAINTS"],
)
def test_quoted_qualified_or_decorated_drops_are_refused(tmp_path: Path, statement: str):
    fwd = _pair(tmp_path, CREATE, f"{statement};\n")
    with pytest.raises(SystemExit, match="DROP TABLE must name one unquoted table"):
        migrate.resolve_scripts(None, str(fwd))


def test_show_prints_without_connecting(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch):
    def no_db():
        raise AssertionError("show must never connect")

    monkeypatch.setattr(migrate, "connect", no_db)
    assert migrate.main(["show", "--version", "001"]) == 0
    out = capsys.readouterr().out
    assert "AIVA_KB_DOCUMENTS" in out and "DROP TABLE AIVA_kb_documents" in out

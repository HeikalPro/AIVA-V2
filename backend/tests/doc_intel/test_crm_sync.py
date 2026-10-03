"""Flow 2: source administration, "Sync now", the listing diff, the five file stages, the worker.

Everything runs on in-memory fakes (see _crm_fakes): no database, no Microsoft, no child process.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import time
from datetime import timedelta

import pytest
from pydantic import SecretStr

from backend.doc_intel import crm_sync, schedule
from backend.doc_intel.crm_extraction import CrmExtractionFailed
from backend.doc_intel.crm_repo import INTERRUPTED_FILE_REASON, INTERRUPTED_RUN_REASON, SHUTDOWN_RUN_REASON
from backend.doc_intel.crm_sync import (
    MAX_FILE_ATTEMPTS,
    TEMP_PREFIX,
    SyncWorker,
    clean_client_id,
    clean_folder_path,
    clean_tenant_id,
    content_changed,
    normalize_extensions,
    plan_sync,
    sweep_stale_temp_dirs,
)
from backend.doc_intel.graph_source import GraphFailed
from backend.doc_intel.kb_repo import loads_json, parse_utc
from backend.doc_intel.runtime import ServiceUnavailableError
from backend.doc_intel.schemas import SourceCreate, SourceUpdate
from backend.doc_intel.textutil import utc_now
from backend.exceptions import BadRequestError, ConflictError, NotFoundError

from ._crm_fakes import (  # noqa: F401  (crm_env is a fixture)
    CLIENT_ID,
    KEY_MISSING_REASON,
    NEW_SECRET,
    SECRET,
    TARGET,
    TENANT_ID,
    CrmEnv,
    crm_env,
    old,
    remote,
    source_body,
)
from .conftest import make_user

SA = make_user("SUPER_ADMIN")
STAGES = ("download", "extraction", "intelligence", "entities", "persist")


async def create(crm: CrmEnv, **overrides) -> int:
    return (await crm.service.create_source(SA, SourceCreate(**source_body(**overrides)))).id


async def wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.02)


# ---- sources: create -------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_source_encrypts_the_credentials_and_masks_the_audit(crm_env: CrmEnv):
    crm_env.settings.audit_enabled = True
    out = await crm_env.service.create_source(SA, SourceCreate(**source_body()))
    row = crm_env.source(out.id)
    stored = json.dumps(row)
    assert SECRET not in stored and TENANT_ID not in stored and CLIENT_ID not in stored  # only ciphertext
    assert crm_env.box.decrypt(row["client_secret_enc"]) == SECRET
    assert (out.tenant_id, out.client_id) == (TENANT_ID, CLIENT_ID) and out.credentials_readable
    assert out.client_secret_set and out.client_secret_hint == "…" + SECRET[-4:]
    assert out.secret_updated_at and out.secret_updated_at.endswith("Z")
    assert SECRET not in out.model_dump_json()
    assert (out.drive_name, out.folder_path, out.file_extensions) == ("Documents", "/CRM/Contracts", [".pdf", ".docx"])
    assert out.status == "ACTIVE" and out.account_name == "Hallan" and out.next_sync_at is None
    assert out.counts == {"files_active": 0, "files_failed": 0, "files_deleted": 0, "entities_active": 0}
    assert out.active_run is None and out.use_intelligence is False

    (audit,) = crm_env.audits
    assert (audit["entity_type"], audit["action_type"], audit["entity_id"]) == ("crm_source", "CREATE", out.id)
    text = json.dumps(audit, default=str, ensure_ascii=False)
    assert SECRET not in text and TENANT_ID not in text and CLIENT_ID not in text
    assert audit["new_value"]["client_secret"] == "set"
    assert audit["new_value"]["tenant_id"] == "…" + TENANT_ID[-4:] and audit["new_value"]["client_id"] == "…" + CLIENT_ID[-4:]


@pytest.mark.asyncio
async def test_create_source_with_a_schedule_computes_the_next_sync(crm_env: CrmEnv):
    before = schedule.next_sync_at(enabled=True, interval_days=7, hour=3, tz_name="Africa/Cairo", now_utc=utc_now())
    out = await crm_env.service.create_source(SA, SourceCreate(**source_body(sync_enabled=True, sync_interval_days=7, sync_hour=3)))
    after = schedule.next_sync_at(enabled=True, interval_days=7, hour=3, tz_name="Africa/Cairo", now_utc=utc_now())
    assert parse_utc(out.next_sync_at) in (before, after)
    assert out.sync_enabled and (out.sync_interval_days, out.sync_hour) == (7, 3)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "status", "detail"),
    [
        ({"file_extensions": [".xlsx"]}, 400, "Unsupported file type: .xlsx (only .pdf, .docx can be processed)"),
        ({"file_extensions": [" ", ""]}, 400, "Select at least one file type"),
        ({"site_url": "http://contoso.sharepoint.com/sites/Sales"}, 400, "The site must be on SharePoint Online"),
        ({"site_url": "https://evil.example.com/sites/x"}, 400, "The site must be on SharePoint Online"),
        ({"account_id": 999}, 404, "Account not found"),
        ({"tenant_id": "not a tenant"}, 400, "The Tenant ID must be a GUID"),
        ({"client_id": "contoso.onmicrosoft.com"}, 400, "The Client ID must be the app's Application (client) ID"),
        ({"client_secret": "   "}, 400, "Client secret is required"),
        ({"client_secret": "abc\ndef"}, 400, "Client secret contains invalid characters"),
        ({"folder_path": "/CRM/../Other"}, 400, "must not contain '.' or '..'"),
        ({"folder_path": "/CRM/Q3:final"}, 400, "character SharePoint does not allow"),
        ({"name": "😀" * 150}, 400, "Name is too long"),
        ({"name": "   "}, 400, "Name is required"),
    ],
)
async def test_create_source_validation(crm_env: CrmEnv, overrides, status, detail):
    with pytest.raises((BadRequestError, NotFoundError)) as info:
        await create(crm_env, **overrides)
    assert info.value.status_code == status and detail in info.value.detail, info.value.detail
    assert SECRET not in info.value.detail
    assert crm_env.repo.sources == {}


@pytest.mark.asyncio
async def test_create_source_normalizes_types_paths_and_ids(crm_env: CrmEnv):
    source_id = await create(
        crm_env,
        file_extensions=["PDF", ".pdf", "docx"],
        folder_path="\\CRM\\ Contracts \\",
        drive_name="  ",
        tenant_id="Contoso.OnMicrosoft.com",
        client_secret=f"  {SECRET}\n",
    )
    row = crm_env.source(source_id)
    assert row["file_extensions"] == ".pdf,.docx" and row["folder_path"] == "/CRM/Contracts" and row["drive_name"] is None
    assert crm_env.box.decrypt(row["client_secret_enc"]) == SECRET  # surrounding whitespace removed
    assert crm_env.box.decrypt(row["tenant_id_enc"]) == "Contoso.OnMicrosoft.com"
    assert clean_folder_path("/") is None and clean_folder_path(None) is None


@pytest.mark.asyncio
async def test_create_source_without_the_encryption_key_is_503(crm_env: CrmEnv):
    crm_env.key_missing = True
    with pytest.raises(ServiceUnavailableError) as info:
        await create(crm_env)
    assert info.value.status_code == 503 and info.value.detail == KEY_MISSING_REASON
    assert crm_env.repo.sources == {}


# ---- sources: update / delete ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_keeps_the_secret_when_omitted_and_rotates_it_when_given(crm_env: CrmEnv):
    source_id = await create(crm_env)
    before = dict(crm_env.source(source_id))
    crm_env.settings.audit_enabled = True

    renamed = await crm_env.service.update_source(SA, source_id, SourceUpdate(name="Renamed"))
    row = crm_env.source(source_id)
    assert renamed.name == "Renamed" and row["updated_by"] == SA.id
    assert (row["client_secret_enc"], row["secret_updated_at"], row["client_secret_hint"]) == (
        before["client_secret_enc"], before["secret_updated_at"], before["client_secret_hint"],
    )
    await asyncio.sleep(0.002)
    rotated = await crm_env.service.update_source(SA, source_id, SourceUpdate(client_secret=SecretStr(NEW_SECRET)))
    row = crm_env.source(source_id)
    assert crm_env.box.decrypt(row["client_secret_enc"]) == NEW_SECRET
    assert rotated.client_secret_hint == "…" + NEW_SECRET[-4:]
    assert parse_utc(row["secret_updated_at"]) > parse_utc(before["secret_updated_at"])

    rename, rotation = crm_env.audits
    assert rename["old_value"] == {"name": "Sales contracts"} and rename["new_value"] == {"name": "Renamed"}
    assert rotation["old_value"] == {"client_secret": "set"} and rotation["new_value"] == {"client_secret": "rotated"}
    assert NEW_SECRET not in json.dumps(crm_env.audits, default=str) and NEW_SECRET not in rotated.model_dump_json()


def _fernet_box(*keys: str):
    from cryptography.fernet import Fernet

    from backend.doc_intel.crypto import SecretBox

    return SecretBox([Fernet(k) for k in keys])


@pytest.mark.asyncio
async def test_key_rotation_resaving_the_secret_moves_every_credential_to_the_new_key(crm_env: CrmEnv):
    """F25: the rotation in crypto.py (new key first, re-save each source's credentials, then drop
    the old key). Re-saving re-encrypted only the secret, so dropping the old key left the Tenant
    and Client IDs undecryptable and every sync of the source failed."""
    from cryptography.fernet import Fernet

    old_key, new_key = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    crm_env.box = _fernet_box(old_key)
    source_id = await create(crm_env)
    before = dict(crm_env.source(source_id))
    crm_env.box = _fernet_box(new_key, old_key)  # step 1: the new key first, the old one kept
    crm_env.settings.audit_enabled = True
    # step 2, as the edit dialog does it: only the secret is sent (the identifiers are unchanged)
    await crm_env.service.update_source(SA, source_id, SourceUpdate(client_secret=SecretStr(NEW_SECRET)))
    crm_env.box = _fernet_box(new_key)  # step 3: the old key dropped
    creds = await crm_env.service._credentials(source_id)
    assert (creds.tenant_id, creds.client_id, creds.client_secret) == (TENANT_ID, CLIENT_ID, NEW_SECRET)
    out = await crm_env.service.get_source_out(source_id, SA)
    assert out.credentials_readable is True and (out.tenant_id, out.client_id) == (TENANT_ID, CLIENT_ID)
    row = crm_env.source(source_id)
    assert row["tenant_id_enc"] != before["tenant_id_enc"] and row["client_id_enc"] != before["client_id_enc"]
    # The identifiers did not change, so the audit records the secret rotation only.
    (audit,) = crm_env.audits
    assert audit["old_value"] == {"client_secret": "set"} and audit["new_value"] == {"client_secret": "rotated"}


@pytest.mark.asyncio
async def test_key_rotation_a_changed_identifier_also_re_encrypts_the_kept_secret(crm_env: CrmEnv):
    from cryptography.fernet import Fernet

    old_key, new_key = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    crm_env.box = _fernet_box(old_key)
    source_id = await create(crm_env)
    before = dict(crm_env.source(source_id))
    crm_env.box = _fernet_box(new_key, old_key)
    await crm_env.service.update_source(SA, source_id, SourceUpdate(tenant_id="contoso.onmicrosoft.com"))
    crm_env.box = _fernet_box(new_key)
    creds = await crm_env.service._credentials(source_id)
    assert (creds.tenant_id, creds.client_id, creds.client_secret) == ("contoso.onmicrosoft.com", CLIENT_ID, SECRET)
    row = crm_env.source(source_id)
    # Re-encrypting the kept secret is not a secret change: its hint and date stay.
    assert (row["client_secret_hint"], row["secret_updated_at"]) == (before["client_secret_hint"], before["secret_updated_at"])


@pytest.mark.asyncio
async def test_update_with_the_same_values_changes_nothing(crm_env: CrmEnv):
    source_id = await create(crm_env)
    crm_env.source(source_id).update(resolved_site_id="s", resolved_drive_id="d", resolved_folder_id="f")
    before = dict(crm_env.source(source_id))
    crm_env.settings.audit_enabled = True
    # What the edit dialog sends back unchanged (the identifiers are shown to the admin).
    same = SourceUpdate(
        name="Sales contracts", tenant_id=TENANT_ID, client_id=CLIENT_ID, site_url=crm_env.source(source_id)["site_url"],
        drive_name="Documents", folder_path="/CRM/Contracts", recursive=True, file_extensions=[".pdf", ".docx"],
        sync_enabled=False, sync_interval_days=14, sync_hour=2, status="ACTIVE", account_id=3,
    )
    await crm_env.service.update_source(SA, source_id, same)
    assert crm_env.source(source_id) == before and crm_env.audits == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"site_url": "https://contoso.sharepoint.com/sites/Finance"},
        {"drive_name": "Contracts"},
        {"drive_name": None},
        {"folder_path": "/Other"},
        {"tenant_id": "11111111-2222-3333-4444-555555555555"},
    ],
)
async def test_changing_the_location_forgets_the_resolved_ids(crm_env: CrmEnv, change):
    source_id = await create(crm_env)
    crm_env.source(source_id).update(resolved_site_id="s", resolved_drive_id="d", resolved_folder_id="f")
    await crm_env.service.update_source(SA, source_id, SourceUpdate(**change))
    row = crm_env.source(source_id)
    assert (row["resolved_site_id"], row["resolved_drive_id"], row["resolved_folder_id"]) == (None, None, None)


@pytest.mark.asyncio
async def test_other_changes_keep_the_resolved_ids(crm_env: CrmEnv):
    source_id = await create(crm_env)
    crm_env.source(source_id).update(resolved_site_id="s", resolved_drive_id="d", resolved_folder_id="f")
    await crm_env.service.update_source(SA, source_id, SourceUpdate(name="x", client_secret=SecretStr(NEW_SECRET), recursive=False))
    assert crm_env.source(source_id)["resolved_folder_id"] == "f" and crm_env.source(source_id)["recursive"] == 0


@pytest.mark.asyncio
async def test_schedule_changes_recompute_next_sync_at(crm_env: CrmEnv):
    tz = crm_env.settings.timezone
    source_id = await create(crm_env)
    await crm_env.service.update_source(SA, source_id, SourceUpdate(sync_enabled=True, sync_hour=4))
    first = parse_utc(crm_env.source(source_id)["next_sync_at"])
    assert first in {schedule.next_hour_occurrence(utc_now() + timedelta(seconds=s), hour=4, tz_name=tz) for s in (-5, 0)}

    # A recent sync: "last sync + interval" is still ahead, so that is the next run.
    last = utc_now() - timedelta(days=2)
    crm_env.source(source_id)["last_sync_at"] = last.isoformat()
    await crm_env.service.update_source(SA, source_id, SourceUpdate(sync_interval_days=7))
    assert parse_utc(crm_env.source(source_id)["next_sync_at"]) == schedule.next_after_run(last, interval_days=7, hour=4, tz_name=tz)

    await crm_env.service.update_source(SA, source_id, SourceUpdate(status="DISABLED"))
    assert crm_env.source(source_id)["next_sync_at"] is None and crm_env.source(source_id)["status"] == "DISABLED"
    await crm_env.service.update_source(SA, source_id, SourceUpdate(status="ACTIVE"))
    assert crm_env.source(source_id)["next_sync_at"] is not None
    await crm_env.service.update_source(SA, source_id, SourceUpdate(sync_enabled=False))
    assert crm_env.source(source_id)["next_sync_at"] is None


@pytest.mark.asyncio
async def test_updating_credentials_needs_the_key_but_other_fields_do_not(crm_env: CrmEnv):
    source_id = await create(crm_env)
    crm_env.key_missing = True
    out = await crm_env.service.update_source(SA, source_id, SourceUpdate(name="Still editable"))
    assert out.name == "Still editable" and out.credentials_readable is False and out.tenant_id is None
    for change in ({"client_secret": NEW_SECRET}, {"tenant_id": TENANT_ID}, {"client_id": CLIENT_ID}):
        with pytest.raises(ServiceUnavailableError) as info:
            await crm_env.service.update_source(SA, source_id, SourceUpdate(**change))
        assert info.value.detail == KEY_MISSING_REASON


@pytest.mark.asyncio
async def test_update_can_clear_the_account_and_rejects_unknown_ones(crm_env: CrmEnv):
    source_id = await create(crm_env)
    out = await crm_env.service.update_source(SA, source_id, SourceUpdate(account_id=None))
    assert out.account_id is None and out.account_name is None
    with pytest.raises(NotFoundError):
        await crm_env.service.update_source(SA, source_id, SourceUpdate(account_id=12345))
    with pytest.raises(NotFoundError):
        await crm_env.service.update_source(SA, 999, SourceUpdate(name="x"))


@pytest.mark.asyncio
async def test_delete_is_soft_withdraws_the_entities_and_waits_for_the_sync(crm_env: CrmEnv):
    crm_env.settings.audit_enabled = True
    crm_env.graph.files = [remote("a")]
    source_id = await create(crm_env, sync_enabled=True)
    await crm_env.sync(source_id)
    queued = await crm_env.repo.create_run(source_id, trigger_type="MANUAL", triggered_by=1)
    for status in ("QUEUED", "RUNNING"):
        crm_env.repo.runs[queued]["status"] = status
        with pytest.raises(ConflictError) as info:
            await crm_env.service.delete_source(SA, source_id)
        assert info.value.detail == "A sync is queued or running for this source — delete it once the sync has finished"
    assert crm_env.source(source_id)["status"] == "ACTIVE" and len(crm_env.active_entities()) == 2
    crm_env.repo.runs[queued]["status"] = "COMPLETED"
    runs_before, files_before = copy.deepcopy(crm_env.repo.runs), copy.deepcopy(crm_env.repo.files)

    await crm_env.service.delete_source(SA, source_id)
    row = crm_env.source(source_id)
    assert row["status"] == "DELETED" and row["client_secret_enc"] is None and row["client_secret_hint"] is None
    assert row["sync_enabled"] == 0 and row["next_sync_at"] is None
    assert crm_env.active_entities() == []
    assert all(e["status"] == "WITHDRAWN" and e["withdrawn_at"] for e in crm_env.repo.entities.values())
    assert crm_env.repo.runs == runs_before and crm_env.repo.files == files_before  # history kept
    listing = await crm_env.service.list_entities(status="WITHDRAWN")
    assert listing.total == 2 and {e.source_name for e in listing.items} == {"Sales contracts (deleted)"}
    delete = crm_env.audits[-1]
    assert delete["action_type"] == "DELETE" and delete["new_value"] == {"status": "DELETED", "entities_withdrawn": 2}
    with pytest.raises(NotFoundError):
        await crm_env.service.get_source_out(source_id)
    with pytest.raises(NotFoundError):
        await crm_env.service.delete_source(SA, source_id)
    with pytest.raises(NotFoundError):
        await crm_env.service.test_connection(source_id)
    assert await crm_env.service.list_sources() == []


# ---- "Sync now" -----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_now_queues_one_manual_run(crm_env: CrmEnv):
    source_id = await create(crm_env)
    run = await crm_env.service.sync_now(SA, source_id)
    assert (run.status, run.trigger_type, run.triggered_by, run.source_id) == ("QUEUED", "MANUAL", SA.id, source_id)
    assert crm_env.wakes == [1]
    with pytest.raises(ConflictError) as info:
        await crm_env.service.sync_now(SA, source_id)
    assert info.value.status_code == 409 and info.value.detail == "A sync is already queued or running for this source"
    source = await crm_env.service.get_source_out(source_id)
    assert source.active_run is not None and source.active_run.id == run.id


@pytest.mark.asyncio
async def test_sync_now_refuses_disabled_and_unknown_sources(crm_env: CrmEnv):
    source_id = await create(crm_env)
    await crm_env.service.update_source(SA, source_id, SourceUpdate(status="DISABLED"))
    with pytest.raises(ConflictError) as info:
        await crm_env.service.sync_now(SA, source_id)
    assert (info.value.status_code, info.value.detail) == (409, "This source is disabled — enable it first")
    assert crm_env.repo.runs == {}
    with pytest.raises(NotFoundError):
        await crm_env.service.sync_now(SA, 404)


# ---- the diff --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_sync_processes_every_file(crm_env: CrmEnv):
    crm_env.graph.files = [remote("a"), remote("b", "b.docx"), remote("c")]
    source_id = await create(crm_env)
    run_id, status = await crm_env.sync(source_id)
    assert status == "COMPLETED"
    run = crm_env.run(run_id)
    assert (run["files_seen"], run["files_new"], run["files_changed"], run["files_unchanged"], run["files_deleted"],
            run["files_failed"]) == (3, 3, 0, 0, 0, 0)
    details = loads_json(run["details_json"], {})
    assert details["listing_complete"] is True and details["files_processed"] == 3 and "seconds" in details
    for item in ("a", "b", "c"):
        assert crm_env.stages(item) == dict.fromkeys(STAGES, "COMPLETED")
        row = crm_env.file_by_item(item)
        assert (row["status"], row["attempts"], row["entity_count"], row["is_valid"]) == ("COMPLETED", 1, 2, 1)
        assert row["content_sha256"] and row["processed_at"] and row["last_run_id"] == run_id
        assert json.loads(row["result_json"])["extraction"]["document_id"]
    assert [c["media_type"] for c in crm_env.extractor.calls] == [
        "application/pdf", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "application/pdf",
    ]
    assert all(c["use_intelligence"] is False and c["source_exists"] for c in crm_env.extractor.calls)

    source = crm_env.source(source_id)
    assert (source["resolved_site_id"], source["resolved_drive_id"], source["resolved_folder_id"]) == ("site-1", "drive-1", "folder-1")
    assert source["last_sync_status"] == "COMPLETED" and source["last_success_at"] and source["last_sync_error"] is None
    (creds,) = crm_env.graph_factory.credentials
    assert (creds.tenant_id, creds.client_id, creds.client_secret) == (TENANT_ID, CLIENT_ID, SECRET)
    assert SECRET not in repr(creds)
    assert crm_env.graph.closed == 1
    ((_, target, recursive, extensions, max_files),) = [c for c in crm_env.graph.calls if c[0] == "list"]
    assert target == TARGET and recursive is True and extensions == (".pdf", ".docx") and max_files == 5000

    entities = {e["match_key"]: e for e in crm_env.active_entities("a")}
    assert set(entities) == {"email:mona.adel+a@example.com", "tax_id:123456789"}
    contact = entities["email:mona.adel+a@example.com"]
    assert (contact["entity_type"], contact["display_value"], contact["confidence"]) == ("contact", "Mona Adel a", 0.9)
    assert contact["account_id"] == 3 and contact["source_id"] == source_id


@pytest.mark.asyncio
async def test_second_sync_handles_new_changed_unchanged_and_deleted(crm_env: CrmEnv):
    crm_env.graph.files = [remote("a"), remote("b"), remote("c")]
    source_id = await create(crm_env)
    await crm_env.sync(source_id)
    b_seen = crm_env.file_by_item("b")["last_seen_at"]
    old_a = {e["id"] for e in crm_env.active_entities("a")}
    b_entities = {e["id"] for e in crm_env.active_entities("b")}

    crm_env.graph.files = [remote("a", ctag="c2"), remote("b"), remote("d")]  # a changed, b same, c gone, d new
    crm_env.extractor.calls.clear()
    await asyncio.sleep(0.002)
    run_id, status = await crm_env.sync(source_id)
    assert status == "COMPLETED"
    run = crm_env.run(run_id)
    assert (run["files_seen"], run["files_new"], run["files_changed"], run["files_unchanged"], run["files_deleted"],
            run["files_failed"]) == (3, 1, 1, 1, 1, 0)
    assert [c["filename"] for c in crm_env.extractor.calls] == ["d.pdf", "a.pdf"]  # b is never re-extracted

    c = crm_env.file_by_item("c")
    assert c["state"] == "DELETED" and c["deleted_at"]
    assert crm_env.active_entities("c") == []
    assert all(e["status"] == "WITHDRAWN" and e["withdrawn_at"] for e in crm_env.repo.entities.values() if e["source_file_id"] == c["id"])
    a = crm_env.file_by_item("a")
    assert a["ctag"] == "c2" and a["attempts"] == 1 and a["status"] == "COMPLETED"
    assert all(crm_env.repo.entities[i]["status"] == "WITHDRAWN" for i in old_a)
    assert {e["id"] for e in crm_env.active_entities("a")}.isdisjoint(old_a) and len(crm_env.active_entities("a")) == 2
    assert {e["id"] for e in crm_env.active_entities("b")} == b_entities
    assert parse_utc(crm_env.file_by_item("b")["last_seen_at"]) > parse_utc(b_seen)
    counts = (await crm_env.service.get_source_out(source_id)).counts
    assert counts == {"files_active": 3, "files_failed": 0, "files_deleted": 1, "entities_active": 6}


@pytest.mark.asyncio
async def test_a_deleted_file_that_comes_back_is_processed_again(crm_env: CrmEnv):
    crm_env.graph.files = [remote("a"), remote("b")]
    source_id = await create(crm_env)
    await crm_env.sync(source_id)
    crm_env.graph.files = [remote("a")]
    await crm_env.sync(source_id)
    assert crm_env.file_by_item("b")["state"] == "DELETED"
    crm_env.graph.files = [remote("a"), remote("b")]  # restored from the recycle bin
    run_id, _ = await crm_env.sync(source_id)
    b = crm_env.file_by_item("b")
    assert (b["state"], b["deleted_at"], b["status"]) == ("ACTIVE", None, "COMPLETED")
    assert crm_env.run(run_id)["files_new"] == 1 and len(crm_env.active_entities("b")) == 2
    assert len([f for f in crm_env.repo.files.values() if f["item_id"] == "b"]) == 1  # the same row, re-activated


@pytest.mark.asyncio
async def test_a_partial_listing_deletes_nothing(crm_env: CrmEnv):
    crm_env.graph.files = [remote("a"), remote("b")]
    source_id = await create(crm_env)
    await crm_env.sync(source_id)
    crm_env.graph.files = [remote("a")]
    crm_env.graph.complete = False
    crm_env.graph.truncated_reason = "The listing stopped at DOC_INTEL_SYNC_MAX_FILES (1)"
    run_id, status = await crm_env.sync(source_id)
    assert status == "PARTIAL"
    run = crm_env.run(run_id)
    assert run["files_deleted"] == 0 and run["files_failed"] == 0
    assert run["error_message"] == (
        "The folder listing was incomplete (The listing stopped at DOC_INTEL_SYNC_MAX_FILES (1)), "
        "so deleted files were not detected"
    )
    assert loads_json(run["details_json"], {})["listing_complete"] is False
    b = crm_env.file_by_item("b")
    assert b["state"] == "ACTIVE" and len(crm_env.active_entities("b")) == 2
    assert crm_env.source(source_id)["last_sync_status"] == "PARTIAL" and crm_env.source(source_id)["last_success_at"]


def test_content_change_falls_back_from_ctag_to_etag_to_hash_to_size_and_time():
    base = remote("a", ctag="c1", etag="e1", quick_xor_hash="h1")
    row = {"ctag": "c1", "etag": "e1", "quick_xor_hash": "h1", "size_bytes": 1234, "modified_at": "2026-09-01T10:00:00"}
    assert content_changed(row, base) is False
    assert content_changed(row, remote("a", ctag="c2", etag="e1")) is True
    assert content_changed(row, remote("a", ctag="c1", etag="e2")) is False  # a rename changes the eTag only
    assert content_changed({**row, "ctag": None}, remote("a", etag="e2")) is True
    assert content_changed({**row, "ctag": None, "etag": None}, remote("a", quick_xor_hash="h2")) is True
    no_tags = {"ctag": None, "etag": None, "quick_xor_hash": None, "size_bytes": 1234, "modified_at": "2026-09-01T10:00:00.4"}
    assert content_changed(no_tags, remote("a", ctag=None, etag=None, quick_xor_hash=None)) is False
    assert content_changed(no_tags, remote("a", ctag=None, etag=None, quick_xor_hash=None, size=99)) is True
    assert content_changed({k: None for k in no_tags}, remote("a", ctag=None, etag=None, quick_xor_hash=None)) is True


def test_plan_sync_rules():
    def tracked(file_id: int, item_id: str, status: str, attempts: int) -> dict:
        return {"id": file_id, "drive_id": "drive-1", "item_id": item_id, "ctag": "c1", "status": status,
                "attempts": attempts, "name": f"{item_id}.pdf", "path": "/CRM", "web_url": None, "etag": "e1",
                "size_bytes": 1234, "modified_at": "2026-09-01T10:00:00"}

    active = [
        tracked(1, "same", "COMPLETED", 1),
        tracked(2, "failed", "FAILED", 2),
        tracked(3, "spent", "FAILED", MAX_FILE_ATTEMPTS),
        tracked(4, "pending", "PENDING", 0),
        tracked(5, "gone", "COMPLETED", 1),
    ]
    files = [
        remote("same", web_url=None),
        remote("failed", web_url=None),
        remote("spent", web_url=None),
        remote("pending", web_url=None),
        remote("new"),
        remote("new"),  # listed twice
        remote("", "nameless.pdf"),  # no id: ignored
        remote("later", ctag="c9"),
    ]
    plan = plan_sync(active, files, complete=True)
    assert [f.item_id for f in plan.new] == ["new", "later"]
    assert [i for i, _ in plan.retry] == [2, 4] and [i for i, _ in plan.unchanged] == [1, 3]
    assert plan.deleted == [5] and plan.seen == 6 and plan.ignored == 1 and plan.renamed == []
    moved = plan_sync(active, [remote("same", "same (1).pdf", web_url=None)], complete=False)
    assert [i for i, _ in moved.renamed] == [1] and moved.deleted == []  # incomplete: nothing deleted


# ---- per-file failures -------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", STAGES)
async def test_a_failing_stage_fails_only_that_file(crm_env: CrmEnv, stage, monkeypatch):
    crm_env.graph.files = [remote("a"), remote("b"), remote("c")]
    reason = {
        "download": "Microsoft Graph denied access to the file (403)",
        "extraction": "PDF is password-protected",
        "intelligence": "The CRM schema file clients.json is invalid",
        "entities": "The CRM extraction process finished without a usable result",
        "persist": "Could not store the entities in the CRM store: ORA-01653: unable to extend table",
    }[stage]
    if stage == "download":
        crm_env.graph.download_errors["b"] = GraphFailed(reason, code="forbidden", suggested_action="Grant Sites.Read.All")
    elif stage == "persist":
        original = crm_env.repo.complete_file

        async def failing(file_id, **kw):
            if crm_env.repo.files[file_id]["item_id"] == "b":
                raise RuntimeError("ORA-01653: unable to extend table")
            return await original(file_id, **kw)

        monkeypatch.setattr(crm_env.repo, "complete_file", failing)
    else:
        crm_env.extractor.errors["b.pdf"] = CrmExtractionFailed(reason, stage=stage, code="x", suggested_action="Do y")
    source_id = await create(crm_env)
    run_id, status = await crm_env.sync(source_id)

    assert status == "PARTIAL"
    run = crm_env.run(run_id)
    assert run["files_failed"] == 1 and run["error_message"] == "1 of 3 files failed"
    b = crm_env.file_by_item("b")
    assert (b["status"], b["failed_stage"], b["error_message"]) == ("FAILED", stage, reason)
    index = STAGES.index(stage)
    expected = {s: "COMPLETED" for s in STAGES[:index]} | {stage: "FAILED"} | {s: "PENDING" for s in STAGES[index + 1:]}
    assert crm_env.stages("b") == expected
    details = loads_json(b["stage_details"], {})
    assert details[stage]["error"] == reason
    if stage != "persist":
        assert details[stage]["metrics"]["suggested_action"]
    assert crm_env.active_entities("b") == []
    for item in ("a", "c"):  # one file failing never stops the run
        assert crm_env.file_by_item(item)["status"] == "COMPLETED"
    out = (await crm_env.service.list_files(source_id, status="FAILED")).items
    assert [f.name for f in out] == ["b.pdf"] and out[0].stages[index].error == reason
    assert crm_env.source(source_id)["last_sync_status"] == "PARTIAL"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "stage", "expected"),
    [
        # Refused before anything was extracted: the stages before the failed one never ran.
        ("not_supported", "intelligence", ("COMPLETED", "PENDING", "FAILED", "PENDING", "PENDING")),
        ("unavailable", "intelligence", ("COMPLETED", "PENDING", "FAILED", "PENDING", "PENDING")),
        ("missing_dependency", "intelligence", ("COMPLETED", "PENDING", "FAILED", "PENDING", "PENDING")),
        ("unavailable", "extraction", ("COMPLETED", "FAILED", "PENDING", "PENDING", "PENDING")),
        ("source_missing", "extraction", ("COMPLETED", "FAILED", "PENDING", "PENDING", "PENDING")),
        # Reported by the child from its progress: it got that far.
        ("timeout", "entities", ("COMPLETED", "COMPLETED", "COMPLETED", "FAILED", "PENDING")),
        ("crash", "intelligence", ("COMPLETED", "COMPLETED", "FAILED", "PENDING", "PENDING")),
        ("schema_error", "intelligence", ("COMPLETED", "COMPLETED", "FAILED", "PENDING", "PENDING")),
    ],
)
async def test_extraction_failures_that_never_started_leave_the_earlier_stages_pending(crm_env: CrmEnv, code, stage, expected):
    crm_env.graph.files = [remote("a")]
    crm_env.extractor.errors["a.pdf"] = CrmExtractionFailed("reason", stage=stage, code=code)
    source_id = await create(crm_env)
    await crm_env.sync(source_id)
    assert tuple(crm_env.stages("a").values()) == expected
    details = loads_json(crm_env.file_by_item("a")["stage_details"], {})
    assert details[stage]["metrics"]["code"] == code
    if expected[1] == "PENDING":
        assert "extraction" not in details  # no timing is kept for a stage that never ran


@pytest.mark.asyncio
async def test_a_file_over_the_size_limit_is_not_downloaded(crm_env: CrmEnv):
    crm_env.settings.sync_max_file_mb = 1
    crm_env.graph.files = [remote("big", size=3 * 1024 * 1024)]
    source_id = await create(crm_env)
    await crm_env.sync(source_id)
    big = crm_env.file_by_item("big")
    assert big["failed_stage"] == "download"
    assert big["error_message"] == "File is 3 MB; the limit is 1 MB (DOC_INTEL_SYNC_MAX_FILE_MB)"
    assert ("download", "big") not in crm_env.graph.calls


@pytest.mark.asyncio
async def test_an_unexpected_error_in_one_file_is_contained_and_scrubbed(crm_env: CrmEnv, caplog):
    crm_env.settings.error_log_enabled = True
    crm_env.graph.files = [remote("a"), remote("b")]
    crm_env.extractor.errors["a.pdf"] = RuntimeError(f"boom while using {SECRET}")
    source_id = await create(crm_env)
    with caplog.at_level(logging.DEBUG):
        run_id, status = await crm_env.sync(source_id)
    assert status == "PARTIAL"
    a = crm_env.file_by_item("a")
    assert (a["failed_stage"], a["error_message"]) == ("extraction", "Unexpected error: RuntimeError: boom while using [redacted]")
    assert crm_env.file_by_item("b")["status"] == "COMPLETED"
    (logged,) = crm_env.errors
    assert logged["path"] == f"/api/doc-intel/sources/{source_id}/runs"
    for text in (logged["exception_message"], logged["stack_trace"], caplog.text):
        assert SECRET not in text
    assert "[redacted]" in logged["stack_trace"]


# ---- run-level failures ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_sign_in_failure_fails_the_run(crm_env: CrmEnv):
    reason = "Microsoft rejected the client secret (AADSTS7000215: invalid client secret)"
    crm_env.graph.token_error = GraphFailed(
        reason, code="auth_invalid_secret", suggested_action="Create a new client secret in Entra ID"
    )
    crm_env.graph.files = [remote("a")]
    source_id = await create(crm_env, sync_enabled=True)
    run_id, status = await crm_env.sync(source_id)
    assert status == "FAILED"
    run = crm_env.run(run_id)
    assert run["error_message"] == reason and run["finished_at"]
    details = loads_json(run["details_json"], {})
    assert (details["failed_step"], details["error_code"]) == ("token", "auth_invalid_secret")
    assert details["suggested_action"] == "Create a new client secret in Entra ID"
    source = crm_env.source(source_id)
    assert (source["last_sync_status"], source["last_sync_error"], source["last_success_at"]) == ("FAILED", reason, None)
    assert crm_env.repo.files == {} and crm_env.graph.closed == 1
    started = parse_utc(run["started_at"])
    assert parse_utc(source["next_sync_at"]) == schedule.next_after_run(
        started, interval_days=14, hour=2, tz_name=crm_env.settings.timezone
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("step", ["resolve", "listing"])
async def test_resolve_and_listing_failures_fail_the_run(crm_env: CrmEnv, step):
    error = GraphFailed("The folder '/CRM' was not found in the document library", code="not_found", suggested_action="Check")
    if step == "resolve":
        crm_env.graph.resolve_error = error
    else:
        crm_env.graph.list_errors = [error]
    source_id = await create(crm_env)
    run_id, status = await crm_env.sync(source_id)
    assert status == "FAILED" and crm_env.run(run_id)["error_message"] == error.reason
    assert loads_json(crm_env.run(run_id)["details_json"], {})["failed_step"] == step


@pytest.mark.asyncio
async def test_missing_key_or_unreadable_credentials_fail_the_run(crm_env: CrmEnv):
    source_id = await create(crm_env)
    crm_env.key_missing = True
    run_id, status = await crm_env.sync(source_id)
    assert status == "FAILED" and crm_env.run(run_id)["error_message"] == KEY_MISSING_REASON
    details = loads_json(crm_env.run(run_id)["details_json"], {})
    assert details["failed_step"] == "credentials" and "DOC_INTEL_SECRETS_KEY" in details["suggested_action"]
    crm_env.key_missing = False
    crm_env.box.broken = True
    run_id, status = await crm_env.sync(source_id)
    assert status == "FAILED" and "could not be decrypted" in crm_env.run(run_id)["error_message"]
    assert "enter the Tenant ID" in loads_json(crm_env.run(run_id)["details_json"], {})["suggested_action"]


@pytest.mark.asyncio
async def test_a_reason_that_echoes_the_secret_is_scrubbed(crm_env: CrmEnv):
    crm_env.graph.token_error = GraphFailed(f"Sign-in failed for secret {SECRET}", code="auth_failed")
    source_id = await create(crm_env)
    run_id, _ = await crm_env.sync(source_id)
    assert crm_env.run(run_id)["error_message"] == "Sign-in failed for secret [redacted]"
    assert SECRET not in json.dumps(crm_env.repo.sources) + json.dumps(crm_env.repo.runs)


@pytest.mark.asyncio
async def test_a_cached_folder_that_is_gone_is_resolved_again(crm_env: CrmEnv):
    source_id = crm_env.seed_source(resolved_site_id="old-site", resolved_drive_id="old-drive", resolved_folder_id="old-folder")
    crm_env.graph.list_errors = [GraphFailed("The folder was not found", code="not_found")]
    crm_env.graph.files = [remote("a")]
    run_id, status = await crm_env.sync(source_id)
    assert status == "COMPLETED"
    lists = [c for c in crm_env.graph.calls if c[0] == "list"]
    assert [c[1].folder_id for c in lists] == ["old-folder", "folder-1"]
    assert sum(1 for c in crm_env.graph.calls if c[0] == "resolve") == 1
    assert crm_env.source(source_id)["resolved_folder_id"] == "folder-1"


@pytest.mark.asyncio
async def test_cached_ids_skip_the_resolution(crm_env: CrmEnv):
    source_id = crm_env.seed_source(resolved_site_id="s", resolved_drive_id="d", resolved_folder_id="f")
    await crm_env.sync(source_id)
    assert not any(c[0] == "resolve" for c in crm_env.graph.calls)


@pytest.mark.asyncio
async def test_a_run_for_a_disabled_or_deleted_source_fails(crm_env: CrmEnv):
    source_id = await create(crm_env)
    await crm_env.service.update_source(SA, source_id, SourceUpdate(status="DISABLED"))
    run_id, status = await crm_env.sync(source_id)
    assert status == "FAILED" and "disabled" in crm_env.run(run_id)["error_message"]

    other = await create(crm_env, name="Other")
    run_id = await crm_env.repo.create_run(other, trigger_type="MANUAL", triggered_by=1)
    row = await crm_env.repo.claim_next_run("w")
    with pytest.raises(ConflictError):  # an admin delete waits for the sync ...
        await crm_env.service.delete_source(SA, other)
    crm_env.repo.sources[other]["status"] = "DELETED"  # ... but a run can still meet a deleted source (a race)
    assert await crm_env.service.process_run(row) == "FAILED"
    assert crm_env.run(run_id)["error_message"] == "The source was deleted"


@pytest.mark.asyncio
async def test_deleting_the_source_mid_sync_stops_at_the_next_file(crm_env: CrmEnv):
    crm_env.graph.files = [remote("a"), remote("b"), remote("c")]
    source_id = await create(crm_env)

    def delete_after_first(filename: str) -> None:
        if filename == "a.pdf":
            crm_env.repo.sources[source_id]["status"] = "DELETED"

    crm_env.extractor.before = delete_after_first
    run_id, status = await crm_env.sync(source_id)
    assert status == "FAILED" and crm_env.run(run_id)["error_message"] == "Stopped: the source was deleted during the sync"
    assert crm_env.file_by_item("a")["status"] == "COMPLETED"
    assert crm_env.file_by_item("b")["status"] == "PENDING" and crm_env.file_by_item("c")["status"] == "PENDING"


# ---- retries ---------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_failed_unchanged_file_is_retried_while_attempts_remain(crm_env: CrmEnv):
    crm_env.graph.files = [remote("a"), remote("b")]
    crm_env.extractor.errors["b.pdf"] = CrmExtractionFailed("File is corrupt", stage="extraction")
    source_id = await create(crm_env)
    for attempt in range(1, MAX_FILE_ATTEMPTS + 1):
        crm_env.extractor.calls.clear()
        _, status = await crm_env.sync(source_id)
        assert status == "PARTIAL"
        assert crm_env.file_by_item("b")["attempts"] == attempt
        assert "b.pdf" in [c["filename"] for c in crm_env.extractor.calls]
        assert "a.pdf" not in [c["filename"] for c in crm_env.extractor.calls][1:]
    crm_env.extractor.calls.clear()
    run_id, status = await crm_env.sync(source_id)  # attempts used up: left FAILED, not retried
    assert status == "COMPLETED" and crm_env.extractor.calls == []
    assert crm_env.file_by_item("b")["status"] == "FAILED" and crm_env.run(run_id)["files_unchanged"] == 2

    # An admin retry resets the budget and queues a run; the file then goes through.
    del crm_env.extractor.errors["b.pdf"]
    file_id = crm_env.file_by_item("b")["id"]
    out = await crm_env.service.retry_file(SA, file_id)
    assert out.status == "PENDING" and out.attempts == 0 and [s.status for s in out.stages] == ["PENDING"] * 5
    assert crm_env.wakes and any(r["status"] == "QUEUED" for r in crm_env.repo.runs.values())
    row = await crm_env.repo.claim_next_run("w")
    assert await crm_env.service.process_run(row) == "COMPLETED"
    assert crm_env.file_by_item("b")["status"] == "COMPLETED" and crm_env.file_by_item("b")["attempts"] == 1


@pytest.mark.asyncio
async def test_a_changed_file_gets_a_fresh_retry_budget(crm_env: CrmEnv):
    crm_env.graph.files = [remote("b")]
    crm_env.extractor.errors["b.pdf"] = CrmExtractionFailed("corrupt", stage="extraction")
    source_id = await create(crm_env)
    for _ in range(MAX_FILE_ATTEMPTS):
        await crm_env.sync(source_id)
    assert crm_env.file_by_item("b")["attempts"] == MAX_FILE_ATTEMPTS
    del crm_env.extractor.errors["b.pdf"]
    crm_env.graph.files = [remote("b", ctag="c-new")]
    await crm_env.sync(source_id)
    assert crm_env.file_by_item("b")["status"] == "COMPLETED" and crm_env.file_by_item("b")["attempts"] == 1


@pytest.mark.asyncio
async def test_retry_rules(crm_env: CrmEnv):
    crm_env.graph.files = [remote("a"), remote("b")]
    crm_env.extractor.errors["b.pdf"] = CrmExtractionFailed("corrupt", stage="extraction")
    source_id = await create(crm_env)
    await crm_env.sync(source_id)
    ok_id, failed_id = crm_env.file_by_item("a")["id"], crm_env.file_by_item("b")["id"]

    with pytest.raises(NotFoundError):
        await crm_env.service.retry_file(SA, 999)
    with pytest.raises(ConflictError) as info:
        await crm_env.service.retry_file(SA, ok_id)
    assert info.value.detail == "Only failed files can be retried (this one is COMPLETED)"

    queued = await crm_env.repo.create_run(source_id, trigger_type="SCHEDULED", triggered_by=None)
    out = await crm_env.service.retry_file(SA, failed_id)  # the queued run picks it up: no second run
    assert out.status == "PENDING" and [r["id"] for r in crm_env.repo.runs.values() if r["status"] == "QUEUED"] == [queued]
    await crm_env.service.retry_file(SA, failed_id)  # PENDING already: idempotent
    await crm_env.repo.claim_next_run("w")  # now RUNNING
    crm_env.repo.files[failed_id]["status"] = "FAILED"
    out = await crm_env.service.retry_file(SA, failed_id)  # reset; the run after the running one takes it
    assert out.status == "PENDING" and out.attempts == 0
    assert [r["status"] for r in crm_env.repo.runs.values() if r["source_id"] == source_id][-1] == "RUNNING"
    crm_env.repo.files[failed_id]["status"] = "PROCESSING"
    with pytest.raises(ConflictError) as info:
        await crm_env.service.retry_file(SA, failed_id)
    assert info.value.detail == "The file is being processed right now"

    crm_env.repo.files[failed_id].update(status="FAILED", state="DELETED")
    with pytest.raises(ConflictError) as info:
        await crm_env.service.retry_file(SA, failed_id)
    assert "deleted from SharePoint" in info.value.detail


# ---- schedule after a run -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_next_sync_at_follows_the_run_start_while_the_schedule_is_on(crm_env: CrmEnv):
    tz = crm_env.settings.timezone
    crm_env.graph.files = [remote("a")]
    source_id = await create(crm_env, sync_enabled=True, sync_interval_days=7, sync_hour=5)
    run_id, _ = await crm_env.sync(source_id)
    started = parse_utc(crm_env.run(run_id)["started_at"])
    assert parse_utc(crm_env.source(source_id)["next_sync_at"]) == schedule.next_after_run(started, interval_days=7, hour=5, tz_name=tz)

    off = await create(crm_env, name="Unscheduled")
    await crm_env.sync(off)
    assert crm_env.source(off)["next_sync_at"] is None and crm_env.source(off)["last_sync_status"] == "COMPLETED"


@pytest.mark.asyncio
async def test_a_schedule_edited_during_the_run_is_respected(crm_env: CrmEnv):
    tz = crm_env.settings.timezone
    crm_env.graph.files = [remote("a")]
    source_id = await create(crm_env, sync_enabled=True, sync_interval_days=7, sync_hour=5)
    crm_env.extractor.before = lambda filename: crm_env.repo.sources[source_id].update(sync_interval_days=21)
    run_id, _ = await crm_env.sync(source_id)
    started = parse_utc(crm_env.run(run_id)["started_at"])
    # The run resets the clock with the interval in force when it finished.
    assert parse_utc(crm_env.source(source_id)["next_sync_at"]) == schedule.next_after_run(started, interval_days=21, hour=5, tz_name=tz)

    other = await create(crm_env, name="Other", sync_enabled=True)
    crm_env.extractor.before = lambda filename: crm_env.repo.sources[other].update(sync_enabled=0, next_sync_at=None)
    await crm_env.sync(other)
    assert crm_env.source(other)["next_sync_at"] is None  # switched off during the run: stays off


# ---- temp files ------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_private_download_directories_are_always_removed(crm_env: CrmEnv):
    crm_env.graph.files = [remote("a"), remote("b"), remote("c")]
    crm_env.extractor.errors["b.pdf"] = CrmExtractionFailed("corrupt", stage="extraction")
    crm_env.graph.download_errors["c"] = GraphFailed("network", code="network")
    source_id = await create(crm_env)
    await crm_env.sync(source_id)
    assert len(crm_env.extractor.work_dirs) == 2
    assert all(not d.exists() for d in crm_env.extractor.work_dirs)
    assert list(crm_env.temp_root.iterdir()) == []


def test_sweep_removes_only_old_leftover_directories(tmp_path):
    stale = tmp_path / f"{TEMP_PREFIX}old"
    fresh = tmp_path / f"{TEMP_PREFIX}new"
    unrelated = tmp_path / "keep-me"
    for path in (stale, fresh, unrelated):
        path.mkdir()
        (path / "original.pdf").write_bytes(b"%PDF")
    past = time.time() - 3 * 24 * 3600
    os.utime(stale, (past, past))
    os.utime(unrelated, (past, past))
    assert sweep_stale_temp_dirs(tmp_path) == 1
    assert not stale.exists() and fresh.exists() and unrelated.exists()


# ---- reads -----------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runs_files_and_entities_can_be_listed_and_filtered(crm_env: CrmEnv):
    crm_env.graph.files = [remote("a"), remote("b")]
    crm_env.extractor.errors["b.pdf"] = CrmExtractionFailed("corrupt", stage="extraction")
    source_id = await create(crm_env)
    run_id, _ = await crm_env.sync(source_id)

    runs = await crm_env.service.list_runs(source_id, limit=10)
    assert runs.total == 1 and runs.items[0].id == run_id and runs.items[0].details["files_processed"] == 2
    files = await crm_env.service.list_files(source_id, state="ACTIVE")
    assert files.total == 2 and {f.status for f in files.items} == {"COMPLETED", "FAILED"}
    done = next(f for f in files.items if f.status == "COMPLETED")
    assert done.web_url.startswith("https://") and done.warnings[0].code == "low_text"
    assert (done.entity_count, done.is_valid, done.last_run_id) == (2, True, run_id)

    everything = await crm_env.service.list_entities()
    assert everything.total == 2
    found = await crm_env.service.list_entities(q="mona.adel+a")
    assert [e.entity_type for e in found.items] == ["contact"]
    orgs = await crm_env.service.list_entities(entity_type="organization", source_id=source_id, status="ACTIVE")
    (org,) = orgs.items
    assert org.display_value == "Acme Trading a" and org.fields == {"name": "Acme Trading a", "tax_id": "123-456-789"}
    one = await crm_env.service.get_entity(found.items[0].id)
    assert one.provenance["entity_type"] == "contact" and "fields" in one.provenance and "_meta" not in one.fields
    assert one.file_name == "a.pdf" and one.source_name == "Sales contracts"
    with pytest.raises(NotFoundError):
        await crm_env.service.get_entity(999)
    with pytest.raises(NotFoundError):
        await crm_env.service.list_runs(999)
    with pytest.raises(NotFoundError):
        await crm_env.service.list_files(999)


# ---- connection test ------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connection_test_returns_the_graph_steps(crm_env: CrmEnv):
    source_id = await create(crm_env)
    out = await crm_env.service.test_connection(source_id)
    assert out.ok and [s.key for s in out.steps] == ["credentials", "token", "site", "drive", "folder", "listing"]
    assert out.sample_files == ["a.pdf", "b.docx"] and out.files_found == 2 and out.checked_at.endswith("Z")
    (call,) = [c for c in crm_env.graph.calls if c[0] == "test"]
    assert call[1:] == ("https://contoso.sharepoint.com/sites/Sales", "Documents", "/CRM/Contracts", True, (".pdf", ".docx"))
    assert crm_env.graph.closed == 1
    assert crm_env.repo.sources[source_id]["resolved_site_id"] is None  # diagnostics write nothing


@pytest.mark.asyncio
async def test_connection_test_with_unreadable_credentials_is_a_failed_step(crm_env: CrmEnv):
    source_id = await create(crm_env)
    crm_env.key_missing = True
    out = await crm_env.service.test_connection(source_id)
    assert out.ok is False
    (step,) = out.steps
    assert (step.key, step.ok, step.detail) == ("credentials", False, KEY_MISSING_REASON)
    assert "DOC_INTEL_SECRETS_KEY" in step.suggested_action
    assert crm_env.graph.calls == []


@pytest.mark.asyncio
async def test_connection_test_output_is_scrubbed_and_bounded(crm_env: CrmEnv, monkeypatch):
    source_id = await create(crm_env)
    crm_env.graph.test_result = {
        "ok": False,
        "steps": [
            {"key": "credentials", "label": "Credentials", "ok": True, "latency_ms": 1, "detail": "set"},
            {"key": "token", "label": "Microsoft sign-in", "ok": False, "latency_ms": 9,
             "detail": f"echo {SECRET}", "suggested_action": f"try {SECRET}"},
        ],
    }
    out = await crm_env.service.test_connection(source_id)
    assert out.ok is False and SECRET not in out.model_dump_json() and "[redacted]" in out.steps[1].detail

    def slow(*args, **kwargs):
        time.sleep(1.0)
        return {"ok": True, "steps": []}

    monkeypatch.setattr(crm_sync, "CONNECTION_TEST_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(crm_env.graph, "test_connection", slow)
    out = await crm_env.service.test_connection(source_id)
    assert out.ok is False and out.steps[-1].key == "timeout"


# ---- the worker ------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_processes_queued_runs(crm_env: CrmEnv):
    crm_env.graph.files = [remote("a")]
    source_id = await create(crm_env)
    worker = SyncWorker(crm_env.service, crm_env.settings, worker_id="w-test")
    crm_env.service.set_wake_callback(worker.wake)
    worker.start()
    try:
        assert worker.running
        run = await crm_env.service.sync_now(SA, source_id)
        await wait_for(lambda: crm_env.run(run.id)["status"] == "COMPLETED")
        assert crm_env.run(run.id)["worker_id"] == "w-test"
    finally:
        await worker.stop()
    assert not worker.running
    assert utc_now() - crm_env.repo.recover_calls[0] >= timedelta(minutes=5) - timedelta(seconds=5)


@pytest.mark.asyncio
async def test_worker_stop_fails_the_run_in_flight_as_interrupted(crm_env: CrmEnv):
    import threading

    crm_env.graph.files = [remote("a"), remote("b")]
    crm_env.extractor.release = threading.Event()
    source_id = await create(crm_env)
    worker = SyncWorker(crm_env.service, crm_env.settings, worker_id="w-stop", heartbeat_seconds=0.05)
    crm_env.service.set_wake_callback(worker.wake)
    worker.start()
    try:
        run = await crm_env.service.sync_now(SA, source_id)
        await asyncio.to_thread(crm_env.extractor.started.wait, 5)
        await wait_for(lambda: len(crm_env.repo.touches) >= 2)  # heartbeat while extracting
        await worker.stop()
        row = crm_env.run(run.id)
        assert (row["status"], row["error_message"]) == ("FAILED", SHUTDOWN_RUN_REASON)
        a = crm_env.file_by_item("a")
        assert (a["status"], a["failed_stage"], a["error_message"]) == ("FAILED", "extraction", INTERRUPTED_FILE_REASON)
        assert crm_env.source(source_id)["last_sync_status"] == "FAILED"
    finally:
        crm_env.extractor.release.set()
        await worker.stop()


@pytest.mark.asyncio
async def test_request_stop_leaves_the_run_for_stop_to_release(crm_env: CrmEnv):
    crm_env.graph.files = [remote("a"), remote("b")]
    source_id = await create(crm_env)
    worker = SyncWorker(crm_env.service, crm_env.settings, worker_id="w-drain")
    crm_env.extractor.before = lambda filename: worker.request_stop()
    crm_env.service.set_wake_callback(worker.wake)
    worker.start()
    run = await crm_env.service.sync_now(SA, source_id)
    await wait_for(lambda: any(f["item_id"] == "a" and f["status"] == "COMPLETED" for f in crm_env.repo.files.values()))
    await asyncio.sleep(0.1)
    assert crm_env.run(run.id)["status"] == "RUNNING"  # stopped at the file boundary, not finished
    await worker.stop()
    assert crm_env.run(run.id)["status"] == "FAILED" and crm_env.run(run.id)["error_message"] == SHUTDOWN_RUN_REASON
    assert crm_env.file_by_item("b")["status"] == "PENDING"  # never started; the next sync takes it


@pytest.mark.asyncio
async def test_stale_runs_are_recovered_when_the_worker_starts(crm_env: CrmEnv):
    source_id = await create(crm_env)
    run_id = await crm_env.repo.create_run(source_id, trigger_type="SCHEDULED", triggered_by=None)
    crm_env.repo.runs[run_id].update(status="RUNNING", worker_id="dead", started_at=old(), updated_at=old())
    file_id = await crm_env.repo.upsert_file(source_id, remote("a"), run_id=run_id)
    crm_env.repo.files[file_id].update(status="PROCESSING", download_status="COMPLETED", extraction_status="RUNNING", last_run_id=run_id)
    worker = SyncWorker(crm_env.service, crm_env.settings)
    worker.start()
    try:
        await wait_for(lambda: crm_env.run(run_id)["status"] == "FAILED")
    finally:
        await worker.stop()
    assert crm_env.run(run_id)["error_message"] == INTERRUPTED_RUN_REASON
    row = crm_env.repo.files[file_id]
    assert (row["status"], row["failed_stage"], row["extraction_status"]) == ("FAILED", "extraction", "FAILED")
    assert crm_env.source(source_id)["last_sync_status"] == "FAILED"


# ---- pure input rules ---------------------------------------------------------------------------------


def test_extension_and_identifier_rules():
    assert normalize_extensions(["PDF", "docx", ".pdf"]) == [".pdf", ".docx"]
    with pytest.raises(BadRequestError):
        normalize_extensions([".doc"])
    assert clean_tenant_id(" contoso.onmicrosoft.com ") == "contoso.onmicrosoft.com"
    assert clean_tenant_id(TENANT_ID.upper()) == TENANT_ID.upper()
    assert clean_client_id(CLIENT_ID) == CLIENT_ID
    for bad in ("", "x", "contoso", "a b.com"):
        with pytest.raises(BadRequestError):
            clean_tenant_id(bad)
    with pytest.raises(BadRequestError):
        clean_client_id("contoso.onmicrosoft.com")

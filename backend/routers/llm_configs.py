from typing import Annotated, Any

from fastapi import APIRouter, Depends

from backend.auth.deps import ROLE_DEVELOPER, ROLE_SUPER_ADMIN, UserContext, require_roles, require_roles_or_nav_permission
from backend.dependencies import DbDep
from backend.exceptions import NotFoundError
from backend.schemas.common import MessageResponse
from backend.schemas.llm_configs import LLMConfigCreate, LLMConfigOut, LLMConfigUpdate
from backend.services.audit import write_audit_log
from backend.utils import serialize_row

router = APIRouter(prefix="/llm-configs", tags=["llm-configs"])


def _row_to_out(row: dict[str, Any] | None) -> LLMConfigOut:
    data = serialize_row(row) or {}
    if "model_comment" in data:
        data["comment"] = data.pop("model_comment")
    data["is_active"] = bool(data.get("is_active"))
    return LLMConfigOut(**data)


def _create_params(body: LLMConfigCreate) -> dict[str, Any]:
    params = body.model_dump()
    params["model_comment"] = params.pop("comment", None)
    params["is_active"] = 1 if body.is_active else 0
    return params


def _map_update_params(updates: dict[str, Any]) -> dict[str, Any]:
    if "comment" in updates:
        updates["model_comment"] = updates.pop("comment")
    if "is_active" in updates:
        updates["is_active"] = 1 if updates["is_active"] else 0
    return updates


@router.get("", response_model=list[LLMConfigOut])
async def list_llm_configs(
    user: Annotated[UserContext, Depends(require_roles_or_nav_permission("llm-configs", ROLE_SUPER_ADMIN, ROLE_DEVELOPER))],
    db: DbDep,
) -> list[LLMConfigOut]:
    rows = await db.fetch_all("SELECT * FROM AIVA_llm_configs ORDER BY id")
    return [_row_to_out(r) for r in rows]


@router.post("", response_model=LLMConfigOut, status_code=201)
async def create_llm_config(
    body: LLMConfigCreate,
    user: Annotated[UserContext, Depends(require_roles_or_nav_permission("llm-configs", ROLE_SUPER_ADMIN, ROLE_DEVELOPER))],
    db: DbDep,
) -> LLMConfigOut:
    config_id = await db.execute(
        """
        INSERT INTO AIVA_llm_configs (
            provider, model_name, model_comment, api_base_url, temperature, max_tokens,
            embedding_model, reranker_model, is_active
        ) VALUES (
            :provider, :model_name, :model_comment, :api_base_url, :temperature, :max_tokens,
            :embedding_model, :reranker_model, :is_active
        )
        RETURNING id INTO :out_id
        """,
        _create_params(body),
        return_id=True,
    )
    row = await db.fetch_one("SELECT * FROM AIVA_llm_configs WHERE id = :id", {"id": config_id})
    await write_audit_log(
        db,
        user_id=user.id,
        entity_type="llm_config",
        entity_id=int(config_id or 0),
        action_type="CREATE",
        new_value=body.model_dump(),
    )
    return _row_to_out(row)


@router.get("/{config_id}", response_model=LLMConfigOut)
async def get_llm_config(
    config_id: int,
    user: Annotated[UserContext, Depends(require_roles_or_nav_permission("llm-configs", ROLE_SUPER_ADMIN, ROLE_DEVELOPER))],
    db: DbDep,
) -> LLMConfigOut:
    row = await db.fetch_one("SELECT * FROM AIVA_llm_configs WHERE id = :id", {"id": config_id})
    if not row:
        raise NotFoundError("LLM config not found")
    return _row_to_out(row)


@router.patch("/{config_id}", response_model=LLMConfigOut)
async def update_llm_config(
    config_id: int,
    body: LLMConfigUpdate,
    user: Annotated[UserContext, Depends(require_roles_or_nav_permission("llm-configs", ROLE_SUPER_ADMIN, ROLE_DEVELOPER))],
    db: DbDep,
) -> LLMConfigOut:
    old = await db.fetch_one("SELECT * FROM AIVA_llm_configs WHERE id = :id", {"id": config_id})
    if not old:
        raise NotFoundError("LLM config not found")

    updates = _map_update_params(body.model_dump(exclude_unset=True))
    if updates:
        updates["id"] = config_id
        set_parts = [f"{k} = :{k}" for k in updates if k != "id"]
        await db.execute(
            f"UPDATE AIVA_llm_configs SET {', '.join(set_parts)} WHERE id = :id",
            updates,
        )
    row = await db.fetch_one("SELECT * FROM AIVA_llm_configs WHERE id = :id", {"id": config_id})
    return _row_to_out(row)


@router.delete("/{config_id}", response_model=MessageResponse)
async def delete_llm_config(
    config_id: int,
    user: Annotated[UserContext, Depends(require_roles_or_nav_permission("llm-configs", ROLE_SUPER_ADMIN, ROLE_DEVELOPER))],
    db: DbDep,
) -> MessageResponse:
    old = await db.fetch_one("SELECT * FROM AIVA_llm_configs WHERE id = :id", {"id": config_id})
    if not old:
        raise NotFoundError("LLM config not found")
    linked = await db.fetch_all(
        "SELECT id, name FROM AIVA_accounts WHERE llm_config_id = :id",
        {"id": config_id},
    )
    linked_rows = [dict(r) for r in linked]
    if linked:
        await db.execute(
            "UPDATE AIVA_accounts SET llm_config_id = NULL WHERE llm_config_id = :id",
            {"id": config_id},
        )
    await db.execute("DELETE FROM AIVA_llm_configs WHERE id = :id", {"id": config_id})
    await write_audit_log(
        db,
        user_id=user.id,
        entity_type="llm_config",
        entity_id=config_id,
        action_type="DELETE",
        old_value=serialize_row(old),
    )
    if linked:
        names = ", ".join(str(r.get("name") or r.get("id")) for r in linked_rows)
        return MessageResponse(
            message=f"LLM config deleted. Unlinked from account(s): {names}."
        )
    return MessageResponse(message="LLM config deleted")

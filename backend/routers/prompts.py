from typing import Annotated

from fastapi import APIRouter, Depends, Query

from backend.auth.deps import (
    ROLE_ACCOUNT_MANAGER,
    ROLE_ORG_ADMIN,
    ROLE_SUPER_ADMIN,
    UserContext,
    require_account_access,
    require_roles,
)
from backend.dependencies import DbDep
from backend.exceptions import ForbiddenError, NotFoundError
from backend.schemas.common import MessageResponse
from backend.schemas.prompts import (
    PromptCreate,
    PromptOut,
    PromptUpdate,
    SystemPromptOut,
    SystemPromptUpdate,
)
from backend.services.audit import write_audit_log
from backend.services.system_prompt import get_system_prompt_text, set_system_prompt_text
from backend.utils import serialize_row

router = APIRouter(prefix="/prompts", tags=["prompts"])

_PROMPT_READ_ROLES = (ROLE_SUPER_ADMIN, ROLE_ORG_ADMIN, ROLE_ACCOUNT_MANAGER)


@router.get("/system", response_model=SystemPromptOut)
async def get_system_prompt(
    user: Annotated[UserContext, Depends(require_roles(*_PROMPT_READ_ROLES))],
    db: DbDep,
) -> SystemPromptOut:
    text = await get_system_prompt_text(db)
    return SystemPromptOut(prompt_text=text, editable=user.is_super_admin)


@router.patch("/system", response_model=SystemPromptOut)
async def update_system_prompt(
    body: SystemPromptUpdate,
    user: Annotated[UserContext, Depends(require_roles(ROLE_SUPER_ADMIN))],
    db: DbDep,
) -> SystemPromptOut:
    await set_system_prompt_text(db, body.prompt_text, user_id=user.id)
    await write_audit_log(
        db,
        user_id=user.id,
        entity_type="system_prompt",
        entity_id=1,
        action_type="UPDATE",
        new_value={"prompt_text_length": len(body.prompt_text)},
    )
    return SystemPromptOut(prompt_text=body.prompt_text, editable=True)


@router.get("", response_model=list[PromptOut])
async def list_prompts(
    user: Annotated[
        UserContext,
        Depends(require_roles(ROLE_SUPER_ADMIN, ROLE_ORG_ADMIN, ROLE_ACCOUNT_MANAGER)),
    ],
    db: DbDep,
    account_id: int | None = Query(default=None),
) -> list[PromptOut]:
    if account_id is not None:
        account = await db.fetch_one("SELECT organization_id FROM AIVA_accounts WHERE id = :id", {"id": account_id})
        if account:
            require_account_access(account_id, user, int(account["organization_id"]))
        rows = await db.fetch_all(
            "SELECT * FROM AIVA_prompts WHERE account_id = :account_id ORDER BY id DESC",
            {"account_id": account_id},
        )
    elif user.is_super_admin:
        rows = await db.fetch_all("SELECT * FROM AIVA_prompts ORDER BY id DESC")
    else:
        rows = await db.fetch_all(
            """
            SELECT p.* FROM AIVA_prompts p
            JOIN AIVA_accounts a ON a.id = p.account_id
            WHERE a.organization_id = :org_id
            ORDER BY p.id DESC
            """,
            {"org_id": user.organization_id},
        )
    return [PromptOut(**{**(serialize_row(r) or {}), "is_active": bool(r.get("is_active"))}) for r in rows]


@router.post("", response_model=PromptOut, status_code=201)
async def create_prompt(
    body: PromptCreate,
    user: Annotated[
        UserContext,
        Depends(require_roles(ROLE_SUPER_ADMIN, ROLE_ORG_ADMIN, ROLE_ACCOUNT_MANAGER)),
    ],
    db: DbDep,
) -> PromptOut:
    account = await db.fetch_one("SELECT organization_id FROM AIVA_accounts WHERE id = :id", {"id": body.account_id})
    if not account:
        raise NotFoundError("Account not found")
    require_account_access(body.account_id, user, int(account["organization_id"]))

    if body.is_active:
        await db.execute(
            "UPDATE AIVA_prompts SET is_active = 0 WHERE account_id = :account_id",
            {"account_id": body.account_id},
        )

    prompt_id = await db.execute(
        """
        INSERT INTO AIVA_prompts (
            account_id, prompt_name, prompt_type, prompt_text, is_active, created_by
        ) VALUES (
            :account_id, :prompt_name, :prompt_type, :prompt_text, :is_active, :created_by
        )
        RETURNING id INTO :out_id
        """,
        {
            "account_id": body.account_id,
            "prompt_name": body.prompt_name,
            "prompt_type": body.prompt_type,
            "prompt_text": body.prompt_text,
            "is_active": 1 if body.is_active else 0,
            "created_by": user.id,
        },
        return_id=True,
    )
    row = await db.fetch_one("SELECT * FROM AIVA_prompts WHERE id = :id", {"id": prompt_id})
    await write_audit_log(
        db,
        user_id=user.id,
        entity_type="prompt",
        entity_id=int(prompt_id or 0),
        action_type="CREATE",
        new_value={"account_id": body.account_id, "prompt_name": body.prompt_name},
    )
    data = serialize_row(row) or {}
    data["is_active"] = bool(data.get("is_active"))
    return PromptOut(**data)


@router.patch("/{prompt_id}", response_model=PromptOut)
async def update_prompt(
    prompt_id: int,
    body: PromptUpdate,
    user: Annotated[
        UserContext,
        Depends(require_roles(ROLE_SUPER_ADMIN, ROLE_ORG_ADMIN, ROLE_ACCOUNT_MANAGER)),
    ],
    db: DbDep,
) -> PromptOut:
    old = await db.fetch_one("SELECT * FROM AIVA_prompts WHERE id = :id", {"id": prompt_id})
    if not old:
        raise NotFoundError("Prompt not found")
    account = await db.fetch_one(
        "SELECT organization_id FROM AIVA_accounts WHERE id = :id",
        {"id": int(old["account_id"])},
    )
    if account:
        require_account_access(int(old["account_id"]), user, int(account["organization_id"]))

    updates = body.model_dump(exclude_unset=True)
    if "is_active" in updates:
        updates["is_active"] = 1 if updates["is_active"] else 0
        if updates["is_active"]:
            await db.execute(
                "UPDATE AIVA_prompts SET is_active = 0 WHERE account_id = :account_id AND id != :id",
                {"account_id": int(old["account_id"]), "id": prompt_id},
            )
    if updates:
        updates["id"] = prompt_id
        set_parts = [f"{k} = :{k}" for k in updates if k != "id"]
        await db.execute(
            f"UPDATE AIVA_prompts SET {', '.join(set_parts)} WHERE id = :id",
            updates,
        )
    row = await db.fetch_one("SELECT * FROM AIVA_prompts WHERE id = :id", {"id": prompt_id})
    data = serialize_row(row) or {}
    data["is_active"] = bool(data.get("is_active"))
    return PromptOut(**data)


@router.delete("/{prompt_id}", response_model=MessageResponse)
async def delete_prompt(
    prompt_id: int,
    user: Annotated[
        UserContext,
        Depends(require_roles(ROLE_SUPER_ADMIN, ROLE_ORG_ADMIN, ROLE_ACCOUNT_MANAGER)),
    ],
    db: DbDep,
) -> MessageResponse:
    old = await db.fetch_one("SELECT * FROM AIVA_prompts WHERE id = :id", {"id": prompt_id})
    if not old:
        raise NotFoundError("Prompt not found")
    account = await db.fetch_one(
        "SELECT organization_id FROM AIVA_accounts WHERE id = :id",
        {"id": int(old["account_id"])},
    )
    if account and not user.can_access_account(int(old["account_id"]), int(account["organization_id"])):
        raise ForbiddenError("No access to this prompt")
    await db.execute("DELETE FROM AIVA_prompts WHERE id = :id", {"id": prompt_id})
    return MessageResponse(message="Prompt deleted")

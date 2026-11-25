"""游客初始化、账号注册/登录和明确确认的游客会话导入。"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update, delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from app.conversations.models import Conversation, IdentitySession, Principal
from app.core.config import Settings
from app.core.dependencies import get_app_settings
from app.core.identity import (
    ACTIVE_COOKIE, GUEST_COOKIE, check_origin, hash_password, issue_session,
    principal_for_token, require_identity, set_identity_cookie, verify_password, hash_token,
)
from app.db.dependencies import get_db_session

router = APIRouter(prefix="/identity", tags=["identity"])


class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=100, pattern=r"^[A-Za-z0-9_]+$")
    password: str = Field(min_length=8, max_length=128)


def identity_payload(principal: Principal, importable_count: int = 0) -> dict[str, str | int | None]:
    """返回前端可显示的身份信息，不泄露密码哈希和会话凭证。"""
    return {"id": str(principal.id), "kind": principal.kind, "username": principal.username,
            "importable_count": importable_count}


async def guest_to_import(request: Request, session: AsyncSession, account_id: UUID) -> Principal | None:
    """通过原游客 Cookie 寻找可导入身份，不能由客户端提交游客 ID。"""
    guest = await principal_for_token(session, request.cookies.get(GUEST_COOKIE))
    return guest if guest and guest.kind == "guest" and guest.id != account_id else None


@router.post("/bootstrap")
async def bootstrap(
    request: Request, response: Response,
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_app_settings),
) -> dict[str, str | int | None]:
    """首次访问签发游客凭证；已有账号或游客会话则直接恢复。"""
    check_origin(request, settings)
    current = await principal_for_token(session, request.cookies.get(ACTIVE_COOKIE))
    if current is not None:
        guest = await guest_to_import(request, session, current.id) if current.kind == "account" else None
        count = await session.scalar(select(func.count()).select_from(Conversation).where(Conversation.owner_id == guest.id)) if guest else 0
        return identity_payload(current, count or 0)
    guest = await principal_for_token(session, request.cookies.get(GUEST_COOKIE))
    if guest is None or guest.kind != "guest":
        guest = Principal(kind="guest")
        session.add(guest)
        await session.flush()
        guest_token = await issue_session(session, guest.id)
        set_identity_cookie(response, GUEST_COOKIE, guest_token, settings, request)
    else:
        guest_token = request.cookies[GUEST_COOKIE]
    set_identity_cookie(response, ACTIVE_COOKIE, guest_token, settings, request)
    await session.commit()
    return identity_payload(guest)


@router.post("/register")
async def register(
    credentials: Credentials, request: Request, response: Response,
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_app_settings),
) -> dict[str, str | int | None]:
    """新建账号并登录；原游客凭证保留，等待用户明确决定是否导入。"""
    check_origin(request, settings)
    username = credentials.username.lower()
    if await session.scalar(select(Principal.id).where(Principal.username == username)):
        raise HTTPException(409, detail={"code": "username_taken", "message": "用户名已被使用"})
    account = Principal(kind="account", username=username, password_hash=hash_password(credentials.password))
    session.add(account)
    try:
        await session.flush()
    except IntegrityError:
        # 两个注册请求可能同时通过预检查；唯一约束仍是最终裁决。
        await session.rollback()
        raise HTTPException(409, detail={"code": "username_taken", "message": "用户名已被使用"}) from None
    token = await issue_session(session, account.id)
    guest = await guest_to_import(request, session, account.id)
    count = await session.scalar(select(func.count()).select_from(Conversation).where(Conversation.owner_id == guest.id)) if guest else 0
    await session.commit()
    set_identity_cookie(response, ACTIVE_COOKIE, token, settings, request)
    return identity_payload(account, count or 0)


@router.post("/login")
async def login(
    credentials: Credentials, request: Request, response: Response,
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_app_settings),
) -> dict[str, str | int | None]:
    """校验密码并签发账号会话，游客会话等待显式导入。"""
    check_origin(request, settings)
    account = await session.scalar(select(Principal).where(Principal.username == credentials.username.lower(), Principal.kind == "account"))
    if account is None or not account.password_hash or not verify_password(credentials.password, account.password_hash):
        raise HTTPException(401, detail={"code": "invalid_credentials", "message": "用户名或密码错误"})
    token = await issue_session(session, account.id)
    guest = await guest_to_import(request, session, account.id)
    count = await session.scalar(select(func.count()).select_from(Conversation).where(Conversation.owner_id == guest.id)) if guest else 0
    await session.commit()
    set_identity_cookie(response, ACTIVE_COOKIE, token, settings, request)
    return identity_payload(account, count or 0)


@router.post("/import")
async def import_guest_conversations(
    request: Request, response: Response,
    account: Principal = Depends(require_identity),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, int]:
    """只在用户确认后原子转移该游客的会话；原 UUID 和历史调用归属不变。"""
    if account.kind != "account":
        raise HTTPException(403, detail={"code": "account_required", "message": "请先登录"})
    guest = await guest_to_import(request, session, account.id)
    if guest is None:
        raise HTTPException(409, detail={"code": "guest_session_missing", "message": "游客凭证已失效"})
    await session.execute(select(Principal.id).where(Principal.id == guest.id).with_for_update())
    result = await session.execute(update(Conversation).where(Conversation.owner_id == guest.id).values(owner_id=account.id))
    # 删除游客会话凭证防止重复导入；调用记录的归属快照不更新。
    await session.execute(delete(IdentitySession).where(IdentitySession.token_hash == hash_token(request.cookies[GUEST_COOKIE])))
    await session.commit()
    response.delete_cookie(GUEST_COOKIE, path="/")
    return {"imported_count": result.rowcount or 0}


@router.post("/logout")
async def logout(
    request: Request, response: Response,
    identity: Principal = Depends(require_identity),
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_app_settings),
) -> dict[str, str | int | None]:
    """退出账号后恢复本机游客身份；没有有效游客凭证则新建一个。"""
    await session.execute(delete(IdentitySession).where(IdentitySession.token_hash == hash_token(request.cookies[ACTIVE_COOKIE])))
    guest = await principal_for_token(session, request.cookies.get(GUEST_COOKIE))
    if guest is None or guest.kind != "guest":
        guest = Principal(kind="guest")
        session.add(guest)
        await session.flush()
        token = await issue_session(session, guest.id)
        set_identity_cookie(response, GUEST_COOKIE, token, settings, request)
    else:
        token = request.cookies[GUEST_COOKIE]
    set_identity_cookie(response, ACTIVE_COOKIE, token, settings, request)
    await session.commit()
    return identity_payload(guest)

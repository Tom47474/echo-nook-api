"""游客初始化与账号注册、登录、退出；两种身份的消息始终隔离。"""

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from app.conversations.models import IdentitySession, Principal
from app.core.config import Settings
from app.core.dependencies import get_app_settings, get_conversation_limit_manager
from app.core.identity import (
    ACTIVE_COOKIE, GUEST_COOKIE, check_origin, hash_password, issue_session,
    principal_for_token, require_identity, set_identity_cookie, verify_password, hash_token,
)
from app.db.dependencies import get_db_session
from app.core.limits import ConversationLimitManager, LimitRejected

router = APIRouter(prefix="/identity", tags=["identity"])


class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=100, pattern=r"^[A-Za-z0-9_]+$")
    password: str = Field(min_length=8, max_length=128)


def identity_payload(principal: Principal) -> dict[str, str | None]:
    """返回前端可显示的身份信息，不泄露密码哈希和会话凭证。"""
    return {"id": str(principal.id), "kind": principal.kind, "username": principal.username}


def login_limit_key(request: Request, username: str) -> str:
    """组合来源地址与规范化用户名，避免一个来源锁死其他人的同名账号。"""
    client_host = request.client.host if request.client is not None else "unknown"
    return f"{client_host}:{username}"


def login_limit_exception(error: LimitRejected) -> HTTPException:
    """登录限流沿用结构化错误，并通过 Retry-After 告知等待秒数。"""
    headers = {"Retry-After": str(error.retry_after)} if error.retry_after is not None else None
    return HTTPException(
        429,
        detail={"code": error.code, "message": error.message},
        headers=headers,
    )


@router.post("/bootstrap")
async def bootstrap(
    request: Request, response: Response,
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_app_settings),
) -> dict[str, str | None]:
    """首次访问签发游客凭证；已有账号或游客会话则直接恢复。"""
    check_origin(request, settings)
    current = await principal_for_token(session, request.cookies.get(ACTIVE_COOKIE))
    if current is not None:
        return identity_payload(current)
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
) -> dict[str, str | None]:
    """新建账号并登录；原游客凭证保留，账号与游客消息互不合并。"""
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
    await session.commit()
    set_identity_cookie(response, ACTIVE_COOKIE, token, settings, request)
    return identity_payload(account)


@router.post("/login")
async def login(
    credentials: Credentials, request: Request, response: Response,
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_app_settings),
    limit_manager: ConversationLimitManager = Depends(get_conversation_limit_manager),
) -> dict[str, str | None]:
    """校验密码并切换到账号窗口；游客凭证仍留在浏览器。"""
    check_origin(request, settings)
    username = credentials.username.lower()
    limit_key = login_limit_key(request, username)
    try:
        # 在昂贵的 scrypt 密码验证之前拦截已达到阈值的来源。
        await limit_manager.ensure_login_allowed(limit_key)
    except LimitRejected as error:
        raise login_limit_exception(error) from None
    account = await session.scalar(select(Principal).where(Principal.username == username, Principal.kind == "account"))
    if account is None or not account.password_hash or not verify_password(credentials.password, account.password_hash):
        await limit_manager.record_login_failure(limit_key)
        raise HTTPException(401, detail={"code": "invalid_credentials", "message": "用户名或密码错误"})
    await limit_manager.clear_login_failures(limit_key)
    token = await issue_session(session, account.id)
    await session.commit()
    set_identity_cookie(response, ACTIVE_COOKIE, token, settings, request)
    return identity_payload(account)


@router.post("/logout")
async def logout(
    request: Request, response: Response,
    identity: Principal = Depends(require_identity),
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_app_settings),
) -> dict[str, str | None]:
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

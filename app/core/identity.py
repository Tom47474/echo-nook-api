"""游客与账号身份：随机 Cookie、密码校验及请求级归属解析。"""

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.conversations.models import IdentitySession, Principal
from app.core.config import Settings
from app.core.dependencies import get_app_settings
from app.db.dependencies import get_db_session

ACTIVE_COOKIE = "echo_nook_session"
GUEST_COOKIE = "echo_nook_guest"
SESSION_DAYS = 30


def hash_token(token: str) -> str:
    """数据库只保存凭证摘要，原始随机值仅存在浏览器 Cookie。"""
    return hashlib.sha256(token.encode()).hexdigest()


def hash_password(password: str) -> str:
    """使用独立盐和 scrypt 派生密码哈希，避免保存明文密码。"""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return salt.hex() + ":" + digest.hex()


def verify_password(password: str, encoded: str) -> bool:
    """重算 scrypt 并恒定时间比较，格式错误的旧哈希按验证失败处理。"""
    try:
        salt_hex, digest_hex = encoded.split(":", 1)
        calculated = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1)
        return hmac.compare_digest(calculated, bytes.fromhex(digest_hex))
    except (ValueError, TypeError):
        return False


async def principal_for_token(session: AsyncSession, token: str | None) -> Principal | None:
    """只接受服务端已签发且未过期的随机会话，不信任客户端自报的 owner_id。"""
    if not token:
        return None
    return await session.scalar(
        select(Principal).join(IdentitySession, IdentitySession.principal_id == Principal.id)
        .where(IdentitySession.token_hash == hash_token(token), IdentitySession.expires_at > datetime.now(timezone.utc))
    )


async def issue_session(session: AsyncSession, principal_id: UUID) -> str:
    """签发高熵随机凭证，数据库仅持久化摘要和失效时间。"""
    token = secrets.token_urlsafe(32)
    session.add(IdentitySession(
        token_hash=hash_token(token), principal_id=principal_id,
        expires_at=datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS),
    ))
    await session.flush()
    return token


def set_identity_cookie(response: Response, name: str, token: str, settings: Settings, request: Request) -> None:
    """HTTPS 或正式环境使用 Secure；本地 HTTP 开发仍可建立游客会话。"""
    response.set_cookie(
        name, token, max_age=SESSION_DAYS * 86400, httponly=True,
        secure=settings.app_env == "production" or request.url.scheme == "https", samesite="lax", path="/",
    )


def check_origin(request: Request, settings: Settings) -> None:
    """Cookie 鉴权的写请求拒绝其他网页的 Origin，降低跨站伪造风险。"""
    origin = request.headers.get("origin")
    if request.method not in {"GET", "HEAD", "OPTIONS"} and origin and origin != settings.frontend_origin:
        raise HTTPException(status_code=403, detail={"code": "origin_forbidden", "message": "请求来源不被允许"})


async def require_identity(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_app_settings),
) -> Principal:
    check_origin(request, settings)
    principal = await principal_for_token(session, request.cookies.get(ACTIVE_COOKIE))
    if principal is None:
        raise HTTPException(status_code=401, detail={"code": "identity_required", "message": "请先建立游客会话"})
    return principal

# app/core/auth.py
# 后端身份解析：从 Authorization: Bearer <JWT> 本地验签出 user uuid。
# 自托管 Supabase 用 HS256 + 共享密钥，本地就能验，无需回头调 GoTrue。
import logging

import jwt
from fastapi import Header, HTTPException

from app.config import settings

logger = logging.getLogger(__name__)


def _decode(token: str) -> dict:
    """HS256 本地验签；失败不区分过期/签名错，一律 401，避免向客户端泄漏细节。

    verify_aud 必须显式关掉：GoTrue 签发的 token 带 aud=authenticated，
    而 PyJWT 只要见到 aud、又没拿到 audience 参数，就判 InvalidAudienceError。
    """
    try:
        return jwt.decode(
            token, settings.supabase_jwt_secret, algorithms=["HS256"],
            options={"verify_aud": False},
        )
    except jwt.PyJWTError as e:
        logger.info("[auth] JWT 验签失败：%s", e)
        raise HTTPException(status_code=401, detail="登录凭证无效或已过期")


async def get_current_user(authorization: str = Header(default="")) -> str:
    """FastAPI 依赖：从 Authorization 头解析出 user uuid（即 JWT 的 sub）。

    exp 由 pyjwt.decode 默认校验；iss 不校验（自托管下 API_EXTERNAL_URL 可变）。
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="未登录")
    payload = _decode(authorization[7:])
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="登录凭证缺少用户标识")
    return user_id

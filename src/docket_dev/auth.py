"""Docket authentication — a small, self-contained login scoped to one project.

Extracted from the in-repo testing-hub auth + the `require_tester` dependency,
with two changes for portability:
  - testers come from the project config (`CONFIG.testers`), not a hardcoded list;
  - tokens are signed with a per-project secret (`CONFIG.jwt_secret`), generated
    at `docket init`, instead of borrowing the host app's JWT secret.

Token shape is unchanged (a `hub: "testing"` claim, `testing_token` cookie), so
the existing React frontend and any stored tokens keep working.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from pydantic import BaseModel

from docket_dev.config import CONFIG

_TOKEN_TTL = timedelta(days=7)
_HUB_CLAIM = "testing"


# ---------------------------------------------------------------------------
# Tester registry — built from project config, hashed per-user on first use.
# ---------------------------------------------------------------------------

_cache: dict = {}


def _registry() -> dict:
    """Lazily build {username: {name, email, hash}} from CONFIG.testers, cached."""
    testers = CONFIG.testers
    if _cache.get("_src") is testers:
        return _cache["reg"]
    reg = {}
    for t in testers:
        uname = (t.get("username") or "").strip().lower()
        if not uname:
            continue
        pw = (t.get("password") or "testing").encode("utf-8")
        reg[uname] = {
            "name": t.get("name") or uname.capitalize(),
            "email": t.get("email") or "",
            "hash": bcrypt.hashpw(pw, bcrypt.gensalt()),
        }
    _cache["_src"] = testers
    _cache["reg"] = reg
    return reg


def authenticate(username: str, password: str) -> Optional[str]:
    """Return the tester's display name on success, else None."""
    if not username or not password:
        return None
    rec = _registry().get(username.strip().lower())
    if not rec:
        return None
    try:
        if bcrypt.checkpw(password.encode("utf-8"), rec["hash"]):
            return rec["name"]
    except (ValueError, TypeError):
        return None
    return None


def make_token(username: str) -> str:
    uname = username.strip().lower()
    rec = _registry().get(uname, {})
    payload = {
        "sub": uname,
        "name": rec.get("name", uname),
        "hub": _HUB_CLAIM,
        "exp": datetime.utcnow() + _TOKEN_TTL,
    }
    return jwt.encode(payload, CONFIG.jwt_secret, algorithm=CONFIG.jwt_algorithm)


def verify_token(token: str) -> Optional[dict]:
    if not token:
        return None
    try:
        data = jwt.decode(token, CONFIG.jwt_secret, algorithms=[CONFIG.jwt_algorithm])
    except JWTError:
        return None
    if data.get("hub") != _HUB_CLAIM:
        return None
    uname = data.get("sub")
    reg = _registry()
    if uname not in reg:
        return None
    return {"username": uname, "name": data.get("name") or reg[uname]["name"]}


def verify_username(username: str) -> bool:
    return (username or "").strip().lower() in _registry()


def tester_email(username: str) -> str:
    rec = _registry().get((username or "").strip().lower())
    return rec["email"] if rec else ""


def all_testers() -> list:
    return [
        {"username": u, "name": r["name"], "email": r["email"]}
        for u, r in _registry().items()
    ]


# ---------------------------------------------------------------------------
# FastAPI auth dependency + login routes
# ---------------------------------------------------------------------------

_security = HTTPBearer(auto_error=False)


def require_tester(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_security),
) -> dict:
    """Resolve the current tester from a Bearer header or `testing_token` cookie.
    Returns {username, name}; raises 401 if missing/invalid."""
    token = None
    if credentials and credentials.credentials:
        token = credentials.credentials
    elif request is not None:
        token = request.cookies.get("testing_token")
    tester = verify_token(token) if token else None
    if not tester:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Sign in to Docket")
    return tester


class LoginIn(BaseModel):
    username: str
    password: str


def build_login_router() -> APIRouter:
    """The /api/testing/login + /me routes the frontend expects."""
    router = APIRouter(tags=["auth"])

    @router.post("/api/testing/login")
    def login(body: LoginIn):
        name = authenticate(body.username, body.password)
        if not name:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail="Invalid username or password")
        token = make_token(body.username)
        return {"token": token, "name": name, "username": body.username.strip().lower()}

    @router.get("/api/testing/me")
    def me(tester: dict = Depends(require_tester)):
        return tester

    return router

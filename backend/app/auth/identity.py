"""Identity resolution — normalizes user context from HTTP request headers.

Resolution priority:
  1. X-MS-CLIENT-PRINCIPAL-ID  (Azure App Service managed auth — future production)
  2. X-MS-CLIENT-PRINCIPAL-NAME (display name from App Service auth)
  3. X-Debug-User-Id            (local dev — simulate different users)
  4. DEFAULT_LOCAL_USER_ID env  (local dev default)
  5. "anonymous"                (final fallback)

This module is isolated from routes so swapping in real auth (Entra / App Service)
requires no route rewrites — only update the resolution logic here.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request

from app.config.settings import DEFAULT_LOCAL_USER_ID


@dataclass
class UserIdentity:
    """Normalized identity resolved from the incoming HTTP request."""

    user_id: str
    user_name: str
    auth_source: str   # "app_service" | "debug_header" | "env_default" | "anonymous"
    is_authenticated: bool


def resolve_identity(request: Request) -> UserIdentity:
    """Return a normalized UserIdentity from request headers or env defaults.

    Does NOT raise — always returns a valid identity object.
    """
    # 1. Azure App Service managed auth (production path — future)
    ms_id = request.headers.get("X-MS-CLIENT-PRINCIPAL-ID")
    ms_name = request.headers.get("X-MS-CLIENT-PRINCIPAL-NAME")
    if ms_id:
        return UserIdentity(
            user_id=ms_id,
            user_name=ms_name or ms_id,
            auth_source="app_service",
            is_authenticated=True,
        )

    # 2. Debug header — lets local testers simulate different users without auth
    debug_id = request.headers.get("X-Debug-User-Id")
    if debug_id:
        return UserIdentity(
            user_id=debug_id,
            user_name=debug_id,
            auth_source="debug_header",
            is_authenticated=False,
        )

    # 3. Env-configured local default
    if DEFAULT_LOCAL_USER_ID and DEFAULT_LOCAL_USER_ID != "anonymous":
        return UserIdentity(
            user_id=DEFAULT_LOCAL_USER_ID,
            user_name=DEFAULT_LOCAL_USER_ID,
            auth_source="env_default",
            is_authenticated=False,
        )

    # 4. Final fallback
    return UserIdentity(
        user_id="anonymous",
        user_name="anonymous",
        auth_source="anonymous",
        is_authenticated=False,
    )

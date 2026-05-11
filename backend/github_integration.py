"""GitHub OAuth + agent-tools integration.

Lets users connect their personal or organisational GitHub accounts so
mission agents (and, later, chat agents) can read repos / open PRs /
comment / push commits on the user's behalf.

Architecture:
  • OAuth flow uses GitHub's standard web-app code grant.
  • Tokens are encrypted with the same Fernet key as `credential_vault`
    so we don't introduce a second KMS surface.
  • Two scopes of connection:
      - `user` — bound to (user_id, org_id). The default for "Connect
        GitHub" in the user dropdown. Each member of an org connects
        their own account.
      - `org`  — bound to org_id only. An admin upgrades a connection
        to "shared", then any teammate's mission can use it. This
        handles the "shared service account" / "company GitHub" case.
  • Token resolution rule for agents: try user-scoped first, fall back
    to org-scoped. Matches the user's "both per-user and per-org"
    answer to the scoping question.
  • Mission tools are registered as ONE async handler each + the
    `Tool(...)` descriptor lives in `missions.build_tools`. Keeps the
    surface area small and discoverable.

Why one file: the OAuth bits + the tool registry both live close to
the tokens they consume. Splitting the OAuth router out of the agent
tools would mean a circular import (router needs `db`, tools need the
same `db`, both need `_fernet()`). One module, three sections, clean.
"""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx
from bson import ObjectId
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request

from credential_vault import _fernet  # reuse the platform Fernet key

logger = logging.getLogger(__name__)


# ── 1. OAuth state cache ────────────────────────────────────────────────────
# Short-lived state tokens for CSRF protection on the OAuth round-trip.
# Persisted in Mongo (NOT in-memory) because the worker that issues the
# state may not be the worker that handles the callback in a multi-pod
# deploy. TTL'd via Mongo's expireAfterSeconds index — see ensure_indexes.
_OAUTH_STATE_TTL_SECONDS = 600  # 10 min


# ── 2. GitHub API constants ────────────────────────────────────────────────
GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_API = "https://api.github.com"
DEFAULT_SCOPES = "repo read:org workflow user:email"


def _client_id() -> str:
    return (os.environ.get("GITHUB_CLIENT_ID") or "").strip()


def _client_secret() -> str:
    return (os.environ.get("GITHUB_CLIENT_SECRET") or "").strip()


def _is_configured() -> bool:
    return bool(_client_id() and _client_secret())


# ── 3. Token vault (Fernet-encrypted) ──────────────────────────────────────
async def _save_connection(db, *, scope: str, org_id: str, user_id: Optional[str],
                           gh_user: dict, access_token: str, scopes: list[str]) -> dict:
    """Upsert a github_connections row. Encrypts the token at rest."""
    cipher = _fernet().encrypt(access_token.encode("utf-8"))
    now = datetime.now(timezone.utc)
    doc = {
        "scope": scope,
        "org_id": org_id,
        "user_id": user_id,  # None for org-scoped
        "github_user_id": gh_user["id"],
        "github_username": gh_user["login"],
        "github_email": gh_user.get("email"),
        "github_avatar_url": gh_user.get("avatar_url"),
        "access_token_encrypted": cipher,
        "scopes": scopes,
        "is_active": True,
        "updated_at": now,
        "last_used_at": None,
    }
    selector = {"scope": scope, "org_id": org_id, "user_id": user_id}
    res = await db.github_connections.find_one_and_update(
        selector,
        {"$set": doc, "$setOnInsert": {"created_at": now}},
        upsert=True, return_document=True,
    )
    return res or doc


async def _get_token(db, *, org_id: str, user_id: Optional[str]) -> Optional[str]:
    """Resolve an access token for an agent operation.

    Resolution order:
      1. user-scoped (org_id, user_id) — most specific, the agent
         operating "as" this human
      2. org-scoped (org_id, None)     — the shared org account, used
         when the human hasn't connected their own
    """
    if user_id:
        row = await db.github_connections.find_one(
            {"scope": "user", "org_id": org_id, "user_id": user_id, "is_active": True},
        )
        if row:
            await db.github_connections.update_one(
                {"_id": row["_id"]},
                {"$set": {"last_used_at": datetime.now(timezone.utc)}},
            )
            return _decrypt(row.get("access_token_encrypted"))
    row = await db.github_connections.find_one(
        {"scope": "org", "org_id": org_id, "is_active": True},
    )
    if row:
        await db.github_connections.update_one(
            {"_id": row["_id"]},
            {"$set": {"last_used_at": datetime.now(timezone.utc)}},
        )
        return _decrypt(row.get("access_token_encrypted"))
    return None


def _decrypt(cipher: Any) -> Optional[str]:
    if not cipher:
        return None
    try:
        return _fernet().decrypt(cipher).decode("utf-8")
    except Exception as exc:
        logger.warning("Failed to decrypt github token: %s", exc)
        return None


# ── 4. Public-shape connection serialiser ──────────────────────────────────
def _serialize_connection(row: dict) -> dict:
    return {
        "id": str(row.get("_id")),
        "scope": row.get("scope"),
        "user_id": row.get("user_id"),
        "github_username": row.get("github_username"),
        "github_email": row.get("github_email"),
        "github_avatar_url": row.get("github_avatar_url"),
        "scopes": row.get("scopes") or [],
        "is_active": bool(row.get("is_active", True)),
        "created_at": (row.get("created_at") or "").isoformat()
            if hasattr(row.get("created_at"), "isoformat") else None,
        "last_used_at": (row.get("last_used_at") or "").isoformat()
            if hasattr(row.get("last_used_at"), "isoformat") else None,
    }


# ── 5. OAuth router ────────────────────────────────────────────────────────
def make_github_router(*, db, get_current_user, require_org_member,
                       _public_origin):
    """Build the FastAPI router for GitHub OAuth + connection management.

    Args:
      db                  — Motor client.
      get_current_user    — auth dependency, returns the user dict.
      require_org_member  — async (org_id, user_id) → role; raises 403.
      _public_origin      — request → "https://host" (mirrors trial.py).
    """
    r = APIRouter(prefix="/api/github", tags=["github"])

    async def _ensure_state_index():
        try:
            await db.github_oauth_states.create_index(
                "created_at", expireAfterSeconds=_OAUTH_STATE_TTL_SECONDS,
            )
            await db.github_connections.create_index(
                [("scope", 1), ("org_id", 1), ("user_id", 1)],
                unique=True,
            )
        except Exception:
            pass

    @r.get("/status")
    async def status(org_id: str, u: dict = Depends(get_current_user)):
        """List the caller's connections inside this org. Used to render
        the Integrations panel."""
        await require_org_member(org_id, u["id"])
        await _ensure_state_index()
        rows = await db.github_connections.find({"org_id": org_id}).to_list(50)
        # Member sees: their own user-scoped + the org-scoped.
        # Admin sees: ALL user-scoped + the org-scoped.
        is_admin = u.get("is_superadmin", False)
        if not is_admin:
            membership = await db.org_members.find_one(
                {"org_id": org_id, "user_id": u["id"]},
            )
            is_admin = (membership or {}).get("role") in {"owner", "admin"}
        out = []
        for row in rows:
            if (row.get("scope") == "org") or (row.get("user_id") == u["id"]) or is_admin:
                out.append(_serialize_connection(row))
        return {
            "configured": _is_configured(),
            "connections": out,
            "is_admin": is_admin,
        }

    @r.post("/oauth/start")
    async def oauth_start(
        body: dict = Body(...),
        request: Request = None,
        u: dict = Depends(get_current_user),
    ):
        """Issue an authorize URL the SPA can redirect to.

        Body: { org_id, scope_type: 'user'|'org' }
        Returns: { authorize_url, state }
        """
        if not _is_configured():
            raise HTTPException(503, "GitHub OAuth is not configured on this server.")
        org_id = (body or {}).get("org_id") or ""
        scope_type = (body or {}).get("scope_type") or "user"
        if scope_type not in ("user", "org"):
            raise HTTPException(400, "scope_type must be 'user' or 'org'")
        await require_org_member(org_id, u["id"])
        if scope_type == "org":
            membership = await db.org_members.find_one(
                {"org_id": org_id, "user_id": u["id"]},
            )
            role = (membership or {}).get("role")
            if role not in {"owner", "admin"} and not u.get("is_superadmin"):
                raise HTTPException(403, "Only org owners/admins can install a shared GitHub connection.")
        await _ensure_state_index()
        state = secrets.token_urlsafe(32)
        await db.github_oauth_states.insert_one({
            "state": state,
            "user_id": u["id"],
            "org_id": org_id,
            "scope_type": scope_type,
            "created_at": datetime.now(timezone.utc),
        })
        # Callback URL is built off the same _public_origin that the
        # trial-code emails use, so prod and preview both route correctly.
        origin = _public_origin(request) if request is not None else ""
        if not origin:
            origin = (os.environ.get("FRONTEND_URL") or "").rstrip("/")
        callback = f"{origin}/auth/github/callback"
        from urllib.parse import urlencode
        qs = urlencode({
            "client_id": _client_id(),
            "redirect_uri": callback,
            "scope": DEFAULT_SCOPES,
            "state": state,
            "allow_signup": "true",
        })
        return {"authorize_url": f"{GITHUB_AUTHORIZE_URL}?{qs}", "state": state}

    @r.post("/oauth/callback")
    async def oauth_callback(
        body: dict = Body(...),
        request: Request = None,
        u: dict = Depends(get_current_user),
    ):
        """Exchange code for token, fetch the GitHub user, persist."""
        if not _is_configured():
            raise HTTPException(503, "GitHub OAuth is not configured.")
        code = (body or {}).get("code") or ""
        state = (body or {}).get("state") or ""
        if not code or not state:
            raise HTTPException(400, "code and state are required")
        # Validate state
        state_row = await db.github_oauth_states.find_one_and_delete({"state": state})
        if not state_row:
            raise HTTPException(400, "Invalid or expired state — please start again.")
        if state_row["user_id"] != u["id"]:
            raise HTTPException(403, "State user mismatch")
        # Exchange code → token
        origin = _public_origin(request) if request is not None else ""
        if not origin:
            origin = (os.environ.get("FRONTEND_URL") or "").rstrip("/")
        callback = f"{origin}/auth/github/callback"
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r1 = await c.post(
                    GITHUB_TOKEN_URL,
                    headers={"Accept": "application/json"},
                    data={
                        "client_id": _client_id(),
                        "client_secret": _client_secret(),
                        "code": code,
                        "redirect_uri": callback,
                    },
                )
            data = r1.json()
        except Exception as exc:
            logger.error("github_oauth_token_exchange_failed: %s", exc)
            raise HTTPException(502, "GitHub did not respond — please try again.")
        if r1.status_code >= 400 or "error" in data:
            err = data.get("error_description") or data.get("error") or "unknown"
            logger.error("github_oauth_token_error code=%s body=%s",
                         r1.status_code, data)
            raise HTTPException(400, f"GitHub rejected the authorization: {err}")
        access_token = data.get("access_token")
        if not access_token:
            raise HTTPException(400, "GitHub did not return an access token.")
        scopes = (data.get("scope") or "").split(",") if data.get("scope") else []
        # Fetch the authenticated user — we need the github_user_id +
        # username to render the connection card.
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r2 = await c.get(
                    f"{GITHUB_API}/user",
                    headers={
                        "Authorization": f"token {access_token}",
                        "Accept": "application/vnd.github+json",
                    },
                )
            gh_user = r2.json()
            if r2.status_code >= 400:
                raise RuntimeError(f"{r2.status_code}: {gh_user}")
        except Exception as exc:
            logger.error("github_user_fetch_failed: %s", exc)
            raise HTTPException(502, "Got a token but couldn't fetch your GitHub profile.")
        scope = state_row["scope_type"]
        org_id = state_row["org_id"]
        user_id = u["id"] if scope == "user" else None
        row = await _save_connection(
            db, scope=scope, org_id=org_id, user_id=user_id,
            gh_user=gh_user, access_token=access_token, scopes=scopes,
        )
        return {
            "ok": True,
            "connection": _serialize_connection(row),
        }

    @r.delete("/connections/{connection_id}")
    async def disconnect(connection_id: str, org_id: str = Query(...),
                          u: dict = Depends(get_current_user)):
        """Revoke a connection. User can only delete their own; admins
        can delete any in the org (including org-scoped ones)."""
        await require_org_member(org_id, u["id"])
        try:
            obj_id = ObjectId(connection_id)
        except Exception as exc:
            raise HTTPException(400, "Invalid connection id") from exc
        row = await db.github_connections.find_one({"_id": obj_id, "org_id": org_id})
        if not row:
            raise HTTPException(404, "Connection not found")
        is_admin = u.get("is_superadmin", False)
        if not is_admin:
            membership = await db.org_members.find_one(
                {"org_id": org_id, "user_id": u["id"]},
            )
            is_admin = (membership or {}).get("role") in {"owner", "admin"}
        if row.get("scope") == "user" and row.get("user_id") != u["id"] and not is_admin:
            raise HTTPException(403, "You can only disconnect your own GitHub account.")
        if row.get("scope") == "org" and not is_admin:
            raise HTTPException(403, "Only org owners/admins can revoke the shared connection.")
        await db.github_connections.delete_one({"_id": obj_id})
        return {"ok": True}

    # ── Quick-edit endpoints (consumed by the GitHubConnectCard UI) ─────────
    # Three thin proxies around the connected token: list-repos, get-tree,
    # commit-file. Lets the user surgical-edit a file from /app/account
    # without spinning up a Mission. Every call requires a live connection;
    # we resolve the token via the same user-then-org fallback the agent
    # tools use so behavior is consistent.
    @r.get("/repos")
    async def list_repos_endpoint(org_id: str = Query(...),
                                  u: dict = Depends(get_current_user)):
        await require_org_member(org_id, u["id"])
        token = await _get_token(db, org_id=org_id, user_id=u["id"])
        if not token:
            raise HTTPException(404, "No GitHub connection — connect one first.")
        try:
            data = await _gh_get(token, "/user/repos",
                                 params={"per_page": 100, "sort": "updated"})
        except Exception as exc:
            raise HTTPException(502, str(exc))
        return {
            "repos": [
                {"full_name": r.get("full_name"),
                 "default_branch": r.get("default_branch"),
                 "private": r.get("private"),
                 "url": r.get("html_url"),
                 "description": r.get("description"),
                 "language": r.get("language")}
                for r in (data or [])
            ],
        }

    @r.get("/tree")
    async def get_tree(
        org_id: str = Query(...), repo: str = Query(...),
        path: str = Query(""), ref: str = Query(""),
        u: dict = Depends(get_current_user),
    ):
        """List a directory's entries OR return a single file's contents."""
        await require_org_member(org_id, u["id"])
        token = await _get_token(db, org_id=org_id, user_id=u["id"])
        if not token:
            raise HTTPException(404, "No GitHub connection.")
        if "/" not in repo:
            raise HTTPException(400, "repo must be 'owner/name'")
        params = {"ref": ref} if ref else {}
        try:
            data = await _gh_get(token, f"/repos/{repo}/contents/{path}",
                                 params=params)
        except Exception as exc:
            raise HTTPException(404, str(exc))
        if isinstance(data, list):
            return {
                "is_directory": True,
                "entries": sorted([
                    {"name": e.get("name"), "type": e.get("type"),
                     "path": e.get("path"), "size": e.get("size", 0),
                     "sha": e.get("sha")}
                    for e in data
                ], key=lambda e: (e["type"] != "dir", e["name"].lower())),
            }
        # Single file
        import base64
        if data.get("encoding") == "base64":
            try:
                content = base64.b64decode(data["content"]).decode("utf-8")
                is_binary = False
            except Exception:
                content = "(binary file — open on GitHub to view)"
                is_binary = True
        else:
            content = data.get("content") or ""
            is_binary = False
        return {
            "is_directory": False,
            "name": data.get("name"),
            "path": data.get("path"),
            "sha": data.get("sha"),
            "size": data.get("size"),
            "is_binary": is_binary,
            "content": content,
            "html_url": data.get("html_url"),
        }

    @r.post("/commit")
    async def commit_file(
        body: dict = Body(...),
        u: dict = Depends(get_current_user),
    ):
        """Push a single-file commit. Body: {org_id, repo, branch, path,
        content, message, sha?}. If `sha` is provided we update that
        revision; otherwise GitHub treats it as a create."""
        org_id = (body or {}).get("org_id") or ""
        await require_org_member(org_id, u["id"])
        token = await _get_token(db, org_id=org_id, user_id=u["id"])
        if not token:
            raise HTTPException(404, "No GitHub connection.")
        repo = (body.get("repo") or "").strip()
        branch = (body.get("branch") or "").strip()
        path = (body.get("path") or "").strip()
        content = body.get("content") or ""
        message = (body.get("message") or "").strip()
        sha = body.get("sha")
        if not (repo and branch and path and message):
            raise HTTPException(400, "repo, branch, path, and message are required")
        import base64
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        payload = {"message": message, "content": encoded, "branch": branch}
        if sha:
            payload["sha"] = sha
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                resp = await c.put(
                    f"{GITHUB_API}/repos/{repo}/contents/{path}",
                    json=payload,
                    headers={
                        "Authorization": f"token {token}",
                        "Accept": "application/vnd.github+json",
                    },
                )
            if resp.status_code >= 400:
                raise HTTPException(resp.status_code,
                                    f"GitHub: {resp.text[:300]}")
            data = resp.json()
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(502, str(exc))
        return {
            "ok": True,
            "commit_sha": (data.get("commit") or {}).get("sha"),
            "commit_url": (data.get("commit") or {}).get("html_url"),
            "new_sha": (data.get("content") or {}).get("sha"),
        }

    return r


# ── 6. Agent tools (consumed by missions.build_tools) ──────────────────────
async def _gh_get(token: str, path: str, *, params: Optional[dict] = None) -> dict:
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(
            f"{GITHUB_API}{path}",
            params=params or {},
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
    if r.status_code >= 400:
        raise RuntimeError(f"GitHub {path} failed [{r.status_code}]: {r.text[:300]}")
    return r.json()


async def _gh_post(token: str, path: str, body: dict) -> dict:
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            f"{GITHUB_API}{path}",
            json=body,
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
    if r.status_code >= 400:
        raise RuntimeError(f"GitHub {path} failed [{r.status_code}]: {r.text[:300]}")
    return r.json()


def make_github_tools(*, db):
    """Return a list of (name, description, args_hint, handler) tuples
    that `missions.build_tools` registers into the agent's tool catalog.

    Each handler takes (ctx, args) like every other mission tool. ctx
    carries `org_id` and `user_id` which we use to resolve the right
    token on every call. If no connection exists, the tool returns a
    structured error so the mission loop's existing "pivot" hint can
    direct the agent to ask the user to connect."""

    async def _resolve(ctx) -> Optional[str]:
        # Mission ctx uses `created_by` as the user id (audit trail field
        # — see missions.py around line 354). Chat ctx uses `user_id`
        # directly. Honor both so the same tool surface works in either.
        return await _get_token(
            db,
            org_id=ctx.get("org_id") or "",
            user_id=ctx.get("user_id") or ctx.get("created_by") or "",
        )

    async def gh_list_repos(ctx, args):
        token = await _resolve(ctx)
        if not token:
            return {"error": "No GitHub connection. Ask the user to click "
                    "**Connect GitHub** in the user dropdown's Integrations panel."}
        per_page = min(int(args.get("limit") or 30), 100)
        try:
            data = await _gh_get(token, "/user/repos",
                                 params={"per_page": per_page, "sort": "updated"})
        except Exception as exc:
            return {"error": str(exc)}
        return {
            "repos": [
                {"full_name": r.get("full_name"),
                 "description": r.get("description"),
                 "private": r.get("private"),
                 "default_branch": r.get("default_branch"),
                 "url": r.get("html_url"),
                 "language": r.get("language"),
                 "updated_at": r.get("updated_at")}
                for r in (data or [])[:per_page]
            ],
        }

    async def gh_read_file(ctx, args):
        token = await _resolve(ctx)
        if not token:
            return {"error": "No GitHub connection."}
        repo = (args.get("repo") or "").strip()
        path = (args.get("path") or "").strip()
        ref = (args.get("ref") or "").strip()
        if not repo or "/" not in repo or not path:
            return {"error": "repo='owner/name' and path='dir/file.ext' are required"}
        params = {"ref": ref} if ref else {}
        try:
            data = await _gh_get(token, f"/repos/{repo}/contents/{path}", params=params)
        except Exception as exc:
            return {"error": str(exc)}
        if isinstance(data, list):
            return {"is_directory": True, "entries": [{"name": e.get("name"),
                    "type": e.get("type"), "size": e.get("size")} for e in data]}
        if data.get("encoding") == "base64":
            import base64
            try:
                content = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
            except Exception:
                content = "(binary file — not displayed)"
        else:
            content = data.get("content") or ""
        return {
            "name": data.get("name"),
            "path": data.get("path"),
            "size": data.get("size"),
            "sha": data.get("sha"),
            "content": content[:50_000],
        }

    async def gh_list_issues(ctx, args):
        token = await _resolve(ctx)
        if not token:
            return {"error": "No GitHub connection."}
        repo = (args.get("repo") or "").strip()
        state = (args.get("state") or "open").strip()
        if not repo or "/" not in repo:
            return {"error": "repo='owner/name' is required"}
        try:
            data = await _gh_get(token, f"/repos/{repo}/issues",
                                 params={"state": state, "per_page": 30})
        except Exception as exc:
            return {"error": str(exc)}
        # GitHub's /issues endpoint returns PRs too; filter PRs out.
        return {
            "issues": [
                {"number": i.get("number"), "title": i.get("title"),
                 "state": i.get("state"),
                 "labels": [lbl.get("name") for lbl in (i.get("labels") or [])],
                 "url": i.get("html_url")}
                for i in (data or []) if not i.get("pull_request")
            ][:30],
        }

    async def gh_comment_issue(ctx, args):
        token = await _resolve(ctx)
        if not token:
            return {"error": "No GitHub connection."}
        repo = (args.get("repo") or "").strip()
        try:
            number = int(args.get("issue_number") or 0)
        except Exception:
            number = 0
        body = (args.get("body") or "").strip()
        if not repo or "/" not in repo or number <= 0 or not body:
            return {"error": "repo, issue_number (>0), and body are required"}
        try:
            data = await _gh_post(token, f"/repos/{repo}/issues/{number}/comments",
                                  {"body": body})
        except Exception as exc:
            return {"error": str(exc)}
        return {"comment_id": data.get("id"), "url": data.get("html_url")}

    async def gh_create_branch(ctx, args):
        token = await _resolve(ctx)
        if not token:
            return {"error": "No GitHub connection."}
        repo = (args.get("repo") or "").strip()
        new_branch = (args.get("branch") or "").strip()
        from_branch = (args.get("from_branch") or "").strip()
        if not repo or "/" not in repo or not new_branch:
            return {"error": "repo and branch are required"}
        # Resolve the source branch's tip SHA. Default to repo's
        # default_branch if from_branch wasn't given.
        try:
            if not from_branch:
                repo_info = await _gh_get(token, f"/repos/{repo}")
                from_branch = repo_info.get("default_branch") or "main"
            ref = await _gh_get(token, f"/repos/{repo}/git/ref/heads/{from_branch}")
            sha = (ref or {}).get("object", {}).get("sha")
            if not sha:
                return {"error": f"Could not resolve tip of {from_branch}"}
            await _gh_post(token, f"/repos/{repo}/git/refs",
                           {"ref": f"refs/heads/{new_branch}", "sha": sha})
        except Exception as exc:
            return {"error": str(exc)}
        return {"branch": new_branch, "from": from_branch, "sha": sha}

    async def gh_create_or_update_file(ctx, args):
        """Push a new commit onto a branch — covers both file creation
        and editing. Use this instead of doing a full clone + push."""
        token = await _resolve(ctx)
        if not token:
            return {"error": "No GitHub connection."}
        repo = (args.get("repo") or "").strip()
        branch = (args.get("branch") or "").strip()
        path = (args.get("path") or "").strip()
        content = args.get("content") or ""
        message = (args.get("message") or "").strip()
        if not (repo and branch and path and message):
            return {"error": "repo, branch, path, and message are required"}
        import base64
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        # If the file exists on this branch we must include its sha.
        sha = None
        try:
            existing = await _gh_get(
                token, f"/repos/{repo}/contents/{path}",
                params={"ref": branch},
            )
            if isinstance(existing, dict):
                sha = existing.get("sha")
        except Exception:
            pass  # file doesn't exist — that's fine
        body = {
            "message": message,
            "content": encoded,
            "branch": branch,
        }
        if sha:
            body["sha"] = sha
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                resp = await c.put(
                    f"{GITHUB_API}/repos/{repo}/contents/{path}",
                    json=body,
                    headers={
                        "Authorization": f"token {token}",
                        "Accept": "application/vnd.github+json",
                    },
                )
            if resp.status_code >= 400:
                return {"error": f"GitHub {resp.status_code}: {resp.text[:300]}"}
            data = resp.json()
        except Exception as exc:
            return {"error": str(exc)}
        return {
            "commit_sha": (data.get("commit") or {}).get("sha"),
            "commit_url": (data.get("commit") or {}).get("html_url"),
            "path": path,
        }

    async def gh_open_pr(ctx, args):
        token = await _resolve(ctx)
        if not token:
            return {"error": "No GitHub connection."}
        repo = (args.get("repo") or "").strip()
        head = (args.get("head") or "").strip()
        base = (args.get("base") or "").strip()
        title = (args.get("title") or "").strip()
        body = (args.get("body") or "").strip()
        if not (repo and head and title):
            return {"error": "repo, head (branch), and title are required"}
        if not base:
            try:
                repo_info = await _gh_get(token, f"/repos/{repo}")
                base = repo_info.get("default_branch") or "main"
            except Exception:
                base = "main"
        try:
            data = await _gh_post(
                token, f"/repos/{repo}/pulls",
                {"title": title, "head": head, "base": base, "body": body},
            )
        except Exception as exc:
            return {"error": str(exc)}
        return {
            "number": data.get("number"),
            "url": data.get("html_url"),
            "state": data.get("state"),
        }

    return [
        ("github_list_repos",
         "List the user's accessible GitHub repos (their own + repos in orgs they belong to). "
         "Call FIRST when the user mentions GitHub but doesn't specify a repo, so you can confirm the right one.",
         '{"limit": 30}',
         gh_list_repos),
        ("github_read_file",
         "Read the contents of a file in a GitHub repo. Supply repo='owner/name' and path. "
         "Pass `ref` to read from a branch other than default. If `path` is a directory the response lists entries.",
         '{"repo": "owner/name", "path": "src/index.js", "ref": "main"}',
         gh_read_file),
        ("github_list_issues",
         "List issues on a repo. state='open'|'closed'|'all'. Excludes pull requests.",
         '{"repo": "owner/name", "state": "open"}',
         gh_list_issues),
        ("github_comment_issue",
         "Post a comment on an issue or PR. body is markdown.",
         '{"repo": "owner/name", "issue_number": 42, "body": "Thanks — I will take a look."}',
         gh_comment_issue),
        ("github_create_branch",
         "Create a new branch from another branch (defaults to the repo's default branch). "
         "Always do this BEFORE github_create_or_update_file when the agent's plan involves shipping changes.",
         '{"repo": "owner/name", "branch": "fix/spelling", "from_branch": "main"}',
         gh_create_branch),
        ("github_create_or_update_file",
         "Push a single-file commit onto a branch. Creates the file if it doesn't exist, "
         "updates if it does. Use for small surgical edits — for multi-file changes call once per file. "
         "`content` is the FULL new file contents (not a diff).",
         '{"repo": "owner/name", "branch": "fix/spelling", "path": "README.md", "content": "# New title\\n", "message": "Fix README title"}',
         gh_create_or_update_file),
        ("github_open_pr",
         "Open a pull request. `head` is the branch you pushed your changes to. `base` defaults to the repo's default branch.",
         '{"repo": "owner/name", "head": "fix/spelling", "title": "Fix README typo", "body": "Closes #42"}',
         gh_open_pr),
    ]


__all__ = [
    "make_github_router", "make_github_tools", "_get_token",
    "_is_configured", "fetch_github_chat_context",
]


# ── 7. Chat-side intent detection + ground-truth block ─────────────────────
# Chat conversations don't have a tool-calling loop yet (that's a P2
# refactor). To make GitHub useful in chat NOW, we run a tiny
# rule-based intent classifier on each user message — when it matches,
# we call the appropriate read-only GitHub endpoint server-side and
# inject the result as a "GROUND TRUTH — GITHUB" block in the system
# prompt. Same pattern as `fetch_user_supplied_urls()` for paste-a-link
# scrapes.
#
# We only auto-execute READ operations (list_repos, list_issues, read
# file). Write operations (open PR, push commit, create branch) require
# explicit human consent → those continue to live in Missions only.
import re as _re

# Match a "owner/repo" mention in the user's message — captures common
# patterns like "in coolgeek/site", "https://github.com/owner/repo",
# "owner/repo issues". Two-segment slug, alnum + dash + underscore.
_REPO_RE = _re.compile(
    r"(?:github\.com/)?([A-Za-z0-9_.-]{1,39})/([A-Za-z0-9_.-]{1,100})"
    r"(?:\.git|/issues|/pulls|/blob/[^\s]+)?\b"
)

_INTENT_LIST_REPOS = _re.compile(
    r"\b(my (?:github )?repos?|list (?:my )?repos?|what repos|"
    r"show.*repos?|active repos?|recent repos?)\b",
    _re.IGNORECASE,
)

_INTENT_LIST_ISSUES = _re.compile(
    r"\b(open issues?|list issues?|issues? in|outstanding issues?|"
    r"bugs? (?:in|on)|tickets? in|what'?s open|what bugs?)\b",
    _re.IGNORECASE,
)

# "review / audit / explore / summarize / look at / check out / examine /
# read / read through / dive into / walk through / take a look at <repo>"
# — catches the natural "help me understand this repo" intent that's
# distinct from "list my repos" or "open issues".
_INTENT_REPO_SUMMARY = _re.compile(
    r"\b(review|audit|explore|summari[sz]e|examine|inspect|"
    r"read(?: through)?|walk through|dive into|"
    r"look at|take a look at|check( out)?|peek at)\b"
    r"[^.?!]{0,80}\brepo\b",
    _re.IGNORECASE,
)

# Bare "github" or "my repo" mention without any repo coordinate —
# fires the "user wants something with GitHub but didn't name a repo"
# branch so the agent prompts for a repo instead of denying access.
_INTENT_GITHUB_BARE = _re.compile(
    r"\b(my github|github (?:account|integration|connection)|"
    r"(?:my )?repo(?:sitor(?:y|ies))?)\b",
    _re.IGNORECASE,
)


async def fetch_github_chat_context(
    db, *, message: str, org_id: str, user_id: str,
) -> tuple[str, list[dict]]:
    """Inspect the user's chat message; if a GitHub intent + scope is
    detected, perform a single read-only API call and format the result
    as a system-prompt block. Returns (block_str, structured_results).

    Structured results are persisted on the message so the UI can later
    render them as cards if desired (we don't render them today — the
    block is enough to ground the agent's reply)."""
    try:
        token = await _get_token(db, org_id=org_id, user_id=user_id)
    except Exception:
        token = None
    if not token or not message:
        return "", []
    msg = message.strip()

    # Pattern 1: "list my repos" / "what are my recent repos"
    if _INTENT_LIST_REPOS.search(msg) and not _REPO_RE.search(msg):
        try:
            data = await _gh_get(token, "/user/repos",
                                 params={"per_page": 15, "sort": "updated"})
        except Exception as exc:
            return f"\n\nGITHUB INTENT FAILED: {exc}\n", []
        rows = []
        for r in (data or [])[:15]:
            rows.append({
                "full_name": r.get("full_name"),
                "description": r.get("description") or "",
                "private": r.get("private"),
                "language": r.get("language"),
                "url": r.get("html_url"),
                "updated_at": r.get("updated_at"),
            })
        block = (
            "\n\nGROUND TRUTH — GITHUB (the user's 15 most recently "
            "updated repos, fetched server-side just now). Cite repo "
            "names as `owner/name` markdown links to their `url`. Do "
            "NOT mention repos that aren't in this list.\n\n"
        )
        for r in rows:
            vis = "private" if r.get("private") else "public"
            lang = f" · {r['language']}" if r.get("language") else ""
            desc = (r.get("description") or "").strip()
            block += f"- [{r['full_name']}]({r['url']}) ({vis}{lang})"
            if desc:
                block += f" — {desc[:140]}"
            block += "\n"
        return block, rows

    # Pattern 2: "open issues in owner/name" / "what's open in owner/name"
    if _INTENT_LIST_ISSUES.search(msg):
        m = _REPO_RE.search(msg)
        if not m:
            return "", []
        repo = f"{m.group(1)}/{m.group(2)}"
        try:
            data = await _gh_get(token, f"/repos/{repo}/issues",
                                 params={"state": "open", "per_page": 20})
        except Exception as exc:
            return f"\n\nGITHUB INTENT FAILED for {repo}: {exc}\n", []
        rows = []
        for i in (data or []):
            if i.get("pull_request"):
                continue  # exclude PRs from the issues view
            rows.append({
                "number": i.get("number"),
                "title": i.get("title"),
                "url": i.get("html_url"),
                "labels": [lbl.get("name") for lbl in (i.get("labels") or [])],
                "state": i.get("state"),
            })
            if len(rows) >= 20:
                break
        block = (
            f"\n\nGROUND TRUTH — GITHUB (open issues on `{repo}`, "
            f"fetched server-side just now). Reference issue numbers "
            f"as `#42` and link to their `url`. Do NOT invent issues "
            f"not in this list.\n\n"
        )
        for r in rows:
            labels = (" `" + "` `".join(r["labels"]) + "`") if r["labels"] else ""
            block += f"- [#{r['number']} {r['title']}]({r['url']}){labels}\n"
        if not rows:
            block += "_(No open issues.)_\n"
        return block, rows

    # Pattern 3: "review / explore / summarize <repo>" — fetch repo
    # metadata + README + recent commits + counts so the agent can
    # actually discuss what's in the repo instead of pleading
    # ignorance. This is the highest-leverage intent because it's
    # what users mean ~80% of the time when they say "look at my
    # repo".
    #
    # Two trip-wires for this intent:
    #   (a) explicit "review/audit/explore/summarize <...> repo" phrasing
    #   (b) a bare `github.com/<owner>/<name>` URL paired with ANY
    #       review-like verb (lower bar — if a user pastes a GitHub
    #       URL into chat they almost always want the agent to look
    #       at it, not just acknowledge the link).
    has_github_url = "github.com/" in msg.lower()
    casual_review_verb = _re.search(
        r"\b(review|audit|explore|examine|inspect|read|look|"
        r"summari[sz]e|walk|dive|peek|check|see)\b",
        msg, _re.IGNORECASE,
    )
    if _INTENT_REPO_SUMMARY.search(msg) or (has_github_url and casual_review_verb):
        m = _REPO_RE.search(msg)
        if m:
            repo = f"{m.group(1)}/{m.group(2)}"
            try:
                meta, readme, commits, lang_breakdown = await asyncio.gather(
                    _gh_get(token, f"/repos/{repo}"),
                    _gh_get(token, f"/repos/{repo}/readme"),
                    _gh_get(token, f"/repos/{repo}/commits", params={"per_page": 10}),
                    _gh_get(token, f"/repos/{repo}/languages"),
                    return_exceptions=True,
                )
            except Exception as exc:
                return f"\n\nGITHUB INTENT FAILED for {repo}: {exc}\n", []
            block = (
                f"\n\nGROUND TRUTH — GITHUB REPO `{repo}` (fetched "
                f"server-side just now). Use this to ground your review; "
                f"do not invent files, commits, or behaviour that aren't "
                f"represented here. If you need deeper detail (file "
                f"contents, specific issues), ASK the user — don't claim "
                f"you can't see the repo, you can.\n\n"
            )
            results: list[dict] = []
            if isinstance(meta, dict):
                desc = (meta.get("description") or "").strip()
                visibility = "private" if meta.get("private") else "public"
                stars = meta.get("stargazers_count", 0)
                forks = meta.get("forks_count", 0)
                open_issues = meta.get("open_issues_count", 0)
                default_branch = meta.get("default_branch", "main")
                size_kb = meta.get("size", 0)
                license_name = ((meta.get("license") or {}).get("name") or "").strip()
                block += f"**Repo**: [{repo}]({meta.get('html_url')}) ({visibility})\n"
                if desc:
                    block += f"**Description**: {desc}\n"
                block += (
                    f"**Default branch**: `{default_branch}` · "
                    f"**Size**: {size_kb} KB · "
                    f"**Stars**: {stars} · **Forks**: {forks} · "
                    f"**Open issues+PRs**: {open_issues}"
                )
                if license_name:
                    block += f" · **License**: {license_name}"
                block += "\n"
                results.append({"kind": "repo_meta", "data": {
                    "full_name": repo, "url": meta.get("html_url"),
                    "description": desc, "visibility": visibility,
                    "stars": stars, "forks": forks,
                    "open_issues": open_issues,
                    "default_branch": default_branch,
                    "size_kb": size_kb, "license": license_name,
                }})
            if isinstance(lang_breakdown, dict) and lang_breakdown:
                total = sum(lang_breakdown.values()) or 1
                breakdown = sorted(lang_breakdown.items(), key=lambda x: -x[1])[:6]
                pretty = ", ".join(
                    f"{name} {round(100*size/total)}%" for name, size in breakdown
                )
                block += f"**Languages**: {pretty}\n"
            if isinstance(readme, dict) and readme.get("content"):
                import base64 as _b64
                try:
                    raw = _b64.b64decode(readme["content"]).decode("utf-8", "replace")
                    snippet = raw.strip()[:2400]
                    truncated = "..." if len(raw.strip()) > 2400 else ""
                    block += f"\n**README (first 2.4KB)**:\n```\n{snippet}{truncated}\n```\n"
                    results.append({"kind": "readme",
                                    "data": {"length_chars": len(raw),
                                             "truncated": bool(truncated)}})
                except Exception:
                    pass
            if isinstance(commits, list) and commits:
                block += "\n**Last 10 commits** (newest first):\n"
                for c in commits[:10]:
                    sha = (c.get("sha") or "")[:7]
                    cm = c.get("commit") or {}
                    msg_line = ((cm.get("message") or "").splitlines() or [""])[0][:90]
                    author = ((cm.get("author") or {}).get("name") or "?")
                    when = ((cm.get("author") or {}).get("date") or "")[:10]
                    block += f"- `{sha}` {when} · {author}: {msg_line}\n"
                results.append({"kind": "commits",
                                "data": {"count": len(commits)}})
            return block, results
        # Intent matched but no repo coord — tell the agent the user
        # is asking about a repo without naming one. The agent should
        # ASK rather than refuse.
        block = (
            "\n\nGITHUB CONTEXT: The user wants to review a GitHub "
            "repo but didn't name one. Their GitHub account IS "
            "connected (you have read access via their OAuth token). "
            "Ask them which repo — accept either `owner/name` or a "
            "GitHub URL. Do NOT say you can't access their repos; "
            "you can.\n"
        )
        return block, []

    # Pattern 4: bare GitHub mention with no other intent — prevent the
    # agent from falling back to "I don't have access" when the user
    # is clearly trying to use the integration.
    if _INTENT_GITHUB_BARE.search(msg):
        block = (
            "\n\nGITHUB CONTEXT: The user's GitHub account is "
            "connected (read access via OAuth). You CAN list their "
            "repos, read repo metadata + README + commits + open "
            "issues, and explore files. If their message is vague, "
            "ask a clarifying question (which repo? what aspect?) "
            "rather than denying capability.\n"
        )
        return block, []

    return "", []

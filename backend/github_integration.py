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
    "_is_configured",
]

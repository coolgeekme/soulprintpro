"""
Foundry — Backend
==========================
Multi-tenant B2B platform: Organizations → Teams → Agents + Conversations.

Architecture
- Auth: JWT (HTTP-only cookies) + bcrypt
- Org layer: users belong to many orgs, orgs own teams
- Team layer: each team has a "SoulPrint" (mission, tone, audience, values)
  which is injected into every agent system prompt when a conversation happens
  inside that team
- Agents: 5 pre-built agency roles (strategist, researcher, copywriter, engineer, ops)
- Conversations: direct (1 user + 1 agent) OR group (many users + >=1 agent).
  In group mode agent responds only when @mentioned unless auto_respond is ON.
"""
from fastapi import FastAPI, APIRouter, HTTPException, Request, Response, Depends, UploadFile, File, Form, Body
from fastapi.responses import FileResponse
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from pathlib import Path
from pydantic import BaseModel, Field, EmailStr
from typing import Dict, List, Optional, Literal, Any
from datetime import datetime, timezone, timedelta
from bson import ObjectId
import os, logging, uuid, secrets, re, json, base64, bcrypt, jwt

from emergentintegrations.llm.chat import LlmChat, UserMessage

from llm_fallback import install_fallback as _install_llm_fallback
_install_llm_fallback()  # patches LlmChat.send_message to use OPENAI_API_KEY on budget errors

# Superadmin gate — these emails get the admin dashboard. List is
# environment-configurable so we can extend without a code change. The
# seed runs on startup (idempotent) and registration auto-promotes.
SUPERADMIN_EMAILS = [
    e.strip().lower() for e in os.environ.get(
        "SUPERADMIN_EMAILS", "reggie@coolgeek.me",
    ).split(",") if e.strip()
]

from brave_search import brave_web_search, format_results_for_prompt
from perplexity_client import (
    perplexity_company_summary,
    perplexity_web_search,
    is_available as _perplexity_available,
)
from kie_client import generate_video as kie_generate_video, submit_video as kie_submit_video, wait_for_video as kie_wait_for_video
import asyncio
import httpx
from assets import (
    extract_text as extract_asset_text,
    is_text_extractable,
    trim as trim_text,
    format_assets_for_prompt,
    IMAGE_EXTS, PDF_EXTS, DOCX_EXTS, TEXT_LIKE_EXTS,
)
from artifacts import (
    RESOURCEFULNESS_DIRECTIVE,
    extract_artifact_blocks,
    save_artifacts as _save_artifacts,
    annotate_empty_promise as _annotate_empty_promise,
    has_empty_promise as _has_empty_promise,
    make_documents_router,
)
from style_guide import (
    make_style_guide_router,
    format_guide_for_prompt as _format_style_guide_prompt,
)
from missions import (  # noqa: E402
    make_missions_router,
    rehydrate_running_missions,
    build_tools as _build_mission_tools,
)
from firebase_auth import (
    firebase_enabled as _firebase_enabled,
    _looks_like_firebase_token,
    verify_firebase_id_token,
    upsert_user_for_firebase_claims,
)
from team_access import (
    can_user_view_team,
    can_user_message_in_team,
    can_user_access_private_cos,
    is_team_admin,
    is_org_owner as _is_org_owner,
    team_visibility,
    team_member_ids,
    team_admin_ids,
    backfill_team_membership,
    serialize_team_with_visibility,
    get_or_create_private_cos,
    PRIVATE_COS_KIND,
)

ROOT_DIR = Path(__file__).parent
UPLOAD_DIR = ROOT_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)
ASSETS_DIR = ROOT_DIR / "uploads" / "assets"
ASSETS_DIR.mkdir(parents=True, exist_ok=True)
DOCS_DIR = ROOT_DIR / "uploads" / "docs"
DOCS_DIR.mkdir(parents=True, exist_ok=True)
load_dotenv(ROOT_DIR / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("soulprint")

# ── DB ───────────────────────────────────────────────────────────────────────
client = AsyncIOMotorClient(os.environ["MONGO_URL"])
db = client[os.environ["DB_NAME"]]

from file_storage import save_blob_to_gridfs, read_blob_from_gridfs  # noqa: E402

async def persist_bytes(disk_path: Path, content: bytes,
                        original_name: str = "", mime: str = "") -> Optional[str]:
    """Write ``content`` to ``disk_path`` AND mirror to MongoDB GridFS.

    Disk stays primary for fast local serving. The returned ``gridfs_id``
    (str or None) should be stored alongside the on-disk filename in the
    owning collection so that, if the container is redeployed and the
    disk file vanishes, the serve handler can recover the bytes from
    GridFS and rehydrate the disk copy. See file_storage.py for details.

    A row is also written to ``media_files`` keyed by the on-disk
    basename, so the global serve handlers can recover bytes for ANY
    file written through this helper without each owning collection
    having to remember the gridfs_id.
    """
    disk_path.parent.mkdir(parents=True, exist_ok=True)
    disk_path.write_bytes(content)
    gridfs_id = await save_blob_to_gridfs(
        db, content, original_name or disk_path.name, mime,
    )
    if gridfs_id:
        try:
            await db.media_files.update_one(
                {"filename": disk_path.name},
                {"$set": {
                    "filename": disk_path.name,
                    "gridfs_id": gridfs_id,
                    "mime": mime,
                    "original_name": original_name or disk_path.name,
                    "size": len(content),
                    "created_at": datetime.now(timezone.utc),
                }},
                upsert=True,
            )
        except Exception:
            pass
    return gridfs_id


async def recover_bytes_from_gridfs(filename: str) -> tuple[Optional[bytes], Optional[str], Optional[str]]:
    """Look up filename → gridfs_id in ``media_files`` and read bytes.

    Returns (bytes, mime, original_name) or (None, None, None) on miss.
    """
    rec = await db.media_files.find_one({"filename": filename})
    if not rec:
        return None, None, None
    gid = rec.get("gridfs_id")
    if not gid:
        return None, None, None
    blob = await read_blob_from_gridfs(db, gid)
    if blob is None:
        return None, None, None
    return blob, rec.get("mime") or None, rec.get("original_name") or filename

# ── App ──────────────────────────────────────────────────────────────────────
app = FastAPI(title="Foundry API")

# ── Security middleware (headers + brute-force lockout) ──────────────────
# Security headers must be added BEFORE other middleware so they're applied
# on every response (including error paths). CORS must remain the outermost
# layer so preflights still resolve cleanly.
from security import (  # noqa: E402
    LoginThrottle, RateLimiter, SecurityHeadersMiddleware,
    SECURITY_POLICY_DOC, _client_ip,
)
app.add_middleware(SecurityHeadersMiddleware)

# Background task refs (held to prevent garbage collection).
_poi_anchor_task: Optional[asyncio.Task] = None
_poi_upgrade_task: Optional[asyncio.Task] = None

JWT_SECRET = os.environ.get("JWT_SECRET", "soulprint-dev-secret")
JWT_ALGO = "HS256"


async def _ensure_persistent_jwt_secret():
    """Make the JWT signing secret survive any redeployment.

    Resolution order:
      1. `JWT_SECRET` env var if set AND not the fallback dev value
         → respect it (the operator has explicitly pinned it).
      2. A persisted value in the `_system_secrets` MongoDB collection
         → use that (token from a previous boot is still valid).
      3. First boot ever → generate a fresh 64-hex-char secret,
         persist it to Mongo, then use it.

    Why this matters: Emergent's deploy pipeline can rotate auto-
    generated env vars between deploys. Without this safeguard, every
    redeploy invalidates every signed-in user. By keeping the secret
    in Mongo (which persists across deploys), tokens stay valid for
    their full 30-day lifetime regardless of how often the app is
    redeployed.
    """
    import secrets as _stdlib_secrets
    global JWT_SECRET
    env_val = os.environ.get("JWT_SECRET", "").strip()
    if env_val and env_val != "soulprint-dev-secret":
        JWT_SECRET = env_val
        return
    try:
        row = await db.system_secrets.find_one({"_id": "jwt"})
        if row and row.get("value"):
            JWT_SECRET = row["value"]
            return
        new_val = _stdlib_secrets.token_hex(32)
        await db.system_secrets.update_one(
            {"_id": "jwt"},
            {"$setOnInsert": {
                "value": new_val,
                "created_at": datetime.now(timezone.utc),
            }},
            upsert=True,
        )
        # Re-read in case a concurrent boot won the race.
        row = await db.system_secrets.find_one({"_id": "jwt"})
        if row and row.get("value"):
            JWT_SECRET = row["value"]
    except Exception as exc:
        logger.warning("Could not persist JWT secret to Mongo: %s", exc)
        # Fall back to the env value (even if it's the dev default) so
        # the app still boots — just with the regression that tokens
        # may invalidate on the next deploy.

# ── LLM key resolution ──────────────────────────────────────────────────────
# Prefer the user's own OPENAI_API_KEY when it's set (per Reggie's explicit
# choice on Feb 5, 2026 — "default to my OpenAI key whenever an LLM is
# needed"). The Emergent universal key is kept as a fallback so the app
# still works for users who haven't supplied their own provider key.
#
# Image generation requires a separate handle — the Gemini Nano Banana
# model is only reachable via the Emergent universal key (user-supplied
# OpenAI keys can't talk to the gemini provider). Hence we keep
# `_EMERGENT_KEY_FOR_IMAGES` available for the Nano Banana wiring below.
_USER_OPENAI_KEY = (os.environ.get("OPENAI_API_KEY") or "").strip()
_EMERGENT_KEY_FOR_IMAGES = os.environ.get("EMERGENT_LLM_KEY", "")
EMERGENT_LLM_KEY = _USER_OPENAI_KEY or _EMERGENT_KEY_FOR_IMAGES
# Default text model when an LlmChat is constructed without an explicit
# ``.with_model()`` call. Set per Reggie's choice; existing per-site model
# choices (e.g. gpt-4.1-mini for cheap helpers) are preserved.
DEFAULT_TEXT_LLM_MODEL = "gpt-5.1"

# ── Auth helpers ─────────────────────────────────────────────────────────────
def hash_password(p: str) -> str:
    return bcrypt.hashpw(p.encode(), bcrypt.gensalt()).decode()

def verify_password(p: str, h: str) -> bool:
    try:
        return bcrypt.checkpw(p.encode(), h.encode())
    except Exception:
        return False

# Sessions persist for 30 days so users don't get punted back to the login
# screen on every deploy / browser-restart. We re-issue a fresh token on
# every successful request via `slide_session_cookie` so an active user's
# expiry keeps rolling forward — long absences (>30d) still re-auth.
JWT_LIFETIME_DAYS = 30
JWT_LIFETIME_SECONDS = JWT_LIFETIME_DAYS * 24 * 60 * 60

def create_access_token(user_id: str, email: str) -> str:
    return jwt.encode({
        "sub": user_id, "email": email, "type": "access",
        "exp": datetime.now(timezone.utc) + timedelta(days=JWT_LIFETIME_DAYS),
    }, JWT_SECRET, algorithm=JWT_ALGO)

def set_auth_cookies(resp: Response, access: str):
    # Cookies are issued over HTTPS in the preview / production. `secure=True` is
    # required by Chrome's recent SameSite rules for cookies set over HTTPS, and
    # avoids silent-drop scenarios where the cookie is set but never sent back.
    resp.set_cookie(
        "access_token", access,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=JWT_LIFETIME_SECONDS,
        path="/",
    )

async def get_current_user(request: Request) -> dict:
    token = request.cookies.get("access_token")
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
    if not token:
        raise HTTPException(401, "Not authenticated")

    # Firebase ID tokens have a recognisable issuer — sniff first so we
    # never feed a Firebase RS256 token to the legacy HS256 decoder. If
    # Firebase isn't configured on this backend, every token is treated
    # as legacy (so existing JWT users keep working untouched).
    user_doc: Optional[dict] = None
    if _firebase_enabled() and _looks_like_firebase_token(token):
        try:
            claims = verify_firebase_id_token(token)
        except ValueError as exc:
            # Surface the actual reason in the response detail so the
            # frontend / agent can debug clock-skew, project-id mismatch,
            # rotated keys, etc. without grepping pod logs.
            logger.warning("Firebase token verification failed: %s", exc)
            raise HTTPException(401, f"Firebase token rejected: {exc}")
        try:
            resolved = await upsert_user_for_firebase_claims(db, claims)
        except Exception as exc:
            logger.exception("Firebase upsert failed: %s", exc)
            raise HTTPException(401, "Could not resolve Firebase user")
        # Block disabled accounts (set by an admin via /api/admin/users/{id}).
        # Re-fetch the doc to check the live flag — the upsert helper
        # doesn't expose it.
        live = await db.users.find_one({"_id": ObjectId(resolved["id"])}, {"is_disabled": 1})
        if live and live.get("is_disabled"):
            raise HTTPException(403, "Account disabled. Contact your administrator.")
        # Capture user-context headers for the agent prompt block.
        tz = (request.headers.get("X-User-Timezone") or "").strip()[:80]
        loc = (request.headers.get("X-User-Location") or "").strip()[:200]
        _USER_CTX.set({"timezone": tz, "location": loc})
        return resolved

    # Legacy HS256 path — unchanged.
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Invalid token")
    user = await db.users.find_one({"_id": ObjectId(payload["sub"])})
    if not user:
        raise HTTPException(401, "User not found")
    if user.get("is_disabled"):
        raise HTTPException(403, "Account disabled. Contact your administrator.")
    # Capture user-context headers (timezone + optional location) so deeper
    # helpers can pick them up via _USER_CTX without threading a request param.
    tz = (request.headers.get("X-User-Timezone") or "").strip()[:80]
    loc = (request.headers.get("X-User-Location") or "").strip()[:200]
    _USER_CTX.set({"timezone": tz, "location": loc})
    return {
        "id": str(user["_id"]),
        "email": user["email"],
        "name": user["name"],
        "is_superadmin": bool(user.get("is_superadmin")),
    }


async def get_user_or_none(request: Request) -> Optional[dict]:
    """Like ``get_current_user`` but returns ``None`` instead of
    raising 401. Used by public-safe endpoints that branch on auth
    state (e.g. /api/trial/status, which surfaces the trial timer to
    a signed-in caller and gracefully falls back to anonymous for
    visitors). Also augments the dict with the trial fields so the
    SPA can show a countdown banner without an extra round-trip.
    """
    try:
        u = await get_current_user(request)
    except HTTPException:
        return None
    # Pull the trial fields off the live row — get_current_user only
    # returns the slim public profile.
    full = await db.users.find_one({"_id": ObjectId(u["id"])},
                                   {"_id": 0, "trial_started_at": 1,
                                    "trial_expires_at": 1, "trial_source": 1})
    if full:
        u.update(full)
    return u

# ── Per-request user context (timezone + approximate location) ──────────────
# Set in `get_current_user` from the X-User-Timezone / X-User-Location headers
# the frontend injects on every request, then read inside `generate_agent_reply`
# so agents know "your local time is …" without polluting every signature.
import contextvars  # noqa: E402  (small import block, kept near use site)
_USER_CTX: contextvars.ContextVar[dict] = contextvars.ContextVar("user_ctx", default={})

def _user_context_block() -> str:
    """Render the current user's timezone + approximate location as a system-prompt
    snippet. Returns "" if nothing is set."""
    ctx = _USER_CTX.get() or {}
    tz = ctx.get("timezone") or ""
    loc = ctx.get("location") or ""
    if not tz and not loc:
        return ""
    parts = []
    if tz:
        try:
            from zoneinfo import ZoneInfo
            now_local = datetime.now(ZoneInfo(tz))
            stamp = now_local.strftime("%A, %B %d %Y at %H:%M %Z")
            parts.append(f"Current local date/time for the user: {stamp} ({tz}).")
        except Exception:
            parts.append(f"User timezone: {tz}.")
    if loc:
        parts.append(f"Approximate user location: {loc}.")
    parts.append(
        "Use this to interpret 'today', 'tomorrow', 'this week' etc., quote times "
        "in the user's local zone unless they ask otherwise, and tailor any "
        "regional advice (weather, events, currency) to that location."
    )
    return "\n".join(parts)

# ── Agent catalogue — loaded from agents_catalogue.json on startup ──────────
AGENTS_CATALOGUE_PATH = ROOT_DIR / "agents_catalogue.json"

def _load_agents() -> dict:
    try:
        data = json.loads(AGENTS_CATALOGUE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning("agents_catalogue.json not found — empty roster.")
        return {}
    out: dict = {}
    for a in data:
        aid = a["id"]
        # Expose `role` alongside `name` so the UI can render both consistently.
        out[aid] = {
            "id": aid,
            "name": a.get("name") or aid,
            "role": a.get("name") or aid,  # legacy alias used by ConversationPage "· role"
            "division": a.get("division", "Specialized"),
            "division_key": a.get("division_key", "specialized"),
            "description": a.get("description", ""),
            "tagline": a.get("tagline") or a.get("description", ""),
            "emoji": a.get("emoji", ""),
            "accent": a.get("accent", "#a78bfa"),
            "system": a.get("system", ""),
        }
    return out

AGENTS = _load_agents()
logger.info("Loaded %d agents from catalogue.", len(AGENTS))

def is_agent(aid: str) -> bool:
    return aid in AGENTS

def public_agent(a: dict) -> dict:
    # Strip the long system prompt before sending over the wire.
    return {k: v for k, v in a.items() if k != "system"}

# ── Models (API contracts) ───────────────────────────────────────────────────
class RegisterBody(BaseModel):
    name: str
    email: str
    password: str
    # Required for the pre-launch site gate. Accepts either an
    # admin-issued private access code or an existing 24h trial code
    # from the audit funnel. Validated + redeemed atomically with the
    # user row so a fresh signup doesn't bounce off the gate UI.
    access_code: Optional[str] = ""

class LoginBody(BaseModel):
    email: str
    password: str

class OrgCreateBody(BaseModel):
    name: str
    website_url: Optional[str] = ""
    soul_description: Optional[str] = ""  # manual override; if empty, generated from website_url

class OrgUpdateBody(BaseModel):
    name: Optional[str] = None
    website_url: Optional[str] = None
    soul_description: Optional[str] = None
    regenerate_soul: bool = False  # if True, re-run the soul generator with current url+desc
    # Auto-archive policy: 0 = off (manual archive only), or 30 / 60 / 90 days
    # of conversation inactivity after which we auto-archive idle threads.
    archive_idle_days: Optional[int] = None

class TeamCreateBody(BaseModel):
    name: str
    description: Optional[str] = ""
    visibility: Optional[str] = "open"   # "open" | "closed"

class TeamUpdateBody(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    visibility: Optional[str] = None     # "open" | "closed"

class ScheduleCreateBody(BaseModel):
    title: str
    prompt: str
    scope: Literal["conversation", "team", "org"] = "conversation"
    target_id: str  # conversation_id, team_id, or org_id depending on scope
    cron: Optional[str] = None  # 5-field cron expression for recurring jobs
    run_at: Optional[str] = None  # ISO-8601 for one-shot
    timezone: Optional[str] = "UTC"
    active: bool = True

class ScheduleUpdateBody(BaseModel):
    title: Optional[str] = None
    prompt: Optional[str] = None
    cron: Optional[str] = None
    run_at: Optional[str] = None
    timezone: Optional[str] = None
    active: Optional[bool] = None

class SoulPrintBody(BaseModel):
    mission: str = ""
    tone: str = ""
    audience: str = ""
    values: str = ""
    north_star: str = ""

class InviteCreateBody(BaseModel):
    role: Literal["admin", "member"] = "member"
    # Optional — if provided, we email the accept link to this address
    # via Resend (see email_service.send_resend_email). The invite row is
    # persisted regardless so the inviter always gets a copy-able link.
    email: Optional[EmailStr] = None

class InviteAcceptBody(BaseModel):
    code: str

class ConversationCreateBody(BaseModel):
    title: Optional[str] = ""
    type: Literal["direct", "group"] = "direct"
    agent_ids: List[str] = []
    member_ids: List[str] = []  # human member ids (excluding creator)
    auto_respond: bool = False  # for group chats
    smart_expand: bool = False  # group chats: auto-add the best new agent when topic shifts

class SendMessageBody(BaseModel):
    text: str

class SmartStartBody(BaseModel):
    text: str
    title: Optional[str] = ""

class SwitchAgentBody(BaseModel):
    agent_id: str
    text: Optional[str] = ""  # optional intro message after the switch

class GenerateImageBody(BaseModel):
    prompt: str
    agent_id: Optional[str] = None  # if omitted, use the conversation's current agent

class GenerateVideoBody(BaseModel):
    prompt: str
    agent_id: Optional[str] = None
    aspect_ratio: Optional[str] = "16:9"  # 16:9, 9:16, Auto

class ForkConvBody(BaseModel):
    message_id: str  # id of an agent message in the source conversation

# ── Access control helpers ───────────────────────────────────────────────────
async def require_org_member(org_id: str, user_id: str) -> dict:
    try:
        org = await db.organizations.find_one({"_id": ObjectId(org_id)})
    except Exception:
        raise HTTPException(400, "Invalid org id")
    if not org:
        raise HTTPException(404, "Organization not found")
    m = await db.org_members.find_one({"org_id": org_id, "user_id": user_id})
    if not m:
        raise HTTPException(403, "Not a member of this organization")
    return {"org": org, "member": m}

async def require_team_member(team_id: str, user_id: str, *, mode: str = "view") -> dict:
    """Gate access to a team. ``mode`` is one of:
      - "view": user can see the team listed (open or member of closed)
      - "message": user can send/receive in the team's conversations
      - "admin": user can edit visibility / invite / remove members
    Org owners always pass every mode.
    """
    try:
        team = await db.teams.find_one({"_id": ObjectId(team_id)})
    except Exception:
        raise HTTPException(400, "Invalid team id")
    if not team:
        raise HTTPException(404, "Team not found")
    org = await db.organizations.find_one({"_id": ObjectId(team["org_id"])})
    if not org:
        raise HTTPException(404, "Organization not found")
    m = await db.org_members.find_one({"org_id": team["org_id"], "user_id": user_id})
    if not m and not _is_org_owner(org, user_id):
        raise HTTPException(403, "Not a member of this organization")
    # Lazy backfill so the new fields are populated for legacy rows.
    team = await backfill_team_membership(db, team, org)

    member_rows = await db.org_members.find({"org_id": team["org_id"]}, {"user_id": 1}).to_list(2000)
    org_member_ids = [str(r["user_id"]) for r in member_rows]

    if mode == "admin":
        if not is_team_admin(team=team, org=org, user_id=user_id):
            raise HTTPException(403, "Only team admins can do this")
    elif mode == "message":
        if not can_user_message_in_team(
            team=team, org=org, user_id=user_id, org_member_ids=org_member_ids,
        ):
            raise HTTPException(403, "Not a member of this team — join first to participate")
    else:
        if not can_user_view_team(
            team=team, org=org, user_id=user_id, org_member_ids=org_member_ids,
        ):
            raise HTTPException(403, "No access to this team")
    return {"team": team, "org": org, "org_role": (m or {}).get("role", "owner"), "org_member_ids": org_member_ids}

def serialize_org(org: dict) -> dict:
    from brand import serialize_brand as _serialize_brand
    return {
        "id": str(org["_id"]),
        "name": org["name"],
        "owner_id": org["owner_id"],
        "website_url": org.get("website_url", ""),
        "soul_description": org.get("soul_description", ""),
        "soul": org.get("soul") or {},
        "brand_assets": _serialize_brand(org),
        "general_team_id": org.get("general_team_id", ""),
        "general_conversation_id": org.get("general_conversation_id", ""),
        "archive_idle_days": int(org.get("archive_idle_days") or 0),
        "created_at": org["created_at"].isoformat() if isinstance(org["created_at"], datetime) else org["created_at"],
    }


def _can_view_mind_map(org: dict, viewer_id: str, role: str = "") -> bool:
    """Mind map is an OWNER tool — only the org owner sees it by default.
    Owners can grant access to specific non-owner members by adding their
    user_id to ``org.mind_map_access``. Site-level superadmins do not get
    access on that basis alone (they must be the actual org owner or
    explicitly granted).
    """
    if not org or not viewer_id:
        return False
    if str(org.get("owner_id")) == str(viewer_id):
        return True
    if role == "owner":
        return True
    allowed = org.get("mind_map_access") or []
    return str(viewer_id) in [str(x) for x in allowed]

def serialize_team(team: dict) -> dict:
    return {
        "id": str(team["_id"]),
        "org_id": team["org_id"],
        "name": team["name"],
        "description": team.get("description", ""),
        "soulprint": team.get("soulprint", {}),
        "is_general": bool(team.get("is_general")),
        "is_direct": bool(team.get("is_direct")),
        "visibility": team_visibility(team),
        "created_by": str(team.get("created_by") or ""),
        "created_at": team["created_at"].isoformat() if isinstance(team["created_at"], datetime) else team["created_at"],
    }

def serialize_conv(c: dict) -> dict:
    return {
        "id": str(c["_id"]),
        "team_id": c["team_id"],
        "title": c.get("title", ""),
        "type": c["type"],
        "agent_ids": c.get("agent_ids", []),
        "member_ids": c.get("member_ids", []),
        "auto_respond": c.get("auto_respond", False),
        "smart_expand": c.get("smart_expand", False),
        "created_by": c["created_by"],
        "created_at": c["created_at"].isoformat() if isinstance(c["created_at"], datetime) else c["created_at"],
        "archived_at": (
            c["archived_at"].isoformat()
            if isinstance(c.get("archived_at"), datetime) else c.get("archived_at")
        ),
        "is_archived": bool(c.get("archived_at")),
    }

# ── Auth routes ──────────────────────────────────────────────────────────────
auth_r = APIRouter(prefix="/api/auth")

# Brute-force lockout for /login + per-IP rate limits for sensitive endpoints.
# All raise 429 when tripped; legit users see a clear "try again in N min" toast.
login_throttle = LoginThrottle(db)
register_limiter = RateLimiter(calls=10, window_s=60)        # 10 reg/min/IP
invite_accept_limiter = RateLimiter(calls=10, window_s=60)   # 10 accepts/min/IP
password_reset_limiter = RateLimiter(calls=5, window_s=300)  # 5 / 5min / IP

@auth_r.post("/register")
async def register(body: RegisterBody, request: Request, resp: Response):
    register_limiter(request)  # raises 429 if abused
    email = body.email.lower().strip()
    if not email or not body.password or len(body.password) < 6:
        raise HTTPException(400, "Email and password (min 6 chars) required")
    if await db.users.find_one({"email": email}):
        raise HTTPException(400, "Email already registered")
    # ── Site access gate ──
    # Validate the access code BEFORE creating the user so a bad code
    # doesn't leave half-baked rows behind. We do the actual redemption
    # AFTER user creation so the redemption row points at a real user_id.
    from access_codes import (
        validate_code_only, redeem_code_for_user,
        mint_device_cookie, set_access_cookie,
    )
    code_raw = (body.access_code or "").strip()
    if not code_raw:
        raise HTTPException(
            400,
            "An access code is required to sign up. Have a 24-hour trial code "
            "or an invite code? Enter it on the sign-up form. No code yet? "
            "Request a free trial at /get-audit.",
        )
    validated = await validate_code_only(db, code_raw)
    if not validated:
        raise HTTPException(
            400,
            "That access code didn't check out — make sure it hasn't been "
            "used, revoked, or expired.",
        )
    doc = {
        "name": body.name.strip() or email.split("@")[0],
        "email": email,
        "password_hash": hash_password(body.password),
        "created_at": datetime.now(timezone.utc),
    }
    # Auto-promote configured superadmin emails on first sign-up so they
    # don't need a manual DB poke after registering.
    if email in {e.lower().strip() for e in SUPERADMIN_EMAILS}:
        doc["is_superadmin"] = True
    res = await db.users.insert_one(doc)
    uid = str(res.inserted_id)
    # Burn the code now that the user exists. Failure here is fatal —
    # we'd rather refuse a half-completed signup than mint a user that
    # can't see the app. Roll back the user row in that case.
    redeemed = await redeem_code_for_user(db, code_raw, uid)
    if not redeemed:
        await db.users.delete_one({"_id": res.inserted_id})
        raise HTTPException(
            400,
            "That access code just became invalid (likely consumed by "
            "another signup). Please try a different code.",
        )
    _reason, code_id = redeemed
    # Mint the long-lived device cookie so the SAME browser doesn't
    # see the post-auth gate fallback if for any reason /auth/me lags.
    set_access_cookie(resp, mint_device_cookie(code_id))
    token = create_access_token(uid, email)
    set_auth_cookies(resp, token)
    # Welcome email — fire-and-forget so a Resend outage / missing API
    # key never blocks the registration flow. The helper itself logs at
    # warning level if delivery fails.
    try:
        from email_service import send_welcome_email
        asyncio.create_task(send_welcome_email(name=doc["name"], email=email))
    except Exception:
        pass
    return {"id": uid, "name": doc["name"], "email": email,
            "is_superadmin": bool(doc.get("is_superadmin")),
            "access_token": token}

@auth_r.post("/login")
async def login(body: LoginBody, request: Request, resp: Response):
    email = body.email.lower().strip()
    ip = _client_ip(request)
    # Brute-force gate — raises 429 with retry-after message if the
    # caller is locked out. Fail-open if Mongo is hiccuping (better to
    # let a real user in than block them on transient infra issues).
    await login_throttle.check(ip=ip, email=email)
    user = await db.users.find_one({"email": email})
    if not user or not verify_password(body.password, user["password_hash"]):
        await login_throttle.record_failure(ip=ip, email=email)
        raise HTTPException(401, "Invalid email or password")
    if user.get("is_disabled"):
        raise HTTPException(403, "Account disabled. Contact your administrator.")
    # Successful login → wipe the failure counter so the next legit
    # mistake doesn't compound with old failed attempts.
    await login_throttle.clear(ip=ip, email=email)
    uid = str(user["_id"])
    token = create_access_token(uid, email)
    set_auth_cookies(resp, token)
    return {"id": uid, "name": user["name"], "email": email,
            "is_superadmin": bool(user.get("is_superadmin")),
            "access_token": token}

@auth_r.post("/logout")
async def logout(resp: Response):
    resp.delete_cookie("access_token", path="/")
    return {"message": "Logged out"}

@auth_r.get("/me")
async def me(u: dict = Depends(get_current_user)):
    # Hydrate the routing preferences (per-type Dynamic Intelligence config).
    # We persist these on the user doc so they survive logout / device hops.
    # Default = "auto" for every media type (Dynamic Intelligence on).
    full = await db.users.find_one({"_id": ObjectId(u["id"])}, {"_id": 0, "password": 0, "password_hash": 0})
    prefs = (full or {}).get("routing_preferences") or {}
    u["routing_preferences"] = {
        "chat":  prefs.get("chat",  "auto"),
        "image": prefs.get("image", "auto"),
        "video": prefs.get("video", "auto"),
        "music": prefs.get("music", "auto"),
    }
    # Profile fields (editable on /app/account). Always present in the
    # me response so the form renders the right initial values.
    u["phone"] = (full or {}).get("phone", "") or ""
    u["timezone"] = (full or {}).get("timezone", "") or ""
    u["firebase_uid"] = (full or {}).get("firebase_uid", "") or ""
    # Tells the frontend whether a local password exists (so AccountPage
    # knows to show the change-password form). Firebase-only sign-ups
    # have no `password_hash` and should manage their password through
    # Google instead. Use a count() query so we never leak the hash itself.
    has_pwd = await db.users.count_documents(
        {"_id": ObjectId(u["id"]), "password_hash": {"$exists": True, "$ne": ""}},
        limit=1,
    )
    u["has_password"] = bool(has_pwd)
    # Trial state — surfaces a 24h countdown banner in TopBar and
    # gates "trial expired" upsells. Both fields are ISO-strings or
    # null. Never falsifies a trial (we only project the fields if
    # they were stamped at trial-redeem time).
    if (full or {}).get("trial_started_at"):
        ts = full["trial_started_at"]
        u["trial_started_at"] = ts.isoformat() if isinstance(ts, datetime) else str(ts)
    if (full or {}).get("trial_expires_at"):
        te = full["trial_expires_at"]
        u["trial_expires_at"] = te.isoformat() if isinstance(te, datetime) else str(te)
    # Pre-launch site access gate state. Mirrors the access_codes module's
    # `_user_has_access` decision so the SPA can render the gate without an
    # extra round-trip on first paint. Superadmins always pass; users with
    # a redeemed access code or trial pass; everyone else hits the gate.
    if (full or {}).get("access_granted_at"):
        ag = full["access_granted_at"]
        u["access_granted_at"] = ag.isoformat() if isinstance(ag, datetime) else str(ag)
    u["has_site_access"] = bool(
        u.get("is_superadmin")
        or u.get("access_granted_at")
        or u.get("trial_started_at")
    )
    # Telegram-connection state for the Phase-1 proactive Chief-of-Staff
    # surface (Feb 2026). `telegram_chat_id` becomes truthy after the
    # user clicks the bot deep-link and our webhook captures their chat.
    # The username + connected_at fields are purely for UI display.
    tg = (full or {}).get("telegram") or {}
    u["telegram"] = {
        "connected": bool(tg.get("chat_id")),
        "username": tg.get("username") or "",
        "connected_at": (
            tg["connected_at"].isoformat()
            if isinstance(tg.get("connected_at"), datetime) else tg.get("connected_at")
        ),
        "preferences": tg.get("preferences") or {
            "outbound_enabled": True,        # Phase 1 nudges on by default
            "digest_enabled": True,          # Phase 2 weekly digest
            "min_severity": "actionable",    # "actionable" | "all" | "weekly_only"
        },
    }
    return u


class ProfileUpdateBody(BaseModel):
    """Editable profile fields. Phone is normalised to a simple E.164-ish
    string client-side; we just persist verbatim."""
    name: Optional[str] = Field(None, min_length=1, max_length=80)
    email: Optional[EmailStr] = None
    phone: Optional[str] = Field(None, max_length=40)
    timezone: Optional[str] = Field(None, max_length=80)


@auth_r.patch("/me")
async def update_my_profile(
    body: ProfileUpdateBody, u: dict = Depends(get_current_user),
):
    update: dict = {}
    if body.name is not None:
        update["name"] = body.name.strip()
    if body.phone is not None:
        update["phone"] = body.phone.strip()
    if body.timezone is not None:
        update["timezone"] = body.timezone.strip()
    if body.email is not None:
        new_email = str(body.email).lower().strip()
        if new_email and new_email != u.get("email", "").lower():
            # Email is the login key — block if another user already
            # owns the new address (case-insensitive).
            clash = await db.users.find_one({
                "email": new_email,
                "_id": {"$ne": ObjectId(u["id"])},
            })
            if clash:
                raise HTTPException(409, "That email is already in use")
            update["email"] = new_email
    if not update:
        return {"ok": True, "unchanged": True}
    await db.users.update_one(
        {"_id": ObjectId(u["id"])}, {"$set": update},
    )
    full = await db.users.find_one(
        {"_id": ObjectId(u["id"])},
        {"_id": 0, "password": 0, "password_hash": 0},
    )
    return {"ok": True, "user": {
        "id": u["id"],
        "name": (full or {}).get("name", ""),
        "email": (full or {}).get("email", ""),
        "phone": (full or {}).get("phone", ""),
        "timezone": (full or {}).get("timezone", ""),
    }}


class PasswordChangeBody(BaseModel):
    current_password: str = Field(..., min_length=1, max_length=200)
    new_password: str = Field(..., min_length=8, max_length=200)


@auth_r.post("/me/password")
async def change_my_password(
    body: PasswordChangeBody, u: dict = Depends(get_current_user),
):
    user_doc = await db.users.find_one({"_id": ObjectId(u["id"])})
    if not user_doc:
        raise HTTPException(404, "User not found")
    # Firebase-auth users don't have a local password_hash — they
    # change passwords through Firebase, not through us. Tell them
    # explicitly instead of silently failing.
    if not user_doc.get("password_hash"):
        raise HTTPException(
            400,
            "This account uses Google sign-in. Manage your password "
            "through your Google account.",
        )
    if not verify_password(body.current_password, user_doc["password_hash"]):
        raise HTTPException(403, "Current password is incorrect")
    await db.users.update_one(
        {"_id": ObjectId(u["id"])},
        {"$set": {"password_hash": hash_password(body.new_password)}},
    )
    return {"ok": True}


class RoutingPreferencesBody(BaseModel):
    """Per-type routing defaults. Each value is either ``"auto"`` (use
    Dynamic Intelligence) or a model key (lock that media type to a
    specific model). Unset keys are left unchanged so the frontend can
    PATCH a single type without resending the others.
    """
    chat:  Optional[str] = None
    image: Optional[str] = None
    video: Optional[str] = None
    music: Optional[str] = None


@auth_r.patch("/me/preferences")
async def update_preferences(
    body: RoutingPreferencesBody,
    u: dict = Depends(get_current_user),
):
    """Server-side persistence for the Dynamic Intelligence picker.
    Survives logout / device hops; the frontend hydrates these on auth/me.
    """
    update: dict = {}
    for k in ("chat", "image", "video", "music"):
        v = getattr(body, k)
        if v is None:
            continue
        v = str(v).strip()
        if not v:
            continue
        update[f"routing_preferences.{k}"] = v
    if not update:
        return {"ok": True, "unchanged": True}
    await db.users.update_one(
        {"_id": ObjectId(u["id"])},
        {"$set": update},
    )
    full = await db.users.find_one({"_id": ObjectId(u["id"])}, {"routing_preferences": 1, "_id": 0})
    prefs = (full or {}).get("routing_preferences") or {}
    return {
        "ok": True,
        "routing_preferences": {
            "chat":  prefs.get("chat",  "auto"),
            "image": prefs.get("image", "auto"),
            "video": prefs.get("video", "auto"),
            "music": prefs.get("music", "auto"),
        },
    }

# ── Org routes ───────────────────────────────────────────────────────────────
org_r = APIRouter(prefix="/api/orgs")

# Default agent that anchors the auto-created "General" group chat
GENERAL_DEFAULT_AGENT = "specialized-chief-of-staff"

async def _fetch_url_text(url: str, max_chars: int = 8000) -> str:
    """Fetch a URL and return a compact plain-text excerpt of its body.

    Hardened for the modern web — especially `.app` / SPA-style sites:
    - Uses a real browser User-Agent (many hosts 403 obvious bot UAs)
    - Auto-prepends `https://` (covers HSTS-only TLDs like `.app`, `.dev`)
    - Tries `https://www.<host>` if the bare host returns 4xx
    - Follows `<meta http-equiv="refresh">` AND inline JS
      (`window.location.href = "/x"`) one hop, which `.app` lander pages rely on
    - Strips scripts/styles, then if the visible text is too short
      (typical SPA shell — `<div id="root"></div>` + bundles), falls back to
      stitching together `<title>`, `<meta name="description">` and OG tags so
      the LLM still gets something to chew on.

    Returns "" on any failure — caller decides whether to surface an error.
    """
    if not url or not url.strip():
        return ""
    target = url.strip()
    if not target.startswith(("http://", "https://")):
        target = "https://" + target

    browser_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    async def _get(client: "httpx.AsyncClient", u: str) -> Optional[str]:
        try:
            resp = await client.get(u)
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("scrape error %s: %s", u, exc)
            return None
        if resp.status_code >= 400:
            logger.warning("scrape %s -> %s", u, resp.status_code)
            return None
        return resp.text or ""

    def _next_hop(html: str, base: str) -> Optional[str]:
        # `<meta http-equiv="refresh" content="0; url=/lander">`
        m = re.search(
            r'<meta[^>]+http-equiv=["\']?refresh["\']?[^>]+content=["\'][^"\']*url=([^"\'>\s]+)',
            html, flags=re.IGNORECASE,
        )
        if not m:
            # Inline JS redirect — common on `.app` lander pages
            m = re.search(
                r'(?:window\.)?location(?:\.href)?\s*=\s*["\']([^"\']+)["\']',
                html, flags=re.IGNORECASE,
            )
        if not m:
            return None
        nxt = m.group(1).strip()
        if nxt.startswith(("http://", "https://")):
            return nxt
        if nxt.startswith("//"):
            scheme = base.split("://", 1)[0]
            return f"{scheme}:{nxt}"
        # path-relative
        try:
            from urllib.parse import urljoin
            return urljoin(base, nxt)
        except Exception:
            return None

    def _meta_fallback(html: str) -> str:
        """Glue together title + description + OG tags as last-resort context."""
        bits: list[str] = []
        title = re.search(r"<title[^>]*>([^<]+)</title>", html, flags=re.IGNORECASE)
        if title:
            bits.append(title.group(1).strip())
        for prop in (
            'name=["\']description["\']',
            'property=["\']og:title["\']',
            'property=["\']og:description["\']',
            'property=["\']og:site_name["\']',
            'name=["\']twitter:description["\']',
            'name=["\']twitter:title["\']',
            'name=["\']apple-mobile-web-app-title["\']',
            'name=["\']application-name["\']',
        ):
            m = re.search(
                rf'<meta[^>]+{prop}[^>]+content=["\']([^"\']+)["\']',
                html, flags=re.IGNORECASE,
            )
            if m:
                bits.append(m.group(1).strip())
        return " — ".join(b for b in bits if b)

    def _manifest_url(html: str, base: str) -> Optional[str]:
        """Many PWAs (esp. on `.app` domains) ship `<link rel="manifest" href="/manifest.json">`
        whose `name` + `description` are the canonical site description, even
        when the static HTML has no title/meta tags at all."""
        m = re.search(
            r'<link[^>]+rel=["\']manifest["\'][^>]+href=["\']([^"\']+)["\']',
            html, flags=re.IGNORECASE,
        )
        if not m:
            return None
        href = m.group(1).strip()
        if href.startswith(("http://", "https://")):
            return href
        try:
            from urllib.parse import urljoin
            return urljoin(base, href)
        except Exception:
            return None

    async def _manifest_text(client: "httpx.AsyncClient", url: str) -> str:
        """Fetch a PWA manifest and stitch its name + short_name + description."""
        try:
            resp = await client.get(url)
        except (httpx.HTTPError, ValueError):
            return ""
        if resp.status_code >= 400:
            return ""
        try:
            data = resp.json()
        except (ValueError, json.JSONDecodeError):
            return ""
        if not isinstance(data, dict):
            return ""
        bits = [
            (data.get("name") or "").strip(),
            (data.get("short_name") or "").strip(),
            (data.get("description") or "").strip(),
        ]
        return " — ".join(b for b in bits if b)

    def _visible_text(html: str) -> str:
        cleaned = re.sub(
            r"<(script|style|svg|noscript|head|template)[^>]*>.*?</\1>",
            " ",
            html,
            flags=re.IGNORECASE | re.DOTALL,
        )
        text = re.sub(r"<[^>]+>", " ", cleaned)
        return re.sub(r"\s+", " ", text).strip()

    # Build the candidate URL list (apex + www variant)
    candidates = [target]
    try:
        from urllib.parse import urlparse, urlunparse
        parsed = urlparse(target)
        if parsed.netloc and not parsed.netloc.startswith("www."):
            candidates.append(urlunparse(parsed._replace(netloc=f"www.{parsed.netloc}")))
    except Exception:
        pass

    async with httpx.AsyncClient(
        timeout=10.0, follow_redirects=True, headers=browser_headers
    ) as client:
        for cand in candidates:
            html = await _get(client, cand)
            if html is None:
                continue

            # Follow ONE meta-refresh / JS redirect if visible text is thin
            visible = _visible_text(html)
            if len(visible) < 200:
                hop = _next_hop(html, cand)
                if hop and hop != cand:
                    hopped = await _get(client, hop)
                    if hopped:
                        html = hopped
                        visible = _visible_text(html)

            if len(visible) >= 200:
                return visible[:max_chars]

            # SPA shells (PWA `.app` sites etc) usually have no title/desc but
            # DO ship a manifest.json — fetch it for the canonical name+desc.
            manifest_href = _manifest_url(html, cand)
            if manifest_href:
                manifest = await _manifest_text(client, manifest_href)
                if len(manifest) >= 40:
                    return manifest[:max_chars]

            # Last resort: meta-tag stitch (title + description + OG)
            meta = _meta_fallback(html)
            if len(meta) >= 40:
                return meta[:max_chars]

    return ""

SOUL_KEYS = ("mission", "audience", "tone", "values", "north_star", "summary")
EMPTY_SOUL = {k: "" for k in SOUL_KEYS}

class SoulGenError(Exception):
    """Raised when we cannot synthesise a company Soul.

    `code` is a short slug ("no_input", "fetch_failed", "llm_failed",
    "bad_json", "empty_output") so callers can map to a friendly message.
    """
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


async def _generate_company_soul(name: str, website_text: str, manual_hint: str = "") -> dict:
    """Distil a company "Soul" — mission, audience, tone, values, north star, summary.

    Raises ``SoulGenError`` when generation cannot produce a meaningful result,
    so callers can decide whether to keep the previous Soul or surface an error.
    Uses the Emergent LLM key (gpt-4.1-mini) — should be cheap (<1k tokens out).
    """
    if not website_text and not manual_hint:
        raise SoulGenError(
            "no_input",
            "Cannot synthesise a Soul without either a website URL or a description.",
        )
    system = (
        "You distil a company's essence (its 'Soul') from public-facing copy.\n"
        "Output ONLY a single JSON object on one line, no markdown, with these keys:\n"
        '{"mission": "...", "audience": "...", "tone": "...", "values": "...", '
        '"north_star": "...", "summary": "..."}\n'
        "Each value: 1–2 short sentences, plain English, no fluff. If you cannot find "
        "something specific, infer the most plausible answer from the context. Never "
        "leave a field blank."
    )
    payload_parts = [f"Company name: {name}"]
    if manual_hint:
        payload_parts.append(f"Founder hint:\n{manual_hint}")
    if website_text:
        payload_parts.append(f"Website excerpt (homepage):\n{website_text}")
    user_payload = "\n\n".join(payload_parts)
    try:
        chat = LlmChat(
            api_key=EMERGENT_LLM_KEY,
            session_id=f"soul:{uuid.uuid4()}",
            system_message=system,
        ).with_model("openai", "gpt-4.1-mini")
        raw = (await chat.send_message(UserMessage(text=user_payload))) or ""
    except Exception as exc:
        logger.warning("Soul generator LLM error: %s", exc)
        raise SoulGenError("llm_failed", "Soul generator is temporarily unavailable. Try again in a moment.") from exc
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        raise SoulGenError("bad_json", "Soul generator returned an unexpected response.")
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError as exc:
        raise SoulGenError("bad_json", "Soul generator returned malformed JSON.") from exc
    soul = {k: (data.get(k) or "").strip()[:600] for k in SOUL_KEYS}
    if not any(soul.values()):
        raise SoulGenError("empty_output", "Soul generator returned an empty result.")
    return soul

# ── URL hygiene ────────────────────────────────────────────────────────
# Users routinely enter a bare domain like "disciplinedos.com" instead
# of "https://disciplinedos.com" — we should accept both. Also strip
# trailing slashes / whitespace and silently normalise common slip-ups.
def _normalise_user_url(raw: str) -> str:
    """Coerce a user-typed URL into a fully-qualified one.

    Accepts: "example.com", "www.example.com", "example.com/foo",
    "http://example.com", "https://example.com/foo?bar=1".

    Returns "" for empty / clearly-not-a-URL input. Defaults to
    https:// scheme when none is supplied.
    """
    if not raw:
        return ""
    s = raw.strip()
    if not s:
        return ""
    # Drop a "www." prefix only if the user wrote no scheme — not strictly
    # necessary but keeps the persisted URL clean.
    has_scheme = s.lower().startswith(("http://", "https://"))
    if not has_scheme:
        # Bare-domain heuristic: must contain a dot, no spaces, looks like
        # a hostname or hostname/path. If it's clearly not a URL (e.g. a
        # short label "test"), return as-is so downstream validation can
        # reject it cleanly.
        if "." not in s or " " in s:
            return s
        s = "https://" + s
    return s


async def _fetch_company_context(name: str, url: str) -> str:
    """Best-effort context for the Soul generator.

    1. Try the regular httpx scraper (fast, cheap).
    2. If we have a URL but the scrape produced too little text, fall back to
       Perplexity Sonar — it sidesteps Cloudflare/SPA blocks since it queries
       Perplexity's own index.

    Returns "" if we couldn't get anything useful from either source.
    """
    text = await _fetch_url_text(url) if url else ""
    if len(text) >= 200 or not url:
        return text
    if _perplexity_available():
        pplx = await perplexity_company_summary(name, url)
        if pplx:
            logger.info("Soul context: Perplexity fallback used for %s", url)
            return pplx
    return text  # may be a short meta-tag stub or empty — caller handles both


@org_r.post("")
async def create_org(body: OrgCreateBody, u: dict = Depends(get_current_user)):
    if not body.name.strip():
        raise HTTPException(400, "Name required")
    now = datetime.now(timezone.utc)

    # Step 1 — capture the company "Soul" (URL scrape → LLM, with optional manual hint).
    # Org creation is forgiving: if scrape/LLM fails we still create the org with
    # an empty Soul so the user isn't blocked. They can re-synthesise from the
    # org page later.
    website_url = _normalise_user_url(body.website_url or "")
    manual = (body.soul_description or "").strip()
    website_text = await _fetch_company_context(body.name.strip(), website_url)
    try:
        soul = await _generate_company_soul(body.name.strip(), website_text, manual)
    except SoulGenError as exc:
        logger.info("Org create: soul gen skipped (%s): %s", exc.code, exc.message)
        soul = dict(EMPTY_SOUL)

    doc = {
        "name": body.name.strip(),
        "owner_id": u["id"],
        "website_url": website_url,
        "soul_description": manual,  # raw founder hint (kept for later edits)
        "soul": soul,                # structured fields injected into agents
        "created_at": now,
    }
    # Workspace layer: every new Org belongs to its owner's personal
    # workspace. Create / fetch that workspace lazily so a fresh user
    # doesn't need an extra call before creating an org.
    owner_ws = await db.workspaces.find_one(
        {"owner_id": u["id"], "kind": "personal"},
    )
    if not owner_ws:
        ws_doc = {
            "name": f"{u.get('name') or u['email'].split('@', 1)[0]}'s Workspace",
            "owner_id": u["id"],
            "kind": "personal",
            "plan": "free",
            "created_at": now,
        }
        ws_res = await db.workspaces.insert_one(ws_doc)
        ws_id = str(ws_res.inserted_id)
        await db.workspace_members.update_one(
            {"workspace_id": ws_id, "user_id": u["id"]},
            {"$setOnInsert": {
                "workspace_id": ws_id,
                "user_id": u["id"],
                "role": "owner",
                "joined_at": now,
            }},
            upsert=True,
        )
    else:
        ws_id = str(owner_ws["_id"])
    doc["workspace_id"] = ws_id
    res = await db.organizations.insert_one(doc)
    oid = str(res.inserted_id)
    await db.org_members.insert_one({
        "org_id": oid, "user_id": u["id"], "role": "owner", "joined_at": now,
    })

    # Step 2 — auto-create a "Chief of Staff" team seeded with the org's
    # Soul so users can chat immediately without having to set up
    # SoulPrint themselves. The internal flag `is_general=True` is
    # unchanged for backwards compatibility.
    team_doc = {
        "org_id": oid,
        "name": "Chief of Staff",
        "description": "Always-on chat with your Chief of Staff — the anchor agent for the whole company",
        "soulprint": {
            "mission": soul.get("mission", ""),
            "tone": soul.get("tone", ""),
            "audience": soul.get("audience", ""),
            "values": soul.get("values", ""),
            "north_star": soul.get("north_star", ""),
        },
        "created_by": u["id"],
        "created_at": now,
        "is_general": True,
    }
    team_res = await db.teams.insert_one(team_doc)
    team_id = str(team_res.inserted_id)

    # Step 3 — auto-create a "Chief of Staff" group conversation. We seat
    # chief-of-staff as the anchor agent, turn auto_respond ON so the user can
    # just type, and turn smart_expand ON so other specialists get pulled in
    # when the topic shifts.
    anchor = GENERAL_DEFAULT_AGENT if GENERAL_DEFAULT_AGENT in AGENTS else next(iter(AGENTS))
    conv_doc = {
        "team_id": team_id,
        "title": "Chief of Staff",
        "type": "group",
        "agent_ids": [anchor],
        "member_ids": [u["id"]],
        "auto_respond": True,
        "smart_expand": True,
        "created_by": u["id"],
        "created_at": now,
        "is_general": True,
    }
    conv_res = await db.conversations.insert_one(conv_doc)
    conv_id = str(conv_res.inserted_id)

    await db.organizations.update_one(
        {"_id": res.inserted_id},
        {"$set": {"general_team_id": team_id, "general_conversation_id": conv_id}},
    )

    org = await db.organizations.find_one({"_id": res.inserted_id})
    return serialize_org(org)

@org_r.get("")
async def list_my_orgs(u: dict = Depends(get_current_user)):
    memberships = await db.org_members.find({"user_id": u["id"]}).to_list(500)
    out = []
    for m in memberships:
        try:
            org = await db.organizations.find_one({"_id": ObjectId(m["org_id"])})
        except Exception:
            continue
        if org:
            o = serialize_org(org)
            o["role"] = m["role"]
            # Cheap counts so the AppHome org cards can show team /
            # member metadata without N extra round-trips (UX audit #3).
            o["team_count"] = await db.teams.count_documents({"org_id": str(org["_id"])})
            o["member_count"] = await db.org_members.count_documents({"org_id": str(org["_id"])})
            out.append(o)
    return out

@org_r.get("/{org_id}")
async def get_org(org_id: str, u: dict = Depends(get_current_user)):
    ctx = await require_org_member(org_id, u["id"])
    data = serialize_org(ctx["org"])
    data["role"] = ctx["member"]["role"]
    # Owners see the Mind Map by default; non-owners only see it if the
    # owner explicitly grants them access via the mind-map-access list.
    data["can_view_mind_map"] = _can_view_mind_map(
        ctx["org"], u["id"], ctx["member"]["role"]
    )
    return data

@org_r.get("/{org_id}/map")
async def get_org_map(
    org_id: str,
    window_days: int = 30,
    u: dict = Depends(get_current_user),
):
    """Org Mind Map payload — owner-gated by default; owner can grant
    additional members via ``mind_map_access``. Returns ``{nodes, edges}``
    that the `/app/org/:id/map` page renders with react-force-graph-2d.
    Window is one of {1, 7, 30, 90, 0(=all)}.
    """
    ctx = await require_org_member(org_id, u["id"])
    if not _can_view_mind_map(ctx["org"], u["id"], ctx["member"]["role"]):
        raise HTTPException(
            403,
            "The Mind Map is an owner tool. Ask the organization owner to "
            "grant you access from the Mind Map page.",
        )
    from org_map import build_org_map
    try:
        return await build_org_map(db, org_id, window_days=window_days)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


# ── Mind Map access management (owner-only) ──────────────────────────
@org_r.get("/{org_id}/mind-map-access")
async def list_mind_map_access(org_id: str, u: dict = Depends(get_current_user)):
    """Owner-only — return the set of org members and their access state
    so the owner can toggle each one in the UI.
    """
    ctx = await require_org_member(org_id, u["id"])
    if ctx["member"]["role"] != "owner":
        raise HTTPException(403, "Only the organization owner can manage Mind Map access.")
    org = ctx["org"]
    allowed = {str(x) for x in (org.get("mind_map_access") or [])}
    members = await db.org_members.find({"org_id": org_id}).to_list(500)
    user_ids = [m["user_id"] for m in members]
    user_oids = []
    for uid in user_ids:
        try:
            user_oids.append(ObjectId(uid))
        except Exception:
            pass
    users = {str(d["_id"]): d async for d in db.users.find({"_id": {"$in": user_oids}})}
    items = []
    for m in members:
        uid = m["user_id"]
        ud = users.get(uid) or {}
        items.append({
            "user_id": uid,
            "name": ud.get("name") or (ud.get("email") or "").split("@")[0] or "Member",
            "email": ud.get("email") or "",
            "role": m.get("role") or "member",
            "is_owner": str(org.get("owner_id")) == str(uid),
            "has_access": (str(org.get("owner_id")) == str(uid)) or (uid in allowed),
        })
    # Owner first, then members alphabetically
    items.sort(key=lambda x: (not x["is_owner"], x["name"].lower()))
    return {"members": items}


@org_r.post("/{org_id}/mind-map-access/{user_id}")
async def grant_mind_map_access(
    org_id: str, user_id: str, u: dict = Depends(get_current_user)
):
    """Owner-only — add a non-owner member to the Mind Map access list."""
    ctx = await require_org_member(org_id, u["id"])
    if ctx["member"]["role"] != "owner":
        raise HTTPException(403, "Only the organization owner can manage Mind Map access.")
    target = await db.org_members.find_one({"org_id": org_id, "user_id": user_id})
    if not target:
        raise HTTPException(404, "That user is not a member of this organization.")
    await db.organizations.update_one(
        {"_id": ObjectId(org_id)},
        {"$addToSet": {"mind_map_access": user_id}},
    )
    return {"ok": True, "user_id": user_id, "has_access": True}


@org_r.delete("/{org_id}/mind-map-access/{user_id}")
async def revoke_mind_map_access(
    org_id: str, user_id: str, u: dict = Depends(get_current_user)
):
    """Owner-only — remove a non-owner member from the Mind Map access list."""
    ctx = await require_org_member(org_id, u["id"])
    if ctx["member"]["role"] != "owner":
        raise HTTPException(403, "Only the organization owner can manage Mind Map access.")
    if str(ctx["org"].get("owner_id")) == str(user_id):
        raise HTTPException(400, "The owner always has access — can't be revoked.")
    await db.organizations.update_one(
        {"_id": ObjectId(org_id)},
        {"$pull": {"mind_map_access": user_id}},
    )
    return {"ok": True, "user_id": user_id, "has_access": False}

@org_r.patch("/{org_id}")
async def update_org(org_id: str, body: OrgUpdateBody, u: dict = Depends(get_current_user)):
    ctx = await require_org_member(org_id, u["id"])
    if ctx["member"]["role"] not in ("owner", "admin"):
        raise HTTPException(403, "Only owners/admins can edit the org")
    # Per-business policy: Soul changes are owner-only. Admins can edit other
    # org settings (name, website) but cannot directly mutate the company Soul.
    # (Members route through soul-suggestions instead — see endpoints below.)
    soul_touched = body.soul_description is not None or body.regenerate_soul
    if soul_touched and ctx["member"]["role"] != "owner":
        raise HTTPException(
            403,
            "Only the org owner can change the company Soul. Submit a suggestion via "
            "POST /api/orgs/{org_id}/soul-suggestions and the owner will review it.",
        )
    update: dict = {}
    if body.name is not None:
        n = body.name.strip()
        if not n:
            raise HTTPException(400, "Name cannot be empty")
        update["name"] = n
    if body.website_url is not None:
        update["website_url"] = _normalise_user_url(body.website_url)
    if body.soul_description is not None:
        update["soul_description"] = body.soul_description.strip()
    if body.archive_idle_days is not None:
        days = int(body.archive_idle_days)
        if days not in (0, 30, 60, 90):
            raise HTTPException(400, "archive_idle_days must be one of 0, 30, 60, 90")
        update["archive_idle_days"] = days

    new_soul: Optional[dict] = None
    if body.regenerate_soul:
        url = update.get("website_url", ctx["org"].get("website_url", "")) or ""
        manual = update.get("soul_description", ctx["org"].get("soul_description", "")) or ""
        name = update.get("name", ctx["org"].get("name", ""))
        if not url and not manual:
            raise HTTPException(
                422,
                "Add a website URL or a description before re-synthesising the Soul.",
            )
        website_text = await _fetch_company_context(name, url) if url else ""
        if url and not website_text and not manual:
            raise HTTPException(
                422,
                f"Couldn't read content from {url}. Add a description, or try a different URL "
                "(some sites block automated requests).",
            )
        try:
            new_soul = await _generate_company_soul(name, website_text, manual)
        except SoulGenError as exc:
            # 422 for caller-fixable issues, 502 for upstream LLM hiccups.
            status = 502 if exc.code in ("llm_failed", "bad_json", "empty_output") else 422
            raise HTTPException(status, exc.message) from exc
        update["soul"] = new_soul

    if update:
        await db.organizations.update_one({"_id": ObjectId(org_id)}, {"$set": update})

        # Cascade fresh Soul into the auto-created "General" team's soulprint
        # snapshot so its agents pick up the new mission/tone/etc immediately.
        if new_soul is not None:
            general_team_id = ctx["org"].get("general_team_id")
            if general_team_id:
                try:
                    await db.teams.update_one(
                        {"_id": ObjectId(general_team_id)},
                        {"$set": {
                            "soulprint": {
                                "mission": new_soul.get("mission", ""),
                                "tone": new_soul.get("tone", ""),
                                "audience": new_soul.get("audience", ""),
                                "values": new_soul.get("values", ""),
                                "north_star": new_soul.get("north_star", ""),
                            }
                        }},
                    )
                except Exception as exc:
                    logger.warning("Cascade soul to General team failed: %s", exc)

    fresh = await db.organizations.find_one({"_id": ObjectId(org_id)})
    out = serialize_org(fresh)
    out["role"] = ctx["member"]["role"]
    return out

@org_r.delete("/{org_id}")
async def delete_org(org_id: str, u: dict = Depends(get_current_user)):
    ctx = await require_org_member(org_id, u["id"])
    if ctx["member"]["role"] != "owner":
        raise HTTPException(403, "Only the org owner can delete this organization")
    # Cascade: gather all teams, conversations, messages, invites, members, assets, schedules
    teams = await db.teams.find({"org_id": org_id}, {"_id": 1}).to_list(1000)
    team_ids = [str(t["_id"]) for t in teams]
    convs = await db.conversations.find({"team_id": {"$in": team_ids}}, {"_id": 1}).to_list(5000) if team_ids else []
    conv_ids = [str(c["_id"]) for c in convs]
    if conv_ids:
        await db.messages.delete_many({"conversation_id": {"$in": conv_ids}})
        await db.conversations.delete_many({"_id": {"$in": [ObjectId(c) for c in conv_ids]}})
    if team_ids:
        await db.teams.delete_many({"_id": {"$in": [ObjectId(t) for t in team_ids]}})
    await db.invites.delete_many({"org_id": org_id})
    await db.org_members.delete_many({"org_id": org_id})
    await db.assets.delete_many({"org_id": org_id})
    # Schedules cascade via the in-process scheduler unregister
    schedules = await db.schedules.find({"org_id": org_id}).to_list(1000)
    for s in schedules:
        _scheduler_unregister(str(s["_id"]))
    await db.schedules.delete_many({"org_id": org_id})
    await db.organizations.delete_one({"_id": ObjectId(org_id)})
    return {"ok": True}


# ── Brand assets ───────────────────────────────────────────────────────────
from brand import (
    BrandUpdateBody as _BrandUpdateBody,
    DEFAULT_LOGO_POSITION as _DEFAULT_LOGO_POS,
    LOGO_POSITION as _LOGO_POSITION_TYPE,  # noqa: F401 (used for typing only)
    save_logo_bytes as _save_logo_bytes,  # noqa: F401 (kept for back-compat)
    prepare_logo_blob as _prepare_logo_blob,
    serialize_brand as _serialize_brand,
    normalise_colors as _normalise_colors,
)

@org_r.post("/{org_id}/brand/logo")
async def upload_brand_logo(
    org_id: str,
    kind: str = "primary",
    file: UploadFile = File(...),
    u: dict = Depends(get_current_user),
):
    """Upload a logo as either ``kind=primary`` or ``kind=alt`` (alt is for
    light-on-dark or dark-on-light variants). Re-encodes to PNG-RGBA so
    transparency carries through to the social-image overlay step.
    """
    if kind not in ("primary", "alt"):
        raise HTTPException(422, "kind must be 'primary' or 'alt'")
    ctx = await require_org_member(org_id, u["id"])
    if ctx["member"]["role"] != "owner":
        raise HTTPException(
            403,
            "Only the organization owner can change brand logos. "
            "Ask the owner — or message them in chat — to update the logo.",
        )
    content = await file.read()
    if len(content) > 8 * 1024 * 1024:
        raise HTTPException(413, "Logo must be 8 MB or smaller")
    # Re-encode the upload as a 1024-cap RGBA PNG, then persist the bytes
    # via ``persist_bytes`` so they're mirrored to GridFS in addition to
    # disk. This is the fix for "brand logo assets randomly disappear" —
    # before, logos were disk-only and the file got wiped on every
    # Kubernetes pod redeploy while the org doc still pointed at the
    # missing filename.
    try:
        blob, fname = _prepare_logo_blob(content)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    await persist_bytes(
        UPLOAD_DIR / fname, blob,
        original_name=file.filename or fname, mime="image/png",
    )
    doc = {
        "filename": fname,
        "url": f"/api/files/{fname}",
        "original_name": (file.filename or "logo")[:200],
    }
    field = "primary_logo" if kind == "primary" else "alt_logo"
    await db.organizations.update_one(
        {"_id": ObjectId(org_id)},
        {"$set": {f"brand_assets.{field}": doc}},
    )
    fresh = await db.organizations.find_one({"_id": ObjectId(org_id)})
    return _serialize_brand(fresh)


@org_r.delete("/{org_id}/brand/logo/{kind}")
async def delete_brand_logo(
    org_id: str, kind: str, u: dict = Depends(get_current_user),
):
    if kind not in ("primary", "alt"):
        raise HTTPException(422, "kind must be 'primary' or 'alt'")
    ctx = await require_org_member(org_id, u["id"])
    if ctx["member"]["role"] != "owner":
        raise HTTPException(
            403,
            "Only the organization owner can remove brand logos.",
        )
    field = "primary_logo" if kind == "primary" else "alt_logo"
    await db.organizations.update_one(
        {"_id": ObjectId(org_id)},
        {"$set": {f"brand_assets.{field}": None}},
    )
    fresh = await db.organizations.find_one({"_id": ObjectId(org_id)})
    return _serialize_brand(fresh)


@org_r.patch("/{org_id}/brand")
async def update_brand_meta(
    org_id: str, body: _BrandUpdateBody, u: dict = Depends(get_current_user),
):
    """Update brand colors and/or tagline. Logos are uploaded via the
    multipart endpoint above so they don't get split across two calls.
    """
    ctx = await require_org_member(org_id, u["id"])
    if ctx["member"]["role"] != "owner":
        raise HTTPException(
            403,
            "Only the organization owner can change brand colors, tagline, "
            "or visual style. Ask the owner to update these for the team.",
        )
    update: dict = {}
    if body.brand_colors is not None:
        update["brand_assets.brand_colors"] = _normalise_colors(body.brand_colors)
    if body.tagline is not None:
        update["brand_assets.tagline"] = body.tagline.strip()[:240]
    if body.visual_style is not None:
        update["brand_assets.visual_style"] = body.visual_style.strip()[:600]
    if update:
        await db.organizations.update_one({"_id": ObjectId(org_id)}, {"$set": update})
    fresh = await db.organizations.find_one({"_id": ObjectId(org_id)})
    return _serialize_brand(fresh)

@org_r.get("/{org_id}/members")
async def list_org_members(org_id: str, u: dict = Depends(get_current_user)):
    await require_org_member(org_id, u["id"])
    members = await db.org_members.find({"org_id": org_id}).to_list(500)
    out = []
    for m in members:
        try:
            usr = await db.users.find_one({"_id": ObjectId(m["user_id"])})
        except Exception:
            continue
        if usr:
            out.append({
                "user_id": str(usr["_id"]),
                "name": usr["name"],
                "email": usr["email"],
                "role": m["role"],
            })
    return out

@org_r.post("/{org_id}/invites")
async def create_invite(org_id: str, body: InviteCreateBody, request: Request, u: dict = Depends(get_current_user)):
    ctx = await require_org_member(org_id, u["id"])
    if ctx["member"]["role"] not in ("owner", "admin"):
        raise HTTPException(403, "Only owners/admins can invite")
    code = secrets.token_urlsafe(8)
    doc = {
        "org_id": org_id, "code": code, "role": body.role,
        "created_by": u["id"], "created_at": datetime.now(timezone.utc),
        "expires_at": datetime.now(timezone.utc) + timedelta(days=14),
        "used_by": None,
    }
    await db.invites.insert_one(doc)
    # Compute the post-insert seat status so the response can carry a
    # soft-enforcement warning when the new invite pushes the org over
    # its plan cap. We intentionally do NOT block here — soft
    # enforcement only, per product call (Feb 2026). The UI surfaces
    # this string as a banner + the inviter still gets the link.
    try:
        from seat_policy import compute_seat_status, warning_for_status
        seat_status = await compute_seat_status(db, org_id)
        seat_warning = warning_for_status(seat_status)
    except Exception as exc:
        logger.warning("seat-status check failed: %s", exc)
        seat_status, seat_warning = None, None
    # Optional: email the invite link via Resend when the inviter gave
    # us an address. Best-effort — persistence already happened, caller
    # still gets the code to paste if Resend is unreachable.
    if body.email:
        try:
            from email_service import send_resend_email
            from workspaces import _public_origin
            frontend = _public_origin(request)
            invite_url = f"{frontend}/invite/{code}"
            org_doc = await db.organizations.find_one(
                {"_id": ObjectId(org_id)}, {"name": 1},
            )
            org_name = (org_doc or {}).get("name") or "an organization"
            inviter_name = u.get("name") or (u.get("email") or "").split("@", 1)[0]
            subject = f"{inviter_name} invited you to {org_name}"
            text = (
                f"Hi,\n\n"
                f"{inviter_name} has invited you to join {org_name} on "
                f"Foundry as a {body.role}.\n\n"
                f"Accept your invite:\n{invite_url}\n\n"
                f"This link expires in 14 days.\n"
            )
            html = (
                f"<div style='font-family:-apple-system,Segoe UI,Roboto,"
                f"sans-serif;max-width:520px;margin:0 auto;padding:24px;"
                f"color:#222'>"
                f"<p style='font-size:13px;color:#888;text-transform:"
                f"uppercase;letter-spacing:0.1em;margin:0 0 8px'>"
                f"Organization invitation</p>"
                f"<h2 style='font-size:22px;margin:0 0 12px;line-height:1.3'>"
                f"{inviter_name} has invited you to join "
                f"<b>{org_name}</b></h2>"
                f"<p style='font-size:14px;line-height:1.6;margin:0 0 16px'>"
                f"You'll join as a <b>{body.role}</b>.</p>"
                f"<div style='margin:24px 0'>"
                f"<a href='{invite_url}' "
                f"style='display:inline-block;background:#f97316;color:#fff;"
                f"padding:12px 22px;border-radius:999px;text-decoration:none;"
                f"font-weight:600;font-size:14px'>Accept invitation</a></div>"
                f"<p style='font-size:12px;color:#888;margin:16px 0 0'>"
                f"Or copy this link: "
                f"<a href='{invite_url}' style='color:#f97316'>"
                f"{invite_url}</a></p>"
                f"<p style='font-size:12px;color:#888;margin:8px 0 0'>"
                f"This link expires in 14 days.</p></div>"
            )
            asyncio.create_task(send_resend_email(
                to=body.email, subject=subject, html=html, text=text,
                reply_to=u.get("email") or None,
            ))
        except Exception as exc:
            logger.warning("Org invite email prep failed: %s", exc)
    return {
        "code": code, "role": body.role,
        "expires_at": doc["expires_at"].isoformat(),
        "email_sent": bool(body.email),
        "seat_status": seat_status,
        "seat_warning": seat_warning,
    }

@org_r.get("/{org_id}/invites")
async def list_invites(org_id: str, u: dict = Depends(get_current_user)):
    ctx = await require_org_member(org_id, u["id"])
    if ctx["member"]["role"] not in ("owner", "admin"):
        raise HTTPException(403, "Only owners/admins can view invites")
    invites = await db.invites.find({"org_id": org_id, "used_by": None}).to_list(200)
    return [{
        "code": i["code"], "role": i["role"],
        "expires_at": i["expires_at"].isoformat() if isinstance(i["expires_at"], datetime) else i["expires_at"],
    } for i in invites]


@org_r.get("/{org_id}/seat-status")
async def get_seat_status(org_id: str, u: dict = Depends(get_current_user)):
    """Return the org's current seat allowance + usage + warning copy.

    Used by the invite UI to render an in-product banner that warns
    the admin when they're inviting beyond their plan's allowance.
    Soft enforcement only — the invite endpoint still creates the
    invite. See seat_policy.py for the full policy doc.
    """
    ctx = await require_org_member(org_id, u["id"])
    is_admin = ctx["member"]["role"] in ("owner", "admin")
    from seat_policy import compute_seat_status, warning_for_status
    status = await compute_seat_status(db, org_id)
    return {
        **status,
        "warning": warning_for_status(status) if is_admin else None,
        "can_invite": is_admin,
    }


@org_r.get("/{org_id}/spend")
async def get_org_spend(
    org_id: str,
    period: str = "current_month",
    u: dict = Depends(get_current_user),
):
    """Per-org spend rollup for the billing dashboard (Phase A).

    Returns aggregated LLM cost + activity counts for the current or
    previous month, plus a per-member breakdown so an owner can see
    *exactly* who is driving spend inside their workspace.

    Powers the seat-policy → billing conversation: an owner sees that
    Ben used $18 and Alice used $11, and decides whether to add seats
    or upgrade the plan.

    Auth: owners/admins only — regular members don't see whole-org
    totals (would expose their teammates' usage to each other).
    """
    ctx = await require_org_member(org_id, u["id"])
    if ctx["member"]["role"] not in ("owner", "admin"):
        raise HTTPException(403, "Only owners/admins can view org spend.")

    # Resolve the month window. Default to the current calendar month
    # so the dashboard updates as usage accrues; `previous_month` lets
    # the owner compare against last month's invoice-ready total.
    now = datetime.now(timezone.utc)
    if period == "previous_month":
        if now.month == 1:
            start = datetime(now.year - 1, 12, 1, tzinfo=timezone.utc)
        else:
            start = datetime(now.year, now.month - 1, 1, tzinfo=timezone.utc)
        # End = first day of the calendar month we're in.
        end = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    else:
        period = "current_month"
        start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
        end = now

    match = {
        "org_id": org_id,
        "created_at": {"$gte": start, "$lt": end},
    }
    # Aggregate org-wide totals + per-user breakdown in two passes.
    pipeline_org = [
        {"$match": match},
        {"$group": {
            "_id": None,
            "cost_usd": {"$sum": "$cost_usd"},
            "calls": {"$sum": 1},
            "prompt_tokens": {"$sum": "$prompt_tokens"},
            "completion_tokens": {"$sum": "$completion_tokens"},
        }},
    ]
    pipeline_users = [
        {"$match": match},
        {"$group": {
            "_id": "$user_id",
            "cost_usd": {"$sum": "$cost_usd"},
            "calls": {"$sum": 1},
            "last_active": {"$max": "$created_at"},
        }},
        {"$sort": {"cost_usd": -1}},
    ]
    pipeline_by_kind = [
        {"$match": match},
        {"$group": {
            "_id": {"$ifNull": ["$kind_label", "other"]},
            "cost_usd": {"$sum": "$cost_usd"},
            "calls": {"$sum": 1},
        }},
        {"$sort": {"cost_usd": -1}},
    ]

    org_doc, by_user_rows, by_kind_rows = await asyncio.gather(
        db.llm_calls.aggregate(pipeline_org).to_list(1),
        db.llm_calls.aggregate(pipeline_users).to_list(200),
        db.llm_calls.aggregate(pipeline_by_kind).to_list(50),
    )
    org_totals = (org_doc[0] if org_doc else {
        "cost_usd": 0, "calls": 0,
        "prompt_tokens": 0, "completion_tokens": 0,
    })
    org_totals.pop("_id", None)

    # Hydrate user names so the dashboard displays real people, not
    # opaque user IDs. One round-trip with $in keeps it cheap.
    user_ids = [r["_id"] for r in by_user_rows if r.get("_id")]
    user_oids = []
    for uid in user_ids:
        try:
            user_oids.append(ObjectId(uid))
        except Exception:
            pass
    name_map: Dict[str, Dict[str, str]] = {}
    if user_oids:
        async for u_doc in db.users.find(
            {"_id": {"$in": user_oids}},
            {"_id": 1, "name": 1, "email": 1},
        ):
            name_map[str(u_doc["_id"])] = {
                "name": u_doc.get("name") or "",
                "email": u_doc.get("email") or "",
            }
    by_user = [
        {
            "user_id": r["_id"] or "system",
            "name": name_map.get(r["_id"] or "", {}).get("name") or "Unknown",
            "email": name_map.get(r["_id"] or "", {}).get("email") or "",
            "cost_usd": round(r["cost_usd"], 4),
            "calls": r["calls"],
            "last_active": (
                r["last_active"].isoformat()
                if isinstance(r.get("last_active"), datetime) else None
            ),
        }
        for r in by_user_rows
    ]
    by_kind = [
        {"label": r["_id"], "cost_usd": round(r["cost_usd"], 4), "calls": r["calls"]}
        for r in by_kind_rows
    ]

    # Activity rollups — missions + audits started in the period.
    missions_total = await db.missions.count_documents({
        "org_id": org_id,
        "created_at": {"$gte": start, "$lt": end},
    })
    audits_total = await db.business_audits.count_documents({
        "org_id": org_id,
        "created_at": {"$gte": start, "$lt": end},
    })
    members_total = await db.org_members.count_documents({"org_id": org_id})

    # Seat status piggy-backs on the same payload so the dashboard
    # can render the "X of Y seats used" pill without a second call.
    from seat_policy import compute_seat_status
    seat_status = await compute_seat_status(db, org_id)

    return {
        "period": period,
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "totals": {
            "cost_usd": round(org_totals.get("cost_usd", 0), 4),
            "llm_calls": org_totals.get("calls", 0),
            "prompt_tokens": org_totals.get("prompt_tokens", 0),
            "completion_tokens": org_totals.get("completion_tokens", 0),
            "missions_started": missions_total,
            "audits_started": audits_total,
            "members": members_total,
        },
        "by_user": by_user,
        "by_kind": by_kind,
        "seat_status": seat_status,
    }

invite_r = APIRouter(prefix="/api/invites")

@invite_r.post("/accept")
async def accept_invite(body: InviteAcceptBody, request: Request, u: dict = Depends(get_current_user)):
    invite_accept_limiter(request)  # raises 429 if abused
    inv = await db.invites.find_one({"code": body.code, "used_by": None})
    if not inv:
        raise HTTPException(404, "Invite not found or already used")
    exp = inv.get("expires_at")
    if isinstance(exp, datetime):
        # Mongo returns naive UTC datetimes; normalize before comparing
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp < datetime.now(timezone.utc):
            raise HTTPException(410, "Invite expired")
    existing = await db.org_members.find_one({"org_id": inv["org_id"], "user_id": u["id"]})
    # Grab the General chat IDs so the caller can drop the new member straight
    # into the org's main conversation without an extra round-trip.
    org_doc = await db.organizations.find_one({"_id": ObjectId(inv["org_id"])})
    gt = (org_doc or {}).get("general_team_id", "")
    gc = (org_doc or {}).get("general_conversation_id", "")
    if existing:
        await db.invites.update_one({"_id": inv["_id"]}, {"$set": {"used_by": u["id"]}})
        return {
            "org_id": inv["org_id"], "already_member": True,
            "general_team_id": gt, "general_conversation_id": gc,
        }
    await db.org_members.insert_one({
        "org_id": inv["org_id"], "user_id": u["id"], "role": inv["role"],
        "joined_at": datetime.now(timezone.utc),
    })
    await db.invites.update_one({"_id": inv["_id"]}, {"$set": {"used_by": u["id"]}})
    return {
        "org_id": inv["org_id"], "already_member": False,
        "general_team_id": gt, "general_conversation_id": gc,
    }

# ── Team routes ──────────────────────────────────────────────────────────────
team_r = APIRouter(prefix="/api")

@team_r.post("/orgs/{org_id}/teams")
async def create_team(org_id: str, body: TeamCreateBody, u: dict = Depends(get_current_user)):
    ctx = await require_org_member(org_id, u["id"])
    org = ctx["org"]
    if not body.name.strip():
        raise HTTPException(400, "Team name required")
    visibility = body.visibility if body.visibility in ("open", "closed") else "open"
    now = datetime.now(timezone.utc)
    # Pre-populate `member_user_ids` with ALL current org members for an
    # Open team (so the existing "everyone in the org sees this team"
    # behaviour holds without a join click) and with just the creator
    # for a Closed team. The creator is always an admin.
    if visibility == "open":
        all_members = await db.org_members.find({"org_id": org_id}, {"user_id": 1}).to_list(2000)
        member_user_ids = [str(r["user_id"]) for r in all_members]
        # Always include the creator + org owner just in case the rows
        # got out of sync.
        for extra in (u["id"], str(org.get("owner_id") or "")):
            if extra and extra not in member_user_ids:
                member_user_ids.append(extra)
    else:
        member_user_ids = [u["id"]]
    doc = {
        "org_id": org_id,
        "name": body.name.strip(),
        "description": body.description or "",
        "soulprint": {"mission": "", "tone": "", "audience": "", "values": "", "north_star": ""},
        "visibility": visibility,
        "member_user_ids": member_user_ids,
        "admin_user_ids": [u["id"]],
        "created_by": u["id"],
        "created_at": now,
    }
    res = await db.teams.insert_one(doc)
    team = await db.teams.find_one({"_id": res.inserted_id})
    member_rows = await db.org_members.find({"org_id": org_id}, {"user_id": 1}).to_list(2000)
    org_member_ids = [str(r["user_id"]) for r in member_rows]
    return serialize_team_with_visibility(team, org=org, viewer_id=u["id"], org_member_ids=org_member_ids)

@team_r.get("/orgs/{org_id}/teams")
async def list_teams(org_id: str, u: dict = Depends(get_current_user)):
    ctx = await require_org_member(org_id, u["id"])
    org = ctx["org"]
    teams = await db.teams.find({"org_id": org_id}).sort("created_at", -1).to_list(500)
    member_rows = await db.org_members.find({"org_id": org_id}, {"user_id": 1}).to_list(2000)
    org_member_ids = [str(r["user_id"]) for r in member_rows]
    out = []
    for t in teams:
        # Lazy backfill keeps legacy teams behaving exactly as before.
        t = await backfill_team_membership(db, t, org)
        if not can_user_view_team(team=t, org=org, user_id=u["id"], org_member_ids=org_member_ids):
            continue
        out.append(serialize_team_with_visibility(
            t, org=org, viewer_id=u["id"], org_member_ids=org_member_ids,
        ))
    return out

@team_r.get("/teams/{team_id}")
async def get_team(team_id: str, u: dict = Depends(get_current_user)):
    ctx = await require_team_member(team_id, u["id"], mode="view")
    return serialize_team_with_visibility(
        ctx["team"], org=ctx["org"], viewer_id=u["id"], org_member_ids=ctx["org_member_ids"],
    )

@team_r.put("/teams/{team_id}/soulprint")
async def update_soulprint(team_id: str, body: SoulPrintBody, u: dict = Depends(get_current_user)):
    await require_team_member(team_id, u["id"], mode="message")
    await db.teams.update_one(
        {"_id": ObjectId(team_id)},
        {"$set": {"soulprint": body.model_dump()}},
    )
    team = await db.teams.find_one({"_id": ObjectId(team_id)})
    return serialize_team(team)

@team_r.patch("/teams/{team_id}")
async def update_team(team_id: str, body: TeamUpdateBody, u: dict = Depends(get_current_user)):
    ctx = await require_team_member(team_id, u["id"], mode="admin")
    team, org = ctx["team"], ctx["org"]
    update: dict = {}
    if body.name is not None:
        n = body.name.strip()
        if not n:
            raise HTTPException(400, "Team name cannot be empty")
        update["name"] = n
    if body.description is not None:
        update["description"] = body.description.strip()[:500]
    if body.visibility is not None:
        if body.visibility not in ("open", "closed"):
            raise HTTPException(400, "Visibility must be 'open' or 'closed'")
        if team.get("is_general") and body.visibility == "closed":
            raise HTTPException(400, "The General team is always Open and shared with the org")
        update["visibility"] = body.visibility
    if update:
        await db.teams.update_one({"_id": ObjectId(team_id)}, {"$set": update})
    fresh = await db.teams.find_one({"_id": ObjectId(team_id)})
    return serialize_team_with_visibility(
        fresh, org=org, viewer_id=u["id"], org_member_ids=ctx["org_member_ids"],
    )

# ── Open team self-join ──────────────────────────────────────────────────
@team_r.post("/teams/{team_id}/join")
async def join_team(team_id: str, u: dict = Depends(get_current_user)):
    """Open teams allow self-join. Closed teams require an admin invite."""
    try:
        team = await db.teams.find_one({"_id": ObjectId(team_id)})
    except Exception:
        raise HTTPException(400, "Invalid team id")
    if not team:
        raise HTTPException(404, "Team not found")
    org = await db.organizations.find_one({"_id": ObjectId(team["org_id"])})
    if not org:
        raise HTTPException(404, "Organization not found")
    m = await db.org_members.find_one({"org_id": team["org_id"], "user_id": u["id"]})
    if not m and not _is_org_owner(org, u["id"]):
        raise HTTPException(403, "Not a member of this organization")
    team = await backfill_team_membership(db, team, org)
    if team_visibility(team) == "closed":
        raise HTTPException(403, "This team is invite-only — ask a team admin to add you")
    members = team_member_ids(team)
    if u["id"] in members:
        return {"ok": True, "already_member": True}
    members.append(u["id"])
    await db.teams.update_one({"_id": team["_id"]}, {"$set": {"member_user_ids": members}})
    return {"ok": True, "joined": True}

# ── Admin invites another org member to this team ────────────────────────
class TeamInviteBody(BaseModel):
    user_ids: List[str]

@team_r.post("/teams/{team_id}/invite")
async def invite_to_team(team_id: str, body: TeamInviteBody, u: dict = Depends(get_current_user)):
    ctx = await require_team_member(team_id, u["id"], mode="admin")
    team, org = ctx["team"], ctx["org"]
    member_rows = await db.org_members.find({"org_id": team["org_id"]}, {"user_id": 1}).to_list(2000)
    valid_ids = {str(r["user_id"]) for r in member_rows}
    if org.get("owner_id"):
        valid_ids.add(str(org["owner_id"]))
    members = team_member_ids(team)
    added = []
    for uid in body.user_ids or []:
        uid = str(uid)
        if not uid or uid not in valid_ids or uid in members:
            continue
        members.append(uid)
        added.append(uid)
    if added:
        await db.teams.update_one({"_id": team["_id"]}, {"$set": {"member_user_ids": members}})
    return {"ok": True, "added": added}

# ── Member leaves a team (or admin removes a member) ─────────────────────
@team_r.delete("/teams/{team_id}/members/{user_id}")
async def remove_team_member(team_id: str, user_id: str, u: dict = Depends(get_current_user)):
    """Self-leave: caller removes themselves. Otherwise requires admin."""
    if str(user_id) != str(u["id"]):
        ctx = await require_team_member(team_id, u["id"], mode="admin")
    else:
        ctx = await require_team_member(team_id, u["id"], mode="view")
    team = ctx["team"]
    if team.get("is_general"):
        raise HTTPException(400, "Cannot leave the General team while you remain in the org")
    members = [m for m in team_member_ids(team) if str(m) != str(user_id)]
    admins = [a for a in team_admin_ids(team) if str(a) != str(user_id)]
    update = {"member_user_ids": members, "admin_user_ids": admins or [str(team.get("created_by") or u["id"])]}
    await db.teams.update_one({"_id": team["_id"]}, {"$set": update})
    return {"ok": True}

# ── Resolve / lazily create the caller's private Chief of Staff ──────────
@team_r.get("/orgs/{org_id}/my-cos")
async def my_chief_of_staff(org_id: str, u: dict = Depends(get_current_user)):
    """Returns the conversation_id of the caller's PRIVATE Chief of Staff
    thread for this org. Auto-creates one on first call so existing
    members get migrated lazily."""
    ctx = await require_org_member(org_id, u["id"])
    org = ctx["org"]
    conv = await get_or_create_private_cos(db, org=org, user_id=u["id"], user_name=u.get("name", ""))
    if not conv:
        raise HTTPException(500, "Could not provision a private Chief of Staff thread")
    return {
        "conversation_id": str(conv["_id"]),
        "team_id": conv["team_id"],
        "title": conv.get("title") or "My Chief of Staff",
        "kind": conv.get("kind") or PRIVATE_COS_KIND,
        "owner_user_id": conv.get("owner_user_id") or u["id"],
        "deeplink": f"/app/team/{conv['team_id']}/c/{str(conv['_id'])}",
    }


@team_r.get("/orgs/{org_id}/my-cos/threads")
async def my_chief_of_staff_threads(org_id: str, u: dict = Depends(get_current_user)):
    """List the caller's private CoS threads in this org, newest first.

    The OrgPage UI uses this to render an expandable thread picker on
    the "Your private Chief of Staff" card so users can either resume
    an old thread or start a fresh one (`+ New chat`)."""
    ctx = await require_org_member(org_id, u["id"])
    from team_access import list_private_cos_threads
    threads = await list_private_cos_threads(db, org=ctx["org"], user_id=u["id"])
    return {"threads": threads}


@team_r.post("/orgs/{org_id}/my-cos/new")
async def my_chief_of_staff_new(org_id: str, u: dict = Depends(get_current_user)):
    """Create a brand-new private CoS thread for the caller.

    Used by the `+ New chat` button on the OrgPage and by the
    GeneralChatCard composer when the user explicitly wants to start
    a fresh conversation rather than appending to the most recent one.
    """
    ctx = await require_org_member(org_id, u["id"])
    from team_access import create_new_private_cos
    conv = await create_new_private_cos(
        db, org=ctx["org"], user_id=u["id"], user_name=u.get("name", ""),
    )
    if not conv:
        raise HTTPException(500, "Could not provision a new Chief of Staff thread")
    return {
        "conversation_id": str(conv["_id"]),
        "team_id": conv["team_id"],
        "title": conv.get("title") or "My Chief of Staff",
        "kind": conv.get("kind") or PRIVATE_COS_KIND,
        "owner_user_id": conv.get("owner_user_id") or u["id"],
        "deeplink": f"/app/team/{conv['team_id']}/c/{str(conv['_id'])}",
    }


# ── Telegram inbound bridge ────────────────────────────────────────────────
# Helper used by `telegram_inbound.run_cos_turn` to execute one full
# Chief-of-Staff turn from the Telegram channel: locate the user's
# private CoS conversation for the given org, persist the inbound user
# message, run the agent, persist the reply, and return its text.
#
# Lives here (not in a separate module) because `generate_agent_reply`,
# `AGENTS`, and the conv/team loaders are all defined in this file
# above and importing them out of order would be a circularity hazard.
_TELEGRAM_IMAGE_BLOCK_RE = re.compile(
    r"\[\[IMAGE\s+[^\]]+?\]\]",
    re.DOTALL | re.IGNORECASE,
)


def _cap_image_blocks_for_telegram(reply_text: str, *, max_blocks: int = 1) -> str:
    """Telegram-specific safety net: keep at most ``max_blocks`` [[IMAGE]]
    blocks in ``reply_text`` and drop the rest. Each block renders into a
    Nano Banana call that costs 5–15s on the wire; emitting 3–5 in one
    turn makes the bot look like it's spamming the user unprompted.

    Idempotent — leaves reply_text untouched when it already has
    ``<= max_blocks`` blocks. Replaces dropped blocks with a single
    trailing nudge so the user knows variations are available on demand.
    """
    if not reply_text:
        return reply_text or ""
    matches = list(_TELEGRAM_IMAGE_BLOCK_RE.finditer(reply_text))
    if len(matches) <= max_blocks:
        return reply_text
    # Walk the string keeping the first ``max_blocks`` blocks; drop later
    # ones. We preserve text BETWEEN blocks as-is.
    keep = matches[:max_blocks]
    drop = matches[max_blocks:]
    out_parts: list[str] = []
    cursor = 0
    keep_spans = {(m.start(), m.end()) for m in keep}
    for m in matches:
        out_parts.append(reply_text[cursor:m.start()])
        if (m.start(), m.end()) in keep_spans:
            out_parts.append(m.group(0))
        cursor = m.end()
    out_parts.append(reply_text[cursor:])
    cleaned = "".join(out_parts)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).rstrip()
    if drop:
        cleaned += (
            f"\n\n_(Held {len(drop)} additional draft visual{'s' if len(drop) != 1 else ''} "
            "to avoid spamming you — reply 'another variation' for the next one.)_"
        )
    return cleaned


async def telegram_cos_turn(
    *, user_id: str, org_id: str, agent_id: str, message_text: str,
) -> str:
    """Run one CoS turn triggered by a Telegram inbound message.

    Returns the agent's reply text (string). Raises on the kind of
    fatal misconfiguration that the user should be told about
    (missing org membership, broken auto-create). For routine "the
    agent had nothing to say" cases we return a friendly placeholder
    rather than raising.
    """
    user_doc = await db.users.find_one({"_id": ObjectId(user_id)})
    if not user_doc:
        raise RuntimeError("Telegram chat is bound to a user that no longer exists.")
    org = await db.organizations.find_one({"_id": ObjectId(org_id)})
    if not org:
        raise RuntimeError("Org not found")
    # Membership gate — matches the rule everywhere else in the app.
    member = await db.org_members.find_one({"org_id": org_id, "user_id": user_id})
    if not member:
        raise RuntimeError("You're no longer a member of that org.")

    # Resolve the user's private Chief-of-Staff conversation (auto-
    # provisions on first call, identical to /api/orgs/{id}/my-cos).
    conv = await get_or_create_private_cos(
        db, org=org, user_id=user_id, user_name=user_doc.get("name", ""),
    )
    if not conv:
        raise RuntimeError("Couldn't provision your Chief of Staff conversation.")
    team = await db.teams.find_one({"_id": ObjectId(conv["team_id"])})
    if not team:
        raise RuntimeError("CoS team not found")

    # Tag this request's LLM telemetry so spend lands under the right
    # org/conv/user in the dashboards (same pattern as /messages).
    from llm_logging import with_call_context  # noqa: E402
    with_call_context(
        org_id=org_id,
        conversation_id=str(conv["_id"]),
        user_id=user_id,
        kind_label="telegram_cos",
    )

    now = datetime.now(timezone.utc)
    # Persist the inbound message so the web UI shows the Telegram
    # thread interleaved with normal chat — voice transcripts included.
    user_message = {
        "id": str(uuid.uuid4()),
        "conversation_id": str(conv["_id"]),
        "sender_type": "user",
        "sender_id": user_id,
        "sender_name": user_doc.get("name") or "You",
        "agent_id": None,
        "text": message_text,
        "timestamp": now,
        "source": "telegram",
    }
    await db.messages.insert_one({**user_message})
    await db.conversations.update_one(
        {"_id": conv["_id"]},
        {"$set": {"last_message_at": now}},
    )

    # Run the agent. `generate_agent_reply` returns (reply_text, web_sources).
    try:
        reply_text, _sources = await generate_agent_reply(
            conv, team, agent_id, message_text,
            user_doc.get("name") or "You",
            latest_user_id=user_id,
        )
    except Exception as exc:
        logger.exception("Telegram CoS turn LLM call failed")
        return f"⚠️ Agent error: {exc}"

    reply_text = (reply_text or "").strip()
    if not reply_text:
        reply_text = "Done."

    # ── Cap [[IMAGE]] blocks at ONE per Telegram turn ────────────────────
    # On the web UI a user can comfortably scroll through five generated
    # variants. In Telegram the same five photos arrive 5–15 seconds
    # apart (sequential Nano Banana calls), which users perceive as the
    # bot "looping" and "spamming drafts unprompted". Keep only the first
    # IMAGE block; if the agent emitted more, fold them into a single
    # closing nudge so the user can ask for a variation explicitly.
    reply_text = _cap_image_blocks_for_telegram(reply_text, max_blocks=1)

    # Persist the agent reply so the web UI shows it too.
    agent_message_id = str(uuid.uuid4())

    # Process [[ARTIFACT]] + [[IMAGE]] blocks — same pipeline the web
    # chat uses. Without this the Telegram bot saying "I'll generate
    # the image" would never actually produce one (the chat-reply LLM
    # has no direct image-gen tool; it emits `[[IMAGE]]` tags that
    # this step intercepts and renders).
    attachments: List[dict] = []
    try:
        reply_text, attachments = await process_agent_artifacts(
            reply_text,
            conv=conv, agent_id=agent_id,
            message_id=agent_message_id, created_by=user_id,
        )
    except Exception as exc:
        logger.warning("telegram artifact processing failed: %s", exc)

    agent_message = {
        "id": agent_message_id,
        "conversation_id": str(conv["_id"]),
        "sender_type": "agent",
        "sender_id": None,
        "sender_name": (AGENTS.get(agent_id) or {}).get("name") or "Chief of Staff",
        "agent_id": agent_id,
        "text": reply_text,
        "attachments": attachments,
        "timestamp": datetime.now(timezone.utc),
        "source": "telegram",
    }
    await db.messages.insert_one({**agent_message})
    await db.conversations.update_one(
        {"_id": conv["_id"]},
        {"$set": {"last_message_at": datetime.now(timezone.utc)}},
    )
    # Hand the image attachments back to the Telegram inbound handler
    # via a module-level cache keyed by user_id — the handler will pop
    # them right after our return and send sendPhoto for each. We don't
    # return them as a tuple to keep this function's existing contract
    # (returns reply text) so every other Telegram entry-point keeps
    # working without changes.
    image_attachments = [a for a in attachments if (a.get("kind") == "image")]
    if image_attachments:
        _PENDING_TELEGRAM_PHOTOS[user_id] = image_attachments
    return reply_text


# Per-user one-shot bucket for image attachments to send AFTER the text
# reply. Populated by `telegram_cos_turn`, drained by the Telegram
# inbound handler. Module-level because we don't want to thread a new
# return value through every caller — only the Telegram path needs it.
_PENDING_TELEGRAM_PHOTOS: Dict[str, List[dict]] = {}


def pop_pending_telegram_photos(user_id: str) -> List[dict]:
    """Drain queued image attachments for a Telegram user. Returns the
    list (possibly empty) and clears the slot atomically."""
    return _PENDING_TELEGRAM_PHOTOS.pop(user_id, [])


@team_r.delete("/teams/{team_id}")
@team_r.delete("/teams/{team_id}")
async def delete_team(team_id: str, u: dict = Depends(get_current_user)):
    ctx = await require_team_member(team_id, u["id"], mode="admin")
    if ctx["team"].get("is_general"):
        raise HTTPException(400, "The Chief of Staff team cannot be deleted")
    if ctx["team"].get("is_direct"):
        raise HTTPException(400, "The Direct chats team cannot be deleted")
    convs = await db.conversations.find({"team_id": team_id}, {"_id": 1}).to_list(2000)
    conv_ids = [str(c["_id"]) for c in convs]
    if conv_ids:
        await db.messages.delete_many({"conversation_id": {"$in": conv_ids}})
        await db.conversations.delete_many({"_id": {"$in": [ObjectId(c) for c in conv_ids]}})
        # Tear down any scheduled jobs that targeted those conversations / this team
        sched = await db.schedules.find(
            {"$or": [
                {"target_id": {"$in": conv_ids}, "scope": "conversation"},
                {"target_id": team_id, "scope": "team"},
            ]}
        ).to_list(500)
        for s in sched:
            _scheduler_unregister(str(s["_id"]))
        await db.schedules.delete_many({
            "$or": [
                {"target_id": {"$in": conv_ids}, "scope": "conversation"},
                {"target_id": team_id, "scope": "team"},
            ]
        })
    await db.teams.delete_one({"_id": ObjectId(team_id)})
    return {"ok": True}

@team_r.get("/teams/{team_id}/agents")
async def team_agents(
    team_id: str,
    q: Optional[str] = None,
    division: Optional[str] = None,
    limit: int = 500,
    u: dict = Depends(get_current_user),
):
    ctx = await require_team_member(team_id, u["id"])
    team_org_id = str(ctx["team"]["org_id"])
    # Built-in agents are visible to everyone. Custom agents (with
    # `org_scope` set) are only visible inside the org that owns them.
    agents = []
    for a in AGENTS.values():
        if a.get("custom") and a.get("org_scope") != team_org_id:
            continue
        agents.append(public_agent(a))
    if division:
        d = division.strip().lower()
        agents = [a for a in agents if a.get("division_key", "").lower() == d
                  or a.get("division", "").lower() == d]
    if q:
        needle = q.strip().lower()
        if needle:
            agents = [
                a for a in agents
                if needle in a["id"]
                or needle in a.get("name", "").lower()
                or needle in a.get("description", "").lower()
                or needle in a.get("division", "").lower()
            ]
    agents.sort(key=lambda a: (a.get("division", ""), a.get("name", "")))
    return agents[: max(1, min(limit, 1000))]

@team_r.get("/agents/divisions")
async def list_divisions(u: dict = Depends(get_current_user)):
    counts: dict = {}
    labels: dict = {}
    for a in AGENTS.values():
        # Custom agents are org-scoped — don't pollute global division
        # counts across tenants.
        if a.get("custom"):
            continue
        key = a.get("division_key", "specialized")
        labels[key] = a.get("division", key.title())
        counts[key] = counts.get(key, 0) + 1
    out = [
        {"key": k, "label": labels[k], "count": counts[k]}
        for k in sorted(counts.keys(), key=lambda x: labels[x])
    ]
    return out

# ── Conversation + messaging ─────────────────────────────────────────────────
conv_r = APIRouter(prefix="/api")

# ── Conversation reply cancellation ────────────────────────────────────────
# Maps `conv_id` to the wall-clock timestamp at which the user clicked
# "Stop" in the UI. The send-message handler captures its own `started_at`
# at request entry; any cancel timestamp >= that started_at means the
# cancel was requested AFTER (or simultaneously with) the request, so the
# handler bails before persisting / returning agent replies. We use a plain
# in-process dict because the backend is single-process; if we ever scale
# horizontally this should move to Redis.
import time as _time  # noqa: E402  (named to avoid colliding with local `time`)
_cancel_requests: dict = {}

def _should_cancel(conv_id: str, started_at: float) -> bool:
    """True iff a cancel for this conversation was requested at or after
    the handler started. Stale entries are pruned opportunistically by the
    cancel endpoint."""
    ts = _cancel_requests.get(conv_id, 0.0)
    return ts >= started_at

def build_system_prompt(agent_id: str, team: dict, current_user_name: Optional[str] = None,
                        org_assets: Optional[List[dict]] = None,
                        org: Optional[dict] = None) -> str:
    meta = AGENTS[agent_id]
    sp = team.get("soulprint") or {}
    lines = [meta["system"]]

    # Org-wide Soul (mission/audience/tone/values/north_star/summary) takes precedence
    soul = (org or {}).get("soul") or {}
    org_name = (org or {}).get("name") or ""
    soul_parts = []
    if soul.get("mission"): soul_parts.append(f"Mission: {soul['mission']}")
    if soul.get("audience"): soul_parts.append(f"Audience: {soul['audience']}")
    if soul.get("tone"): soul_parts.append(f"Tone of voice: {soul['tone']}")
    if soul.get("values"): soul_parts.append(f"Values: {soul['values']}")
    if soul.get("north_star"): soul_parts.append(f"North star: {soul['north_star']}")
    if soul.get("summary"): soul_parts.append(f"In short: {soul['summary']}")
    if soul_parts:
        lines.append(
            f"\nThe organization you work for ({org_name}) — its SOUL is:\n- "
            + "\n- ".join(soul_parts)
            + "\nEvery answer must respect and embody this Soul."
        )

    sp_parts = []
    if sp.get("mission"): sp_parts.append(f"Mission: {sp['mission']}")
    if sp.get("audience"): sp_parts.append(f"Audience: {sp['audience']}")
    if sp.get("tone"): sp_parts.append(f"Tone of voice: {sp['tone']}")
    if sp.get("values"): sp_parts.append(f"Values: {sp['values']}")
    if sp.get("north_star"): sp_parts.append(f"North star: {sp['north_star']}")
    if sp_parts:
        lines.append(
            "\nThe team you're working with ({}) has this SoulPrint — honor it in every response:\n- ".format(team["name"])
            + "\n- ".join(sp_parts)
        )
    if org_assets:
        block = format_assets_for_prompt(org_assets)
        if block:
            lines.append("\n" + block)
    # Inject the org's social style guide (if synthesized) so any agent that
    # writes customer-facing copy or kicks off an image/video stays on-brand.
    style_guide = ((org or {}).get("style_guide") or {}).get("guide") or {}
    sg_block = _format_style_guide_prompt(style_guide)
    if sg_block:
        lines.append("\n" + sg_block)
    if current_user_name:
        lines.append(f"\nThe person you're talking to is {current_user_name}.")
    user_ctx_block = _user_context_block()
    if user_ctx_block:
        lines.append("\n" + user_ctx_block)
    # Resourcefulness protocol — teaches every agent how to fall back to
    # downloadable artifacts when a native integration is missing.
    lines.append(RESOURCEFULNESS_DIRECTIVE)
    # Anti-procrastination protocol — agents do NOT "send images in the
    # next message". Every reply is its own complete unit. The platform
    # generates images / files / videos AUTOMATICALLY when the user's
    # current message asks for them — the agent never controls timing.
    lines.append(NO_FAKE_PROMISES_DIRECTIVE)
    return "\n".join(lines)


# Why this exists: a real customer chat (May 2026) had the Product
# Manager say "I'll generate the logos in the next message" five times
# in a row, never producing any image — the agent has no scheduling
# power and no way to send a follow-up message later. Every reply is
# stateless. So we tell the agent that explicitly.
NO_FAKE_PROMISES_DIRECTIVE = (
    "\n\nNO FAKE PROMISES — your reply IS the complete deliverable.\n"
    "• You CANNOT send 'in the next message', 'in a moment', 'shortly', "
    "'in just a sec', or schedule any future autonomous action. "
    "Every chat reply you write is the entire turn. Period.\n"
    "• Image generation is INSIDE YOUR CONTROL via the [[IMAGE]] block "
    "(see the artifact directive above). To produce an image, emit "
    "[[IMAGE prompt=\"<full visual description>\" aspect=\"1:1\"]] in "
    "the SAME reply as your prose. The runtime intercepts these blocks, "
    "renders the image via Nano Banana, and attaches it inline. NEVER "
    "tell the user 'I can't create images' or 'the platform will "
    "generate it' — that is FALSE. You can, and you must, when the "
    "request is for a social-media post / ad creative / launch graphic "
    "/ banner / cover / story / Reel / thumbnail / carousel slide.\n"
    "• File / video / music generation: for non-image binaries you "
    "either use the [[ARTIFACT]] block (`.pdf` / `.docx` / `.md` / etc.) "
    "or, inside a Mission, call the corresponding tool (`generate_video`, "
    "`generate_music`). In chat, video/music aren't yet inlineable — "
    "say so honestly and ask the user to start a Mission for those.\n"
    "• If you cannot complete a task in this single reply, SAY SO "
    "EXPLICITLY: state what blocks you (missing input, capability gap, "
    "needs Mission), and ask the user to provide what's missing or to "
    "kick off a Mission. Do NOT pretend you'll get to it later.\n"
    "• Phrases that are FORBIDDEN: 'I'll send', 'I'll generate it next', "
    "'I'll have those ready', 'expect them next', 'in just a moment', "
    "'stand by', 'just wrapping up', 'final touches', 'right away', "
    "'the platform will generate the image', 'I can't create or send "
    "images'. These create a fake sense of progress and frustrate the user.\n"
    "• If a user asks for an image and you didn't attach one in your "
    "previous reply, STOP apologising and STOP punting back to the "
    "platform — emit an [[IMAGE]] block RIGHT NOW in this reply. "
    "That IS how you generate the image.\n"
)

async def load_assets_brief(org_id: str, max_items: int = 30) -> List[dict]:
    """Compact list of assets for prompt injection. Limits text excerpts."""
    cur = db.assets.find({"org_id": org_id}).sort("uploaded_at", -1).limit(max_items)
    out: List[dict] = []
    async for a in cur:
        excerpt = a.get("text_excerpt") or ""
        out.append({
            "original_name": a.get("original_name", ""),
            "kind": a.get("kind", "file"),
            "description": a.get("description", ""),
            "preview": (excerpt[:240] + ("…" if len(excerpt) > 240 else "")) if excerpt else "",
        })
    return out

@conv_r.post("/teams/{team_id}/conversations")
async def create_conversation(team_id: str, body: ConversationCreateBody, u: dict = Depends(get_current_user)):
    ctx = await require_team_member(team_id, u["id"], mode="message")
    for aid in body.agent_ids:
        if not is_agent(aid):
            raise HTTPException(400, f"Unknown agent: {aid}")
    if body.type == "direct":
        if len(body.agent_ids) != 1:
            raise HTTPException(400, "Direct conversations need exactly one agent")
        if body.member_ids:
            raise HTTPException(400, "Direct conversations cannot have extra members")
    else:
        if not body.agent_ids:
            raise HTTPException(400, "Group conversations need at least one agent")

    # validate member ids against org
    member_ids = list({m for m in body.member_ids if m and m != u["id"]})
    if member_ids:
        org_members = await db.org_members.find(
            {"org_id": ctx["team"]["org_id"], "user_id": {"$in": member_ids}}
        ).to_list(500)
        valid_ids = {m["user_id"] for m in org_members}
        bad = [m for m in member_ids if m not in valid_ids]
        if bad:
            raise HTTPException(400, "Some member ids are not in this org")

    now = datetime.now(timezone.utc)
    title = body.title.strip() if body.title else (
        AGENTS[body.agent_ids[0]]["name"] if body.type == "direct"
        else "New group"
    )
    doc = {
        "team_id": team_id,
        "title": title,
        "type": body.type,
        "agent_ids": body.agent_ids,
        "member_ids": [u["id"]] + member_ids,
        "auto_respond": body.auto_respond and body.type == "group",
        "smart_expand": body.smart_expand and body.type == "group",
        "created_by": u["id"],
        "created_at": now,
    }
    res = await db.conversations.insert_one(doc)
    doc["_id"] = res.inserted_id
    return serialize_conv(doc)

@conv_r.get("/teams/{team_id}/conversations")
async def list_conversations(
    team_id: str,
    archived: str = "false",
    u: dict = Depends(get_current_user),
):
    """List conversations in this team that the caller is allowed to see.

    Query param `archived`:
      • `false` (default) — hide archived threads
      • `true` — only archived threads
      • `all` — both, sorted with active first then archived

    Visibility rule (Feb 2026 product call): **team membership = read
    access**. Any member of a team can see every conversation that
    lives in that team — owners and admins can coach, members can
    learn from each other's threads.

    Exception: **private Chief-of-Staff threads** (`kind=private_cos`)
    stay 1:1 with their owner. Org owners retain audit access (covered
    by `load_conv_or_403` when they open the thread) but they don't
    appear in the team list view to avoid drowning the list with
    teammates' private CoS rooms.
    """
    ctx = await require_team_member(team_id, u["id"])
    # Decode the archive filter. Anything other than "true" / "all"
    # means "hide archived" — safer default for everyday browsing.
    archived = (archived or "false").strip().lower()
    if archived == "true":
        archive_q = {"archived_at": {"$ne": None, "$exists": True}}
    elif archived == "all":
        archive_q = {}
    else:
        archive_q = {"$or": [{"archived_at": None}, {"archived_at": {"$exists": False}}]}
    base_q: Dict[str, Any] = {
        "team_id": team_id,
        "$and": [
            {"$or": [
                # Public team conversations — visible to every team member.
                {"kind": {"$ne": PRIVATE_COS_KIND}},
                # The caller's own private CoS thread still surfaces in
                # their list so they can find it.
                {"kind": PRIVATE_COS_KIND, "member_ids": u["id"]},
            ]},
            archive_q,
        ] if archive_q else [
            {"$or": [
                {"kind": {"$ne": PRIVATE_COS_KIND}},
                {"kind": PRIVATE_COS_KIND, "member_ids": u["id"]},
            ]},
        ],
    }
    convs = await db.conversations.find(base_q).sort("created_at", -1).to_list(500)
    # Resolve org-ownership ONCE (the team has a single parent org) so
    # we can stamp `can_delete` on every conversation without N round-trips.
    is_org_owner = False
    protected_conv_id = ""
    try:
        team = ctx.get("team") or await db.teams.find_one({"_id": ObjectId(team_id)})
        if team:
            org = await db.organizations.find_one({"_id": ObjectId(team["org_id"])})
            if org:
                if str(org.get("owner_id")) == str(u["id"]):
                    is_org_owner = True
                protected_conv_id = str(org.get("general_conversation_id") or "")
    except Exception:
        pass
    out: List[Dict[str, object]] = []
    for c in convs:
        ser = serialize_conv(c)
        is_creator = c.get("created_by") == u["id"]
        is_protected = str(c.get("_id")) == protected_conv_id
        ser["can_delete"] = (is_creator or is_org_owner) and not is_protected
        ser["is_org_owner"] = is_org_owner
        ser["is_creator"] = is_creator
        out.append(ser)
    return out

async def load_conv_or_403(conv_id: str, user_id: str) -> dict:
    """Authorize a single-conversation read.

    Mirror of the list-time visibility rule above:
      • Direct member of the conversation → allowed (covers the
        legacy 1:1 / DM case).
      • Member of the team that owns the conversation → allowed for
        non-private-CoS threads (the open-by-default rule).
      • Org owner → allowed for private CoS threads in their org
        (moderation / oversight).
    """
    try:
        c = await db.conversations.find_one({"_id": ObjectId(conv_id)})
    except Exception:
        raise HTTPException(400, "Invalid conversation id")
    if not c:
        raise HTTPException(404, "Conversation not found")
    # Direct member access — covers the vast majority of conversations.
    if user_id in c.get("member_ids", []):
        return c
    # Team-membership access for non-private threads. This is the fix
    # for the Feb 2026 report that owners/admins couldn't see member
    # conversations in a team they manage. We reuse the same `can_user_view_team`
    # gate that drives the team list so visibility is consistent
    # across list / detail / messages endpoints.
    if c.get("kind") != PRIVATE_COS_KIND and c.get("team_id"):
        try:
            team = await db.teams.find_one({"_id": ObjectId(c["team_id"])})
        except Exception:
            team = None
        if team:
            org = await db.organizations.find_one({"_id": ObjectId(team["org_id"])})
            if org:
                # Lazy-backfill so old teams have the membership arrays.
                team = await backfill_team_membership(db, team, org)
                member_rows = await db.org_members.find(
                    {"org_id": team["org_id"]}, {"user_id": 1},
                ).to_list(2000)
                org_member_ids = [str(r["user_id"]) for r in member_rows]
                if can_user_view_team(
                    team=team, org=org,
                    user_id=user_id, org_member_ids=org_member_ids,
                ):
                    return c
    # Private CoS audit-access: the org owner can read any private CoS
    # in their org for moderation / oversight purposes.
    if c.get("kind") == PRIVATE_COS_KIND:
        team = await db.teams.find_one({"_id": ObjectId(c["team_id"])}) if c.get("team_id") else None
        if team:
            org = await db.organizations.find_one({"_id": ObjectId(team["org_id"])})
            if org and _is_org_owner(org, user_id):
                return c
    raise HTTPException(403, "Not a participant")

@conv_r.get("/conversations/{conv_id}")
async def get_conv(conv_id: str, u: dict = Depends(get_current_user)):
    c = await load_conv_or_403(conv_id, u["id"])
    out = serialize_conv(c)
    # Decide whether the caller can delete this thread so the UI can
    # show or hide the Trash button without an extra round-trip. Same
    # rule the DELETE endpoint enforces:
    #   creator OR org-owner — and never on the always-on Chief of Staff
    #   thread (auto-created with the org).
    is_creator = c.get("created_by") == u["id"]
    is_org_owner = False
    is_protected = False
    try:
        team = await db.teams.find_one({"_id": ObjectId(c["team_id"])})
        if team:
            org = await db.organizations.find_one({"_id": ObjectId(team["org_id"])})
            if org:
                if str(org.get("owner_id")) == str(u["id"]):
                    is_org_owner = True
                if str(org.get("general_conversation_id") or "") == str(c["_id"]):
                    is_protected = True
    except Exception:
        pass
    out["can_delete"] = (is_creator or is_org_owner) and not is_protected
    out["is_org_owner"] = is_org_owner
    out["is_creator"] = is_creator
    return out

@conv_r.delete("/conversations/{conv_id}")
async def delete_conv(conv_id: str, u: dict = Depends(get_current_user)):
    """Delete a conversation. Allowed for:
      - The user who created it (any member can delete their own threads).
      - The owner of the org the conversation lives in (can delete any thread).
    Members who didn't create the thread cannot delete it (returns 403)."""
    try:
        c = await db.conversations.find_one({"_id": ObjectId(conv_id)})
    except Exception:
        raise HTTPException(400, "Invalid conversation id")
    if not c:
        raise HTTPException(404, "Conversation not found")

    is_creator = c.get("created_by") == u["id"]

    # Resolve org owner via team → org and protect the always-on Chief of
    # Staff thread (auto-created on org provision; the Org page links to it
    # directly so deletion would orphan the workspace).
    is_org_owner = False
    is_protected = False
    org = None
    try:
        team = await db.teams.find_one({"_id": ObjectId(c["team_id"])})
        if team:
            org = await db.organizations.find_one({"_id": ObjectId(team["org_id"])})
            if org:
                if str(org.get("owner_id")) == str(u["id"]):
                    is_org_owner = True
                if str(org.get("general_conversation_id") or "") == str(c["_id"]):
                    is_protected = True
    except Exception:
        pass

    if is_protected:
        raise HTTPException(
            400,
            "The always-on Chief of Staff thread can't be deleted. "
            "It's auto-created with the org and serves as your default workspace.",
        )

    if not (is_creator or is_org_owner):
        # Member trying to delete someone else's conversation. Surface a
        # friendly message instead of the generic "forbidden" so the UI can
        # show it as a toast.
        if u["id"] in c.get("member_ids", []):
            raise HTTPException(403, "Only the conversation creator or the org owner can delete this thread.")
        raise HTTPException(403, "Not a participant")

    await db.messages.delete_many({"conversation_id": conv_id})
    await db.conversations.delete_one({"_id": ObjectId(conv_id)})
    return {"ok": True}


async def _archive_permission_or_403(conv_id: str, user_id: str) -> tuple[dict, dict | None, bool, bool]:
    """Shared auth helper for archive/unarchive. Mirrors the delete rule:
    creator OR org owner can flip the archive flag. The always-on Chief
    of Staff thread (org.general_conversation_id) is protected.

    Returns (conv, org, is_org_owner, is_protected).
    """
    try:
        c = await db.conversations.find_one({"_id": ObjectId(conv_id)})
    except Exception:
        raise HTTPException(400, "Invalid conversation id")
    if not c:
        raise HTTPException(404, "Conversation not found")
    is_creator = c.get("created_by") == user_id
    is_org_owner = False
    is_protected = False
    org = None
    try:
        team = await db.teams.find_one({"_id": ObjectId(c["team_id"])})
        if team:
            org = await db.organizations.find_one({"_id": ObjectId(team["org_id"])})
            if org:
                if str(org.get("owner_id")) == str(user_id):
                    is_org_owner = True
                if str(org.get("general_conversation_id") or "") == str(c["_id"]):
                    is_protected = True
    except Exception:
        pass
    if is_protected:
        raise HTTPException(
            400,
            "The always-on Chief of Staff thread can't be archived — "
            "it's auto-created with the org and serves as your default workspace.",
        )
    if not (is_creator or is_org_owner):
        if user_id in c.get("member_ids", []):
            raise HTTPException(403, "Only the conversation creator or the org owner can archive this thread.")
        raise HTTPException(403, "Not a participant")
    return c, org, is_org_owner, is_protected


@conv_r.post("/conversations/{conv_id}/archive")
async def archive_conv(conv_id: str, u: dict = Depends(get_current_user)):
    """Archive a conversation. Hidden from default lists but still
    readable, still feeds user/org memory, and easy to restore via
    `/unarchive`. Creator or org owner only — same rule as delete.
    """
    c, _org, _is_owner, _is_protected = await _archive_permission_or_403(conv_id, u["id"])
    if c.get("archived_at"):
        return {"ok": True, "already_archived": True}
    now = datetime.now(timezone.utc)
    await db.conversations.update_one(
        {"_id": ObjectId(conv_id)},
        {"$set": {"archived_at": now, "archived_by": u["id"]}},
    )
    return {"ok": True, "archived_at": now.isoformat()}


@conv_r.post("/conversations/{conv_id}/unarchive")
async def unarchive_conv(conv_id: str, u: dict = Depends(get_current_user)):
    """Restore an archived conversation back into the default lists."""
    c, _org, _is_owner, _is_protected = await _archive_permission_or_403(conv_id, u["id"])
    if not c.get("archived_at"):
        return {"ok": True, "already_active": True}
    await db.conversations.update_one(
        {"_id": ObjectId(conv_id)},
        {"$unset": {"archived_at": "", "archived_by": ""}},
    )
    return {"ok": True}


@conv_r.get("/orgs/{org_id}/archived-conversations")
async def list_archived_in_org(
    org_id: str,
    limit: int = 200,
    u: dict = Depends(get_current_user),
):
    """List every archived conversation in this org the caller can see
    (creator/member or org owner). Used by the Org-level Archive view."""
    ctx = await require_org_member(org_id, u["id"])
    limit = max(1, min(500, limit))
    team_docs = await db.teams.find(
        {"org_id": org_id}, {"_id": 1, "name": 1},
    ).to_list(500)
    team_lookup = {str(t["_id"]): t.get("name", "") for t in team_docs}
    team_ids = list(team_lookup.keys())
    if not team_ids:
        return []
    is_owner = ctx["member"].get("role") == "owner"
    base_q: Dict[str, Any] = {
        "team_id": {"$in": team_ids},
        "archived_at": {"$ne": None, "$exists": True},
    }
    if not is_owner:
        base_q["member_ids"] = u["id"]
    convs = await db.conversations.find(base_q).sort("archived_at", -1).limit(limit).to_list(limit)
    protected_conv_id = str((ctx["org"] or {}).get("general_conversation_id") or "")
    out: List[Dict[str, Any]] = []
    for c in convs:
        ser = serialize_conv(c)
        ser["team_name"] = team_lookup.get(c.get("team_id", ""), "")
        ser["deeplink"] = f"/app/team/{c.get('team_id', '')}/c/{str(c['_id'])}"
        is_creator = c.get("created_by") == u["id"]
        is_protected = str(c["_id"]) == protected_conv_id
        ser["can_delete"] = (is_creator or is_owner) and not is_protected
        ser["can_unarchive"] = (is_creator or is_owner) and not is_protected
        out.append(ser)
    return out




async def _get_or_create_direct_team(org_id: str, user_id: str) -> dict:
    """Lazily create the per-org "Direct chats" team. Idempotent — every fork
    routes here, but the team is created at most once per org. Inherits the
    org's Soul into its soulprint snapshot so direct-chat agents stay on-brand.
    """
    existing = await db.teams.find_one({"org_id": org_id, "is_direct": True})
    if existing:
        return existing
    org = await db.organizations.find_one({"_id": ObjectId(org_id)})
    soul = (org or {}).get("soul") or {}
    now = datetime.now(timezone.utc)
    doc = {
        "org_id": org_id,
        "name": "Direct chats",
        "description": "Private one-on-one threads forked from group conversations.",
        "soulprint": {
            "mission":   soul.get("mission", ""),
            "tone":      soul.get("tone", ""),
            "audience":  soul.get("audience", ""),
            "values":    soul.get("values", ""),
            "north_star": soul.get("north_star", ""),
        },
        "created_by": user_id,
        "created_at": now,
        "is_direct": True,
    }
    res = await db.teams.insert_one(doc)
    doc["_id"] = res.inserted_id
    return doc


@conv_r.post("/conversations/{conv_id}/fork")
async def fork_conversation(
    conv_id: str, body: ForkConvBody, u: dict = Depends(get_current_user),
):
    """Fork a single agent reply into a brand-new direct chat.

    The new conversation lives in the org's auto-managed "Direct chats" team
    and is seeded with a fresh copy of that agent message as its first turn.
    Smart-expand is on so the system can pull in additional specialists when
    the conversation's topic shifts later.
    """
    src = await load_conv_or_403(conv_id, u["id"])
    src_team = await db.teams.find_one({"_id": ObjectId(src["team_id"])})
    if not src_team:
        raise HTTPException(404, "Source team not found")

    msg = await db.messages.find_one({"id": body.message_id, "conversation_id": conv_id})
    if not msg:
        raise HTTPException(404, "Message not found in this conversation")
    if msg.get("sender_type") != "agent":
        raise HTTPException(400, "Only agent replies can be forked into a direct chat")
    agent_id = msg.get("agent_id") or ""
    if agent_id not in AGENTS:
        raise HTTPException(400, "Source agent is no longer available")

    direct_team = await _get_or_create_direct_team(src_team["org_id"], u["id"])
    direct_team_id = str(direct_team["_id"])
    now = datetime.now(timezone.utc)

    new_conv = {
        "team_id": direct_team_id,
        "title": f"Chat with {AGENTS[agent_id]['name']}",
        "type": "direct",
        "agent_ids": [agent_id],
        "member_ids": [u["id"]],
        # Direct chats always auto-respond; smart_expand lets the system pull
        # in more specialists if the topic shifts beyond this agent's lane.
        "auto_respond": True,
        "smart_expand": True,
        "created_by": u["id"],
        "created_at": now,
        "forked_from": {
            "conversation_id": conv_id,
            "message_id": body.message_id,
        },
    }
    res = await db.conversations.insert_one(new_conv)
    new_conv["_id"] = res.inserted_id
    new_conv_id = str(res.inserted_id)

    # Seed the new conv with a fresh copy of the agent's reply. We carry
    # over the substantive fields (text, media, web sources, attachments)
    # but assign a new id and conversation_id so this stands on its own.
    seed = {
        "id": str(uuid.uuid4()),
        "conversation_id": new_conv_id,
        "sender_type": "agent",
        "sender_id": agent_id,
        "sender_name": AGENTS[agent_id]["name"],
        "agent_id": agent_id,
        "text": msg.get("text", ""),
        "timestamp": now,
        "is_seed": True,
    }
    # Preserve any inline media so the user sees the same artefact in the
    # forked thread (image/video/web sources/attachments).
    for k in ("media_type", "media_filename", "media_url", "media_caption",
              "video_status", "video_aspect_ratio", "video_provider_task_id",
              "web_sources", "attachments"):
        if msg.get(k) is not None:
            seed[k] = msg[k]
    await db.messages.insert_one(seed)
    seed.pop("_id", None)

    return {
        "conversation": serialize_conv(new_conv),
        "team": serialize_team(direct_team),
        "seed_message": seed,
    }


@conv_r.get("/conversations/{conv_id}/messages")
async def get_messages(conv_id: str, u: dict = Depends(get_current_user)):
    await load_conv_or_403(conv_id, u["id"])
    msgs = await db.messages.find(
        {"conversation_id": conv_id}, {"_id": 0}
    ).sort("timestamp", 1).to_list(1000)
    # Inline the feedback tally + this user's vote so the frontend renders
    # in a single round-trip. One aggregation pass over message_feedback —
    # cheaper than N lookups. Feedback is opt-in, so most messages will
    # have `up_count: 0, down_count: 0, my_feedback: ""`.
    if msgs:
        ids = [m["id"] for m in msgs if m.get("id")]
        if ids:
            pipe = [
                {"$match": {"message_id": {"$in": ids}}},
                {"$group": {
                    "_id": "$message_id",
                    "up": {"$sum": {"$cond": [{"$eq": ["$value", "up"]}, 1, 0]}},
                    "down": {"$sum": {"$cond": [{"$eq": ["$value", "down"]}, 1, 0]}},
                    "mine": {"$max": {"$cond": [{"$eq": ["$user_id", u["id"]]}, "$value", None]}},
                }},
            ]
            tally_by_id = {}
            async for row in db.message_feedback.aggregate(pipe):
                tally_by_id[row["_id"]] = row
            for m in msgs:
                t = tally_by_id.get(m.get("id"))
                if t:
                    m["up_count"] = t.get("up") or 0
                    m["down_count"] = t.get("down") or 0
                    m["my_feedback"] = t.get("mine") or ""
                else:
                    m["up_count"] = 0
                    m["down_count"] = 0
                    m["my_feedback"] = ""
    return msgs


@conv_r.get("/conversations/{conv_id}/messages/since")
async def get_messages_since(
    conv_id: str,
    cursor: str = "",
    u: dict = Depends(get_current_user),
):
    """Cursor-based incremental fetch for live team-chat polling.

    The frontend polls this every ~3s, passing the ISO timestamp of the
    newest message it has in state. We return ONLY messages strictly
    newer than that cursor, keyed by `timestamp`. Empty cursor returns
    nothing — callers must do the initial load via `/messages` first.

    Auth is enforced via `load_conv_or_403`, same as the full fetch.
    Feedback tallies are inlined for parity with `get_messages` so newly-
    arriving bubbles render with the up/down counts already populated.
    """
    await load_conv_or_403(conv_id, u["id"])
    if not cursor:
        return []
    try:
        # Frontend sends ISO 8601. `fromisoformat` handles "+00:00" and
        # naked timestamps; we normalise to aware UTC for the comparison.
        cur_dt = datetime.fromisoformat(cursor.replace("Z", "+00:00"))
        if cur_dt.tzinfo is None:
            cur_dt = cur_dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        raise HTTPException(400, "Invalid cursor — expected ISO 8601 timestamp")
    msgs = await db.messages.find(
        {
            "conversation_id": conv_id,
            "$or": [
                {"timestamp": {"$gt": cur_dt}},
                # Pending media messages get patched in-place with a
                # `ready_at` timestamp when the background generator
                # finishes. Surface those too so the placeholder bubble
                # swaps to the rendered image/video without a refresh.
                {"ready_at": {"$gt": cur_dt}},
            ],
        },
        {"_id": 0},
    ).sort("timestamp", 1).to_list(500)
    if msgs:
        ids = [m["id"] for m in msgs if m.get("id")]
        if ids:
            pipe = [
                {"$match": {"message_id": {"$in": ids}}},
                {"$group": {
                    "_id": "$message_id",
                    "up": {"$sum": {"$cond": [{"$eq": ["$value", "up"]}, 1, 0]}},
                    "down": {"$sum": {"$cond": [{"$eq": ["$value", "down"]}, 1, 0]}},
                    "mine": {"$max": {"$cond": [{"$eq": ["$user_id", u["id"]]}, "$value", None]}},
                }},
            ]
            tally_by_id = {}
            async for row in db.message_feedback.aggregate(pipe):
                tally_by_id[row["_id"]] = row
            for m in msgs:
                t = tally_by_id.get(m.get("id"))
                m["up_count"] = (t or {}).get("up") or 0
                m["down_count"] = (t or {}).get("down") or 0
                m["my_feedback"] = (t or {}).get("mine") or ""
    return msgs


def detect_mentions(text: str, agent_ids: List[str]) -> List[str]:
    """Return agent_ids that are @mentioned.

    Matches either:
      - the full agent id:                    @engineering-frontend-developer
      - its short slug (id minus division):   @frontend-developer
      - the display name as a slug:           @frontend-developer  (e.g. "Frontend Developer")
    Matching is case-insensitive. Order preserved, no duplicates.
    """
    def slugify(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")

    lower = text.lower()
    hits: List[str] = []
    seen: set = set()
    for aid in agent_ids:
        if aid in seen:
            continue
        a = AGENTS.get(aid) or {}
        tokens = {aid, slugify(a.get("name", ""))}
        div_key = a.get("division_key", "")
        if div_key and aid.startswith(div_key + "-"):
            tokens.add(aid[len(div_key) + 1 :])
        for tok in tokens:
            if not tok:
                continue
            if re.search(rf"@{re.escape(tok)}\b", lower):
                hits.append(aid)
                seen.add(aid)
                break
    return hits

async def build_transcript_tail(conv_id: str, limit: int = 12) -> List[dict]:
    msgs = await db.messages.find(
        {"conversation_id": conv_id}, {"_id": 0}
    ).sort("timestamp", -1).to_list(limit)
    return list(reversed(msgs))

# ── Web-search intent classifier + injection ─────────────────────────────────
WEB_SEARCH_AVAILABLE = bool(os.environ.get("BRAVE_API_KEY")) or _perplexity_available()

_IMAGE_KEYWORDS_RE = re.compile(
    r"\b("
    r"image|images|picture|pic|pics|photo|photos|graphic|graphics|"
    r"poster|flyer|banner|thumbnail|thumb|"
    r"logo|logos|illustration|illustrations|artwork|art|painting|drawing|sketch|"
    r"meme|infographic|"
    r"(instagram|insta|ig|linkedin|facebook|twitter|x|tiktok|pinterest|youtube)\s*(post|story|reel|cover|header|thumbnail|banner|graphic|ad|carousel)?|"
    r"social\s*(media)?\s*(post|graphic|ad|asset)|"
    r"hero\s*(image|shot|graphic)|"
    r"product\s*(shot|image|photo|render)|"
    r"album\s*(art|cover)|"
    r"book\s*cover|"
    r"avatar|profile\s*pic"
    r")\b",
    re.IGNORECASE,
)
_VIDEO_KEYWORDS_RE = re.compile(
    r"\b("
    r"video|videos|clip|clips|reel|reels|"
    r"animation|animations|animate|animated|"
    r"motion|movie|film|trailer|teaser|"
    r"commercial|ad\s*spot|tv\s*spot|"
    r"cinematic|cinemagraph|"
    r"\d+\s*[-]?\s*(second|sec|s)\s*(video|clip|spot|ad)?|"
    r"(youtube|tiktok|instagram|reels|shorts)\s*video|"
    r"b[-]?roll"
    r")\b",
    re.IGNORECASE,
)
_VERB_HINTS_RE = re.compile(
    r"\b(make|create|generate|design|draft|render|produce|whip\s*up|build|"
    r"give\s*me|show\s*me|see|view|look\s*at|i\s*need|i\s*want|"
    r"i'?d\s*like|where\s*(?:is|are|did)|"
    r"can\s*you\s*(make|create|design|generate|render|give|show|share|send))\b",
    re.IGNORECASE,
)

# Hard-veto keywords — when the user explicitly asks for a document-style
# artifact, never shortcut to image generation even if the message also
# contains an ambiguous verb like "create" or "design".
_DOC_INTENT_RE = re.compile(
    r"\b(?:\.pdf|\.docx|\.doc|\.txt|\.md|\.csv|\.tsv|\.json|\.html|\.ics|\.vcf|\.eml|\.sh|\.py|\.js|\.ts|\.sql|\.yaml|\.yml|\.xml|\.rtf|"
    r"pdf|docx|word\s+doc(?:ument)?|markdown|"
    r"brief|report|memo|whitepaper|white\s+paper|plan|proposal|spec(?:ification)?|"
    r"checklist|outline|agenda|meeting\s+notes?|transcript|"
    r"invoice|receipt|contract|sop|"
    r"spreadsheet|csv|tsv|table|"
    r"calendar\s+invite|\.ics\b|vcard|contact\s+card)\b",
    re.IGNORECASE,
)


def _quick_image_intent(text: str) -> Optional[bool]:
    """Cheap deterministic pre-classifier. Returns True/False if confident,
    None if we should ask the LLM. Saves a roundtrip on the obvious cases."""
    if _VIDEO_KEYWORDS_RE.search(text):
        return False  # video request — don't shortcut to image
    # Document requests ALWAYS win — "write a PDF brief" mentions "brief"
    # which is ambiguous, but ".pdf" / "brief" / "report" are the user's
    # explicit deliverable. Never route these through image gen.
    if _DOC_INTENT_RE.search(text):
        return False
    if _IMAGE_KEYWORDS_RE.search(text) and _VERB_HINTS_RE.search(text):
        return True
    return None


# Bulk image count parser. When the user says "create 5 hero images" or
# "design 10 thumbnails", we want to render that many distinct outputs
# (each with a slightly different creative twist) instead of a single
# image. We cap at 8 to bound LLM/budget exposure.
_BULK_COUNT_RE = re.compile(
    r"\b(?:make|create|generate|design|render|draft|give|produce|"
    r"want|need|whip\s+up)(?:\s+(?:me|us|them))?\s+(\d{1,3})\s+"
    r"(?:different\s+|unique\s+|variation[s]?\s+of\s+)?"
    r"(?:image|photo|picture|graphic|illustration|poster|banner|"
    r"thumbnail|hero|cover|asset|design|render|variation)s?\b",
    re.IGNORECASE,
)
_BULK_VARIATIONS_RE = re.compile(
    r"\b(\d{1,3})\s+(?:variation[s]?|version[s]?|option[s]?|alternative[s]?)\b",
    re.IGNORECASE,
)
MAX_BULK_IMAGES = 8


def detect_image_count(text: str) -> int:
    """Return how many images the user is asking for. Defaults to 1.

    Matches patterns like "create 5 thumbnails", "give me 4 hero images",
    "render 3 variations", "design 10 banner options". Cap at MAX_BULK_IMAGES
    so a typo like "create 100 images" doesn't drain the budget.
    """
    if not text:
        return 1
    m = _BULK_COUNT_RE.search(text) or _BULK_VARIATIONS_RE.search(text)
    if not m:
        return 1
    try:
        n = int(m.group(1))
    except (TypeError, ValueError):
        return 1
    return max(1, min(n, MAX_BULK_IMAGES))

def _quick_video_intent(text: str) -> Optional[bool]:
    if _VIDEO_KEYWORDS_RE.search(text) and _VERB_HINTS_RE.search(text):
        return True
    return None

_EDIT_VERBS_RE = re.compile(
    r"\b(add|put|insert|place|attach|stamp|overlay|"
    r"remove|erase|delete|strip|drop|"
    r"change|swap|replace|switch|modify|alter|adjust|tweak|edit|update|"
    r"make\s+(it|this)|turn\s+(it|this)\s+into|"
    r"recolor|re-?color|colorize|tint|"
    r"crop|zoom|rotate|flip|mirror|resize|enlarge|shrink|"
    r"clean\s*up|fix|retouch|enhance|sharpen|blur|brighten|darken|"
    r"give\s+(it|this|him|her)\s+a)\b",
    re.IGNORECASE,
)

# Phrases like "based on this", "version of this", "inspired by", etc.
# When the user attaches an image AND uses one of these, they almost always
# want a NEW image derived from it — not a vision description.
_IMAGE_DERIVE_RE = re.compile(
    r"\b(based\s+on|inspired\s+by|derived\s+from|adapted\s+from|"
    r"version\s+of|variant\s+of|variation\s+of|"
    r"using\s+this|from\s+this(?:\s+image|\s+photo|\s+picture)?|"
    r"like\s+this(?:\s+but|\s+except|\s+with)|"
    r"redesign|reimagine|restyle|recreate|remake|redo)\b",
    re.IGNORECASE,
)

def _quick_image_edit_intent(text: str, has_image: bool) -> bool:
    """Treat as edit intent when the user has just attached an image AND
    one of these is true:

    1. The prompt contains a narrow edit verb (add / remove / change / ...).
    2. The prompt contains an image keyword (banner / poster / Instagram
       post / ...) AND a creation verb (make / create / generate / ...) —
       e.g. "make me a 160x600 banner version of this image".
    3. The prompt contains a derivation phrase (based on / version of /
       inspired by / redesign / ...).

    Returns False (→ vision-describe path) for plain "describe this" or
    "summarize what's in here" prompts where no image keyword + verb pair
    exists.
    """
    if not has_image or not text:
        return False
    if _EDIT_VERBS_RE.search(text):
        return True
    if _IMAGE_KEYWORDS_RE.search(text) and _VERB_HINTS_RE.search(text):
        return True
    if _IMAGE_DERIVE_RE.search(text):
        return True
    return False

async def _generate_bulk_images(
    *, db, conv_id: str, primary_agent_id: str, primary_agent_name: str,
    user_text: str, sys_msg: str, count: int,
) -> int:
    """Generate `count` distinct images for a single user request.

    Each image is persisted as its own agent message so the chat UI shows
    them as a sequence (which is also how the user wants to share / save
    individual ones). We add a tiny variant tag to each prompt so the
    image model produces meaningful diversity instead of N near-duplicates.

    Returns the number of images that successfully rendered. Failures
    don't bubble — we degrade gracefully (e.g. budget runs out after the
    first 3 of 5).
    """
    if count <= 0:
        return 0
    # Variation hints — light, generic, model-agnostic. They nudge
    # different compositions / lighting / colour palettes per image.
    variants = [
        "Variation A — bold + saturated palette",
        "Variation B — minimalist + airy whitespace",
        "Variation C — high-contrast cinematic lighting",
        "Variation D — playful + asymmetric composition",
        "Variation E — flat illustration, vector aesthetic",
        "Variation F — photorealistic, golden-hour lighting",
        "Variation G — moody + monochromatic",
        "Variation H — vibrant gradient + soft glow",
    ]
    rendered = 0
    for i in range(min(count, MAX_BULK_IMAGES)):
        variant_hint = variants[i] if i < len(variants) else f"Variation {i + 1}"
        # Append the variant ONLY when count > 1 — single asks should keep
        # the user's prompt verbatim.
        prompt = (
            f"{user_text}\n\n[Render this as image {i + 1} of {count}. "
            f"{variant_hint}.]"
            if count > 1 else user_text
        )
        try:
            fname, caption = await generate_image_via_nano_banana(prompt, sys_msg)
        except Exception as exc:
            logger.warning("bulk image %d/%d failed: %s", i + 1, count, exc)
            continue
        msg = {
            "id": str(uuid.uuid4()),
            "conversation_id": conv_id,
            "sender_type": "agent",
            "sender_id": primary_agent_id,
            "sender_name": primary_agent_name,
            "agent_id": primary_agent_id,
            "text": (
                (caption or "Here's a draft.").strip()
                + (f"\n\n_({i + 1} of {count})_" if count > 1 else "")
            ),
            "media_type": "image",
            "media_filename": fname,
            "media_url": f"/api/files/{fname}",
            "media_prompt": prompt,
            "media_system": sys_msg,
            "media_model": "nano-banana",
            "timestamp": datetime.now(timezone.utc),
        }
        await db.messages.insert_one({**msg})
        rendered += 1
    return rendered


async def detect_image_intent(message: str) -> bool:
    """Quick LLM classifier: does this message want an image generated?

    Returns True for clear image asks ("design a logo", "create an Instagram
    post", "give me a hero illustration", etc.). Returns False for everything
    else, including text that merely *mentions* images.

    A deterministic keyword pre-check covers the obvious cases ("Instagram
    post" + a verb like "create / make") so we never miss them and never
    burn an LLM roundtrip on them.
    """
    if not message.strip():
        return False
    # Long pasted bodies (transcripts, briefs) blow up LLM classifier
    # latency without affecting the answer. The intent is virtually always
    # determined by the first ~1KB ("summarize this transcript:"). Clip
    # for classification only — the full text still flows to the agent.
    if len(message) > 2000:
        message = message[:1500] + "\n…\n" + message[-300:]
    fast = _quick_image_intent(message)
    if fast is not None:
        return fast
    system = (
        "You are a binary classifier. Decide whether the user's latest message is "
        "asking the assistant to GENERATE a visual image (picture / photo / graphic / "
        "logo / illustration / banner / thumbnail / Instagram or LinkedIn or Twitter "
        "post / story / cover photo). "
        "Lean STRONG NO whenever the user asks for a DOCUMENT / PDF / DOCX / WORD DOC / "
        "Word / brief / report / memo / plan / proposal / spec / whitepaper / "
        "checklist / outline / SOP / invoice / receipt / contract / agenda / "
        "meeting notes / transcript / CSV / spreadsheet / table / calendar invite / "
        ".ics / .vcf / .eml — those are text artifacts, not images. "
        "Also lean NO for: describing an existing image, asking for image-related "
        "advice, mere mentions, or any request that involves motion / animation / "
        "video. "
        "Reply with ONLY one word: yes or no."
    )
    try:
        chat = LlmChat(
            api_key=EMERGENT_LLM_KEY,
            session_id=f"img-intent:{uuid.uuid4()}",
            system_message=system,
        ).with_model("openai", "gpt-4.1-mini")
        raw = (await chat.send_message(UserMessage(text=message))) or ""
    except Exception as exc:
        logger.warning("image-intent error: %s", exc)
        return False
    return raw.strip().lower().startswith("y")

async def detect_video_intent(message: str) -> bool:
    """Binary classifier: does the user want a VIDEO generated?

    Lean YES for explicit video / clip / animation / reel / motion / commercial
    / cinematic / 5-second/10-second video asks. Lean NO for stills.
    """
    if not message.strip():
        return False
    # Clip very long inputs so the classifier doesn't time out on
    # transcript-length bodies — the head + tail captures the intent.
    if len(message) > 2000:
        message = message[:1500] + "\n…\n" + message[-300:]
    fast = _quick_video_intent(message)
    if fast is not None:
        return fast
    system = (
        "You are a binary classifier. Decide whether the user's latest message is "
        "asking the assistant to GENERATE a VIDEO (a moving clip with motion / "
        "audio / shots). Lean YES for: 'create/make/generate a video / clip / reel "
        "/ animation / commercial / cinematic shot / 5-second video / 10-second "
        "video / film / movie / trailer'. Lean NO for: still images, photos, "
        "graphics, posters, illustrations, or anything that does not move. "
        "Reply with ONLY one word: yes or no."
    )
    try:
        chat = LlmChat(
            api_key=EMERGENT_LLM_KEY,
            session_id=f"vid-intent:{uuid.uuid4()}",
            system_message=system,
        ).with_model("openai", "gpt-4.1-mini")
        raw = (await chat.send_message(UserMessage(text=message))) or ""
    except Exception as exc:
        logger.warning("video-intent error: %s", exc)
        return False
    return raw.strip().lower().startswith("y")

async def detect_search_intent(message: str, transcript: str = "") -> dict:
    """Decide whether the agent should pull fresh web data before replying.

    Returns {"search": bool, "query": str, "freshness": Optional[str]}.
    Cheap one-shot LLM call (~150 tokens). Empty/safe defaults on error.
    """
    if not WEB_SEARCH_AVAILABLE or not message.strip():
        return {"search": False, "query": "", "freshness": None}
    system = (
        "Decide if the user's latest message would clearly benefit from a fresh web search. "
        "Lean YES for: news, current events, prices, scores, sports, weather, statistics, "
        "company/product updates, releases, schedules, anyone or anything that may have "
        "changed since training (mentions of 'latest', 'today', 'recent', 'this week', a "
        "specific year, a specific person/company tied to a current event), facts likely "
        "out of a model's memory, or 'who is X' for living people. "
        "Lean NO for: code questions, generic advice, creative writing, opinions, internal "
        "team logic, math, brainstorming, anything timeless. "
        "Respond with ONLY a compact JSON object on one line: "
        "{\"search\": true|false, \"query\": \"<concise search query, omit if false>\", "
        "\"freshness\": \"pd\"|\"pw\"|\"pm\"|\"py\"|null} "
        "where freshness is the time window: pd=24h, pw=7d, pm=30d, py=365d, null=any."
    )
    payload = (
        (f"Recent conversation (most recent last):\n{transcript}\n\n" if transcript else "")
        + f"Latest user message: {message}"
    )
    try:
        chat = LlmChat(
            api_key=EMERGENT_LLM_KEY,
            session_id=f"search-intent:{uuid.uuid4()}",
            system_message=system,
        ).with_model("openai", "gpt-4.1-mini")
        raw = await chat.send_message(UserMessage(text=payload))
    except Exception as exc:
        logger.warning("search-intent classifier error: %s", exc)
        return {"search": False, "query": "", "freshness": None}

    raw = (raw or "").strip()
    # Pull the first JSON object out of the response, tolerating prose around it
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return {"search": False, "query": "", "freshness": None}
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {"search": False, "query": "", "freshness": None}
    out = {
        "search": bool(data.get("search")),
        "query": (data.get("query") or "").strip(),
        "freshness": data.get("freshness") if data.get("freshness") in ("pd", "pw", "pm", "py") else None,
    }
    if not out["query"]:
        out["search"] = False
    return out

async def maybe_fetch_web_context(message: str, transcript: str = "") -> tuple[str, list, dict]:
    """If the message warrants it, search the web and return (formatted_block, sources, intent).

    `sources` is a list of {title, url, snippet, age, source} dicts safe to persist.
    `intent` is the raw classifier output for diagnostics.
    """
    intent = await detect_search_intent(message, transcript)
    if not intent.get("search"):
        return "", [], intent
    results = await brave_web_search(intent["query"], count=5, freshness=intent.get("freshness"))
    # Fall back to Perplexity Sonar when Brave returns nothing (rate-limited,
    # 0 results, or BRAVE_API_KEY absent). Output shape is identical so the
    # downstream prompt formatter doesn't care which source produced them.
    if not results and _perplexity_available():
        results = await perplexity_web_search(
            intent["query"], count=5, freshness=intent.get("freshness")
        )
        if results:
            logger.info("Web search: Perplexity fallback used for %r", intent["query"])
    if not results:
        return "", [], intent
    return format_results_for_prompt(results), results, intent


# ── User-supplied URL fetching (anti-hallucination ground truth) ──────────
# When a user pastes "audit https://linkedin.com/in/foo and https://x.com/bar",
# the chat-side agent has no `browse_url` tool and no way to actually visit
# those pages — so it would hallucinate follower counts / engagement / posts.
# Fix: detect URLs in the user's message, fetch them server-side via
# Playwright, inject the rendered text as a "GROUND TRUTH" block in the
# system prompt, and forbid the agent from inventing data not in that block.
_URL_IN_TEXT_RE = re.compile(
    r"(?:^|[\s\(\[\{<,;])"
    r"(https?://[^\s\(\)\[\]\{\}<>'\"`]+?)"
    r"(?=[\s\(\)\[\]\{\}<>'\"`,;:.!?]*(?:$|[\s]))",
    re.IGNORECASE,
)
# Hard cap: avoid spending too much time on a single user message.
_MAX_URLS_PER_TURN = 4
# Per-URL fetch budget. Set generously because real-world pages on the
# modern web (heavy JS, CDN cold caches, retries on flaky links) can
# legitimately take 25-30s to render. We use 45s here so a slow page
# isn't aborted before Playwright's own 30s nav timeout fires + the
# 8s networkidle wait + the extract steps inside browse_url. If the
# Playwright path still times out, we fall back to a static HTTP fetch
# below — so the agent NEVER comes back empty-handed asking the user
# for a "mirror".
_MAX_FETCH_TIMEOUT = 45  # seconds per URL


def _extract_urls(text: str) -> list[str]:
    """Pull http(s) URLs from a free-form user message. De-dupes while
    preserving order, strips trailing punctuation that's almost certainly
    not part of the URL."""
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in _URL_IN_TEXT_RE.finditer(text):
        u = m.group(1).rstrip(").,;:!?\"'`")
        if u in seen:
            continue
        # Defensive: skip URLs that point to our own backend (avoids hitting
        # ourselves or revealing internal paths to the LLM).
        from urllib.parse import urlparse
        try:
            host = (urlparse(u).hostname or "").lower()
        except Exception:
            continue
        if host.endswith("emergentagent.com") or host == "localhost":
            continue
        seen.add(u)
        out.append(u)
        if len(out) >= _MAX_URLS_PER_TURN:
            break
    return out


async def _fetch_url_for_chat(url: str) -> dict:
    """Single URL fetch with hard timeout. Returns
    {'url', 'title', 'text', 'final_url', 'status', 'error'}.

    Strategy (in order):
      1. Playwright (`browse_url`) — fully rendered, JS-aware. Best for
         SPAs, paywalls, lazy-loaded content. Bounded by `_MAX_FETCH_TIMEOUT`.
      2. On Playwright failure / timeout / empty body, fall back to a
         static HTTP fetch via the existing `_fetch_url_text` helper
         (raw HTML + meta-tag stitch). Even SPA shells return *something*
         this way (title + description + manifest.json).

    The two-tier strategy means agents almost never come back empty.
    Before Feb 2026 a single Playwright timeout aborted the whole fetch
    and the agent told the user "I couldn't access the site, do you
    have a mirror?" — no fallback, no static HTML, nothing.

    `text` is truncated so a single chatty page can't blow the prompt
    budget — agents need a representative excerpt, not the whole DOM.
    """
    from browser_tools import browse_url as _browse
    pw_error = ""
    pw_timed_out = False
    result: dict = {}
    try:
        result = await asyncio.wait_for(
            _browse(url, take_screenshot=False, upload_dir=None, mission_id=None),
            timeout=_MAX_FETCH_TIMEOUT,
        )
    except asyncio.TimeoutError:
        pw_timed_out = True
        pw_error = f"Playwright timed out after {_MAX_FETCH_TIMEOUT}s"
    except Exception as exc:
        pw_error = f"{type(exc).__name__}: {exc}"[:200]

    text = (result.get("text") or "").strip() if result else ""
    title = (result.get("title") or "")[:240] if result else ""
    final_url = (result.get("final_url") or url) if result else url
    status = int((result.get("status") or 0)) if result else 0

    # FALLBACK: Playwright timed out or returned nothing meaningful.
    # The static HTTP path is much faster (~3-5s) and often enough for
    # SEO/audit-style asks because it surfaces title, description,
    # canonical, OG tags, manifest, and the first ~6k chars of body
    # text from server-rendered HTML.
    if (pw_error or len(text) < 80):
        try:
            static_text = await asyncio.wait_for(
                _fetch_url_text(url, max_chars=8000),
                timeout=12.0,
            )
        except Exception:
            static_text = ""
        if static_text:
            text = static_text
            if not title:
                # Pull a title out of the static fetch if Playwright didn't.
                m = re.search(r"<title[^>]*>([^<]+)</title>", static_text, re.I)
                if m:
                    title = m.group(1).strip()[:240]
            # If we recovered useful content via fallback, status 200
            # is a reasonable signal even if Playwright timed out.
            if status == 0:
                status = 200
            # Keep the original Playwright error in the response so the
            # caller can log / inspect, but downgrade severity since the
            # fallback succeeded.
            pw_error = f"(playwright fallback used: {pw_error})" if pw_error else ""

    if len(text) > 6000:
        text = text[:6000] + "\n…[truncated]"

    return {
        "url": url,
        "title": title,
        "text": text,
        "final_url": final_url,
        "status": status,
        "error": pw_error[:200] if (pw_timed_out and not text) else pw_error[:200],
    }


async def fetch_user_supplied_urls(message: str) -> tuple[str, list[dict]]:
    """Fetch every URL the user pasted, format as a prompt block, return
    (formatted_block, fetched_list). Returns ("", []) when no URLs detected
    so callers can branch cleanly.

    Each fetch is independent — failures are reported in the block (so the
    agent can tell the user "linkedin.com returned a login wall") rather
    than swallowed.
    """
    urls = _extract_urls(message)
    if not urls:
        return "", []
    fetched = await asyncio.gather(*[_fetch_url_for_chat(u) for u in urls])
    # Build the prompt block. Use a strong directive so the agent doesn't
    # paper over fetch failures by inventing plausible numbers.
    lines = [
        "## GROUND-TRUTH PAGE CONTENT (user-supplied URLs)",
        "",
        "The user pasted the URL(s) below. The system fetched the rendered "
        "page content and you are seeing the ACTUAL text the page returned. "
        "Base your audit / analysis ONLY on this content. **Do NOT invent "
        "follower counts, engagement metrics, post counts, dates, names, "
        "or any number/string that doesn't appear in the GROUND TRUTH "
        "below.** If a metric isn't in the rendered page (e.g. private "
        "engagement data, paywalled content, login-walled feeds), say "
        "explicitly: \"That metric isn't visible in the page content I "
        "could fetch — to get it I'd need <X> (logged-in cookies, an API "
        "key, etc.).\"",
        "",
    ]
    for f in fetched:
        sep = "─" * 60
        lines.append(sep)
        lines.append(f"URL: {f['url']}")
        if f["final_url"] and f["final_url"] != f["url"]:
            lines.append(f"Final URL (after redirects): {f['final_url']}")
        if f["title"]:
            lines.append(f"Title: {f['title']}")
        if f["status"]:
            lines.append(f"HTTP status: {f['status']}")
        if f["error"]:
            lines.append(f"Fetch note: {f['error']}")
        if f["text"]:
            lines.append("Rendered content (or static HTML fallback):")
            lines.append("```")
            lines.append(f["text"])
            lines.append("```")
        else:
            lines.append("(No readable text recovered — JS-only SPA or login wall.)")
            lines.append(
                "Because no text was extracted, do NOT invent metrics for "
                "this URL. Instead: (1) try a related public surface (e.g. "
                "their LinkedIn / GitHub / Reddit if that's where their "
                "audience is), (2) use what general public knowledge you "
                "have about the site / brand to provide a useful "
                "high-level audit / framework, or (3) ask the user a "
                "specific clarifying question about what they want — never "
                "ask them for a 'mirror' or 'cached copy' as a stock "
                "fallback. The fetch system already tries multiple "
                "strategies under the hood; if it failed, asking for a "
                "mirror won't help."
            )
        lines.append("")
    return "\n".join(lines), list(fetched)

async def generate_agent_reply(conv: dict, team: dict, agent_id: str,
                               latest_user_text: str, latest_user_name: str,
                               *, force_delivery: bool = False,
                               latest_user_id: Optional[str] = None) -> tuple[str, list]:
    """One LLM turn for `agent_id`. Returns (reply_text, web_sources).

    When ``force_delivery=True`` we append a strong "deliver as [[ARTIFACT]]
    NOW" instruction to the prompt — used as a single-shot retry when the
    first turn produced empty-promise prose without an actual deliverable.
    """
    tail = await build_transcript_tail(str(conv["_id"]), limit=12)

    transcript_lines = []
    for m in tail:
        if m["sender_type"] == "user":
            transcript_lines.append(f"{m.get('sender_name','User')}: {m['text']}")
        else:
            a = m.get("agent_id")
            aname = AGENTS[a]["name"] if a in AGENTS else "Agent"
            transcript_lines.append(f"{aname} (agent): {m['text']}")
    transcript = "\n".join(transcript_lines) if transcript_lines else ""

    # Web-search augmentation (parallel to building the system prompt)
    web_block, web_sources, _intent = await maybe_fetch_web_context(latest_user_text, transcript)

    # User-supplied URL ground truth — when the user pastes URLs in their
    # message, fetch them server-side via Playwright and inject the
    # rendered text as a "GROUND TRUTH" block. Without this the agent
    # has no way to actually visit those pages (browse_url is mission-only)
    # and would otherwise hallucinate follower counts / engagement / etc.
    user_url_block, user_url_fetches = await fetch_user_supplied_urls(latest_user_text)

    # GitHub auto-detect — if the user's message mentions "my repos" /
    # "open issues in <repo>" / similar, hit the GitHub API server-side
    # with their connected token and inject the result as ground truth.
    # Read-only intents only (writes still go through Missions for the
    # explicit-consent UX).
    github_block = ""
    try:
        from github_integration import fetch_github_chat_context
        github_block, _gh_results = await fetch_github_chat_context(
            db, message=latest_user_text or "",
            org_id=str(team.get("org_id") or ""),
            user_id=latest_user_id or "",
        )
    except Exception as exc:
        logger.warning("github_chat_context_failed: %s", exc)
        github_block = ""

    # Pull a compact view of the org's asset library so the agent can cite it.
    org_assets_brief: List[dict] = []
    org_doc: Optional[dict] = None
    try:
        org_assets_brief = await load_assets_brief(team["org_id"])
    except Exception as exc:
        logger.warning("asset brief load failed: %s", exc)
    try:
        org_doc = await db.organizations.find_one({"_id": ObjectId(team["org_id"])})
    except Exception as exc:
        logger.warning("org load failed: %s", exc)

    system = build_system_prompt(agent_id, team, current_user_name=latest_user_name,
                                 org_assets=org_assets_brief, org=org_doc)
    # Inject user-scoped memories (Phase 1). Empty string when the user
    # has no extracted/manual memories yet, so the prompt stays clean.
    # STRICT org isolation — only memories tagged with this org's id
    # are returned, so a fact captured in Client A's workspace can't
    # leak into Client B's agent replies.
    if latest_user_id and team.get("org_id"):
        try:
            from memory import build_memory_context_block as _mem_block
            mem_block = await _mem_block(
                db, user_id=latest_user_id, org_id=str(team["org_id"]),
            )
            if mem_block:
                system += "\n" + mem_block
        except Exception as exc:
            logger.info("memory injection skipped: %s", exc)
    if conv["type"] == "group":
        system += (
            "\n\nYou are one of several participants in a GROUP conversation. "
            "Other humans and other AI agents may also respond. Stay in your role — "
            "don't impersonate other agents or humans. Keep replies tight and additive."
        )
    if web_block:
        system += (
            "\n\nFresh web search has been performed for this turn. Use the results below "
            "as authoritative recent context. When you cite a fact pulled from them, end the "
            "sentence with a markdown link `[source](url)` referencing the most relevant URL.\n\n"
            f"{web_block}"
        )
    if user_url_block:
        # Critical: this block goes AFTER web search so it visually sits
        # closest to the user's message in the prompt — the agent will
        # weight it heavier. The block itself contains the strict "do not
        # invent metrics" directive.
        system += "\n\n" + user_url_block
    if github_block:
        # GitHub ground truth lands LAST so it's the most-recent context
        # the LLM sees before generating. The block already contains
        # an anti-hallucination directive ("Do NOT invent issues / repos
        # not in this list"). Citation rules baked in.
        system += github_block
    else:
        # Even when no GitHub intent fires, tell the agent the integration
        # EXISTS for this org if it's connected — so the agent never
        # falls back to "I don't have access to your repos" when the user
        # is clearly trying to use it but phrased the ask unusually.
        try:
            gh_conn = await db.github_connections.find_one(
                {"$or": [
                    {"scope": "org", "org_id": str(team.get("org_id") or "")},
                    {"scope": "user", "user_id": latest_user_id or ""},
                ]},
                {"github_username": 1, "scope": 1},
            )
            if gh_conn:
                system += (
                    f"\n\nGITHUB CAPABILITY (PASSIVE): The user has connected "
                    f"their GitHub account (`{gh_conn.get('github_username') or 'unknown'}`, "
                    f"scope `{gh_conn.get('scope')}`). You DO have read access to "
                    f"their repos via the platform — list repos, read READMEs, "
                    f"check commits, view open issues. If they reference a repo "
                    f"by name (`owner/name` or a github.com URL), ask them to "
                    f"say what kind of review they want (architecture, code "
                    f"quality, security, README polish, etc.) and I'll fetch "
                    f"the data on the next turn. Never tell them you don't "
                    f"have access — you do."
                )
        except Exception:
            pass

    chat = LlmChat(
        api_key=EMERGENT_LLM_KEY,
        session_id=f"{conv['_id']}:{agent_id}",
        system_message=system,
    ).with_model("openai", "gpt-4.1-mini")

    prompt = (
        f"Conversation so far:\n{transcript}\n\nMost recent message is from {latest_user_name}: "
        f"{latest_user_text}\n\nRespond as {AGENTS[agent_id]['name']} ({AGENTS[agent_id]['role']})."
        if transcript else latest_user_text
    )
    if force_delivery:
        # The previous turn made an empty promise ("I'll send the report
        # shortly") without attaching anything. Force the agent to produce
        # the deliverable RIGHT NOW in this turn — no more "I'll get back
        # to you" — and to use the [[ARTIFACT]] mechanism so the user gets
        # a downloadable file.
        prompt = (
            f"{prompt}\n\n"
            "FORCED-DELIVERY MODE: Your previous reply promised to deliver "
            "something later (a report / brief / audit / plan / document) but "
            "never attached it. The user is still waiting. Produce the FULL "
            "deliverable RIGHT NOW in this single reply — no preamble, no "
            "'I'll send shortly', no 'expect within 24 hours'. "
            "Use a [[ARTIFACT name=\"...\" mime=\"...\"]] block with a sensible "
            "filename ending in .pdf (preferred) or .docx — the body is "
            "Markdown and the system will render real binary bytes. Include "
            "rich content: headings, bullets, tables, recommendations. End "
            "with one short sentence of prose introducing the file."
        )
    # Composio tool-calling subturn — when the org has flipped the
    # per-agent tools toggle ON AND the user has at least one connected
    # account, route this turn through OpenAI's native function-calling
    # so the agent can actually do things (send Gmail, post Slack, etc.)
    # instead of just saying "I would send that email for you." Falls
    # back silently to LlmChat when tools aren't applicable.
    try:
        if latest_user_id and team.get("org_id"):
            from composio_agent import is_agent_tools_enabled, run_with_tools
            if await is_agent_tools_enabled(db, str(team["org_id"]), agent_id):
                tool_text, _audit = await run_with_tools(
                    db,
                    org_id=str(team["org_id"]),
                    user_id=latest_user_id,
                    agent_id=agent_id,
                    agent_name=AGENTS[agent_id]["name"],
                    conv_id=str(conv["_id"]),
                    system_prompt=system,
                    user_prompt=prompt,
                )
                if tool_text is not None:
                    tool_text = _inject_social_image_block_if_missing(
                        latest_user_text or "", tool_text or "",
                        team=team, conv=conv,
                    )
                    return tool_text, web_sources
    except Exception as exc:
        # Never let tool failures break the chat — fall through to the
        # regular LlmChat path so the user still gets a reply.
        logger.warning("Composio tool subturn failed (falling back to LlmChat): %s", exc)
    try:
        text = await chat.send_message(UserMessage(text=prompt))
    except Exception as exc:
        logger.exception("LLM error: %s", exc)
        return "I'm having trouble thinking right now — try me again in a moment.", []
    # Safety-net: if the user's message clearly asked for a social-media
    # post and the LLM forgot to emit an [[IMAGE]] block (e.g. asked the
    # user "want me to generate a visual?" instead — a recurring gpt-
    # 4.1-mini failure mode), auto-inject one derived from the reply.
    # This guarantees every social-post request ships with an image
    # without depending on perfect LLM compliance.
    text = _inject_social_image_block_if_missing(
        latest_user_text or "", text or "",
        team=team, conv=conv,
    )
    return text, web_sources


# ── Social-post auto-image injector ─────────────────────────────────────────
# Trigger phrases that signal "this is a social-media post request".
# Matched case-insensitively against the user's message; one hit is enough.
_SOCIAL_POST_TRIGGERS = re.compile(
    r"\b("
    r"instagram\s+post|ig\s+post|ig\s+story|ig\s+reel|ig\s+carousel|"
    r"insta\s+post|reels?\s+post|"
    r"facebook\s+post|fb\s+post|facebook\s+cover|"
    r"linkedin\s+post|linkedin\s+banner|linkedin\s+header|"
    r"tiktok\s+(?:post|cover|thumbnail)|"
    r"x\s+post|twitter\s+post|x\s+header|twitter\s+header|"
    r"youtube\s+thumbnail|yt\s+thumbnail|youtube\s+banner|"
    r"pinterest\s+pin|reddit\s+post\s+art|"
    r"threads?\s+post|bluesky\s+post|mastodon\s+post|"
    r"social\s+(?:media\s+)?post|social\s+graphic|"
    r"ad\s+creative|launch\s+graphic|launch\s+banner|"
    r"promo\s+image|campaign\s+creative|"
    r"draft\s+(?:an?\s+)?(?:post|graphic|creative|banner)\s+for|"
    r"draft\s+(?:a\s+)?(?:caption|copy)\s+(?:and|with|plus)\s+(?:image|visual|graphic)"
    r")\b",
    re.IGNORECASE,
)

# Opt-outs — when the user explicitly says they don't want an image, we
# never inject one. Matched against the user's CURRENT message only.
_SOCIAL_POST_OPTOUT = re.compile(
    r"\b("
    r"text\s+only|caption\s+only|just\s+the\s+caption|just\s+the\s+copy|"
    r"no\s+image|no\s+visual|skip\s+the\s+(?:image|visual|graphic)|"
    r"draft\s+(?:only|just)\s+the\s+(?:caption|copy|text)|"
    r"i('?ll|\s+will)\s+(?:provide|attach|supply)\s+(?:the\s+)?(?:image|photo|visual)|"
    r"i\s+have\s+(?:my\s+own|the)\s+(?:image|photo|visual)"
    r")\b",
    re.IGNORECASE,
)


def _aspect_from_user_text(text: str) -> str:
    """Pick a sensible default aspect ratio based on what the user said.

    Order matters — LinkedIn/Twitter/Facebook win over generic 'banner'
    so "LinkedIn launch banner" routes to 1.91:1, not 16:9.
    """
    t = (text or "").lower()
    if any(p in t for p in ("story", "reel", "tiktok", "9:16", "vertical")):
        return "9:16"
    if any(p in t for p in ("linkedin", "twitter post", "x post", "1.91:1",
                              "fb cover", "facebook cover", "facebook post")):
        return "1.91:1"
    if any(p in t for p in ("youtube thumbnail", "yt thumbnail", "16:9",
                              "youtube banner", "yt banner", "banner")):
        return "16:9"
    if "4:5" in t or "portrait" in t:
        return "4:5"
    # Default: square IG post.
    return "1:1"


def _derive_image_prompt_from_reply(reply: str, *, team: dict, user_text: str) -> str:
    """Build a sensible image prompt from the agent's caption + team
    SoulPrint when the LLM forgot to attach one. Strips markdown
    hashtags / emojis, keeps the first ~600 chars of substantive copy
    so the image actually matches the message."""
    body = re.sub(r"\[\[ARTIFACT[^\]]*\]\]", "", reply, flags=re.I)
    body = re.sub(r"\[\[/ARTIFACT\]\]", "", body, flags=re.I)
    body = re.sub(r"#[\w]+", "", body)            # drop hashtags
    body = re.sub(r"\*+", "", body)                # drop ** **
    body = re.sub(r"\s{2,}", " ", body).strip()
    body = body[:600]
    sp = (team or {}).get("soulprint") or {}
    palette_hint = ""
    tone = (sp.get("tone") or "").strip()
    audience = (sp.get("audience") or "").strip()
    brand = (team or {}).get("name") or ""
    parts = [
        "A polished modern social-media post image",
        f"for {brand}." if brand else ".",
        "Editorial-grade visual quality, on-brand, no AI giveaways.",
    ]
    if tone:
        parts.append(f"Tone: {tone}.")
    if audience:
        parts.append(f"Audience: {audience}.")
    if body:
        parts.append(f"The post copy reads: \"{body}\".")
    parts.append(
        "Compose a single hero image — strong focal subject, generous "
        "negative space, room for any short headline text overlay. "
        "Render any text crisply if included. Avoid stock-photo cliches."
    )
    return " ".join(parts)


def _inject_social_image_block_if_missing(
    user_text: str, reply_text: str, *, team: Optional[dict] = None,
    conv: Optional[dict] = None,
) -> str:
    """Auto-append an [[IMAGE]] block when the user clearly asked for a
    social-media post but the LLM forgot to attach one. Idempotent —
    if the reply already contains an [[IMAGE]] tag we leave it alone.

    Why this is needed: gpt-4.1-mini has a stubborn habit of asking
    permission ("Want me to generate a matching image?") despite the
    explicit "AUTO-IMAGE FOR SOCIAL POSTS" directive in the system
    prompt. This post-process guarantees the image ships with the
    caption every time.
    """
    if not user_text or not reply_text:
        return reply_text or ""
    if "[[IMAGE" in (reply_text or "").upper():
        return reply_text
    if not _SOCIAL_POST_TRIGGERS.search(user_text):
        return reply_text
    if _SOCIAL_POST_OPTOUT.search(user_text):
        return reply_text
    # Build the prompt + aspect from context.
    image_prompt = _derive_image_prompt_from_reply(
        reply_text, team=team or {}, user_text=user_text,
    )
    aspect = _aspect_from_user_text(user_text)
    # Sanitise the prompt for inclusion inside a `prompt="..."` attribute
    # (escape quotes; collapse newlines so the regex parser doesn't trip).
    safe_prompt = image_prompt.replace("\\", "\\\\").replace('"', '\\"')
    safe_prompt = re.sub(r"\s+", " ", safe_prompt).strip()
    injected = (
        f'\n\n[[IMAGE prompt="{safe_prompt}" aspect="{aspect}" '
        f'title="Auto-generated visual"]]'
    )
    return (reply_text.rstrip() + injected)

async def process_agent_artifacts(reply_text: str, *, conv: dict, agent_id: str,
                                  message_id: str, created_by: str) -> tuple[str, list]:
    """Pull [[ARTIFACT]] + [[APP]] + [[IMAGE]] blocks out of an agent
    reply, persist them as files + Documents records, and return
    (cleaned_text, attachments_list).

    `conv` must include `team_id` so we can resolve the org. If artifact
    extraction fails for any reason we swallow the error and return the
    original reply unchanged — never block a chat reply on a side-effect.
    """
    from app_artifacts import (
        extract_app_bundles, strip_app_blocks, looks_like_html_app,
        save_app_bundle, upgrade_html_to_app,
    )
    from artifacts import extract_image_blocks
    try:
        # ── Stage 1: multi-file [[APP]] bundles ───────────────────────
        # Extract bundles first (they own their own block syntax), then
        # strip the raw JSON from the reply so the chat bubble shows
        # clean prose, not the manifest.
        app_bundles = extract_app_bundles(reply_text or "")
        if app_bundles:
            reply_text = strip_app_blocks(reply_text)

        # ── Stage 2: [[IMAGE]] blocks ─────────────────────────────────
        # Process these BEFORE the text-artifact pass so the cleaned
        # text we hand back to the user never contains an unrendered
        # `[[IMAGE]]` tag (which would leak the internal block syntax
        # if image-gen fails). Each block becomes one image attachment
        # in the returned list — the frontend renders them inline
        # under the message bubble; `telegram_cos_turn` sends them as
        # native photos via Telegram's sendPhoto endpoint.
        image_text, image_blocks = extract_image_blocks(reply_text or "")
        if image_blocks:
            reply_text = image_text

        # ── Stage 3: standard [[ARTIFACT]] blocks ─────────────────────
        cleaned, blocks = extract_artifact_blocks(reply_text or "")
        if not blocks and not app_bundles and not image_blocks:
            return _annotate_empty_promise(reply_text or "", has_artifacts=False), []
        team_id = conv.get("team_id")
        team_doc = await db.teams.find_one({"_id": ObjectId(team_id)}) if team_id else None
        if not team_doc:
            return _annotate_empty_promise(reply_text or "", has_artifacts=False), []
        org_id = team_doc.get("org_id") or ""
        if not org_id:
            return _annotate_empty_promise(reply_text or "", has_artifacts=False), []
        attachments = []
        if blocks:
            attachments = await _save_artifacts(
                db, blocks,
                docs_root=DOCS_DIR,
                org_id=org_id,
                conversation_id=str(conv.get("_id") or ""),
                message_id=message_id,
                agent_id=agent_id,
                agent_name=AGENTS[agent_id]["name"] if agent_id in AGENTS else "Agent",
                created_by=created_by,
            )
            # Promote single-file HTML artifacts to interactive apps so
            # they get a sandboxed preview + share URL + ZIP download
            # treatment in the chat stream.
            for i, att in enumerate(attachments):
                if att.get("mime") == "text/html" or (att.get("original_name") or "").lower().endswith((".html", ".htm")):
                    doc_id = att.get("document_id")
                    block_body = blocks[i].get("content") if i < len(blocks) else ""
                    if doc_id and looks_like_html_app(block_body or ""):
                        token = await upgrade_html_to_app(db, doc_id)
                        if token:
                            att["kind"] = "app"
                            att["app_token"] = token
                            att["share_url"] = f"/api/share/app/{token}"
                            att["download_url"] = f"/api/share/app/{token}/_download"
        # Multi-file bundles persist via their own helper
        for bundle in app_bundles:
            att = await save_app_bundle(
                db,
                bundle=bundle,
                org_id=org_id,
                conversation_id=str(conv.get("_id") or ""),
                message_id=message_id,
                agent_id=agent_id,
                agent_name=AGENTS[agent_id]["name"] if agent_id in AGENTS else "Agent",
                created_by=created_by,
            )
            if att:
                attachments.append(att)

        # ── Stage 4: render & persist [[IMAGE]] blocks in parallel ────
        # We run image generation AFTER text artifacts so the user-facing
        # cleaned_text is finalised before we kick off the slow image
        # calls. Each block is rendered via Nano Banana; errors per-
        # block are isolated (one failed image doesn't drop the others).
        if image_blocks:
            img_attachments = await _generate_image_attachments(
                image_blocks,
                conv=conv, team=team_doc, agent_id=agent_id,
                message_id=message_id, created_by=created_by,
            )
            attachments.extend(img_attachments)

        if not cleaned.strip():
            n = len(attachments)
            cleaned = f"📎 Attached {n} file{'s' if n != 1 else ''} for you."
        cleaned = _annotate_empty_promise(cleaned, has_artifacts=bool(attachments))
        return cleaned, attachments
    except Exception as exc:
        logger.warning("artifact extraction failed: %s", exc)
        return reply_text or "", []


# ── Image-block helper ──────────────────────────────────────────────────────
_ASPECT_HINTS = {
    "1:1": "Render as a perfect SQUARE 1:1, suitable for Instagram feed posts.",
    "9:16": "Render as a TALL 9:16 vertical, suitable for Instagram Stories / Reels / TikTok.",
    "16:9": "Render as a WIDE 16:9 horizontal, suitable for YouTube thumbnails / landing-page heroes.",
    "4:5": "Render as a 4:5 portrait, suitable for Instagram portrait posts.",
    "1.91:1": "Render as a 1.91:1 horizontal banner, suitable for LinkedIn / Twitter / Facebook link previews.",
    "3:2": "Render as a 3:2 horizontal, classic photographic ratio.",
    "2:3": "Render as a 2:3 portrait, classic photographic ratio.",
}


async def _generate_image_attachments(
    image_blocks: List[dict],
    *,
    conv: dict, team: dict, agent_id: str,
    message_id: str, created_by: str,
) -> list:
    """Generate every block in parallel, save each as a Document, and
    return an attachment dict per success. Failures are logged but
    don't break the others.
    """
    org_id = team.get("org_id") or ""
    sp = team.get("soulprint") or {}
    sp_lines = [f"{k.capitalize()}: {sp[k]}"
                for k in ("mission", "tone", "audience", "values") if sp.get(k)]
    agent_name = AGENTS[agent_id]["name"] if agent_id in AGENTS else "Agent"

    # ── Org logo injection ─────────────────────────────────────────────
    # If the org uploaded brand logos (primary and/or alt), pass them to
    # Nano Banana as reference images so every generated visual carries
    # the brand mark. Nano Banana's multi-image input lets it compose
    # the logo onto the scene with correct scale and lighting — see the
    # `generate_image_via_nano_banana` docstring for details.
    logo_paths: List[Path] = []
    org_doc: Optional[dict] = None
    if org_id:
        try:
            org_doc = await db.organizations.find_one(
                {"_id": ObjectId(org_id)},
                {"brand_assets": 1, "name": 1},
            )
        except Exception as exc:
            logger.warning("logo lookup failed for org %s: %s", org_id, exc)
            org_doc = None
        brand = (org_doc or {}).get("brand_assets") or {}
        for kind in ("primary_logo", "alt_logo"):
            asset = brand.get(kind) or {}
            fname = (asset or {}).get("filename") or ""
            if not fname:
                continue
            candidate = UPLOAD_DIR / fname
            if candidate.is_file():
                logo_paths.append(candidate)
                continue
            # Fallback: rehydrate from GridFS (mirror is keyed by filename
            # in `media_files`) if the disk copy got swept on redeploy.
            try:
                blob, _mime, _orig = await recover_bytes_from_gridfs(fname)
            except Exception as exc:
                logger.warning("GridFS logo load failed (%s): %s", fname, exc)
                blob = None
            if blob:
                try:
                    candidate.parent.mkdir(parents=True, exist_ok=True)
                    candidate.write_bytes(blob)
                    logo_paths.append(candidate)
                except Exception as exc:
                    logger.warning("Could not rehydrate logo %s: %s", fname, exc)

    logo_directive = ""
    if logo_paths:
        brand_name = (org_doc or {}).get("name") if org_id else None
        logo_directive = (
            f"\n\nBRAND LOGO — REFERENCE IMAGE{'S' if len(logo_paths) > 1 else ''} "
            f"ATTACHED: the {len(logo_paths)} reference image"
            f"{'s' if len(logo_paths) > 1 else ''} provided IS the brand logo "
            f"{('for ' + brand_name) if brand_name else ''}. Incorporate it into "
            "the generated image tastefully — overlay it at corner/header scale "
            "(do NOT make it the hero subject), preserve its colors and "
            "proportions exactly, do NOT redraw or stylize the logo itself."
        )

    async def _one(block: dict) -> Optional[dict]:
        prompt = (block.get("prompt") or "").strip()
        if not prompt:
            return None
        aspect = (block.get("aspect") or "").strip()
        aspect_hint = _ASPECT_HINTS.get(aspect, "")
        system_msg = (
            f"You are {agent_name}, rendering an image on behalf of the "
            f"team \"{team.get('name') or 'the team'}\". Honor the team's "
            f"voice and aesthetics. {aspect_hint} Render any text crisp "
            f"and legible."
            + (("\n" + "\n".join(sp_lines)) if sp_lines else "")
            + logo_directive
        )
        # Lift the aspect hint into the prompt too — Nano Banana doesn't
        # accept an explicit aspect-ratio param, so the only way to bias
        # it is via prose. Doubling-up between system + prompt is fine.
        if aspect and aspect not in prompt:
            prompt = f"{prompt}\n\nAspect ratio: {aspect}."
        try:
            filename, _caption = await generate_image_via_nano_banana(
                prompt, system_msg,
                input_image_paths=(logo_paths or None),
            )
        except Exception as exc:
            logger.warning("Inline image-block generation failed: %s", exc)
            return None
        img_bytes_path = UPLOAD_DIR / filename
        try:
            img_bytes = img_bytes_path.read_bytes()
        except Exception:
            img_bytes = b""
        # Register as a real Document so the org library + share URL flows
        # work without special-casing. Mirrors the bookkeeping done by
        # the existing `/generate-image` route.
        org_folder = DOCS_DIR / org_id
        org_folder.mkdir(parents=True, exist_ok=True)
        doc_filename = f"{uuid.uuid4().hex}.png"
        try:
            (org_folder / doc_filename).write_bytes(img_bytes)
        except Exception as exc:
            logger.warning("Could not persist generated image to docs: %s", exc)
        # Mirror to GridFS so the file survives container redeploys.
        gridfs_id = None
        try:
            from file_storage import save_blob_to_gridfs as _save_gfs
            gridfs_id = await _save_gfs(db, img_bytes, doc_filename, "image/png")
        except Exception as exc:
            logger.warning("GridFS mirror failed for generated image: %s", exc)
        friendly_title = (block.get("title") or "").strip() or "Generated image"
        doc = {
            "id": str(uuid.uuid4()),
            "org_id": org_id,
            "conversation_id": str(conv.get("_id") or ""),
            "message_id": message_id,
            "agent_id": agent_id,
            "agent_name": agent_name,
            "created_by": created_by,
            "filename": doc_filename,
            "gridfs_id": gridfs_id,
            "original_name": f"{friendly_title.replace(' ', '_')[:60]}.png",
            "title": friendly_title,
            "mime": "image/png",
            "kind": "image",
            "ext": "png",
            "size": len(img_bytes),
            "preview": "",
            "tags": [],
            "media_prompt": block.get("prompt"),
            "created_at": datetime.now(timezone.utc),
        }
        await db.documents.insert_one({**doc})
        return {
            "document_id": doc["id"],
            "filename": filename,
            "original_name": doc["original_name"],
            "title": friendly_title,
            "mime": "image/png",
            "kind": "image",
            "ext": "png",
            "size": len(img_bytes),
            # The chat & Telegram paths both serve images from /api/files
            # for the original (uploads dir) copy. Keep both so callers
            # can pick whichever is more convenient.
            "url": f"/api/files/{filename}",
            "media_url": f"/api/files/{filename}",
            "media_filename": filename,
            "media_prompt": block.get("prompt"),
        }

    results = await asyncio.gather(
        *[_one(b) for b in image_blocks], return_exceptions=True,
    )
    out: list = []
    for r in results:
        if isinstance(r, Exception):
            logger.warning("image-block worker raised: %s", r)
            continue
        if r:
            out.append(r)
    return out

# ── Smart agent router ──────────────────────────────────────────────────────
ROUTER_FALLBACK = "specialized-chief-of-staff"

def _agent_menu(exclude_ids: Optional[List[str]] = None) -> str:
    """Compact menu sent to the routing LLM. ~30 tokens × 169 ≈ 5k tokens."""
    excl = set(exclude_ids or [])
    lines = []
    for a in AGENTS.values():
        if a["id"] in excl:
            continue
        tag = (a.get("tagline") or a.get("description") or "")[:90]
        lines.append(f"- {a['id']} | {a['division']} | {a['name']}: {tag}")
    return "\n".join(lines)

def _soulprint_brief(team: dict) -> str:
    sp = team.get("soulprint") or {}
    parts = []
    for k, label in [("mission", "Mission"), ("audience", "Audience"),
                     ("tone", "Tone"), ("values", "Values"), ("north_star", "North star")]:
        if sp.get(k):
            parts.append(f"{label}: {sp[k]}")
    return "\n".join(parts) if parts else "(no SoulPrint set)"

async def route_best_agent(team: dict, message: str,
                           exclude_ids: Optional[List[str]] = None,
                           transcript: str = "") -> str:
    """Pick the single best agent for `message` given team SoulPrint + recent transcript.

    Falls back to ROUTER_FALLBACK if the LLM returns something unparseable.
    """
    # Long pasted transcripts (10k-100k chars) make the router LLM slow
    # and prone to timeout. The route is determined by the *kind* of
    # request, which is virtually always clear in the first ~1.5KB
    # ("summarize this transcript: …" / "draft a press release for: …").
    # Clip for routing only — the full text still goes to the agent.
    if len(message) > 2000:
        message = message[:1500] + "\n…[truncated for routing]…\n" + message[-300:]
    menu = _agent_menu(exclude_ids)
    sp_brief = _soulprint_brief(team)
    system = (
        "You are an AI router. Given a team's SoulPrint, the recent conversation "
        "(if any), and the latest user message, pick the single best specialised "
        "agent from the catalogue.\n"
        "Reply with ONLY the agent id (e.g. `engineering-frontend-developer`). "
        "No prose, no markdown, no quotes — just the id."
    )
    user_payload_parts = [f"Team SoulPrint:\n{sp_brief}"]
    if transcript:
        user_payload_parts.append(f"Recent conversation:\n{transcript}")
    user_payload_parts.append(f"Latest user message: {message}")
    user_payload_parts.append(f"Catalogue:\n{menu}")
    user_payload = "\n\n".join(user_payload_parts)

    chat = LlmChat(
        api_key=EMERGENT_LLM_KEY,
        session_id=f"router:{team['_id']}:{uuid.uuid4()}",
        system_message=system,
    ).with_model("openai", "gpt-4.1-mini")
    try:
        raw = await chat.send_message(UserMessage(text=user_payload))
    except Exception as exc:
        logger.warning("Router LLM error, falling back: %s", exc)
        return ROUTER_FALLBACK if ROUTER_FALLBACK in AGENTS else next(iter(AGENTS))

    # Pull the first valid id token from the response.
    candidates = re.findall(r"[a-z0-9][a-z0-9\-]{2,80}", (raw or "").lower())
    excl = set(exclude_ids or [])
    for c in candidates:
        if c in AGENTS and c not in excl:
            return c
    fb = ROUTER_FALLBACK if ROUTER_FALLBACK in AGENTS and ROUTER_FALLBACK not in excl else None
    if fb:
        return fb
    for a in AGENTS:
        if a not in excl:
            return a
    return next(iter(AGENTS))

async def should_expand_group(conv: dict, team: dict, message: str) -> Optional[str]:
    """For smart-expand groups: decide if a NEW agent (not already in the room)
    should be brought in for this message. Returns agent_id or None."""
    tail = await build_transcript_tail(str(conv["_id"]), limit=8)
    transcript_lines = []
    for m in tail:
        speaker = m.get("sender_name") or ("Agent" if m["sender_type"] == "agent" else "User")
        transcript_lines.append(f"{speaker}: {m['text']}")
    transcript = "\n".join(transcript_lines)

    current_names = ", ".join(AGENTS[a]["name"] for a in conv.get("agent_ids", []) if a in AGENTS) or "(none)"
    sp_brief = _soulprint_brief(team)
    menu = _agent_menu(exclude_ids=conv.get("agent_ids", []))

    system = (
        "You are an AI router for a GROUP conversation that already has these agents: "
        f"{current_names}.\n"
        "Decide whether the latest message would clearly benefit from bringing in ONE new "
        "specialist who is not already in the room. Be conservative — only expand when the "
        "topic genuinely shifts to a domain none of the current agents cover well.\n"
        "Reply with ONLY one of:\n"
        "  - the agent id of the new specialist to add\n"
        "  - the literal word `none` (lowercase) if no expansion is needed."
    )
    payload = (
        f"Team SoulPrint:\n{sp_brief}\n\n"
        f"Recent conversation:\n{transcript}\n\n"
        f"Latest user message: {message}\n\n"
        f"Available specialists (you may pick one or say none):\n{menu}"
    )
    chat = LlmChat(
        api_key=EMERGENT_LLM_KEY,
        session_id=f"expand:{conv['_id']}:{uuid.uuid4()}",
        system_message=system,
    ).with_model("openai", "gpt-4.1-mini")
    try:
        raw = (await chat.send_message(UserMessage(text=payload))) or ""
    except Exception as exc:
        logger.warning("Expansion router error: %s", exc)
        return None
    lower = raw.strip().lower()
    if "none" in lower.split()[:2]:
        return None
    candidates = re.findall(r"[a-z0-9][a-z0-9\-]{2,80}", lower)
    already = set(conv.get("agent_ids", []))
    for c in candidates:
        if c in AGENTS and c not in already and c != "none":
            return c
    return None

async def _find_last_image_in_conv(conv_id: str) -> Optional[Path]:
    """Locate the on-disk path of the most recent image in this conversation.

    Looks at both agent-generated images (`media_filename` on disk in
    `UPLOAD_DIR`) and user-uploaded images (`attachment_filename` in
    `CONV_UPLOAD_DIR/<conv_id>/`). Returns the first existing file found,
    walking newest-first. Returns None if nothing usable is found.

    After Feb 2026: if the disk copy is missing (pod restart wiped the
    ephemeral filesystem), we REHYDRATE from GridFS before returning —
    so "edit the previous image" keeps working even after a redeploy.
    Without this, edits silently fell back to the review path and the
    agent told the user the edit happened when it actually didn't.
    """
    async def _rehydrate(target: Path, filename: str) -> bool:
        """Try to restore bytes from GridFS. Returns True on success."""
        blob, _mime, _orig = await recover_bytes_from_gridfs(filename)
        if not blob:
            return False
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(blob)
            return target.is_file()
        except Exception as exc:
            logger.warning("GridFS rehydrate to %s failed: %s", target, exc)
            return False

    cur = db.messages.find(
        {
            "conversation_id": conv_id,
            "$or": [
                {"media_type": "image", "media_filename": {"$exists": True}},
                {"attachment_kind": "image", "attachment_filename": {"$exists": True}},
            ],
        },
        sort=[("timestamp", -1)],
        limit=8,
    )
    async for msg in cur:
        if msg.get("media_type") == "image" and msg.get("media_filename"):
            fname = msg["media_filename"]
            p = UPLOAD_DIR / fname
            if p.is_file():
                return p
            # Disk copy gone — try GridFS before giving up.
            if await _rehydrate(p, fname):
                return p
        if msg.get("attachment_kind") == "image" and msg.get("attachment_filename"):
            fname = msg["attachment_filename"]
            p = CONV_UPLOAD_DIR / conv_id / fname
            if p.is_file():
                return p
            if await _rehydrate(p, fname):
                return p
    return None

@conv_r.post("/conversations/{conv_id}/cancel")
async def cancel_conversation_reply(conv_id: str, u: dict = Depends(get_current_user)):
    """Mark this conversation's currently-running reply as cancelled. The
    send-message handler checks the registry between agent turns / before
    persisting expensive image-gen results, so the user gets an immediate
    UI stop AND we avoid persisting orphan replies. Member-gated."""
    await load_conv_or_403(conv_id, u["id"])
    _cancel_requests[conv_id] = _time.time()
    # Opportunistic prune: drop any entries older than 10 minutes so the
    # registry doesn't grow unbounded across long-lived processes.
    cutoff = _time.time() - 600
    stale = [k for k, v in _cancel_requests.items() if v < cutoff]
    for k in stale:
        _cancel_requests.pop(k, None)
    return {"ok": True}

@conv_r.post("/conversations/{conv_id}/messages")
async def send_message(conv_id: str, body: SendMessageBody, u: dict = Depends(get_current_user)):
    if not body.text.strip():
        raise HTTPException(400, "Message cannot be empty")
    conv = await load_conv_or_403(conv_id, u["id"])
    team = await db.teams.find_one({"_id": ObjectId(conv["team_id"])})
    if not team:
        raise HTTPException(404, "Team not found")

    # Capture wall-clock entry time so the user's "Stop" button (which
    # calls /cancel) can mark cancels that arrive AFTER this — anything
    # older is a stale registry entry from a previous request and is
    # ignored. Also clear any stale entry for this conv before we start.
    started_at = _time.time()
    _cancel_requests.pop(conv_id, None)

    # Tag any LLM call this request triggers with org/conv/user metadata
    # so the admin dashboard attributes spend correctly.
    from llm_logging import with_call_context  # noqa: E402
    with_call_context(
        org_id=str(team.get("org_id") or "") or None,
        conversation_id=conv_id,
        user_id=u["id"],
        kind_label="agent_reply",
    )

    now = datetime.now(timezone.utc)
    user_doc = {
        "id": str(uuid.uuid4()),
        "conversation_id": conv_id,
        "sender_type": "user",
        "sender_id": u["id"],
        "sender_name": u["name"],
        "agent_id": None,
        "text": body.text,
        "timestamp": now,
    }
    await db.messages.insert_one({**user_doc})

    # Decide which agents respond
    if conv["type"] == "direct":
        responders = conv["agent_ids"][:1]
    else:
        mentioned = detect_mentions(body.text, conv["agent_ids"])
        if mentioned:
            # Explicit @mentions ALWAYS win — the user told us who to pull in
            responders = mentioned
        elif conv.get("auto_respond"):
            # Auto-respond: pick the SINGLE best specialist for this message
            # rather than spamming a reply from every agent in the room.
            # This prevents the "social-media strategist" from answering a
            # PDF-generation request just because it's in the conversation.
            try:
                best = await route_best_agent(
                    team, body.text,
                    exclude_ids=None,
                    transcript="",
                )
            except Exception as exc:
                logger.warning("auto-respond router error: %s", exc)
                best = None
            if best and best in conv.get("agent_ids", []):
                responders = [best]
            elif best and best in AGENTS:
                # Router picked an agent that's not in this room — only honour
                # it if smart-expand is on; otherwise fall back to the first
                # in-room agent so somebody still answers.
                if conv.get("smart_expand"):
                    responders = [best]  # smart_expand block below adds them
                else:
                    responders = (conv.get("agent_ids") or [])[:1]
            else:
                responders = (conv.get("agent_ids") or [])[:1]
        else:
            responders = []

        # Smart-expand: decide if a brand-new agent should join the room
        if conv.get("smart_expand"):
            try:
                new_agent_id = await should_expand_group(conv, team, body.text)
            except Exception as exc:
                logger.warning("smart_expand error: %s", exc)
                new_agent_id = None
            if new_agent_id and new_agent_id in AGENTS and new_agent_id not in conv.get("agent_ids", []):
                await db.conversations.update_one(
                    {"_id": ObjectId(conv_id)},
                    {"$addToSet": {"agent_ids": new_agent_id}},
                )
                conv["agent_ids"] = list(conv.get("agent_ids", [])) + [new_agent_id]
                if new_agent_id not in responders:
                    responders.append(new_agent_id)

    # ── Private CoS guarantee ──────────────────────────────────────────
    # Private Chief-of-Staff threads MUST always have the CoS agent in
    # the responder set, regardless of router/smart_expand decisions.
    # The whole point of the "private CoS" room is a 1:1 with the chief
    # of staff — other specialists can still be pulled in by smart_expand
    # alongside CoS, but CoS itself must never be silently dropped.
    #
    # Bug fix Feb 2026: ben@archeforge.com saw 3 user messages with zero
    # agent replies because his legacy conv had auto_respond=False and
    # smart_expand's router returned None three times in a row, leaving
    # responders=[]. With CoS forced in, the loop is guaranteed to
    # produce at least one reply. Also self-heals the conv document so
    # the routing branch finds CoS in agent_ids on the next request.
    if conv.get("kind") == "private_cos":
        cos_id = "specialized-chief-of-staff"
        if cos_id in AGENTS and cos_id not in responders:
            responders.insert(0, cos_id)
        try:
            patch = {"auto_respond": True}
            await db.conversations.update_one(
                {"_id": ObjectId(conv_id)},
                {
                    "$addToSet": {"agent_ids": cos_id},
                    "$set": patch,
                },
            )
            if cos_id not in (conv.get("agent_ids") or []):
                conv["agent_ids"] = list(conv.get("agent_ids", [])) + [cos_id]
            conv["auto_respond"] = True
        except Exception as exc:
            logger.warning("private-CoS self-heal failed: %s", exc)

    # ── Video / Image generation auto-routing ─────────────────────────────
    # If the user clearly asked for a video, kick that off (async polling) and
    # skip any text reply. If not, fall through to the image classifier.
    image_msg = None
    video_msg = None
    if responders:
        # ── Inline edit-iteration ─────────────────────────────────────────
        # If the user's latest message is clearly asking for an edit AND we
        # have a recent image in this conversation (agent-generated OR user-
        # uploaded), pipe that image into Nano Banana as the source. This
        # lets users iterate on their last image without re-uploading
        # anything. e.g. "darker letter B, more curved".
        edit_src: Optional[Path] = None
        if _quick_image_edit_intent(body.text or "", True):
            try:
                edit_src = await _find_last_image_in_conv(conv_id)
            except Exception as exc:
                logger.warning("could not look up last image: %s", exc)
                edit_src = None
        if edit_src is not None:
            primary = responders[0]
            sp = team.get("soulprint") or {}
            sp_lines = [f"{k.capitalize()}: {sp[k]}" for k in ("mission", "tone", "audience", "values") if sp.get(k)]
            sys_msg = (
                f"You are {AGENTS[primary]['name']}, editing the previous image in this "
                f"conversation. Apply the user's requested change precisely while "
                f"preserving the rest of the image, its composition, and visual style. "
                f"Render any requested text crisply.\n" + "\n".join(sp_lines)
            )
            try:
                fname, caption = await generate_image_via_nano_banana(
                    f"Source image is attached (the previous image in this chat). "
                    f"Apply this edit: {body.text}",
                    sys_msg,
                    input_image_paths=[edit_src],
                )
                image_msg = {
                    "id": str(uuid.uuid4()),
                    "conversation_id": conv_id,
                    "sender_type": "agent",
                    "sender_id": primary,
                    "sender_name": AGENTS[primary]["name"],
                    "agent_id": primary,
                    "text": (caption or "Here's the edit. Want any tweaks?").strip(),
                    "media_type": "image",
                    "media_filename": fname,
                    "media_url": f"/api/files/{fname}",
                    "intent": "image_edit_reply",
                    "timestamp": datetime.now(timezone.utc),
                }
                await db.messages.insert_one({**image_msg})
                responders = []  # edited image alone is enough
            except Exception as exc:
                logger.warning("inline image edit failed, falling back: %s", exc)
        elif _quick_image_edit_intent(body.text or "", True):
            # User clearly asked for an edit but we couldn't locate a source
            # image AT ALL (disk wiped + GridFS empty). Better to tell them
            # truthfully than to silently generate an unrelated vision
            # summary. Post a single clarifying agent message instead.
            primary = responders[0]
            await db.messages.insert_one({
                "id": str(uuid.uuid4()),
                "conversation_id": conv_id,
                "sender_type": "agent",
                "sender_id": primary,
                "sender_name": AGENTS[primary]["name"],
                "agent_id": primary,
                "text": (
                    "I can't find the source image to edit — the last one I generated "
                    "or you uploaded isn't accessible anymore. Could you re-upload it, "
                    "or paste a URL and I'll apply your edit from there?"
                ),
                "intent": "image_edit_missing_source",
                "timestamp": datetime.now(timezone.utc),
            })
            responders = []  # don't pile a normal reply on top

    if responders:
        try:
            wants_video = bool(os.environ.get("KIE_API_KEY")) and await detect_video_intent(body.text)
        except Exception:
            wants_video = False
        if wants_video:
            primary = responders[0]
            ar = parse_video_aspect_ratio(body.text)
            video_msg = await _kick_off_video(conv_id, primary, body.text, ar)
            responders = []  # video reply is enough — no text
        else:
            try:
                wants_image = await detect_image_intent(body.text)
            except Exception:
                wants_image = False
            if wants_image:
                primary = responders[0]
                sp = team.get("soulprint") or {}
                sp_lines = [f"{k.capitalize()}: {sp[k]}" for k in ("mission", "tone", "audience", "values") if sp.get(k)]
                sys_msg = (
                    f"You are {AGENTS[primary]['name']}, generating an image on behalf of the "
                    f"team \"{team['name']}\". Honor the team's voice and aesthetics.\n"
                    "If the user requests a social media graphic (Instagram post, story, "
                    "LinkedIn banner, Twitter/X header, Facebook cover, YouTube thumbnail), "
                    "match the appropriate aspect ratio (Instagram post 1:1; story / Reels / "
                    "TikTok 9:16; LinkedIn / Twitter post 1.91:1; YouTube thumbnail 16:9) and "
                    "render any text legibly in the safe area."
                    + (("\n" + "\n".join(sp_lines)) if sp_lines else "")
                )
                try:
                    requested_count = detect_image_count(body.text)
                    rendered = await _generate_bulk_images(
                        db=db, conv_id=conv_id,
                        primary_agent_id=primary,
                        primary_agent_name=AGENTS[primary]["name"],
                        user_text=body.text, sys_msg=sys_msg,
                        count=requested_count,
                    )
                    if rendered > 0:
                        responders = []  # Image(s) alone are enough
                except Exception as exc:
                    logger.warning("auto image generation failed: %s", exc)

    # ── Final private-CoS guarantee ────────────────────────────────────
    # Last line of defense before the responder loop. The image/video
    # intent classifiers clear responders when they fire — that's
    # correct for Group chats (the image IS the reply) but WRONG for
    # private Chief-of-Staff threads where the user expects the CoS to
    # talk to them every message regardless. We force CoS back in here
    # so a misfiring intent classifier (e.g. wrongly tagging "contract"
    # as an image request) can't silence the room.
    if conv.get("kind") == "private_cos":
        cos_id = "specialized-chief-of-staff"
        if cos_id in AGENTS and cos_id not in responders:
            responders.append(cos_id)

    agent_messages = []
    for aid in responders:
        # Bail if the user clicked Stop on the client. We skip the LLM
        # call AND the persist so the conversation history doesn't show
        # orphan replies the user never wanted.
        if _should_cancel(conv_id, started_at):
            break
        reply, web_sources = await generate_agent_reply(conv, team, aid, body.text, u["name"], latest_user_id=u["id"])
        # Re-check after the (potentially long) LLM call before we persist.
        if _should_cancel(conv_id, started_at):
            break
        msg_id = str(uuid.uuid4())
        clean_reply, artifacts = await process_agent_artifacts(
            reply, conv=conv, agent_id=aid, message_id=msg_id, created_by=u["id"],
        )
        # ── Auto-retry on empty promises ────────────────────────────────
        # If the agent said something like "I'll prepare the report
        # shortly" but never attached anything, fire one forced-delivery
        # retry so the deliverable lands without requiring the user to
        # type "draft it". Single retry only — if the agent still refuses
        # to deliver, we keep the friendly footer the post-processor
        # already appended. Skipped when canceled mid-flight.
        if (not artifacts
            and _has_empty_promise(reply or "")
            and not _should_cancel(conv_id, started_at)):
            try:
                logger.info("Auto-retrying agent %s with forced-delivery (empty promise detected).", aid)
                retry_reply, retry_sources = await generate_agent_reply(
                    conv, team, aid, body.text, u["name"], force_delivery=True,
                    latest_user_id=u["id"],
                )
                if retry_reply and not _should_cancel(conv_id, started_at):
                    retry_clean, retry_artifacts = await process_agent_artifacts(
                        retry_reply, conv=conv, agent_id=aid,
                        message_id=msg_id, created_by=u["id"],
                    )
                    # Only adopt the retry if it actually delivered an
                    # artifact — otherwise the original reply (with the
                    # heads-up footer) is more useful to the user.
                    if retry_artifacts:
                        clean_reply = retry_clean
                        artifacts = retry_artifacts
                        if retry_sources:
                            web_sources = retry_sources
            except Exception as exc:
                logger.warning("Forced-delivery retry failed: %s", exc)
        # If we STILL don't have artifacts AND the cleaned reply still
        # looks like an empty promise, flag the message so the frontend
        # can render a "Generate now" pill the user can click to trigger
        # a manual retry. Lightweight UX rescue when the LLM-side fixes
        # don't catch it.
        message_empty_promise = (
            not artifacts and _has_empty_promise(clean_reply or reply or "")
        )
        msg = {
            "id": msg_id,
            "conversation_id": conv_id,
            "sender_type": "agent",
            "sender_id": aid,
            "sender_name": AGENTS[aid]["name"],
            "agent_id": aid,
            "text": clean_reply,
            "timestamp": datetime.now(timezone.utc),
        }
        if web_sources:
            msg["web_sources"] = web_sources
        if artifacts:
            msg["attachments"] = artifacts
        if message_empty_promise:
            msg["empty_promise"] = True
        await db.messages.insert_one({**msg})
        agent_messages.append({k: v for k, v in msg.items() if k != "_id"})
        # ── Background memory extraction (Phase 1, user-scoped) ────────
        # Fire-and-forget: never blocks the response. The helper swallows
        # every error so a slow/failing extraction can't break the chat.
        try:
            from memory import extract_and_store, schedule_extraction
            schedule_extraction(extract_and_store(
                db,
                llm_chat_cls=LlmChat,
                user_message_cls=UserMessage,
                emergent_llm_key=EMERGENT_LLM_KEY,
                user_id=u["id"],
                user_message=body.text,
                agent_message=clean_reply or "",
                org_id=str(team.get("org_id") or "") or None,
                team_id=str(team.get("_id") or "") or None,
                conv_id=conv_id,
                msg_id=msg_id,
            ))
        except Exception as exc:
            logger.info("memory extraction schedule failed: %s", exc)

    user_doc_resp = {k: v for k, v in user_doc.items() if k != "_id"}
    # Re-fetch fresh conv so frontend can mirror new agent_ids if expanded
    fresh_conv = await db.conversations.find_one({"_id": ObjectId(conv_id)})
    if image_msg is not None:
        agent_messages.insert(0, {k: v for k, v in image_msg.items() if k != "_id"})
    if video_msg is not None:
        agent_messages.insert(0, video_msg)
    # Surface cancel state to the client so the UI can display a small
    # "Stopped" hint instead of treating an empty reply list as a silent
    # router failure.
    cancelled = _should_cancel(conv_id, started_at)
    if cancelled:
        _cancel_requests.pop(conv_id, None)
    return {
        "user_message": user_doc_resp,
        "agent_messages": agent_messages,
        "conversation": serialize_conv(fresh_conv) if fresh_conv else None,
        "cancelled": cancelled,
    }

# ── Smart start: route → create direct conv → post first message ──────────
@conv_r.post("/teams/{team_id}/start")
async def smart_start(team_id: str, body: SmartStartBody, u: dict = Depends(get_current_user)):
    if not body.text.strip():
        raise HTTPException(400, "Message cannot be empty")
    await require_team_member(team_id, u["id"])
    team = await db.teams.find_one({"_id": ObjectId(team_id)})
    if not team:
        raise HTTPException(404, "Team not found")

    chosen = await route_best_agent(team, body.text)
    title = body.title.strip() if body.title else (body.text.strip()[:60] + ("…" if len(body.text.strip()) > 60 else ""))
    now = datetime.now(timezone.utc)
    conv_doc = {
        "team_id": team_id,
        "title": title,
        "type": "direct",
        "agent_ids": [chosen],
        "member_ids": [u["id"]],
        "auto_respond": False,
        "smart_expand": False,
        "created_by": u["id"],
        "created_at": now,
    }
    res = await db.conversations.insert_one(conv_doc)
    conv_doc["_id"] = res.inserted_id
    conv_id = str(res.inserted_id)

    user_doc = {
        "id": str(uuid.uuid4()),
        "conversation_id": conv_id,
        "sender_type": "user",
        "sender_id": u["id"],
        "sender_name": u["name"],
        "agent_id": None,
        "text": body.text,
        "timestamp": now,
    }
    await db.messages.insert_one({**user_doc})

    # If the user clearly asked for a video, kick off Kie.ai async polling
    try:
        wants_video = bool(os.environ.get("KIE_API_KEY")) and await detect_video_intent(body.text)
    except Exception:
        wants_video = False
    if wants_video:
        ar = parse_video_aspect_ratio(body.text)
        pending = await _kick_off_video(conv_id, chosen, body.text, ar)
        return {
            "conversation": serialize_conv(conv_doc),
            "routed_agent_id": chosen,
            "user_message": {k: v for k, v in user_doc.items() if k != "_id"},
            "agent_message": pending,
        }

    # If the user clearly asked for an image, auto-generate it as the first reply.
    try:
        wants_image = await detect_image_intent(body.text)
    except Exception:
        wants_image = False
    if wants_image:
        sp = team.get("soulprint") or {}
        sp_lines = [f"{k.capitalize()}: {sp[k]}" for k in ("mission", "tone", "audience", "values") if sp.get(k)]
        sys_msg = (
            f"You are {AGENTS[chosen]['name']}, generating an image on behalf of the "
            f"team \"{team['name']}\". Honor the team's voice and aesthetics.\n"
            "If the user requests a social media graphic (Instagram post, story, "
            "LinkedIn banner, Twitter/X header, Facebook cover, YouTube thumbnail), "
            "match the appropriate aspect ratio (Instagram post 1:1; story / Reels / "
            "TikTok 9:16; LinkedIn / Twitter post 1.91:1; YouTube thumbnail 16:9) and "
            "render any text legibly in the safe area."
            + (("\n" + "\n".join(sp_lines)) if sp_lines else "")
        )
        try:
            requested_count = detect_image_count(body.text)
            if requested_count > 1:
                rendered = await _generate_bulk_images(
                    db=db, conv_id=conv_id,
                    primary_agent_id=chosen,
                    primary_agent_name=AGENTS[chosen]["name"],
                    user_text=body.text, sys_msg=sys_msg,
                    count=requested_count,
                )
                if rendered > 0:
                    return {
                        "conversation": serialize_conv(conv_doc),
                        "routed_agent_id": chosen,
                        "user_message": {k: v for k, v in user_doc.items() if k != "_id"},
                        "agent_message": {"bulk": True, "rendered": rendered, "requested": requested_count},
                    }
            fname, caption = await generate_image_via_nano_banana(body.text, sys_msg)
            agent_msg = {
                "id": str(uuid.uuid4()),
                "conversation_id": conv_id,
                "sender_type": "agent",
                "sender_id": chosen,
                "sender_name": AGENTS[chosen]["name"],
                "agent_id": chosen,
                "text": (caption or "Here's a draft.").strip(),
                "media_type": "image",
                "media_filename": fname,
                "media_url": f"/api/files/{fname}",
                "media_prompt": body.text,
                "media_system": sys_msg,
                "media_model": "nano-banana",
                "timestamp": datetime.now(timezone.utc),
            }
            await db.messages.insert_one({**agent_msg})
            return {
                "conversation": serialize_conv(conv_doc),
                "routed_agent_id": chosen,
                "user_message": {k: v for k, v in user_doc.items() if k != "_id"},
                "agent_message": {k: v for k, v in agent_msg.items() if k != "_id"},
            }
        except Exception as exc:
            logger.warning("smart_start image generation failed, falling back to text: %s", exc)

    # ── Agent reply — wrapped so a transient LLM / web-search / brave
    # error doesn't orphan the conversation. Before this guard, any
    # exception here would 500 the endpoint AFTER the conversation +
    # user-message were already inserted, so the frontend retried and
    # the user wound up with N empty conversations from N retries.
    # The conversation is now ALWAYS returned 200 — if the agent reply
    # fails, we post a single agent message with a friendly retry
    # nudge so the user knows what to do next.
    try:
        reply, web_sources = await generate_agent_reply(conv_doc, team, chosen, body.text, u["name"], latest_user_id=u["id"])
        msg_id = str(uuid.uuid4())
        clean_reply, artifacts = await process_agent_artifacts(
            reply, conv=conv_doc, agent_id=chosen, message_id=msg_id, created_by=u["id"],
        )
        agent_msg = {
            "id": msg_id,
            "conversation_id": conv_id,
            "sender_type": "agent",
            "sender_id": chosen,
            "sender_name": AGENTS[chosen]["name"],
            "agent_id": chosen,
            "text": clean_reply,
            "timestamp": datetime.now(timezone.utc),
        }
        if web_sources:
            agent_msg["web_sources"] = web_sources
        if artifacts:
            agent_msg["attachments"] = artifacts
        await db.messages.insert_one({**agent_msg})
        # Background user-memory extraction (Phase 1) — same fire-and-forget
        # pattern as send_message. Doesn't block, can't fail the request.
        try:
            from memory import extract_and_store, schedule_extraction
            schedule_extraction(extract_and_store(
                db,
                llm_chat_cls=LlmChat,
                user_message_cls=UserMessage,
                emergent_llm_key=EMERGENT_LLM_KEY,
                user_id=u["id"],
                user_message=body.text,
                agent_message=clean_reply or "",
                org_id=str(team.get("org_id") or "") or None,
                team_id=str(team.get("_id") or "") or None,
                conv_id=conv_id,
                msg_id=msg_id,
            ))
        except Exception as exc:
            logger.info("memory extraction schedule failed (smart_start): %s", exc)
    except Exception as exc:
        logger.warning("smart_start agent reply failed: %s", exc)
        agent_msg = {
            "id": str(uuid.uuid4()),
            "conversation_id": conv_id,
            "sender_type": "agent",
            "sender_id": chosen,
            "sender_name": AGENTS[chosen]["name"],
            "agent_id": chosen,
            "text": (
                "I started the conversation but hit a hiccup drafting my first "
                "reply (the model or a web fetch timed out). Try rephrasing or "
                "sending again — the conversation is saved so you won't lose context."
            ),
            "intent": "smart_start_fallback",
            "timestamp": datetime.now(timezone.utc),
        }
        await db.messages.insert_one({**agent_msg})

    return {
        "conversation": serialize_conv(conv_doc),
        "routed_agent_id": chosen,
        "user_message": {k: v for k, v in user_doc.items() if k != "_id"},
        "agent_message": {k: v for k, v in agent_msg.items() if k != "_id"},
    }

@conv_r.post("/conversations/{conv_id}/switch-agent")
async def switch_agent(conv_id: str, body: SwitchAgentBody, u: dict = Depends(get_current_user)):
    """Hand off a DIRECT conversation to a different agent.

    Optional `text` lets the user write a short bridging note that the new agent will see.
    """
    conv = await load_conv_or_403(conv_id, u["id"])
    if conv["type"] != "direct":
        raise HTTPException(400, "Only direct conversations support a single-agent switch")
    if not is_agent(body.agent_id):
        raise HTTPException(400, f"Unknown agent: {body.agent_id}")
    if body.agent_id == (conv.get("agent_ids") or [None])[0]:
        raise HTTPException(400, "Already talking to that agent")

    team = await db.teams.find_one({"_id": ObjectId(conv["team_id"])})
    if not team:
        raise HTTPException(404, "Team not found")

    await db.conversations.update_one(
        {"_id": ObjectId(conv_id)},
        {"$set": {"agent_ids": [body.agent_id]}},
    )
    conv["agent_ids"] = [body.agent_id]

    intro_msg = body.text.strip() or (
        "Quick handoff — bringing in a new specialist. Please catch up on the conversation "
        "above and pick up from where we are."
    )
    intro = {
        "id": str(uuid.uuid4()),
        "conversation_id": conv_id,
        "sender_type": "user",
        "sender_id": u["id"],
        "sender_name": u["name"],
        "agent_id": None,
        "text": intro_msg,
        "timestamp": datetime.now(timezone.utc),
    }
    await db.messages.insert_one({**intro})

    reply, web_sources = await generate_agent_reply(conv, team, body.agent_id, intro_msg, u["name"], latest_user_id=u["id"])
    msg_id = str(uuid.uuid4())
    clean_reply, artifacts = await process_agent_artifacts(
        reply, conv=conv, agent_id=body.agent_id, message_id=msg_id, created_by=u["id"],
    )
    agent_msg = {
        "id": msg_id,
        "conversation_id": conv_id,
        "sender_type": "agent",
        "sender_id": body.agent_id,
        "sender_name": AGENTS[body.agent_id]["name"],
        "agent_id": body.agent_id,
        "text": clean_reply,
        "timestamp": datetime.now(timezone.utc),
    }
    if web_sources:
        agent_msg["web_sources"] = web_sources
    if artifacts:
        agent_msg["attachments"] = artifacts
    await db.messages.insert_one({**agent_msg})

    fresh = await db.conversations.find_one({"_id": ObjectId(conv_id)})
    return {
        "conversation": serialize_conv(fresh),
        "user_message": {k: v for k, v in intro.items() if k != "_id"},
        "agent_message": {k: v for k, v in agent_msg.items() if k != "_id"},
    }

# ── Manage agents in a GROUP conversation ──────────────────────────────────
class ConvAgentBody(BaseModel):
    agent_id: str

@conv_r.post("/conversations/{conv_id}/agents")
async def add_conv_agent(conv_id: str, body: ConvAgentBody, u: dict = Depends(get_current_user)):
    """Add a specialist to a conversation. For DIRECT chats this turns the
    chat into a group chat so multiple specialists can collaborate. Posts a
    small system note so everyone can see who joined."""
    conv = await load_conv_or_403(conv_id, u["id"])
    if not is_agent(body.agent_id):
        raise HTTPException(400, f"Unknown agent: {body.agent_id}")
    if body.agent_id in (conv.get("agent_ids") or []):
        raise HTTPException(400, "That agent is already in this conversation")
    update_set: dict = {}
    if conv["type"] == "direct":
        update_set["type"] = "group"
        update_set["auto_respond"] = True
    if update_set:
        await db.conversations.update_one(
            {"_id": ObjectId(conv_id)},
            {"$set": update_set, "$addToSet": {"agent_ids": body.agent_id}},
        )
    else:
        await db.conversations.update_one(
            {"_id": ObjectId(conv_id)},
            {"$addToSet": {"agent_ids": body.agent_id}},
        )
    note = {
        "id": str(uuid.uuid4()),
        "conversation_id": conv_id,
        "sender_type": "system",
        "sender_id": "system",
        "sender_name": "System",
        "text": f"{u['name']} added {AGENTS[body.agent_id]['name']} to the room.",
        "intent": "agent_added",
        "agent_id": body.agent_id,
        "timestamp": datetime.now(timezone.utc),
    }
    await db.messages.insert_one({**note})
    fresh = await db.conversations.find_one({"_id": ObjectId(conv_id)})
    return {
        "conversation": serialize_conv(fresh),
        "system_message": {k: v for k, v in note.items() if k != "_id"},
    }

@conv_r.delete("/conversations/{conv_id}/agents/{agent_id}")
async def remove_conv_agent(conv_id: str, agent_id: str, u: dict = Depends(get_current_user)):
    conv = await load_conv_or_403(conv_id, u["id"])
    if agent_id not in (conv.get("agent_ids") or []):
        raise HTTPException(404, "That agent isn't in this conversation")
    if len(conv.get("agent_ids") or []) <= 1:
        raise HTTPException(400, "A conversation must keep at least one agent — add another before removing this one")
    await db.conversations.update_one(
        {"_id": ObjectId(conv_id)},
        {"$pull": {"agent_ids": agent_id}},
    )
    note = {
        "id": str(uuid.uuid4()),
        "conversation_id": conv_id,
        "sender_type": "system",
        "sender_id": "system",
        "sender_name": "System",
        "text": f"{u['name']} removed {AGENTS[agent_id]['name']} from the room.",
        "intent": "agent_removed",
        "agent_id": agent_id,
        "timestamp": datetime.now(timezone.utc),
    }
    await db.messages.insert_one({**note})
    fresh = await db.conversations.find_one({"_id": ObjectId(conv_id)})
    return {
        "conversation": serialize_conv(fresh),
        "system_message": {k: v for k, v in note.items() if k != "_id"},
    }

# ── Image generation tool (Nano Banana via Emergent LLM key) ─────────────────
NANO_BANANA_MODEL = "gemini-3.1-flash-image-preview"
OPENAI_IMAGE_MODEL = "gpt-image-1"


async def generate_image_via_openai(
    prompt: str,
    system_message: str = "",
) -> tuple[str, str]:
    """Text-to-image via OpenAI GPT Image 1. Returns (filename, caption).

    OpenAI's image endpoint doesn't take a separate system message — we fold
    it into the prompt so the model still honours brand/Soul direction.
    Image-to-image editing isn't supported here (Nano Banana covers that).
    """
    from emergentintegrations.llm.openai.image_generation import OpenAIImageGeneration
    # Image gen uses the Emergent universal key (when configured) — even
    # though we have a user OpenAI key for text, image-gen routes through
    # the same shared key for billing predictability and provider parity.
    gen = OpenAIImageGeneration(api_key=_EMERGENT_KEY_FOR_IMAGES or EMERGENT_LLM_KEY)
    full_prompt = prompt
    if system_message:
        full_prompt = f"{system_message.strip()}\n\n{prompt.strip()}"
    images = await gen.generate_images(
        prompt=full_prompt[:3800],
        model=OPENAI_IMAGE_MODEL,
        number_of_images=1,
    )
    if not images:
        raise RuntimeError("No image returned by OpenAI")
    img_bytes = images[0]
    filename = f"{uuid.uuid4().hex}.png"
    await persist_bytes(UPLOAD_DIR / filename, img_bytes,
                        original_name=filename, mime="image/png")
    return filename, ""


async def generate_image_for(
    prompt: str, system_message: str, *,
    model: str = "nano-banana",
    input_image_paths: Optional[List[Path]] = None,
) -> tuple[str, str, str]:
    """Single entry-point that dispatches to Nano Banana or GPT Image 1.

    Returns `(filename, caption, model_used)`. Falls back to Nano Banana if
    OpenAI image gen fails for any reason (budget, API outage, etc.) so the
    user always gets *something* instead of an error page.
    """
    m = (model or "nano-banana").strip().lower().replace("_", "-")
    # Normalise alternative spellings
    if m in {"openai", "gpt-image", "gpt-image-1", "openai-image"}:
        m = "gpt-image-1"
    elif m in {"gemini", "gemini-image", "nano-banana", "nano"}:
        m = "nano-banana"
    else:
        m = "nano-banana"

    # GPT Image 1 can't take source images — if the caller passed inputs,
    # silently route through Nano Banana which handles image editing.
    if m == "gpt-image-1" and input_image_paths:
        m = "nano-banana"

    if m == "gpt-image-1":
        try:
            fname, caption = await generate_image_via_openai(prompt, system_message)
            return fname, caption, "gpt-image-1"
        except Exception as exc:
            logger.warning("OpenAI image gen failed, falling back to Nano Banana: %s", exc)
            m = "nano-banana"

    fname, caption = await generate_image_via_nano_banana(
        prompt, system_message, input_image_paths=input_image_paths,
    )
    return fname, caption, "nano-banana"


async def generate_image_via_nano_banana(
    prompt: str,
    system_message: str,
    input_image_paths: Optional[List[Path]] = None,
) -> tuple[str, str]:
    """Generate (or EDIT) an image and persist it to disk.

    `input_image_paths` may contain one OR multiple source images. Multi-image
    inputs let the model compose ("add the logo from image 2 onto the
    background in image 1"). When none are passed we fall back to plain
    text-to-image generation.

    Returns (filename, caption_text).
    """
    from emergentintegrations.llm.chat import (
        LlmChat as _LlmChat,
        UserMessage as _UM,
        ImageContent as _IC,
    )
    chat = _LlmChat(
        # Nano Banana only works via the Emergent universal key (gemini
        # provider isn't reachable with a user-supplied OpenAI key).
        api_key=_EMERGENT_KEY_FOR_IMAGES or EMERGENT_LLM_KEY,
        session_id=f"img:{uuid.uuid4()}",
        system_message=system_message,
    ).with_model("gemini", NANO_BANANA_MODEL).with_params(modalities=["image", "text"])

    file_contents = None
    if input_image_paths:
        loaded: List = []
        for p in input_image_paths:
            try:
                if p and p.is_file():
                    b64 = base64.b64encode(p.read_bytes()).decode("ascii")
                    loaded.append(_IC(image_base64=b64))
            except Exception as exc:
                logger.warning("Could not load source image %s: %s", p, exc)
        if loaded:
            file_contents = loaded

    user_msg = _UM(text=prompt, file_contents=file_contents) if file_contents else _UM(text=prompt)
    text, images = await chat.send_message_multimodal_response(user_msg)
    if not images:
        raise RuntimeError("No image returned by the model")
    img = images[0]
    img_bytes = base64.b64decode(img["data"])
    mime = (img.get("mime_type") or "image/png").lower()
    # Always normalize to PNG. PNG preserves alpha (so users can later
    # remove backgrounds, paste logos, etc.), is universally accepted by
    # social composers / browsers, and avoids JPEG re-compression artifacts
    # bleeding into the brand. Transcoding via Pillow is cheap (ms-scale)
    # for typical 1024px social-post outputs. Falls back to the raw bytes
    # if Pillow isn't available or the bytes can't be decoded.
    if "png" not in mime:
        try:
            from PIL import Image as _PILImage  # noqa: WPS433
            from io import BytesIO as _BytesIO
            with _PILImage.open(_BytesIO(img_bytes)) as _im:
                # Preserve alpha if the source had it; otherwise PNG with RGB.
                if _im.mode not in ("RGB", "RGBA"):
                    _im = _im.convert("RGBA" if "A" in _im.getbands() else "RGB")
                buf = _BytesIO()
                _im.save(buf, format="PNG", optimize=True)
                img_bytes = buf.getvalue()
                mime = "image/png"
        except Exception as exc:
            logger.warning("PNG transcode failed, keeping original mime %s: %s", mime, exc)
    ext = "png" if "png" in mime else (
        "webp" if "webp" in mime else
        "gif"  if "gif"  in mime else
        "jpg"  if ("jpeg" in mime or "jpg" in mime) else
        "png"
    )
    filename = f"{uuid.uuid4().hex}.{ext}"
    await persist_bytes(UPLOAD_DIR / filename, img_bytes,
                        original_name=filename, mime=mime or f"image/{ext}")
    # Strip any markdown image references (`![...](...)`) the model snuck into
    # the caption — we render the image inline ourselves, so leaving them in
    # the text would make the same image look like it appeared multiple times.
    caption = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text or "").strip()
    return filename, caption

@conv_r.post("/conversations/{conv_id}/generate-image")
async def generate_image(conv_id: str, body: GenerateImageBody, u: dict = Depends(get_current_user)):
    if not body.prompt.strip():
        raise HTTPException(400, "Prompt cannot be empty")
    conv = await load_conv_or_403(conv_id, u["id"])
    team = await db.teams.find_one({"_id": ObjectId(conv["team_id"])})
    if not team:
        raise HTTPException(404, "Team not found")

    agent_id = body.agent_id or (conv.get("agent_ids") or [None])[0]
    if not agent_id or agent_id not in AGENTS:
        raise HTTPException(400, "No valid agent for this conversation")
    if conv["type"] == "group" and agent_id not in conv.get("agent_ids", []):
        raise HTTPException(400, "Agent is not part of this group")

    now = datetime.now(timezone.utc)
    user_msg = {
        "id": str(uuid.uuid4()),
        "conversation_id": conv_id,
        "sender_type": "user",
        "sender_id": u["id"],
        "sender_name": u["name"],
        "agent_id": None,
        "text": f"🎨 {body.prompt}",
        "intent": "image_request",
        "timestamp": now,
    }
    await db.messages.insert_one({**user_msg})

    # Build a SoulPrint-aware system message so the image follows team aesthetics
    sp = team.get("soulprint") or {}
    sp_lines = []
    for k in ("mission", "tone", "audience", "values"):
        if sp.get(k):
            sp_lines.append(f"{k.capitalize()}: {sp[k]}")
    system_msg = (
        f"You are {AGENTS[agent_id]['name']}, generating an image on behalf of the "
        f"team \"{team['name']}\". Honor the team's voice and aesthetics.\n"
        "If the user requests a social media graphic (Instagram post, story, LinkedIn banner, "
        "Twitter/X header, Facebook cover, YouTube thumbnail, etc.), match the appropriate "
        "aspect ratio and put text legibly in the safe area: Instagram post = 1:1 square, "
        "Instagram story / Reels / TikTok = 9:16 vertical, LinkedIn / Twitter post = 1.91:1 "
        "horizontal banner, YouTube thumbnail = 16:9. Always render text crisp and on-brand."
        + (("\n" + "\n".join(sp_lines)) if sp_lines else "")
    )

    try:
        filename, caption = await generate_image_via_nano_banana(body.prompt, system_msg)
    except Exception as exc:
        logger.exception("Image generation failed: %s", exc)
        # Save a polite agent error message so the user has feedback in-thread
        err_msg = {
            "id": str(uuid.uuid4()),
            "conversation_id": conv_id,
            "sender_type": "agent",
            "sender_id": agent_id,
            "sender_name": AGENTS[agent_id]["name"],
            "agent_id": agent_id,
            "text": "I couldn't generate that image — try again, or rephrase the prompt.",
            "timestamp": datetime.now(timezone.utc),
        }
        await db.messages.insert_one({**err_msg})
        return {
            "user_message": {k: v for k, v in user_msg.items() if k != "_id"},
            "agent_message": {k: v for k, v in err_msg.items() if k != "_id"},
            "ok": False,
        }

    agent_msg = {
        "id": str(uuid.uuid4()),
        "conversation_id": conv_id,
        "sender_type": "agent",
        "sender_id": agent_id,
        "sender_name": AGENTS[agent_id]["name"],
        "agent_id": agent_id,
        "text": caption or "Here's what I came up with.",
        "media_type": "image",
        "media_filename": filename,
        "media_url": f"/api/files/{filename}",
        "media_prompt": body.prompt,
        "media_system": system_msg,
        "media_model": "nano-banana",
        "timestamp": datetime.now(timezone.utc),
    }
    await db.messages.insert_one({**agent_msg})
    return {
        "user_message": {k: v for k, v in user_msg.items() if k != "_id"},
        "agent_message": {k: v for k, v in agent_msg.items() if k != "_id"},
        "ok": True,
    }


class RegenerateImageBody(BaseModel):
    prompt: Optional[str] = None       # if blank, reuse the original prompt
    model: Optional[str] = None        # "nano-banana" (default) | "gpt-image-1"


class MessageFeedbackBody(BaseModel):
    # "up" or "down" to register/flip. Omit or pass "" to clear.
    value: str = Field("", max_length=8)


@conv_r.post("/conversations/{conv_id}/messages/{msg_id}/feedback")
async def set_message_feedback(
    conv_id: str, msg_id: str,
    body: MessageFeedbackBody,
    u: dict = Depends(get_current_user),
):
    """Set or clear the current user's thumbs-up / thumbs-down on a message.

    Thumbs are per-user (each member can vote independently). We store
    feedback in a dedicated `message_feedback` collection keyed by
    (message_id, user_id) — so flipping up↔down is idempotent and clearing
    just removes the row. We also maintain a denormalised tally on the
    message itself (`up_count` / `down_count` / `my_feedback`) so the chat
    endpoint doesn't need a second query to render counts.
    """
    val = (body.value or "").strip().lower()
    if val not in {"up", "down", ""}:
        raise HTTPException(400, "value must be 'up', 'down', or '' to clear")

    conv = await load_conv_or_403(conv_id, u["id"])
    # Cheap existence check — keeps the happy-path response identical to
    # the typical agent reply shape. Fails fast if the message was deleted.
    msg = await db.messages.find_one({"id": msg_id}, {"_id": 0, "conversation_id": 1})
    if not msg or str(msg.get("conversation_id", "")) != conv_id:
        raise HTTPException(404, "Message not found")

    key = {"message_id": msg_id, "user_id": u["id"]}
    if not val:
        await db.message_feedback.delete_one(key)
    else:
        await db.message_feedback.update_one(
            key,
            {"$set": {**key, "value": val,
                      "updated_at": datetime.now(timezone.utc)}},
            upsert=True,
        )
    # Refresh tallies
    ups = await db.message_feedback.count_documents({"message_id": msg_id, "value": "up"})
    downs = await db.message_feedback.count_documents({"message_id": msg_id, "value": "down"})
    return {"my_feedback": val, "up_count": ups, "down_count": downs}


@conv_r.post("/conversations/{conv_id}/messages/{msg_id}/generate-now")
async def generate_now(
    conv_id: str, msg_id: str,
    u: dict = Depends(get_current_user),
):
    """User-triggered "Generate now" for an agent message that promised
    a deliverable but didn't actually deliver one (the `empty_promise`
    flag is set on the message).

    Replays the original USER message that prompted this agent reply,
    forcing `force_delivery=True` so the agent actually produces the
    artifact this time. Posts the result as a NEW agent message —
    the original empty-promise reply stays visible (just stripped of
    the `empty_promise` flag) so the user can still see what was said.
    """
    conv = await load_conv_or_403(conv_id, u["id"])
    team = await db.teams.find_one({"_id": ObjectId(conv["team_id"])})
    if not team:
        raise HTTPException(404, "Team not found")
    target = await db.messages.find_one({"id": msg_id, "conversation_id": conv_id})
    if not target:
        raise HTTPException(404, "Message not found")
    if not target.get("empty_promise"):
        raise HTTPException(400, "This message isn't flagged for retry — it already has its deliverable.")
    aid = target.get("agent_id")
    if not aid or aid not in AGENTS:
        raise HTTPException(400, "Could not resolve the agent for this message")

    # Find the user message that prompted the empty-promise reply.
    # Strategy: walk the messages collection backwards from target's
    # timestamp until we hit a user-typed message in the same conv.
    target_ts = target.get("timestamp")
    if not target_ts:
        raise HTTPException(400, "Original message has no timestamp; can't retry")
    user_prompt_doc = await db.messages.find_one(
        {"conversation_id": conv_id,
         "sender_type": "user",
         "timestamp": {"$lt": target_ts}},
        sort=[("timestamp", -1)],
    )
    if not user_prompt_doc:
        raise HTTPException(400, "No prompting user message found to replay.")
    user_text = user_prompt_doc.get("text") or ""
    user_name = user_prompt_doc.get("sender_name") or u["name"]

    # Re-run with forced delivery. We piggyback on generate_agent_reply
    # so the same image-intent classifier + ground-truth blocks fire.
    try:
        reply, web_sources = await generate_agent_reply(
            conv, team, aid, user_text, user_name, force_delivery=True,
            latest_user_id=user_prompt_doc.get("sender_id") or u["id"],
        )
    except Exception as exc:
        logger.exception("Generate-now retry failed: %s", exc)
        raise HTTPException(502, f"Couldn't regenerate: {exc}") from exc

    new_msg_id = str(uuid.uuid4())
    clean_reply, attachments = await process_agent_artifacts(
        reply or "", conv=conv, agent_id=aid,
        message_id=new_msg_id, created_by=u["id"],
    )
    new_msg = {
        "id": new_msg_id,
        "conversation_id": conv_id,
        "sender_type": "agent",
        "sender_id": aid,
        "sender_name": AGENTS[aid]["name"],
        "agent_id": aid,
        "text": clean_reply,
        "timestamp": datetime.now(timezone.utc),
        "regenerated_from": msg_id,
        "intent": "generate_now_retry",
    }
    if attachments:
        new_msg["attachments"] = attachments
    if web_sources:
        new_msg["web_sources"] = web_sources
    # Tag the original message as "consumed" so the pill doesn't render
    # again — even if the retry didn't produce an artifact, the user
    # already pulled their "one click" lever and shouldn't get repeated
    # prompts. Same-thread retries can still be done via the regular
    # composer.
    await db.messages.update_one(
        {"id": msg_id},
        {"$unset": {"empty_promise": ""},
         "$set": {"empty_promise_retried": True}},
    )
    await db.messages.insert_one({**new_msg})
    return {
        "ok": True,
        "message": {k: v for k, v in new_msg.items() if k != "_id"},
        "delivered": bool(attachments),
    }





@conv_r.post("/conversations/{conv_id}/messages/{msg_id}/regenerate-image")
async def regenerate_image(
    conv_id: str, msg_id: str,
    body: RegenerateImageBody,
    u: dict = Depends(get_current_user),
):
    """Produce a new variant of an existing image message.

    - Reuses the original message's saved `media_prompt` / `media_system`
      unless the caller overrides them (body.prompt).
    - `body.model` lets the user pick between Nano Banana (Gemini, default)
      and GPT Image 1 (OpenAI). Silently falls back to Nano Banana if the
      requested model fails.
    - Posts a NEW agent message with the regenerated image so the original
      stays visible for A/B comparison.
    """
    conv = await load_conv_or_403(conv_id, u["id"])
    orig = await db.messages.find_one({"id": msg_id, "conversation_id": conv_id})
    if not orig:
        raise HTTPException(404, "Source image message not found")
    if orig.get("media_type") != "image":
        raise HTTPException(400, "Only image messages can be regenerated")

    agent_id = orig.get("agent_id") or (conv.get("agent_ids") or [None])[0]
    if not agent_id or agent_id not in AGENTS:
        raise HTTPException(400, "Could not resolve the agent for this image")

    new_prompt = (body.prompt or "").strip() or (orig.get("media_prompt") or "").strip()
    if not new_prompt:
        # Older messages pre-dating the media_prompt field — fall back to
        # the agent's text caption so we don't regenerate from nothing.
        new_prompt = (orig.get("text") or "").strip()
    if not new_prompt:
        raise HTTPException(422, "No prompt to regenerate from — please type one")

    # Rebuild a system message if we don't have the saved one
    system_msg = orig.get("media_system") or ""
    if not system_msg:
        team = await db.teams.find_one({"_id": ObjectId(conv["team_id"])})
        sp = (team or {}).get("soulprint") or {}
        sp_lines = [f"{k.capitalize()}: {sp[k]}"
                    for k in ("mission", "tone", "audience", "values") if sp.get(k)]
        system_msg = (
            f"You are {AGENTS[agent_id]['name']}, generating an image on behalf of "
            f"the team. Honor the team's voice and aesthetics. Render any text "
            f"crisply." + (("\n" + "\n".join(sp_lines)) if sp_lines else "")
        )

    try:
        fname, caption, used_model = await generate_image_for(
            new_prompt, system_msg, model=(body.model or "nano-banana"),
        )
    except Exception as exc:
        logger.exception("Image regeneration failed: %s", exc)
        raise HTTPException(502, f"Couldn't regenerate the image: {exc}") from exc

    edit_note = "edit" if body.prompt and body.prompt.strip() != (orig.get("media_prompt") or "").strip() else "variant"
    new_msg = {
        "id": str(uuid.uuid4()),
        "conversation_id": conv_id,
        "sender_type": "agent",
        "sender_id": agent_id,
        "sender_name": AGENTS[agent_id]["name"],
        "agent_id": agent_id,
        "text": (caption or f"Here's a fresh {edit_note}.").strip(),
        "media_type": "image",
        "media_filename": fname,
        "media_url": f"/api/files/{fname}",
        "media_prompt": new_prompt,
        "media_system": system_msg,
        "media_model": used_model,
        "regenerated_from": msg_id,
        "intent": "image_regenerate",
        "timestamp": datetime.now(timezone.utc),
    }
    await db.messages.insert_one({**new_msg})
    return {
        "agent_message": {k: v for k, v in new_msg.items() if k != "_id"},
        "model_used": used_model,
        "ok": True,
    }


# ── Batch image-resize endpoint (chat-side companion to the mission tool) ──
class ResizeSpec(BaseModel):
    label: str = ""
    width: int
    height: int
    mode: Optional[str] = "fit"   # fit | fill | stretch


class ResizeImageBody(BaseModel):
    """Resize an existing chat image (or any uploaded file accessible at
    `/api/files/<filename>`) into multiple exact pixel sizes.
    Only one of ``message_id`` (preferred — uses that message's first image)
    or ``filename`` (raw upload-dir filename) needs to be supplied.
    """
    message_id: Optional[str] = None
    filename: Optional[str] = None
    sizes: List[ResizeSpec]
    base_name: Optional[str] = "image"


@conv_r.post("/conversations/{conv_id}/resize-image")
async def resize_image_endpoint(
    conv_id: str,
    body: ResizeImageBody,
    u: dict = Depends(get_current_user),
):
    """Posts a single agent reply containing all resized variants as
    inline image attachments. Mirrors the mission tool but lives in chat
    so a user who pastes "recreate at IAB sizes" gets immediate results
    without spinning up a mission.
    """
    conv = await load_conv_or_403(conv_id, u["id"])
    if not body.sizes:
        raise HTTPException(400, "Pass at least one {label,width,height} size.")
    if len(body.sizes) > 24:
        raise HTTPException(400, "Cap is 24 sizes per call.")

    # Resolve source bytes
    src_bytes: Optional[bytes] = None
    src_label = ""
    if body.message_id:
        msg = await db.messages.find_one(
            {"id": body.message_id, "conversation_id": conv_id}
        )
        if not msg:
            raise HTTPException(404, "message_id not found in this conversation")
        for a in msg.get("attachments") or []:
            if (a.get("mime") or "").startswith("image/") or a.get("kind") == "image":
                fname = (a.get("url") or "").rsplit("/", 1)[-1]
                fp = UPLOAD_DIR / fname
                if not fp.is_file() and "/files/docs/" in (a.get("url") or ""):
                    parts = a["url"].split("/files/docs/", 1)[1].split("/", 1)
                    if len(parts) == 2:
                        fp = DOCS_DIR / parts[0] / parts[1]
                if fp.is_file():
                    src_bytes = fp.read_bytes()
                    src_label = a.get("original_name") or fname
                    break
    if src_bytes is None and body.filename:
        fname = body.filename.rsplit("/", 1)[-1]
        fp = UPLOAD_DIR / fname
        if fp.is_file():
            src_bytes = fp.read_bytes()
            src_label = fname

    if src_bytes is None:
        raise HTTPException(404, "Couldn't load source image — pass message_id or filename.")

    # Get team_id + org_id for documents persistence
    team = await db.teams.find_one({"_id": ObjectId(conv["team_id"])})
    org_id = team.get("org_id") if team else ""

    from image_resize import smart_resize_batch
    base = body.base_name or src_label.rsplit(".", 1)[0] or "image"
    sizes_dicts = [s.model_dump() for s in body.sizes]
    variants = smart_resize_batch(src_bytes, sizes_dicts, base_name=base)

    attachments_out: List[dict] = []
    errors: List[dict] = []
    for v in variants:
        if v.get("error"):
            errors.append({"label": v.get("label"), "error": v["error"]})
            continue
        from artifacts import save_artifacts
        atts = await save_artifacts(
            db, [{
                "name": v["filename"],
                "ext": "png",
                "mime": "image/png",
                "title": f"{base} — {v['label']} ({v['width']}×{v['height']})",
                "content": "",
                "_raw_bytes": v["bytes"],
            }],
            docs_root=DOCS_DIR,
            org_id=org_id,
            conversation_id=conv_id,
            message_id="",
            agent_id="",
            agent_name="Resize tool",
            created_by=u["id"],
        )
        if atts:
            a = atts[0]
            attachments_out.append({
                **a,
                "kind": "image",
                "label": v["label"],
                "width": v["width"],
                "height": v["height"],
                "title": f"{v['label']} ({v['width']}×{v['height']})",
            })

    if not attachments_out:
        raise HTTPException(500, f"All {len(variants)} resize attempts failed: {errors}")

    agent_id = (conv.get("agent_ids") or [None])[0] or "specialized-chief-of-staff"
    agent = AGENTS.get(agent_id) or {"name": "Resize tool"}
    summary_lines = [
        f"Done — produced {len(attachments_out)} variant{'s' if len(attachments_out) != 1 else ''} from **{src_label}**:",
        "",
    ]
    for a in attachments_out:
        summary_lines.append(f"- **{a['label']}** ({a['width']}×{a['height']}) — `{a['original_name']}`")
    if errors:
        summary_lines.append("")
        summary_lines.append(f"_{len(errors)} size(s) failed: {', '.join(e['label'] for e in errors)}_")
    text = "\n".join(summary_lines)
    msg = {
        "id": str(uuid.uuid4()),
        "conversation_id": conv_id,
        "sender_type": "agent",
        "sender_name": agent.get("name") or "Agent",
        "agent_id": agent_id,
        "agent_color": agent.get("color"),
        "text": text,
        "attachments": attachments_out,
        "timestamp": datetime.now(timezone.utc),
    }
    await db.messages.insert_one({**msg})
    return {
        "ok": True,
        "agent_message": {k: v for k, v in msg.items() if k != "_id"},
        "produced": len(attachments_out),
        "failed": len(errors),
        "errors": errors,
    }


# ── Video generation tool (Kie.ai Veo 3.1) ──────────────────────────────────
_VIDEO_AR_RE = re.compile(
    r"(?<!\d)(9\s*[:xX/]\s*16|16\s*[:xX/]\s*9|1\s*[:xX/]\s*1)(?!\d)"
)

def parse_video_aspect_ratio(text: str) -> str:
    """Extract a Kie-compatible aspect ratio from the user's prompt.

    Recognises explicit ratios (9:16, 16:9, 1:1) and natural-language hints
    (portrait / vertical / reel / tiktok / story → 9:16; square → 1:1;
    landscape / horizontal / youtube / widescreen / cinematic → 16:9). Falls
    back to "16:9" when no signal is present. Kie.ai only accepts 16:9 and
    9:16 today, so 1:1 collapses to 9:16 (closest square-ish crop) rather
    than failing the request.
    """
    if not text:
        return "16:9"
    raw = text.lower()
    m = _VIDEO_AR_RE.search(raw)
    if m:
        token = re.sub(r"\s+", "", m.group(1)).replace("x", ":").replace("X", ":").replace("/", ":")
        if token == "9:16":
            return "9:16"
        if token == "16:9":
            return "16:9"
        if token == "1:1":
            return "9:16"  # Kie doesn't support 1:1 for video; vertical is closer
    # Natural-language portrait / vertical signals
    vertical_kw = (
        "portrait", "vertical", "9x16", "9 by 16", "tiktok", "tik tok",
        "reel", "reels", "ig story", "instagram story", "ig stories",
        "shorts", "youtube short", "youtube shorts", "snap", "mobile-first",
    )
    if any(k in raw for k in vertical_kw):
        return "9:16"
    horizontal_kw = (
        "landscape", "horizontal", "widescreen", "wide screen", "cinematic",
        "16x9", "16 by 9", "youtube video", "youtube clip", "youtube ad",
    )
    if any(k in raw for k in horizontal_kw):
        return "16:9"
    return "16:9"

async def _poll_and_save_video(message_id: str, prompt: str, aspect_ratio: str = "16:9") -> None:
    """Background coroutine: call Kie.ai, then update the placeholder message.

    The agent message is created with `media_status="pending"` immediately so
    the UI can render a spinner. When polling resolves we DOWNLOAD the video
    (via `_save_remote_url_to_uploads` → persistent GridFS mirror) and patch
    the message with a `/api/files/<local>` URL.

    Before Feb 2026 we stored the raw Kie.ai CDN URL which silently expired
    after a few days — producing exactly the "broken video link" symptom
    the user reported. The local file is always served by our own
    capability-URL route, so it can never expire unless manually deleted.
    """
    try:
        url = await kie_generate_video(prompt, aspect_ratio=aspect_ratio)
    except Exception as exc:
        logger.exception("Kie polling crash: %s", exc)
        url = None
    now = datetime.now(timezone.utc)
    if url:
        # Download + mirror to GridFS so the link survives indefinitely.
        local_path = await _save_remote_url_to_uploads(url, suffix=".mp4")
        final_url = f"/api/files/{local_path.name}" if local_path else url
        update = {
            "media_url": final_url,
            "media_status": "ready",
            "ready_at": now,
        }
        if local_path:
            update["media_filename"] = local_path.name
            update["media_remote_url"] = url  # keep the original for audit
        await db.messages.update_one({"id": message_id}, {"$set": update})
    else:
        await db.messages.update_one(
            {"id": message_id},
            {"$set": {
                "media_status": "failed",
                "text": "I couldn't generate that video — try a different prompt or run it again.",
                "ready_at": now,
            }},
        )

async def _kick_off_video(conv_id: str, agent_id: str, prompt: str, aspect_ratio: str = "16:9") -> dict:
    """Persist a placeholder agent message and fire off the Kie polling task.
    Returns the message dict (sans _id) for the API response."""
    if aspect_ratio not in ("16:9", "9:16", "Auto"):
        aspect_ratio = "16:9"
    msg = {
        "id": str(uuid.uuid4()),
        "conversation_id": conv_id,
        "sender_type": "agent",
        "sender_id": agent_id,
        "sender_name": AGENTS[agent_id]["name"],
        "agent_id": agent_id,
        "text": "Generating your video — this usually takes 60–180s.",
        "media_type": "video",
        "media_status": "pending",
        "media_prompt": prompt[:500],
        "aspect_ratio": aspect_ratio,
        "timestamp": datetime.now(timezone.utc),
    }
    await db.messages.insert_one({**msg})
    asyncio.create_task(_poll_and_save_video(msg["id"], prompt, aspect_ratio))
    return {k: v for k, v in msg.items() if k != "_id"}

@conv_r.post("/conversations/{conv_id}/generate-video")
async def generate_video_endpoint(conv_id: str, body: GenerateVideoBody, u: dict = Depends(get_current_user)):
    if not body.prompt.strip():
        raise HTTPException(400, "Prompt cannot be empty")
    if not os.environ.get("KIE_API_KEY"):
        raise HTTPException(503, "Video generation is not configured")
    conv = await load_conv_or_403(conv_id, u["id"])
    agent_id = body.agent_id or (conv.get("agent_ids") or [None])[0]
    if not agent_id or agent_id not in AGENTS:
        raise HTTPException(400, "No valid agent for this conversation")
    if conv["type"] == "group" and agent_id not in conv.get("agent_ids", []):
        raise HTTPException(400, "Agent is not part of this group")

    now = datetime.now(timezone.utc)
    user_msg = {
        "id": str(uuid.uuid4()),
        "conversation_id": conv_id,
        "sender_type": "user",
        "sender_id": u["id"],
        "sender_name": u["name"],
        "agent_id": None,
        "text": f"🎬 {body.prompt}",
        "intent": "video_request",
        "timestamp": now,
    }
    await db.messages.insert_one({**user_msg})
    pending = await _kick_off_video(conv_id, agent_id, body.prompt, body.aspect_ratio or "16:9")
    return {
        "user_message": {k: v for k, v in user_msg.items() if k != "_id"},
        "agent_message": pending,
        "ok": True,
    }

# ── Conversation file uploads (in-chat attachments) ─────────────────────────
CONV_UPLOAD_DIR = ROOT_DIR / "uploads" / "conv"
CONV_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
MAX_CONV_UPLOAD_SIZE = 25 * 1024 * 1024  # 25MB

# Map raw extensions → kind label used for icon + handling. We share the
# extension sets imported from `assets` so the rules stay consistent with org
# uploads (images, pdfs, docs, text-like, audio, video).
def _attachment_kind(ext: str) -> str:
    e = ext.lower().lstrip(".")
    if e in IMAGE_EXTS:
        return "image"
    if e in PDF_EXTS:
        return "pdf"
    if e in DOCX_EXTS:
        return "doc"
    if e in TEXT_LIKE_EXTS:
        return "text"
    if e in {"mp3", "wav", "m4a", "ogg", "flac", "aac"}:
        return "audio"
    if e in {"mp4", "mov", "webm", "avi", "mkv"}:
        return "video"
    return "file"


# ── Universal media generation (kie.ai marketplace) ──────────────────
from kie_client import (  # noqa: E402
    submit_kie_task as _kie_submit_any,
    wait_for_kie_task as _kie_wait_any,
    kie_provider_info as _kie_info,
    KIE_PROVIDERS as _KIE_PROVIDERS,
)


class GenerateMediaBody(BaseModel):
    """Body for the universal `POST /generate-media` endpoint.

    `media_type` is one of "image" | "video" | "music". `model` is a
    kie.ai model key (or "nano-banana" / "veo-3-1" to fall through to the
    existing Emergent / Veo paths). The endpoint resolves the right
    backend automatically.
    """
    prompt: str
    model: str
    media_type: Optional[str] = None    # auto-detected from model if missing
    aspect_ratio: Optional[str] = "16:9"
    image_url: Optional[str] = None     # for image-to-image / video-from-image
    extra: Optional[dict] = None


async def _save_remote_url_to_uploads(url: str, *, suffix: str = "") -> Optional[Path]:
    """Download a remote URL into /app/backend/uploads/ and MIRROR to
    MongoDB GridFS so the file survives a pod restart. Returns the local
    Path or None on failure.

    Before Feb 2026 this function wrote to disk only — which meant every
    Kie.ai-generated image / video became a broken link the moment the
    Kubernetes pod migrated (disk is ephemeral, Mongo is persistent).
    Now we call ``persist_bytes`` so downloads get the same GridFS
    mirror every other file path uses.
    """
    try:
        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
            resp = await client.get(url)
        if resp.status_code != 200 or not resp.content:
            logger.warning("Remote download failed: %s -> %s", url, resp.status_code)
            return None
    except Exception as exc:
        logger.warning("Remote download crashed for %s: %s", url, exc)
        return None
    fname = f"kie-{uuid.uuid4().hex[:12]}{suffix}"
    fp = UPLOAD_DIR / fname
    fp.parent.mkdir(parents=True, exist_ok=True)
    # Guess a mime from the suffix so the GridFS record has useful
    # metadata for the fallback resolver. Kie.ai URLs occasionally redirect
    # through their CDN which strips content-type on the 302.
    mime = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".webp": "image/webp", ".mp4": "video/mp4", ".mov": "video/quicktime",
        ".mp3": "audio/mpeg", ".wav": "audio/wav",
    }.get(suffix.lower(), resp.headers.get("content-type") or "application/octet-stream")
    try:
        await persist_bytes(fp, resp.content, original_name=fname, mime=mime)
    except Exception as exc:
        # If the GridFS mirror fails (e.g. very large video + oplog
        # trouble), still return the disk path — the file is at least
        # reachable until the next pod restart.
        logger.warning("GridFS mirror failed for %s: %s", fname, exc)
    return fp


async def _poll_and_save_kie_media(
    message_id: str, *, model: str, prompt: str, media_type: str,
    image_url: Optional[str] = None, extra: Optional[dict] = None,
) -> None:
    """Background coroutine — submit + poll a kie.ai task, download the
    result(s), patch the placeholder agent message with attachments."""
    try:
        task_id = await _kie_submit_any(
            model_key=model, prompt=prompt,
            image_url=image_url, extra=extra,
        )
        if not task_id:
            urls: list[str] = []
        else:
            urls = await _kie_wait_any(model, task_id)
    except Exception as exc:
        logger.exception("Kie %s polling crash: %s", model, exc)
        urls = []
    now = datetime.now(timezone.utc)
    if not urls:
        await db.messages.update_one(
            {"id": message_id},
            {"$set": {
                "media_status": "failed",
                "text": (
                    f"I couldn't generate that {media_type} with **{model}**. "
                    "The model may be busy — try again or pick a different one."
                ),
                "ready_at": now,
            }},
        )
        return
    # Download every result URL into local uploads + build attachment dicts
    suffix_by_type = {"image": ".png", "video": ".mp4", "music": ".mp3"}
    suffix = suffix_by_type.get(media_type, "")
    attachments: list[dict] = []
    for u in urls:
        fp = await _save_remote_url_to_uploads(u, suffix=suffix)
        if not fp:
            continue
        mime = (
            "image/png" if media_type == "image"
            else "video/mp4" if media_type == "video"
            else "audio/mpeg"
        )
        kind = "image" if media_type == "image" else (
            "video" if media_type == "video" else "audio"
        )
        attachments.append({
            "id": uuid.uuid4().hex,
            "kind": kind,
            "filename": fp.name,
            "url": f"/api/files/{fp.name}",
            "mime": mime,
            "original_name": fp.name,
            "size": fp.stat().st_size,
            "model": model,
        })
    if not attachments:
        await db.messages.update_one(
            {"id": message_id},
            {"$set": {
                "media_status": "failed",
                "text": "Generation succeeded but the file couldn't be saved.",
                "ready_at": now,
            }},
        )
        return
    # Picker label for the message body
    info = _kie_info(model) or {}
    label = info.get("label") or model
    n = len(attachments)
    body_text = (
        f"Generated {n} {media_type}{'s' if n > 1 else ''} with **{label}**."
    )
    patch = {
        "media_status": "ready",
        "media_url": attachments[0]["url"],
        "attachments": attachments,
        "text": body_text,
        "ready_at": now,
    }
    # ALSO stamp `media_filename` on the root message for image/video so
    # downstream lookups that don't know about `attachments[*]` — most
    # notably the image-edit source finder — can still locate the file.
    if media_type in ("image", "video"):
        patch["media_filename"] = attachments[0]["filename"]
    await db.messages.update_one(
        {"id": message_id},
        {"$set": patch},
    )


@conv_r.post("/conversations/{conv_id}/generate-media")
async def generate_media_endpoint(
    conv_id: str, body: GenerateMediaBody,
    u: dict = Depends(get_current_user),
):
    """Universal media-gen entry point. Resolves the right backend by
    model id, posts a placeholder agent message, kicks off background
    polling, returns the placeholder. The frontend re-fetches the
    message list and the placeholder fills in once the task is ready.

    Supported model keys cover all kie.ai marketplace models (image:
    flux-pro, imagen-4-ultra, ideogram-3, seedream-5, gpt-image-1-5;
    video: kling-3, kling-3-pro, wan-2-6, seedance-1-5, sora-2,
    runway-aleph; music: suno) PLUS the legacy Veo (`veo3_fast` /
    `veo3` / `veo3_lite`) paths and Emergent's Nano Banana for cheap
    image generation.
    """
    if not body.prompt.strip():
        raise HTTPException(400, "Prompt cannot be empty")
    conv = await load_conv_or_403(conv_id, u["id"])
    info = _kie_info(body.model)
    media_type = body.media_type or (info["type"] if info else "image")

    # Special-case the existing Nano Banana path (Emergent LLM Key) and
    # the existing Veo legacy keys — we already have battle-tested
    # endpoints for those, so fall through here rather than duplicating.
    if body.model == "nano-banana":
        from server import GenerateImageBody as _GIB
        legacy = _GIB(prompt=body.prompt, agent_id=None)
        return await generate_image(conv_id, legacy, u)
    if body.model in ("veo3", "veo3_fast", "veo3_lite"):
        legacy = GenerateVideoBody(
            prompt=body.prompt,
            aspect_ratio=body.aspect_ratio or "16:9",
        )
        return await generate_video_endpoint(conv_id, legacy, u)

    if not info:
        raise HTTPException(
            400,
            f"Unknown model `{body.model}`. Allowed kie.ai keys: "
            + ", ".join(sorted(_KIE_PROVIDERS.keys())),
        )

    # Persist the user's request line so the thread reads naturally
    icon = "🎵" if media_type == "music" else ("🎬" if media_type == "video" else "🎨")
    await db.messages.insert_one({
        "id": str(uuid.uuid4()),
        "conversation_id": conv_id,
        "sender_type": "user",
        "sender_id": u["id"],
        "sender_name": u["name"],
        "text": f"{icon} {body.prompt}",
        "intent": f"{media_type}_request",
        "timestamp": datetime.now(timezone.utc),
    })

    # Pick an agent for the response bubble
    agent_id = (conv.get("agent_ids") or [None])[0] or "specialized-chief-of-staff"
    agent = AGENTS.get(agent_id) or {"name": "Studio"}

    # Per-type ETA copy so the spinner has the right vibe
    eta_copy = {
        "image": "Generating your image — usually 10-40 seconds.",
        "video": "Generating your video — this usually takes 60–180s.",
        "music": "Composing your track — usually 30–120 seconds.",
    }
    placeholder = {
        "id": str(uuid.uuid4()),
        "conversation_id": conv_id,
        "sender_type": "agent",
        "sender_id": agent_id,
        "sender_name": agent["name"],
        "agent_id": agent_id,
        "text": eta_copy.get(media_type, "Working on it…"),
        "media_type": media_type,
        "media_status": "pending",
        "media_prompt": body.prompt[:500],
        "media_model": body.model,
        "aspect_ratio": body.aspect_ratio,
        "timestamp": datetime.now(timezone.utc),
    }
    await db.messages.insert_one({**placeholder})
    asyncio.create_task(_poll_and_save_kie_media(
        placeholder["id"],
        model=body.model,
        prompt=body.prompt,
        media_type=media_type,
        image_url=body.image_url,
        extra=body.extra,
    ))
    return {k: v for k, v in placeholder.items() if k != "_id"}


@app.get("/api/media/models")
async def list_media_models(_: dict = Depends(get_current_user)):
    """Expose the kie.ai marketplace registry to the frontend so the
    Generate-Media modal can render a dynamic model picker. Provider
    names are intentionally omitted from the response — users don't need
    to know whether a model lives on kie.ai or Emergent's universal key.
    """
    rows = []
    # Chat models — map to friendly display names
    for key, label in (
        ("gpt-4.1-mini",     "GPT-4.1 Mini (fast & cheap)"),
        ("gpt-5.2",          "GPT-5.2 (deep reasoning)"),
        ("claude-sonnet-4-5", "Claude Sonnet 4.5"),
        ("claude-opus-4-5",  "Claude Opus 4.5 (highest quality)"),
        ("claude-haiku-4-5", "Claude Haiku 4.5 (fastest)"),
        ("gemini-3-pro",     "Gemini 3 Pro"),
        ("gemini-3-flash",   "Gemini 3 Flash (fast)"),
    ):
        rows.append({"key": key, "label": label, "type": "chat"})
    for key, info in _KIE_PROVIDERS.items():
        rows.append({"key": key, "label": info["label"], "type": info["type"]})
    rows.append({"key": "nano-banana", "label": "Nano Banana (default)",
                 "type": "image"})
    rows.append({"key": "veo3_fast", "label": "Veo 3.1 Fast (default)",
                 "type": "video"})
    rows.append({"key": "veo3", "label": "Veo 3.1", "type": "video"})
    return {"models": rows}


# Allowlist for chat attachments — superset of org-asset list, but no .zip
# (we don't want to dump a binary archive into a vision model).
ALLOWED_ATTACH_EXTS = (
    IMAGE_EXTS | PDF_EXTS | DOCX_EXTS | TEXT_LIKE_EXTS |
    {"mp3", "wav", "m4a", "ogg", "flac", "aac"} |
    {"mp4", "mov", "webm", "avi", "mkv"} |
    {"xlsx", "pptx", "rtf"}  # accepted but not parsed for text
)

async def _agent_review_attachment(
    conv: dict, team: dict, agent_id: str, user_text: str,
    attachment: dict, file_path: Path, current_user_name: str,
) -> tuple[str, list]:
    """One LLM turn that reviews an uploaded file.

    For images: sends the file as multimodal vision input. For text-bearing
    files: injects the extracted text. For everything else: just acknowledges
    the file by name + size and answers the user's question if any.
    """
    org_assets_brief: List[dict] = []
    org_doc: Optional[dict] = None
    try:
        org_assets_brief = await load_assets_brief(team["org_id"])
    except Exception:
        pass
    try:
        org_doc = await db.organizations.find_one({"_id": ObjectId(team["org_id"])})
    except Exception:
        pass

    system = build_system_prompt(agent_id, team, current_user_name=current_user_name,
                                 org_assets=org_assets_brief, org=org_doc)
    system += (
        "\n\nThe user has just attached a file for you to review. Open with a "
        "one-line confirmation of what you received, then deliver the analysis "
        "they asked for (or, if no specific question was asked, a concise but "
        "useful breakdown of the file). Keep it tight."
    )

    kind = attachment["kind"]
    file_label = attachment["original_name"]
    extracted = attachment.get("attachment_text") or ""
    ask = (user_text or "").strip() or "Take a look and tell me what stands out."

    if kind == "image":
        # Multimodal vision call
        try:
            from emergentintegrations.llm.chat import LlmChat as _LlmChat, UserMessage as _UM, ImageContent as _IC
            img_b64 = base64.b64encode(file_path.read_bytes()).decode("ascii")
            chat = _LlmChat(
                api_key=EMERGENT_LLM_KEY,
                session_id=f"vision:{conv['_id']}:{agent_id}",
                system_message=system,
            ).with_model("openai", "gpt-4.1-mini")
            msg = _UM(
                text=f"User uploaded `{file_label}`. {ask}",
                file_contents=[_IC(image_base64=img_b64)],
            )
            text = await chat.send_message(msg)
            return text or "", []
        except Exception as exc:
            logger.warning("vision review failed, falling back: %s", exc)
            # Fall through to a text-only acknowledgement below

    prompt_parts = [f"User uploaded `{file_label}` ({kind})."]
    if extracted:
        snippet = extracted[:8000]
        prompt_parts.append(f"\nExtracted text:\n---\n{snippet}\n---")
        if len(extracted) > 8000:
            prompt_parts.append("(text truncated)")
    elif kind in ("audio", "video"):
        prompt_parts.append(
            "(This is a media file — no transcription available in this turn. "
            "Acknowledge it and offer to dig deeper if the user shares context.)"
        )
    elif kind not in ("image",):
        prompt_parts.append("(No text extractable. Acknowledge by name and ask for context.)")
    prompt_parts.append(f"\nUser's question: {ask}")
    prompt = "\n".join(prompt_parts)

    chat = LlmChat(
        api_key=EMERGENT_LLM_KEY,
        session_id=f"upload:{conv['_id']}:{agent_id}",
        system_message=system,
    ).with_model("openai", "gpt-4.1-mini")
    try:
        text = await chat.send_message(UserMessage(text=prompt))
    except Exception as exc:
        logger.exception("upload-review LLM error: %s", exc)
        return "I got the file but couldn't process it just now — try again in a moment.", []
    return text or "", []

@conv_r.post("/conversations/{conv_id}/upload")
async def upload_to_conversation(
    conv_id: str,
    text: str = Form(""),
    files: List[UploadFile] = File(...),
    u: dict = Depends(get_current_user),
):
    """Attach one or more files to a conversation. Persists each file,
    posts a single user message with the full `attachments` list, and
    triggers an agent reply that:

    - For multiple images with an edit verb → composes them (image-1 as the
      base canvas, image-2/3/... layered/incorporated) via Nano Banana.
    - For a single image with an edit verb → edits it (existing behaviour).
    - For images without an edit verb → vision-describes the first one.
    - For text-bearing files (PDF/DOCX/TXT/MD/CSV/JSON) → extracts text from
      ALL of them and lets the agent answer questions across them.
    - For other binaries → acknowledges them by name.
    """
    if not files:
        raise HTTPException(400, "At least one file is required")
    if len(files) > 6:
        raise HTTPException(400, "Up to 6 files per message")

    conv = await load_conv_or_403(conv_id, u["id"])
    team = await db.teams.find_one({"_id": ObjectId(conv["team_id"])})
    if not team:
        raise HTTPException(404, "Team not found")

    folder = CONV_UPLOAD_DIR / conv_id
    folder.mkdir(parents=True, exist_ok=True)
    attachments: List[dict] = []   # serialised attachment metadata for the response
    file_paths: List[Path] = []    # on-disk paths in upload order
    extracted_blocks: List[str] = []  # text extracts from text-bearing files
    total_size = 0

    for upload in files:
        original = (upload.filename or "file").strip().replace("/", "_").replace("\\", "_")
        ext = original.rsplit(".", 1)[-1].lower() if "." in original else ""
        if ext not in ALLOWED_ATTACH_EXTS:
            raise HTTPException(400, f"Unsupported file type: .{ext or '?'}")
        blob = await upload.read()
        if not blob:
            raise HTTPException(400, f"Empty file: {original}")
        total_size += len(blob)
        if len(blob) > MAX_CONV_UPLOAD_SIZE or total_size > MAX_CONV_UPLOAD_SIZE * 2:
            raise HTTPException(400, f"File(s) too large (max {MAX_CONV_UPLOAD_SIZE // (1024 * 1024)} MB each)")

        fname = f"{uuid.uuid4().hex}.{ext}"
        fpath = folder / fname
        await persist_bytes(fpath, blob, original_name=original,
                            mime=upload.content_type or "")
        file_paths.append(fpath)

        kind = _attachment_kind(ext)
        if is_text_extractable(ext):
            try:
                snippet = trim_text(extract_asset_text(fpath, ext), limit=4000)
                if snippet:
                    extracted_blocks.append(f"--- {original} ---\n{snippet}")
            except Exception as exc:
                logger.warning("attachment text extract failed for %s: %s", original, exc)

        attachments.append({
            "filename": fname,
            "url": f"/api/files/conv/{conv_id}/{fname}",
            "mime": upload.content_type or "",
            "kind": kind,
            "size": len(blob),
            "original_name": original,
        })

    # Aggregate file kind for routing & rendering — "all images" lights up the
    # composition path; mixed kinds fall through to the regular review path.
    image_paths = [p for p, a in zip(file_paths, attachments) if a["kind"] == "image"]
    is_all_images = len(image_paths) == len(file_paths) and len(image_paths) >= 1
    primary = attachments[0]
    extracted_text = "\n\n".join(extracted_blocks)

    now = datetime.now(timezone.utc)
    user_doc = {
        "id": str(uuid.uuid4()),
        "conversation_id": conv_id,
        "sender_type": "user",
        "sender_id": u["id"],
        "sender_name": u["name"],
        "agent_id": None,
        "text": (text or "").strip() or (
            f"📎 {primary['original_name']}" + (f" + {len(attachments) - 1} more" if len(attachments) > 1 else "")
        ),
        "intent": "attachment",
        "timestamp": now,
        "attachments": attachments,
        # Legacy single-file mirror for older renderers — points at the first file
        "attachment_filename": primary["filename"],
        "attachment_url": primary["url"],
        "attachment_mime": primary["mime"],
        "attachment_kind": primary["kind"],
        "attachment_size": primary["size"],
        "attachment_original_name": primary["original_name"],
    }
    await db.messages.insert_one({**user_doc})

    # Pick a responder. For groups → smart-route to the best specialist for
    # this kind of file. For direct → the conversation's agent.
    if conv["type"] == "group":
        try:
            chosen = await route_best_agent(
                team,
                f"Review {len(attachments)} {primary['kind']} file(s): "
                f"{', '.join(a['original_name'] for a in attachments)}\n"
                f"{(text or '').strip()}\n{extracted_text[:600]}",
                exclude_ids=None, transcript="",
            )
        except Exception:
            chosen = (conv.get("agent_ids") or [None])[0]
        if chosen and chosen not in (conv.get("agent_ids") or []):
            await db.conversations.update_one(
                {"_id": ObjectId(conv_id)},
                {"$addToSet": {"agent_ids": chosen}},
            )
            conv["agent_ids"] = list(conv.get("agent_ids", [])) + [chosen]
        responder = chosen or (conv.get("agent_ids") or [None])[0]
    else:
        responder = (conv.get("agent_ids") or [None])[0]

    if not responder or responder not in AGENTS:
        return {
            "user_message": {k: v for k, v in user_doc.items() if k != "_id"},
            "agent_messages": [],
            "ok": True,
        }

    # ── Edit / compose-image fast path ────────────────────────────────────
    # If the user attached image(s) AND the prompt contains a clear edit /
    # composition verb, feed ALL images to Nano Banana and return a freshly
    # composed result (intent=image_edit_reply). With multiple images this
    # is the "add my logo to this background" composition use case.
    edit_intent = (
        is_all_images
        and (_quick_image_edit_intent(text or "", True) or len(image_paths) >= 2)
    )
    if edit_intent:
        try:
            sp = team.get("soulprint") or {}
            sp_lines = [f"{k.capitalize()}: {sp[k]}"
                        for k in ("mission", "tone", "audience", "values") if sp.get(k)]
            n = len(image_paths)
            if n >= 2:
                sys_msg = (
                    f"You are {AGENTS[responder]['name']}, composing a single new image "
                    f"from {n} reference images the user uploaded. Treat IMAGE 1 as the "
                    f"base canvas and incorporate the elements from IMAGE 2"
                    + (f" through {n}" if n > 2 else "")
                    + " into it precisely. Preserve the base composition + style; "
                    "place the additional elements naturally; render any text crisply.\n"
                    + "\n".join(sp_lines)
                )
                prompt_intro = (
                    f"{n} images are attached in order. Apply this composition / edit: "
                    f"{text or 'Combine them into a single coherent image.'}"
                )
            else:
                sys_msg = (
                    f"You are {AGENTS[responder]['name']}, editing an image the user uploaded. "
                    f"Apply the requested change precisely while preserving the rest of the image, "
                    f"its composition, and its visual style. Render any requested text crisply.\n"
                    + "\n".join(sp_lines)
                )
                prompt_intro = f"Source image is attached. Apply this edit: {text}"
            new_fname, caption = await generate_image_via_nano_banana(
                prompt_intro, sys_msg, input_image_paths=image_paths,
            )
            edited_msg = {
                "id": str(uuid.uuid4()),
                "conversation_id": conv_id,
                "sender_type": "agent",
                "sender_id": responder,
                "sender_name": AGENTS[responder]["name"],
                "agent_id": responder,
                "text": (caption or "Here's the result. Want any tweaks?").strip(),
                "media_type": "image",
                "media_filename": new_fname,
                "media_url": f"/api/files/{new_fname}",
                "intent": "image_edit_reply",
                "timestamp": datetime.now(timezone.utc),
            }
            await db.messages.insert_one({**edited_msg})
            fresh = await db.conversations.find_one({"_id": ObjectId(conv_id)})
            return {
                "user_message": {k: v for k, v in user_doc.items() if k != "_id"},
                "agent_messages": [{k: v for k, v in edited_msg.items() if k != "_id"}],
                "conversation": serialize_conv(fresh) if fresh else None,
                "ok": True,
            }
        except Exception as exc:
            logger.warning("image edit/compose fast-path failed, falling back to review: %s", exc)

    # ── Vision / text review path ─────────────────────────────────────────
    reply, _sources = await _agent_review_attachment(
        conv, team, responder, text or "",
        {
            "kind": primary["kind"],
            "original_name": primary["original_name"]
                             + (f" (+{len(attachments)-1} more)" if len(attachments) > 1 else ""),
            "attachment_mime": primary["mime"],
            "attachment_text": extracted_text,
        },
        file_paths[0], u["name"],
    )
    msg_id = str(uuid.uuid4())
    clean_reply, art_attach = await process_agent_artifacts(
        reply or "Got the file — let me know what to dig into.",
        conv=conv, agent_id=responder, message_id=msg_id, created_by=u["id"],
    )
    agent_msg = {
        "id": msg_id,
        "conversation_id": conv_id,
        "sender_type": "agent",
        "sender_id": responder,
        "sender_name": AGENTS[responder]["name"],
        "agent_id": responder,
        "text": clean_reply,
        "intent": "attachment_reply",
        "timestamp": datetime.now(timezone.utc),
    }
    if art_attach:
        agent_msg["attachments"] = art_attach
    await db.messages.insert_one({**agent_msg})

    fresh = await db.conversations.find_one({"_id": ObjectId(conv_id)})
    return {
        "user_message": {k: v for k, v in user_doc.items() if k != "_id"},
        "agent_messages": [{k: v for k, v in agent_msg.items() if k != "_id"}],
        "conversation": serialize_conv(fresh) if fresh else None,
        "ok": True,
    }

# ── Org assets — uploads, listing, deletion, serving ────────────────────────
assets_r = APIRouter(prefix="/api")

# Larger filename regex for asset files (uuid hex + arbitrary extension)
ASSET_FILENAME_RE = re.compile(r"^[a-f0-9]{16,}\.[a-z0-9]{1,8}$", re.IGNORECASE)
MAX_ASSET_SIZE = 10 * 1024 * 1024  # 10 MB
ALLOWED_ASSET_EXTS = (
    {"png", "jpg", "jpeg", "webp", "gif", "svg",
     "pdf", "docx", "doc",
     "txt", "md", "json", "csv", "log",
     "mp3", "wav", "m4a",
     "mp4", "mov", "webm",
     "zip"}
)

def kind_for_ext(ext: str) -> str:
    e = ext.lower()
    if e in IMAGE_EXTS:    return "image"
    if e in PDF_EXTS:      return "pdf"
    if e in DOCX_EXTS:     return "doc"
    if e in TEXT_LIKE_EXTS: return "text"
    if e in {"mp3", "wav", "m4a"}:    return "audio"
    if e in {"mp4", "mov", "webm"}:   return "video"
    return "file"

def serialize_asset(a: dict) -> dict:
    return {
        "id": str(a["_id"]),
        "org_id": a["org_id"],
        "original_name": a["original_name"],
        "filename": a["filename"],
        "kind": a.get("kind", "file"),
        "ext": a.get("ext", ""),
        "mime": a.get("mime", ""),
        "size": a.get("size", 0),
        "description": a.get("description", ""),
        "category": a.get("category", ""),
        "usable_in_social": bool(a.get("usable_in_social", False)),
        "uploaded_by": a.get("uploaded_by", ""),
        "uploaded_by_name": a.get("uploaded_by_name", ""),
        "uploaded_at": a["uploaded_at"].isoformat() if isinstance(a["uploaded_at"], datetime) else a["uploaded_at"],
        "url": f"/api/files/assets/{a['filename']}",
        "has_text": bool(a.get("text_excerpt")),
    }

@assets_r.post("/orgs/{org_id}/assets")
async def upload_asset(
    org_id: str,
    description: str = Form(""),
    category: str = Form(""),       # product|lifestyle|team|venue|logo_variant|other
    usable_in_social: bool = Form(True),  # allow agents to pick this for post cover images
    file: UploadFile = File(...),
    u: dict = Depends(get_current_user),
):
    await require_org_member(org_id, u["id"])
    original_name = (file.filename or "file").strip().replace("/", "_").replace("\\", "_")
    ext = original_name.rsplit(".", 1)[-1].lower() if "." in original_name else ""
    if ext not in ALLOWED_ASSET_EXTS:
        raise HTTPException(400, f"Unsupported file type: .{ext or '?'}")

    # Stream up to MAX_ASSET_SIZE to disk
    org_dir = ASSETS_DIR / org_id
    org_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid.uuid4().hex}.{ext}"
    path = org_dir / filename
    total = 0
    with path.open("wb") as out:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_ASSET_SIZE:
                out.close()
                path.unlink(missing_ok=True)
                raise HTTPException(413, "File too large (10 MB max)")
            out.write(chunk)

    text_excerpt = ""
    if is_text_extractable(ext):
        raw = extract_asset_text(path, ext)
        text_excerpt = trim_text(raw, limit=4000)

    # Media Library categorisation — helps agents and the Social composer
    # pick an on-brand asset for a given post topic. Whitelist on write so
    # unknown values don't leak into the filter UI.
    _VALID_CATEGORIES = {"product", "lifestyle", "team", "venue", "logo_variant", "other", ""}
    safe_category = category.strip().lower() if category.strip().lower() in _VALID_CATEGORIES else ""
    doc = {
        "org_id": org_id,
        "filename": filename,
        "original_name": original_name,
        "ext": ext,
        "mime": file.content_type or "application/octet-stream",
        "size": total,
        "kind": kind_for_ext(ext),
        "description": description.strip()[:500],
        "category": safe_category,
        "usable_in_social": bool(usable_in_social) and kind_for_ext(ext) == "image",
        "uploaded_by": u["id"],
        "uploaded_by_name": u["name"],
        "uploaded_at": datetime.now(timezone.utc),
        "text_excerpt": text_excerpt,
    }
    res = await db.assets.insert_one(doc)
    doc["_id"] = res.inserted_id
    return serialize_asset(doc)

@assets_r.get("/orgs/{org_id}/assets")
async def list_assets(org_id: str, u: dict = Depends(get_current_user)):
    await require_org_member(org_id, u["id"])
    cur = db.assets.find({"org_id": org_id}).sort("uploaded_at", -1)
    out = [serialize_asset(a) async for a in cur]
    return out

@assets_r.patch("/orgs/{org_id}/assets/{asset_id}")
async def update_asset(
    org_id: str, asset_id: str,
    description: Optional[str] = Form(None),
    category: Optional[str] = Form(None),
    usable_in_social: Optional[bool] = Form(None),
    u: dict = Depends(get_current_user),
):
    await require_org_member(org_id, u["id"])
    try:
        existing = await db.assets.find_one({"_id": ObjectId(asset_id), "org_id": org_id})
    except Exception:
        raise HTTPException(400, "Invalid asset id")
    if not existing:
        raise HTTPException(404, "Asset not found")
    update = {}
    if description is not None:
        update["description"] = description.strip()[:500]
    if category is not None:
        _VALID_CATS = {"product", "lifestyle", "team", "venue", "logo_variant", "other", ""}
        c = category.strip().lower()
        update["category"] = c if c in _VALID_CATS else ""
    if usable_in_social is not None:
        # Only images can be picked by the social composer.
        update["usable_in_social"] = bool(usable_in_social) and existing.get("kind") == "image"
    if update:
        await db.assets.update_one({"_id": ObjectId(asset_id)}, {"$set": update})
    fresh = await db.assets.find_one({"_id": ObjectId(asset_id)})
    return serialize_asset(fresh)

@assets_r.delete("/orgs/{org_id}/assets/{asset_id}")
async def delete_asset(org_id: str, asset_id: str, u: dict = Depends(get_current_user)):
    ctx = await require_org_member(org_id, u["id"])
    try:
        existing = await db.assets.find_one({"_id": ObjectId(asset_id), "org_id": org_id})
    except Exception:
        raise HTTPException(400, "Invalid asset id")
    if not existing:
        raise HTTPException(404, "Asset not found")
    if existing["uploaded_by"] != u["id"] and ctx["member"]["role"] not in ("owner", "admin"):
        raise HTTPException(403, "Only the uploader or org admins can delete")
    path = ASSETS_DIR / org_id / existing["filename"]
    path.unlink(missing_ok=True)
    await db.assets.delete_one({"_id": ObjectId(asset_id)})
    return {"ok": True}

# ── Auth-checked file streaming for generated media ──────────────────────────
files_r = APIRouter(prefix="/api/files")

# ── Unfurl (rich link previews) ─────────────────────────────────────────────
unfurl_r = APIRouter(prefix="/api")

_OG_PATTERNS = {
    "title": [
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+name=["\']twitter:title["\'][^>]+content=["\']([^"\']+)["\']',
        r'<title[^>]*>([^<]+)</title>',
    ],
    "description": [
        r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+name=["\']twitter:description["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)["\']',
    ],
    "image": [
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']',
    ],
    "site_name": [
        r'<meta[^>]+property=["\']og:site_name["\'][^>]+content=["\']([^"\']+)["\']',
    ],
}

def _first_match(patterns: List[str], html: str) -> str:
    for p in patterns:
        m = re.search(p, html, re.IGNORECASE | re.DOTALL)
        if m:
            return _html_unescape(m.group(1).strip())[:500]
    return ""

def _html_unescape(s: str) -> str:
    import html as _html
    return _html.unescape(s)

def _absolute_url(maybe_relative: str, base: str) -> str:
    if not maybe_relative:
        return ""
    if maybe_relative.startswith(("http://", "https://", "data:")):
        return maybe_relative
    try:
        from urllib.parse import urljoin
        return urljoin(base, maybe_relative)
    except Exception:
        return maybe_relative

@unfurl_r.get("/unfurl")
async def unfurl(url: str, u: dict = Depends(get_current_user)):
    """Fetch a URL and return a rich preview (title/description/image/favicon).

    Results are cached in `unfurl_cache` for 24h to avoid hammering origins
    when the same source is rendered across many messages."""
    target = (url or "").strip()
    if not target:
        raise HTTPException(400, "url required")
    if not target.startswith(("http://", "https://")):
        target = "https://" + target

    cached = await db.unfurl_cache.find_one({"url": target})
    now = datetime.now(timezone.utc)
    if cached and cached.get("fetched_at"):
        fa = cached["fetched_at"]
        # MongoDB occasionally hands us a tz-naive datetime depending on
        # driver settings — coerce both sides to UTC-aware before diffing.
        if fa.tzinfo is None:
            fa = fa.replace(tzinfo=timezone.utc)
        if (now - fa).total_seconds() < 86400:
            return {k: v for k, v in cached.items() if k != "_id" and k != "fetched_at"}

    preview = {"url": target, "title": "", "description": "", "image": "", "site_name": "", "favicon": "", "host": ""}
    try:
        from urllib.parse import urlparse
        host = urlparse(target).netloc
        preview["host"] = host
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True,
                                    headers={
                                        "User-Agent": "Mozilla/5.0 (compatible; FoundryBot/1.0; +https://foundryagents.ai/bot)",
                                        "Accept": "text/html,application/xhtml+xml",
                                        "Accept-Language": "en-US,en;q=0.9",
                                    }) as client:
            resp = await client.get(target)
        if resp.status_code < 400:
            html = resp.text or ""
            # Truncate to head only — meta tags live there and we don't need 5MB pages
            head_match = re.search(r"<head[^>]*>(.*?)</head>", html, re.IGNORECASE | re.DOTALL)
            head = head_match.group(1) if head_match else html[:50000]
            for key, patterns in _OG_PATTERNS.items():
                preview[key] = _first_match(patterns, head)
            if preview["image"]:
                preview["image"] = _absolute_url(preview["image"], target)
            # Favicon: link rel="icon" or fallback /favicon.ico
            fav = re.search(
                r'<link[^>]+rel=["\'](?:icon|shortcut icon|apple-touch-icon)["\'][^>]+href=["\']([^"\']+)["\']',
                head, re.IGNORECASE,
            )
            if fav:
                preview["favicon"] = _absolute_url(fav.group(1).strip(), target)
            else:
                preview["favicon"] = f"https://{host}/favicon.ico" if host else ""
        else:
            logger.info("unfurl %s -> %s", target, resp.status_code)
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("unfurl error %s: %s", target, exc)

    # Persist (upsert) so we don't refetch for 24h
    await db.unfurl_cache.update_one(
        {"url": target},
        {"$set": {**preview, "fetched_at": now}},
        upsert=True,
    )
    return preview

ALLOWED_FILENAME_RE = re.compile(r"^([a-z]+-)?[a-f0-9][a-f0-9\-]{14,}\.(png|jpg|jpeg|webp|gif|pdf)$", re.IGNORECASE)

@files_r.get("/assets/{filename}")
async def serve_org_asset(filename: str):
    """Serve an org-uploaded asset by its UUID-style filename.

    Same capability-URL pattern as `serve_file`: the random filename is the
    cap. Auth is dropped so `<img>` tags can render asset thumbnails inline
    in the org page (they cannot send Bearer tokens). The DB lookup keeps
    this from being a generic file dump. Falls back to GridFS on missing
    disk file (redeploy survival, Feb 2026)."""
    if not ASSET_FILENAME_RE.match(filename):
        raise HTTPException(400, "Invalid filename")
    asset = await db.assets.find_one({"filename": filename})
    if not asset:
        raise HTTPException(404, "Asset not found")
    path = ASSETS_DIR / asset["org_id"] / asset["filename"]
    original = asset.get("original_name") or filename
    mime = asset.get("mime")
    if path.is_file():
        return FileResponse(path, filename=original, media_type=mime)
    blob, m2, o2 = await recover_bytes_from_gridfs(filename)
    if blob is not None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(blob)
        except Exception:
            pass
        from fastapi.responses import Response  # noqa: E402
        headers = {"Content-Disposition": f'attachment; filename="{o2 or original}"'}
        return Response(content=blob, media_type=m2 or mime or "application/octet-stream", headers=headers)
    raise HTTPException(404, "File missing on disk")

CONV_ATTACH_FILENAME_RE = re.compile(r"^[a-f0-9]{16,}\.[a-z0-9]{1,8}$", re.IGNORECASE)

@files_r.get("/conv/{conv_id}/{filename}")
async def serve_conv_attachment(conv_id: str, filename: str):
    """Serve an in-conversation file upload.

    Capability-URL pattern (random uuid filename inside a random conv id),
    no auth so `<img>` / `<video>` can render inline. Defensive lookup
    confirms the message exists before serving. Falls back to GridFS when
    the on-disk file vanishes after a redeploy."""
    if not CONV_ATTACH_FILENAME_RE.match(filename):
        raise HTTPException(400, "Invalid filename")
    # Lookup must handle THREE attachment-storage shapes:
    #  (1) legacy single-file — root-level `attachment_filename`
    #  (2) multi-file uploads — file lives in `attachments[*].filename`
    #  (3) agent-generated images written into a conversation's attachments
    # Without this, any file past the first one in a multi-file upload 404ed.
    msg = await db.messages.find_one({
        "conversation_id": conv_id,
        "$or": [
            {"attachment_filename": filename},
            {"attachments.filename": filename},
        ],
    })
    if not msg:
        raise HTTPException(404, "Not linked to any message")
    # Resolve per-file metadata — prefer the exact attachments[*] match
    # so the download gets the right original name / mime.
    original = msg.get("attachment_original_name") or filename
    mime = msg.get("attachment_mime") or None
    for att in (msg.get("attachments") or []):
        if (att or {}).get("filename") == filename:
            original = att.get("original_name") or original
            mime = att.get("mime") or mime
            break
    path = CONV_UPLOAD_DIR / conv_id / filename
    if path.is_file():
        return FileResponse(path, filename=original, media_type=mime)
    blob, m2, o2 = await recover_bytes_from_gridfs(filename)
    if blob is not None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(blob)
        except Exception:
            pass
        from fastapi.responses import Response  # noqa: E402
        headers = {"Content-Disposition": f'attachment; filename="{o2 or original}"'}
        return Response(content=blob, media_type=m2 or mime or "application/octet-stream", headers=headers)
    raise HTTPException(404, "File missing on disk")

@files_r.get("/{filename}")
async def serve_file(filename: str):
    """Serve a chat-generated media file by its UUID4 filename.

    The filenames are 122-bit-random UUID4s emitted only inside messages of
    conversations the user is a member of, so the URL itself is the
    capability. Auth is intentionally NOT enforced here so that <img> tags
    (which cannot send Bearer tokens and may have their cookies dropped by
    third-party browsers) can render images inline.

    Falls back to MongoDB GridFS via the global ``media_files`` map when
    the on-disk file is missing — the most common cause is a production
    redeploy wiping ephemeral container storage (Feb 2026)."""
    if not ALLOWED_FILENAME_RE.match(filename):
        raise HTTPException(400, "Invalid filename")
    path = UPLOAD_DIR / filename

    # Defensive ownership check — confirm this filename was actually
    # written by an agent reply, brand asset, social post, OR a multi-file
    # chat attachment (the latter missed the root `media_filename` check
    # before Feb 2026 and 404ed despite the file being on disk).
    msg = await db.messages.find_one({
        "$or": [
            {"media_filename": filename},
            {"attachments.filename": filename},
        ],
    })
    if not msg:
        org = await db.organizations.find_one({
            "$or": [
                {"brand_assets.primary_logo.filename": filename},
                {"brand_assets.alt_logo.filename": filename},
            ]
        })
        if not org:
            sp = await db.social_posts.find_one({
                "$or": [
                    {"image.filename": filename},
                    {"image.raw_filename": filename},
                ]
            })
            if not sp:
                au = await db.business_audits.find_one({
                    "$or": [
                        {"pdf_filename": filename},
                        # Multi-viewport audit screenshots (mobile/tablet/desktop)
                        # generated by ``audit_signals.capture_viewport_screenshots``
                        # — same capability-URL pattern as the PDF, just nested
                        # one level deeper in the doc.
                        {"tech_signals.screenshots.shots.filename": filename},
                    ],
                })
                if not au:
                    raise HTTPException(404, "Not linked to any message")

    if path.is_file():
        return FileResponse(path)

    # Disk gone — try GridFS via the global map.
    blob, mime, _orig = await recover_bytes_from_gridfs(filename)
    if blob is not None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(blob)
        except Exception:
            pass
        from fastapi.responses import Response  # noqa: E402
        return Response(content=blob, media_type=mime or "application/octet-stream")
    raise HTTPException(404, "File not found")

# ── Schedules — cron-style automation for prompts/agents ───────────────────
from apscheduler.schedulers.asyncio import AsyncIOScheduler  # noqa: E402
from apscheduler.triggers.cron import CronTrigger  # noqa: E402
from apscheduler.triggers.date import DateTrigger  # noqa: E402
try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None  # type: ignore

scheduler = AsyncIOScheduler(timezone="UTC")
schedules_r = APIRouter(prefix="/api")

def _serialize_schedule(s: dict) -> dict:
    def _iso(v):
        if isinstance(v, datetime):
            return v.isoformat()
        return v
    return {
        "id": str(s["_id"]),
        "title": s.get("title", ""),
        "prompt": s.get("prompt", ""),
        "scope": s.get("scope", "conversation"),
        "target_id": s.get("target_id", ""),
        "org_id": s.get("org_id", ""),
        "cron": s.get("cron"),
        "run_at": _iso(s.get("run_at")),
        "timezone": s.get("timezone", "UTC"),
        "active": bool(s.get("active", True)),
        "created_by": s.get("created_by", ""),
        "created_at": _iso(s.get("created_at")),
        "last_run_at": _iso(s.get("last_run_at")),
        "last_status": s.get("last_status", ""),
        "next_run_at": _iso(s.get("next_run_at")),
    }

def _schedule_job_id(sched_id: str) -> str:
    return f"sp:{sched_id}"

def _build_trigger(s: dict):
    """Return an APScheduler trigger for the schedule doc, or None if invalid."""
    tz = s.get("timezone") or "UTC"
    if s.get("cron"):
        try:
            return CronTrigger.from_crontab(s["cron"], timezone=tz)
        except Exception as exc:
            logger.warning("Bad cron %s: %s", s.get("cron"), exc)
            return None
    run_at = s.get("run_at")
    if run_at:
        try:
            if isinstance(run_at, str):
                # Accept "YYYY-MM-DDTHH:MM" plus TZ-aware ISO strings
                run_at = datetime.fromisoformat(run_at.replace("Z", "+00:00"))
            if run_at.tzinfo is None and ZoneInfo:
                run_at = run_at.replace(tzinfo=ZoneInfo(tz))
            return DateTrigger(run_date=run_at)
        except Exception as exc:
            logger.warning("Bad run_at %s: %s", s.get("run_at"), exc)
            return None
    return None

async def _run_schedule(sched_id: str) -> None:
    """Worker: fetch the schedule, resolve the target conversation, post the
    prompt as a synthetic user message and let the existing send_message flow
    generate the agent reply."""
    try:
        s = await db.schedules.find_one({"_id": ObjectId(sched_id)})
    except Exception:
        return
    if not s or not s.get("active"):
        return
    conv_id = await _resolve_target_conv(s)
    if not conv_id:
        await db.schedules.update_one(
            {"_id": ObjectId(sched_id)},
            {"$set": {"last_run_at": datetime.now(timezone.utc), "last_status": "skipped: no target conversation"}},
        )
        return
    try:
        conv = await db.conversations.find_one({"_id": ObjectId(conv_id)})
        if not conv:
            return
        team = await db.teams.find_one({"_id": ObjectId(conv["team_id"])})
        if not team:
            return
        creator = await db.users.find_one({"_id": ObjectId(s["created_by"])})
        creator_name = (creator or {}).get("name") or "Scheduler"
        now = datetime.now(timezone.utc)
        user_doc = {
            "id": str(uuid.uuid4()),
            "conversation_id": conv_id,
            "sender_type": "user",
            "sender_id": s["created_by"],
            "sender_name": creator_name,
            "agent_id": None,
            "text": f"⏰ {s.get('title') or 'Scheduled task'}\n\n{s['prompt']}",
            "intent": "scheduled",
            "schedule_id": sched_id,
            "timestamp": now,
        }
        await db.messages.insert_one({**user_doc})

        # Pick a responder. Direct → the agent; group → first agent (auto-respond).
        responders = list(conv.get("agent_ids") or [])[:1]
        for aid in responders:
            if aid not in AGENTS:
                continue
            reply, web_sources = await generate_agent_reply(
                conv, team, aid, user_doc["text"], creator_name,
                latest_user_id=s.get("created_by") or "",
            )
            msg_id = str(uuid.uuid4())
            clean_reply, artifacts = await process_agent_artifacts(
                reply, conv=conv, agent_id=aid, message_id=msg_id, created_by=s["created_by"],
            )
            msg = {
                "id": msg_id,
                "conversation_id": conv_id,
                "sender_type": "agent",
                "sender_id": aid,
                "sender_name": AGENTS[aid]["name"],
                "agent_id": aid,
                "text": clean_reply,
                "schedule_id": sched_id,
                "timestamp": datetime.now(timezone.utc),
            }
            if web_sources:
                msg["web_sources"] = web_sources
            if artifacts:
                msg["attachments"] = artifacts
            await db.messages.insert_one({**msg})

        # Compute next run for UI
        next_run = None
        job = scheduler.get_job(_schedule_job_id(sched_id))
        if job and job.next_run_time:
            next_run = job.next_run_time
        await db.schedules.update_one(
            {"_id": ObjectId(sched_id)},
            {"$set": {
                "last_run_at": datetime.now(timezone.utc),
                "last_status": "ok",
                "next_run_at": next_run,
            }},
        )
    except Exception as exc:
        logger.exception("Schedule %s failed: %s", sched_id, exc)
        await db.schedules.update_one(
            {"_id": ObjectId(sched_id)},
            {"$set": {"last_run_at": datetime.now(timezone.utc), "last_status": f"error: {exc}"[:200]}},
        )

async def _resolve_target_conv(s: dict) -> Optional[str]:
    """Resolve the conversation a schedule should post into, based on scope."""
    scope = s.get("scope", "conversation")
    target = s.get("target_id")
    if not target:
        return None
    if scope == "conversation":
        return target
    if scope == "team":
        # Use the first conversation in the team that the creator is in
        c = await db.conversations.find_one(
            {"team_id": target, "member_ids": s["created_by"]},
            sort=[("created_at", 1)],
        )
        return str(c["_id"]) if c else None
    if scope == "org":
        # Use the org's General conversation
        try:
            org = await db.organizations.find_one({"_id": ObjectId(target)})
        except Exception:
            return None
        if org and org.get("general_conversation_id"):
            return org["general_conversation_id"]
    return None

def _scheduler_register(s: dict) -> None:
    """Register/refresh the APScheduler job for a schedule doc."""
    sched_id = str(s["_id"])
    job_id = _schedule_job_id(sched_id)
    try:
        scheduler.remove_job(job_id)
    except Exception:
        pass
    if not s.get("active"):
        return
    trigger = _build_trigger(s)
    if not trigger:
        return
    scheduler.add_job(_run_schedule, trigger=trigger, id=job_id, args=[sched_id], replace_existing=True)

def _scheduler_unregister(sched_id: str) -> None:
    try:
        scheduler.remove_job(_schedule_job_id(sched_id))
    except Exception:
        pass

async def _require_target_access(scope: str, target_id: str, user_id: str) -> str:
    """Permission gate: confirm the user can target this object, return its org_id."""
    if scope == "conversation":
        try:
            c = await db.conversations.find_one({"_id": ObjectId(target_id)})
        except Exception:
            raise HTTPException(400, "Invalid conversation id")
        if not c:
            raise HTTPException(404, "Conversation not found")
        if user_id not in (c.get("member_ids") or []):
            raise HTTPException(403, "Not a participant in this conversation")
        team = await db.teams.find_one({"_id": ObjectId(c["team_id"])})
        return team["org_id"] if team else ""
    if scope == "team":
        ctx = await require_team_member(target_id, user_id)
        return ctx["team"]["org_id"]
    if scope == "org":
        await require_org_member(target_id, user_id)
        return target_id
    raise HTTPException(400, "Unknown scope")

@schedules_r.post("/schedules")
async def create_schedule(body: ScheduleCreateBody, u: dict = Depends(get_current_user)):
    if not body.title.strip() or not body.prompt.strip():
        raise HTTPException(400, "Title and prompt are required")
    if not body.cron and not body.run_at:
        raise HTTPException(400, "Provide either cron or run_at")
    if body.cron and body.run_at:
        raise HTTPException(400, "Provide cron OR run_at, not both")
    org_id = await _require_target_access(body.scope, body.target_id, u["id"])
    doc = {
        "title": body.title.strip()[:120],
        "prompt": body.prompt.strip()[:4000],
        "scope": body.scope,
        "target_id": body.target_id,
        "org_id": org_id,
        "cron": body.cron.strip() if body.cron else None,
        "run_at": body.run_at,
        "timezone": (body.timezone or "UTC").strip()[:80] or "UTC",
        "active": body.active,
        "created_by": u["id"],
        "created_at": datetime.now(timezone.utc),
        "last_run_at": None,
        "last_status": "",
    }
    res = await db.schedules.insert_one(doc)
    doc["_id"] = res.inserted_id
    _scheduler_register(doc)
    job = scheduler.get_job(_schedule_job_id(str(res.inserted_id)))
    if job and job.next_run_time:
        await db.schedules.update_one({"_id": res.inserted_id}, {"$set": {"next_run_at": job.next_run_time}})
        doc["next_run_at"] = job.next_run_time
    return _serialize_schedule(doc)

@schedules_r.get("/schedules")
async def list_schedules(
    scope: Optional[str] = None,
    target_id: Optional[str] = None,
    org_id: Optional[str] = None,
    u: dict = Depends(get_current_user),
):
    """List the caller's schedules. If `org_id` given (and caller is a member),
    return all schedules in that org. If `target_id` + `scope`, scope to that.
    Otherwise return schedules created by the caller."""
    q: dict = {}
    if org_id:
        await require_org_member(org_id, u["id"])
        q["org_id"] = org_id
    elif scope and target_id:
        await _require_target_access(scope, target_id, u["id"])
        q.update({"scope": scope, "target_id": target_id})
    else:
        q["created_by"] = u["id"]
    cur = db.schedules.find(q).sort("created_at", -1)
    out = [_serialize_schedule(s) async for s in cur]
    return out

@schedules_r.patch("/schedules/{sched_id}")
async def update_schedule(sched_id: str, body: ScheduleUpdateBody, u: dict = Depends(get_current_user)):
    try:
        s = await db.schedules.find_one({"_id": ObjectId(sched_id)})
    except Exception:
        raise HTTPException(400, "Invalid id")
    if not s:
        raise HTTPException(404, "Schedule not found")
    if s["created_by"] != u["id"]:
        raise HTTPException(403, "Only the creator can edit a schedule")
    update: dict = {}
    if body.title is not None:
        update["title"] = body.title.strip()[:120]
    if body.prompt is not None:
        update["prompt"] = body.prompt.strip()[:4000]
    if body.cron is not None:
        update["cron"] = body.cron.strip() or None
        update["run_at"] = None
    if body.run_at is not None:
        update["run_at"] = body.run_at
        update["cron"] = None
    if body.timezone is not None:
        update["timezone"] = body.timezone.strip()[:80] or "UTC"
    if body.active is not None:
        update["active"] = bool(body.active)
    if update:
        await db.schedules.update_one({"_id": ObjectId(sched_id)}, {"$set": update})
    fresh = await db.schedules.find_one({"_id": ObjectId(sched_id)})
    _scheduler_register(fresh)
    job = scheduler.get_job(_schedule_job_id(sched_id))
    if job and job.next_run_time:
        await db.schedules.update_one({"_id": ObjectId(sched_id)}, {"$set": {"next_run_at": job.next_run_time}})
        fresh = await db.schedules.find_one({"_id": ObjectId(sched_id)})
    return _serialize_schedule(fresh)

@schedules_r.delete("/schedules/{sched_id}")
async def delete_schedule(sched_id: str, u: dict = Depends(get_current_user)):
    try:
        s = await db.schedules.find_one({"_id": ObjectId(sched_id)})
    except Exception:
        raise HTTPException(400, "Invalid id")
    if not s:
        raise HTTPException(404, "Schedule not found")
    if s["created_by"] != u["id"]:
        raise HTTPException(403, "Only the creator can delete this schedule")
    _scheduler_unregister(sched_id)
    await db.schedules.delete_one({"_id": ObjectId(sched_id)})
    return {"ok": True}

@schedules_r.post("/schedules/{sched_id}/run-now")
async def run_schedule_now(sched_id: str, u: dict = Depends(get_current_user)):
    try:
        s = await db.schedules.find_one({"_id": ObjectId(sched_id)})
    except Exception:
        raise HTTPException(400, "Invalid id")
    if not s:
        raise HTTPException(404, "Schedule not found")
    if s["created_by"] != u["id"]:
        raise HTTPException(403, "Only the creator can trigger this schedule")
    asyncio.create_task(_run_schedule(sched_id))
    return {"ok": True}

# ── Register routers & CORS ──────────────────────────────────────────────────
base_r = APIRouter(prefix="/api")

@base_r.get("/health")
async def health():
    return {"status": "ok"}


# ── About / marketing copy ────────────────────────────────────────────────
# Single-source-of-truth markdown lives at /app/memory/ABOUT.md so it can
# be updated without redeploying. Read on every request — file is tiny.
_ABOUT_MD_PATH = Path(__file__).resolve().parent.parent / "memory" / "ABOUT.md"


@base_r.get("/about")
async def get_about():
    """Marketing-grade overview of the platform. Rendered in-app on /about
    and reused for marketing material. Public — no auth required."""
    try:
        md = _ABOUT_MD_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        md = "# Foundry\n\nAbout content not configured."
    return {"markdown": md}

app.include_router(base_r)
app.include_router(auth_r)
app.include_router(org_r)
app.include_router(invite_r)

# Composio integration — per-user managed OAuth + tool execution.
# Routes mounted under /api/orgs/{org_id}/composio so visibility is
# automatically scoped by the existing require_org_member gate.
composio_r = APIRouter(prefix="/api/orgs")


@composio_r.get("/{org_id}/composio/toolkits")
async def composio_list_toolkits(org_id: str, u: dict = Depends(get_current_user)):
    """Featured toolkits + this user's existing connections. Drives the
    above-the-fold Connections panel. Use `/composio/catalog` for the
    full searchable list."""
    from composio_integration import (
        list_featured_toolkits, list_connections, is_available,
    )
    await require_org_member(org_id, u["id"])
    if not is_available():
        return {"available": False, "toolkits": [], "connections": []}
    return {
        "available": True,
        "toolkits": list_featured_toolkits(),
        "connections": await list_connections(db, org_id, u["id"]),
    }


class _ComposioConnectBody(BaseModel):
    toolkit: str
    nickname: Optional[str] = None


@composio_r.post("/{org_id}/composio/connect")
async def composio_connect(
    org_id: str,
    body: _ComposioConnectBody,
    request: Request,
    u: dict = Depends(get_current_user),
):
    """Begin OAuth for a toolkit. Returns the Composio-hosted redirect URL."""
    from composio_integration import initiate_connection, is_available
    await require_org_member(org_id, u["id"])
    if not is_available():
        raise HTTPException(503, "Composio is not configured on this deployment")
    # Build callback URL from the user's *actual* current origin so the
    # OAuth round-trip lands them back where they started — works on
    # production custom domains, preview hosts, anywhere. Falls back to
    # env vars only when no Origin header is present (e.g. curl tests).
    origin = (request.headers.get("origin") or "").rstrip("/")
    if not origin:
        ref = request.headers.get("referer") or ""
        if ref:
            from urllib.parse import urlparse
            p = urlparse(ref)
            if p.scheme and p.netloc:
                origin = f"{p.scheme}://{p.netloc}"
    frontend_base = origin or (
        os.environ.get("FRONTEND_BASE_URL")
        or os.environ.get("REACT_APP_BACKEND_URL")
        or ""
    ).rstrip("/")
    callback_url = f"{frontend_base}/app/org/{org_id}?composio_callback=1#connections"
    try:
        return await initiate_connection(
            db, org_id, u["id"], body.toolkit, callback_url,
            nickname=body.nickname,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        logger.exception("Composio connect failed")
        raise HTTPException(500, f"Failed to start OAuth: {exc}")


@composio_r.get("/{org_id}/composio/catalog")
async def composio_catalog(
    org_id: str,
    q: str = "",
    u: dict = Depends(get_current_user),
):
    """Full Composio toolkit catalog (~250 entries) for the Browse-all
    picker. Featured slugs are flagged so the UI can pin them. Toolkits
    we've previously discovered to require user-provided OAuth credentials
    (BYOA — e.g. Shopify) are flagged `byoa_only=true` so the UI can
    render them as not-yet-supported instead of letting the user click
    Connect and hit a 400. Optional `q` query filters by slug or name."""
    from composio_integration import (
        list_all_toolkits, list_featured_toolkits, is_available,
    )
    await require_org_member(org_id, u["id"])
    if not is_available():
        return {"available": False, "toolkits": []}
    featured = {t["slug"] for t in list_featured_toolkits()}
    all_tk = await list_all_toolkits()
    if q:
        lc = q.lower()
        all_tk = [
            t for t in all_tk
            if lc in (t.get("slug") or "").lower()
            or lc in (t.get("name") or "").lower()
        ]
    # Fetch BYOA-only sentinels once and stamp them onto matching toolkits.
    byoa_rows = await db.composio_auth_configs.find(
        {"byoa_only": True}, {"_id": 0, "toolkit": 1, "byoa_reason": 1},
    ).to_list(500)
    byoa_lookup = {
        r["toolkit"]: r.get("byoa_reason") or "" for r in byoa_rows
    }
    for t in all_tk:
        slug = t.get("slug") or ""
        t["featured"] = slug in featured
        if slug in byoa_lookup:
            t["byoa_only"] = True
            t["byoa_reason"] = byoa_lookup[slug]
    # Featured first, then non-BYOA, then alphabetical by name. BYOA
    # toolkits sink to the bottom so they don't crowd out workable ones.
    all_tk.sort(key=lambda t: (
        not t.get("featured"),
        bool(t.get("byoa_only")),
        (t.get("name") or "").lower(),
    ))
    return {"available": True, "toolkits": all_tk[:300]}


class _ComposioNicknameBody(BaseModel):
    nickname: str


@composio_r.put("/{org_id}/composio/connections/{connection_id}/nickname")
async def composio_set_nickname(
    org_id: str,
    connection_id: str,
    body: _ComposioNicknameBody,
    u: dict = Depends(get_current_user),
):
    """Rename a connected account (e.g. 'Work Gmail', 'Personal Gmail')."""
    from composio_integration import set_connection_nickname
    await require_org_member(org_id, u["id"])
    try:
        return await set_connection_nickname(
            db, org_id, u["id"], connection_id, body.nickname,
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@composio_r.post("/{org_id}/composio/connections/{connection_id}/refresh")
async def composio_refresh(
    org_id: str,
    connection_id: str,
    u: dict = Depends(get_current_user),
):
    """Pull latest status from Composio (called after the OAuth redirect lands).

    Also re-runs metadata discovery for Facebook / Instagram so existing
    ACTIVE connections that pre-date the discovery code can pick up their
    Page name / @handle without forcing the user to reconnect.
    """
    from composio_integration import refresh_connection_status, composio_user_id
    await require_org_member(org_id, u["id"])
    row = await db.composio_connections.find_one(
        {"connection_id": connection_id, "composio_user_id": composio_user_id(org_id, u["id"])},
    )
    if not row:
        raise HTTPException(404, "Connection not found")
    try:
        result = await refresh_connection_status(db, connection_id)
    except Exception as exc:
        logger.exception("Composio refresh failed")
        raise HTTPException(500, f"Failed to refresh status: {exc}")
    # Already-ACTIVE FB/IG connections won't enter the first-activation
    # branch in refresh_connection_status, so explicitly run discovery
    # here for back-fill. Idempotent — early-returns if already cached.
    if row.get("toolkit") in ("facebook", "instagram") and row.get("status") == "ACTIVE":
        try:
            from social_publishing import discover_connection_metadata
            meta = await discover_connection_metadata(
                db, org_id=org_id, user_id=u["id"], toolkit=row["toolkit"],
            )
            if meta:
                result["account_metadata"] = meta
        except Exception as exc:
            logger.warning("connection metadata back-fill failed: %s", exc)
    return result


@composio_r.get("/{org_id}/composio/connections/{connection_id}/available-targets")
async def composio_available_targets(
    org_id: str,
    connection_id: str,
    u: dict = Depends(get_current_user),
):
    """List every Facebook Page this connection can manage, plus the
    linked Instagram Business account for each Page. Used by the
    Meta-target picker UI so the user can pin a specific Page (and
    its IG) as the active publishing target for this org.

    Only valid for `toolkit` in {facebook, instagram}. For Instagram
    connections we walk the connection's parent FB connection (Composio
    issues IG through the linked Page's token), so the list shows the
    same Pages either way — picking a Page implicitly selects the IG
    account linked to it.
    """
    from composio_integration import composio_user_id
    from social_publishing import list_available_meta_targets
    await require_org_member(org_id, u["id"])
    cu = composio_user_id(org_id, u["id"])
    row = await db.composio_connections.find_one(
        {"connection_id": connection_id, "composio_user_id": cu},
    )
    if not row:
        raise HTTPException(404, "Connection not found")
    if row.get("toolkit") not in ("facebook", "instagram"):
        raise HTTPException(400, "Page targeting is only supported for Facebook / Instagram connections.")
    # IG-only connections still need a FB connection in this org to
    # query managed pages. If there's no FB row, prompt the user.
    fb_row = row
    if row.get("toolkit") == "instagram":
        fb_row = await db.composio_connections.find_one(
            {"composio_user_id": cu, "toolkit": "facebook", "status": "ACTIVE"},
        )
        if not fb_row:
            raise HTTPException(
                400,
                "Connect Facebook for this org first. Instagram Business publishing requires "
                "a Facebook Page connection — Meta wires IG through the linked Page's token.",
            )
    try:
        targets = await list_available_meta_targets(db, org_id, u["id"], fb_row)
    except Exception as exc:
        logger.exception("Meta targets lookup failed")
        raise HTTPException(500, f"Failed to list Meta targets: {exc}")
    return targets


@composio_r.post("/{org_id}/composio/connections/{connection_id}/select-target")
async def composio_select_target(
    org_id: str,
    connection_id: str,
    body: Dict[str, Any] = Body(...),
    u: dict = Depends(get_current_user),
):
    """Pin a Facebook Page (and its linked IG account) as the active
    publishing target for this org's Meta connections.

    Body: `{"page_id": "<fb_page_id>"}`. Selecting a Page implicitly
    selects whatever IG Business account that Page links to. Picking
    a Page with no linked IG clears the IG cache so we don't publish
    to a stale IG account.
    """
    from composio_integration import composio_user_id
    from social_publishing import select_meta_target
    await require_org_member(org_id, u["id"])
    cu = composio_user_id(org_id, u["id"])
    row = await db.composio_connections.find_one(
        {"connection_id": connection_id, "composio_user_id": cu},
    )
    if not row:
        raise HTTPException(404, "Connection not found")
    if row.get("toolkit") not in ("facebook", "instagram"):
        raise HTTPException(400, "Page targeting is only supported for Facebook / Instagram connections.")
    page_id = str((body or {}).get("page_id") or "").strip()
    if not page_id:
        raise HTTPException(400, "Provide a page_id to pin as the active target.")
    fb_row = row
    if row.get("toolkit") == "instagram":
        fb_row = await db.composio_connections.find_one(
            {"composio_user_id": cu, "toolkit": "facebook", "status": "ACTIVE"},
        )
        if not fb_row:
            raise HTTPException(400, "Connect Facebook for this org first.")
    try:
        result = await select_meta_target(db, org_id, u["id"], fb_row, page_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        logger.exception("Select Meta target failed")
        raise HTTPException(500, f"Failed to pin target: {exc}")
    return result



@composio_r.delete("/{org_id}/composio/connections/{connection_id}")
async def composio_disconnect(
    org_id: str,
    connection_id: str,
    u: dict = Depends(get_current_user),
):
    """Revoke a connection. The composio_integration module enforces
    (org, user) ownership before talking to Composio."""
    from composio_integration import disconnect_connection
    await require_org_member(org_id, u["id"])
    try:
        await disconnect_connection(db, org_id, u["id"], connection_id)
        return {"status": "disconnected"}
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    except Exception as exc:
        logger.exception("Composio disconnect failed")
        raise HTTPException(500, f"Failed to disconnect: {exc}")


class _ComposioExecuteBody(BaseModel):
    tool_slug: str
    arguments: Dict = Field(default_factory=dict)


@composio_r.post("/{org_id}/composio/execute")
async def composio_execute(
    org_id: str,
    body: _ComposioExecuteBody,
    u: dict = Depends(get_current_user),
):
    """Run a single Composio tool action on behalf of the caller.

    Any org member can call this against their OWN connected accounts —
    no admin gate, because the credentials being used belong to them.
    Telemetry lands in `llm_calls` with `kind_label="composio:<toolkit>:<slug>"`.
    """
    from composio_integration import execute_tool, is_available
    await require_org_member(org_id, u["id"])
    if not is_available():
        raise HTTPException(503, "Composio is not configured on this deployment")
    if not body.tool_slug.strip():
        raise HTTPException(400, "tool_slug is required")
    try:
        return await execute_tool(db, org_id, u["id"], body.tool_slug, body.arguments)
    except Exception as exc:
        logger.exception("Composio execute failed")
        raise HTTPException(500, f"Failed to execute tool: {exc}")


@composio_r.get("/{org_id}/agent-tool-prefs")
async def list_agent_tool_prefs(
    org_id: str,
    u: dict = Depends(get_current_user),
):
    """List which agents (built-in + custom) have Composio tools enabled
    for this org. Every member can see the list; only admins can flip.

    Default for any agent without an explicit row is ON — every agent
    can use the org's connected toolkits unless someone has explicitly
    toggled it OFF. Matches the gating logic in
    `composio_agent.is_agent_tools_enabled`.
    """
    ctx = await require_org_member(org_id, u["id"])
    rows = await db.agent_tool_prefs.find(
        {"org_id": org_id},
        {"_id": 0, "agent_id": 1, "tools_enabled": 1, "updated_at": 1},
    ).to_list(500)
    # Map by agent_id, preserving the raw value so we can distinguish
    # "no row → default ON" from "row with tools_enabled=False → OFF".
    prefs = {r["agent_id"]: r for r in rows}
    # Surface the full agent catalogue so the UI can render every agent
    # (built-in + this org's custom agents) with a toggle in one shot.
    agents_out: List[Dict[str, object]] = []
    for aid, meta in AGENTS.items():
        if meta.get("org_id") and meta.get("org_id") != org_id:
            continue  # custom agent from another org — invisible
        row = prefs.get(aid)
        if row is None:
            enabled_val = True  # default ON
        else:
            enabled_val = row.get("tools_enabled") is not False
        agents_out.append({
            "agent_id": aid,
            "name": meta.get("name"),
            "role": meta.get("role"),
            "division_key": meta.get("division_key"),
            "tools_enabled": enabled_val,
            "is_custom": bool(meta.get("org_id")),
        })
    agents_out.sort(key=lambda a: (a["is_custom"], a["name"] or ""))
    is_admin = ctx["member"].get("role") in ("owner", "admin")
    return {"agents": agents_out, "can_edit": is_admin}


class _AgentToolPrefBody(BaseModel):
    tools_enabled: bool


@composio_r.put("/{org_id}/agent-tool-prefs/{agent_id}")
async def set_agent_tool_pref(
    org_id: str,
    agent_id: str,
    body: _AgentToolPrefBody,
    u: dict = Depends(get_current_user),
):
    """Flip the Composio tools toggle for one agent in this org. Admin-gated."""
    ctx = await require_org_member(org_id, u["id"])
    if ctx["member"].get("role") not in ("owner", "admin"):
        raise HTTPException(403, "Only org owners/admins can change agent tool prefs")
    if agent_id not in AGENTS:
        raise HTTPException(404, "Unknown agent")
    await db.agent_tool_prefs.update_one(
        {"org_id": org_id, "agent_id": agent_id},
        {"$set": {
            "org_id": org_id,
            "agent_id": agent_id,
            "tools_enabled": bool(body.tools_enabled),
            "updated_at": datetime.now(timezone.utc),
            "updated_by": u["id"],
        }},
        upsert=True,
    )
    return {"agent_id": agent_id, "tools_enabled": bool(body.tools_enabled)}


@composio_r.get("/{org_id}/agent-tool-recommendations")
async def agent_tool_recommendations(
    org_id: str,
    u: dict = Depends(get_current_user),
):
    """Suggest tool-enabled agents based on each agent's division +
    which toolkits the caller has actually connected.

    Since every agent is now ON by default, this only returns
    suggestions for agents the user has EXPLICITLY toggled OFF that
    match a connected toolkit — useful as a "want to re-enable these?"
    nudge after a fresh connection lands.
    """
    from composio_integration import (
        recommended_toolkits_for_division, list_connections,
    )
    await require_org_member(org_id, u["id"])
    conns = await list_connections(db, org_id, u["id"])
    connected_slugs = {c["toolkit"] for c in conns if c.get("status") == "ACTIVE"}
    if not connected_slugs:
        return {"recommendations": [], "connected_toolkits": []}
    out: List[Dict[str, object]] = []
    explicit_off_rows = await db.agent_tool_prefs.find(
        {"org_id": org_id, "tools_enabled": False},
        {"_id": 0, "agent_id": 1},
    ).to_list(500)
    explicitly_off = {r["agent_id"] for r in explicit_off_rows}
    for aid, meta in AGENTS.items():
        if meta.get("org_id") and meta.get("org_id") != org_id:
            continue
        if aid not in explicitly_off:
            continue  # already implicitly ON
        recs = recommended_toolkits_for_division(meta.get("division_key", ""))
        matched = [s for s in recs if s in connected_slugs]
        if matched:
            out.append({
                "agent_id": aid,
                "name": meta.get("name"),
                "division_key": meta.get("division_key"),
                "matched_toolkits": matched,
            })
    out.sort(key=lambda r: (-len(r["matched_toolkits"]), r["name"] or ""))
    return {
        "recommendations": out,
        "connected_toolkits": sorted(connected_slugs),
    }


class _ApplyRecsBody(BaseModel):
    agent_ids: List[str]


@composio_r.post("/{org_id}/agent-tool-prefs/apply-recommendations")
async def apply_recommendations(
    org_id: str,
    body: _ApplyRecsBody,
    u: dict = Depends(get_current_user),
):
    """Bulk-enable tools for a list of agents. Admin-gated."""
    ctx = await require_org_member(org_id, u["id"])
    if ctx["member"].get("role") not in ("owner", "admin"):
        raise HTTPException(403, "Only org owners/admins can change agent tool prefs")
    valid = [aid for aid in body.agent_ids if aid in AGENTS]
    if not valid:
        return {"updated": 0}
    now = datetime.now(timezone.utc)
    ops = [
        {
            "filter": {"org_id": org_id, "agent_id": aid},
            "update": {"$set": {
                "org_id": org_id,
                "agent_id": aid,
                "tools_enabled": True,
                "updated_at": now,
                "updated_by": u["id"],
            }},
            "upsert": True,
        }
        for aid in valid
    ]
    for op in ops:
        await db.agent_tool_prefs.update_one(
            op["filter"], op["update"], upsert=op["upsert"],
        )
    return {"updated": len(valid), "agent_ids": valid}


app.include_router(composio_r)


# Phase-1 Telegram integration (direct Bot API — bypasses Composio so
# we don't burn tool-call credits on proactive nudges).
from telegram_routes import register_telegram_routes  # noqa: E402
_telegram_router = APIRouter(prefix="/api")
register_telegram_routes(
    _telegram_router,
    db,
    get_current_user=get_current_user,
)
app.include_router(_telegram_router)


# Self-serve 24-hour trial — request a code, redeem it as registration,
# clock starts at redeem time. See trial.py.
from trial import make_trial_router  # noqa: E402
from workspaces import _public_origin  # noqa: E402
from access_codes import make_access_codes_router  # noqa: E402
app.include_router(make_access_codes_router(
    db=db,
    get_current_user=get_current_user,
    get_user_or_none=get_user_or_none,
))
app.include_router(make_trial_router(
    db=db,
    set_auth_cookies=set_auth_cookies,
    create_access_token=create_access_token,
    public_origin=_public_origin,
))
# Workspace layer (NEW) — sits above Orgs. See workspaces.py.
from workspaces import (  # noqa: E402
    make_workspaces_router,
    make_workspace_invite_accept_router,
    migrate_existing_orgs_to_workspaces,
)
app.include_router(make_workspaces_router(
    db=db, get_current_user=get_current_user,
    require_org_member=require_org_member,
))
app.include_router(make_workspace_invite_accept_router(
    db=db, get_current_user=get_current_user,
))
app.include_router(team_r)
app.include_router(conv_r)
app.include_router(assets_r)
app.include_router(files_r)
app.include_router(unfurl_r)
app.include_router(schedules_r)

from soul_suggestions import make_soul_suggestions_router  # noqa: E402
app.include_router(make_soul_suggestions_router(
    db=db,
    require_org_member=require_org_member,
    get_current_user=get_current_user,
))

# User-scoped Memory (Phase 1) — CRUD over /api/memory.
from memory_routes import make_memory_router  # noqa: E402
app.include_router(make_memory_router(
    db=db, get_current_user=get_current_user,
))

from social_posts import make_social_router  # noqa: E402
from brand import compose_logo_on_image as _compose_logo  # noqa: E402
social_r = make_social_router(
    db=db, AGENTS=AGENTS, EMERGENT_LLM_KEY=EMERGENT_LLM_KEY,
    LlmChat=LlmChat, UserMessage=UserMessage,
    require_org_member=require_org_member,
    get_current_user=get_current_user,
    route_best_agent=route_best_agent,
    load_conv_or_403=load_conv_or_403,
    generate_image_via_nano_banana=generate_image_via_nano_banana,
    compose_logo_on_image=_compose_logo,
    upload_dir=UPLOAD_DIR,
    recover_bytes_from_gridfs=recover_bytes_from_gridfs,
)
app.include_router(social_r)

# ── Custom agents — user-defined agents per org (Temper+ feature) ─────────
from custom_agents import (  # noqa: E402
    router as _custom_agents_router,
    init as _init_custom_agents,
    load_into_catalogue as _load_custom_agents,
)
_init_custom_agents(
    db=db,
    agents_dict=AGENTS,
    get_current_user=get_current_user,
    require_org_member=require_org_member,
    LlmChat=LlmChat,
    UserMessage=UserMessage,
    EMERGENT_LLM_KEY=EMERGENT_LLM_KEY,
)
app.include_router(_custom_agents_router)

# ── Unified org-wide search + AI synthesis (Spotlight / ⌘K) ───────────────
from search import (  # noqa: E402
    router as _search_router,
    init as _init_search,
)
_init_search(
    db=db,
    AGENTS=AGENTS,
    get_current_user=get_current_user,
    require_org_member=require_org_member,
    LlmChat=LlmChat,
    UserMessage=UserMessage,
    EMERGENT_LLM_KEY=EMERGENT_LLM_KEY,
)
app.include_router(_search_router)

from activity import make_activity_router  # noqa: E402
activity_r = make_activity_router(
    db=db, AGENTS=AGENTS,
    require_org_member=require_org_member,
    get_current_user=get_current_user,
)
app.include_router(activity_r)

# Admin dashboard — superadmin-only. Logging hook is installed BEFORE we
# include the router so any LLM call made during request handling already
# emits telemetry to db.llm_calls.
from llm_logging import install_logging as _install_llm_logging  # noqa: E402
_install_llm_logging(db)

from admin import make_admin_router, ensure_superadmin_seed  # noqa: E402
app.include_router(make_admin_router(db=db, get_current_user=get_current_user))

# ── Business Audit prospecting tool (superadmin-only + public gated page) ──
from audits import make_audits_router  # noqa: E402
from artifact_synthesis import _render_pdf as _audit_render_pdf  # noqa: E402
from brave_search import brave_web_search as _audit_brave_search  # noqa: E402
from browser_tools import browse_url as _audit_browse_url  # noqa: E402

async def _require_superadmin_dep(u: dict = Depends(get_current_user)) -> dict:
    """Mirror of the closure inside admin.py, exposed at module scope so
    the audits router can import it without re-implementing the rule."""
    if not u.get("is_superadmin"):
        raise HTTPException(403, "Superadmin only")
    return u

_audit_admin_router, _audit_public_router, _audit_app_router, _audit_recover_orphans, _audit_backfill_platform_name = make_audits_router(
    db=db,
    get_current_user=get_current_user,
    require_superadmin=_require_superadmin_dep,
    require_org_member=require_org_member,
    LlmChat=LlmChat, UserMessage=UserMessage,
    EMERGENT_LLM_KEY=EMERGENT_LLM_KEY,
    brave_web_search=_audit_brave_search,
    browse_url=_audit_browse_url,
    persist_bytes=persist_bytes,
    upload_dir=UPLOAD_DIR,
    render_pdf=_audit_render_pdf,
)
app.include_router(_audit_admin_router)
app.include_router(_audit_public_router)
app.include_router(_audit_app_router)

# ── Privacy / GDPR endpoints ───────────────────────────────────────────────
# Data export (Article 15 / 20) + right-to-erasure (Article 17). The
# module is self-contained; `schedule_privacy_jobs` registers the daily
# housekeeping + deletion-sweep jobs into our APScheduler instance.
from privacy import make_privacy_router, schedule_privacy_jobs  # noqa: E402
app.include_router(make_privacy_router(get_current_user=get_current_user, db=db))

# ── Forge Crawl HTTP endpoints ─────────────────────────────────────────────
# Thin wrappers over forge_crawl.py so the frontend can call our in-house
# Firecrawl alternative directly (mission tools already expose the same
# capability for the LLM).
from forge_routes import make_forge_router  # noqa: E402
app.include_router(make_forge_router(
    get_current_user=get_current_user,
    LlmChat=LlmChat, UserMessage=UserMessage,
    EMERGENT_LLM_KEY=EMERGENT_LLM_KEY,
))

# ── GitHub OAuth integration (per-user + per-org) ──────────────────────────
# Lets users connect GitHub once → mission agents can read repos, open
# PRs, comment, push. Self-disables when GITHUB_CLIENT_ID isn't set, so
# preview deploys without the secret won't expose half-broken UI.
from github_integration import make_github_router as _make_github_router  # noqa: E402
from workspaces import _public_origin as _gh_public_origin  # noqa: E402
app.include_router(_make_github_router(
    db=db,
    get_current_user=get_current_user,
    require_org_member=require_org_member,
    _public_origin=_gh_public_origin,
))

@app.on_event("startup")
async def _seed_superadmins():
    """Stamp configured emails as superadmin if their accounts already
    exist. New sign-ups with these emails are promoted in the register
    handler. Idempotent — safe to run on every boot."""
    # FIRST: lock in a persistent JWT secret so tokens survive any
    # subsequent redeployment of this container.
    await _ensure_persistent_jwt_secret()
    await ensure_superadmin_seed(db, SUPERADMIN_EMAILS)
    # Helpful index for the LLM telemetry queries.
    try:
        await db.llm_calls.create_index([("created_at", -1)])
        await db.llm_calls.create_index([("org_id", 1), ("created_at", -1)])
        await db.llm_calls.create_index([("model", 1), ("created_at", -1)])
    except Exception:
        pass
    # Load any persisted custom agents into the in-memory catalogue so
    # they're usable immediately after boot. Idempotent.
    try:
        n = await _load_custom_agents()
        if n:
            logger.info("Loaded %d custom agents from DB into catalogue", n)
    except Exception as exc:
        logger.warning("Could not load custom agents: %s", exc)
    # Recover any audits orphaned by the previous boot (status stuck at
    # 'generating' with no live task) — re-kick them so they finish or
    # cleanly fail rather than hanging forever.
    try:
        await _audit_recover_orphans()
    except Exception as exc:
        logger.warning("Audit orphan recovery failed: %s", exc)
    # Sweep legacy audits to replace any "SoulPrint" / "SoulPrint Engine"
    # text with "Foundry". Idempotent — the next boot finds nothing.
    try:
        await _audit_backfill_platform_name()
    except Exception as exc:
        logger.warning("Audit platform-name backfill failed: %s", exc)
    # Grandfather any pre-existing users into the site access gate so
    # the gate is invite-only for NEW signups, never a friction for
    # existing customers. Idempotent — only touches rows missing
    # `access_granted_at`.
    try:
        from access_codes import grandfather_existing_users
        n = await grandfather_existing_users(db)
        if n:
            logger.info("Grandfathered %d existing users into the access gate", n)
    except Exception as exc:
        logger.warning("Grandfather migration skipped: %s", exc)


@app.on_event("startup")
async def _migrate_general_to_chief_of_staff():
    """One-shot rename of the legacy 'General' team / conversation name to
    'Chief of Staff' for existing orgs. Uses ``is_general=True`` as the
    authoritative selector so we only rewrite the auto-created anchor
    team, never a user-created team that happens to be called 'General'.
    Idempotent — does nothing on orgs already renamed.
    """
    try:
        r1 = await db.teams.update_many(
            {"is_general": True, "name": "General"},
            {"$set": {"name": "Chief of Staff",
                      "description": "Always-on chat with your Chief of Staff — the anchor agent for the whole company"}},
        )
        r2 = await db.conversations.update_many(
            {"is_general": True, "title": "General"},
            {"$set": {"title": "Chief of Staff"}},
        )
        if r1.modified_count or r2.modified_count:
            logger.info(
                "Migrated General -> Chief of Staff: %d team(s), %d conversation(s).",
                r1.modified_count, r2.modified_count,
            )
    except Exception as exc:
        logger.warning("Chief of Staff rename migration failed: %s", exc)


@app.on_event("startup")
async def _heal_private_cos_convs():
    """Ensure every existing private-CoS thread has the fields it needs
    to actually produce a reply when the user sends a message.

    Background — the original team_access.create helpers stamp
    ``auto_respond=True`` + ``agent_ids=[]`` (relying on smart-expand to
    pull in CoS on the first message). When the router LLM ever failed
    or the conv was created before that flag existed, ``agent_ids``
    stayed empty AND no responder was picked, so the user's messages
    saved silently with no agent reply (reported by ben@archeforge.com,
    Feb 2026). The runtime safety net handles new messages on legacy
    convs; this migration heals them proactively so the FIRST message
    after deploy doesn't have to do it. Idempotent.
    """
    try:
        cos_id = "specialized-chief-of-staff"
        res = await db.conversations.update_many(
            {"kind": "private_cos"},
            [
                {"$set": {
                    "auto_respond": True,
                    "agent_ids": {
                        "$cond": [
                            {"$in": [cos_id, {"$ifNull": ["$agent_ids", []]}]},
                            "$agent_ids",
                            {"$concatArrays": [
                                {"$ifNull": ["$agent_ids", []]},
                                [cos_id],
                            ]},
                        ]
                    },
                }},
            ],
        )
        if res.modified_count:
            logger.info(
                "Healed %d private-CoS conv(s): auto_respond=True + CoS in agent_ids.",
                res.modified_count,
            )
    except Exception as exc:
        logger.warning("Private CoS heal migration failed: %s", exc)

# ── Conversation summary (rolling AI recap for long threads) ──────────
from conversation_summary import make_summary_router as _make_summary_r  # noqa: E402
summary_r = _make_summary_r(
    db=db, llm_chat_cls=LlmChat, user_message_cls=UserMessage,
    emergent_key=EMERGENT_LLM_KEY,
    get_current_user=get_current_user,
    load_conv_or_403=load_conv_or_403,
)
app.include_router(summary_r)


# Org Documents library — populated automatically when agents emit
# [[ARTIFACT]] blocks in their replies. See artifacts.py.
docs_r = make_documents_router(
    db=db, docs_root=DOCS_DIR,
    require_org_member=require_org_member,
    get_current_user=get_current_user,
)
app.include_router(docs_r)

# Public app-artifact share router — capability-URL serving for the
# interactive HTML/JS apps agents generate. See app_artifacts.py.
from app_artifacts import make_share_router as _make_app_share_router  # noqa: E402
app.include_router(_make_app_share_router(
    db, docs_root=DOCS_DIR, read_blob_from_gridfs=read_blob_from_gridfs,
))

# Social Style Guide — analyzes the org's social profiles + samples to
# produce a structured brand-voice + visual-style guide that's auto-injected
# into every social post draft and every agent's system prompt. See
# style_guide.py.
style_r = make_style_guide_router(
    db=db, AGENTS=AGENTS, EMERGENT_LLM_KEY=EMERGENT_LLM_KEY,
    LlmChat=LlmChat, UserMessage=UserMessage,
    require_org_member=require_org_member,
    get_current_user=get_current_user,
)
app.include_router(style_r)


# ── Mission Mode — autonomous multi-step agent workflows ───────────────────
def _build_mission_tools_factory():
    """Return freshly-built tools each call (they're stateless but cheap)."""
    return _build_mission_tools(
        db=db, docs_root=DOCS_DIR, UPLOAD_DIR=UPLOAD_DIR, AGENTS=AGENTS,
        brave_search=brave_web_search,
        perplexity_web_search=perplexity_web_search,
        perplexity_available=_perplexity_available,
        generate_image_for=generate_image_for,
        save_artifacts=_save_artifacts,
    )


def _build_lead_mission_system_prompt(tool_catalog: str) -> str:
    """Compact system prompt for the mission loop's LLM."""
    from missions import JSON_STEP_SCHEMA
    today = datetime.now(timezone.utc).strftime("%A, %B %d, %Y")
    return (
        f"You are the lead agent on an autonomous SoulPrint mission. Today "
        f"is {today} (UTC). Your job is to achieve the user's goal by "
        "iteratively choosing tools from the catalog below and producing "
        "real artifacts (files, images, research summaries) along the way. "
        "Be decisive: prefer action over asking.\n\n"
        "REAL-TIME DATA RULE — NON-NEGOTIABLE:\n"
        "Your training data is months or years old. NEVER answer from memory "
        "for anything involving:\n"
        "  • current events, recent launches, today's news\n"
        "  • product features, pricing, version numbers\n"
        "  • dates, schedules, who-leads-what at a company\n"
        "  • market data, stock prices, social media metrics\n"
        "Always call web_search FIRST for these. If web_search returns no "
        "results, retry with a refined query, or call ask_user for "
        "guidance — DO NOT fabricate plausible-sounding facts from training "
        "data. A short honest answer beats a long stale one. When you cite "
        "a fact in an artifact, prefer to include the source URL from your "
        "search results.\n\n"
        "DEPTH RULE — DELIVER MORE THAN SEARCH SNIPPETS:\n"
        "Search snippets are starting points, not deliverables. For any "
        "goal that says 'top N', 'competitive brief', 'research', "
        "'analysis', 'compare', 'pricing', 'features', 'review', or asks "
        "for a per-item breakdown:\n"
        "  1. Run web_search to identify candidates.\n"
        "  2. Then call browse_url on the top results' actual websites to "
        "     pull real feature lists / pricing / specifics — DO NOT just "
        "     paraphrase the search snippet.\n"
        "  3. Synthesise the artifact from what you learned by browsing, "
        "     not from your guess at what the products do.\n"
        "If you can finish in 1–2 tool calls and the goal is genuinely "
        "shallow ('what's the URL of X', 'summarise this page'), great — "
        "ship and finish. But shallow tool use on a deep goal produces a "
        "thin artifact that fails the user.\n\n"
        "RESEARCH-BREADTH RULE — MULTIPLE SOURCES, NON-NEGOTIABLE:\n"
        "If the goal mentions any of: 'research', 'find', 'identify', "
        "'common', 'top', 'most', 'biggest', 'best', 'compare', 'analyse', "
        "'analysis', 'survey', 'study', 'trends', 'industry', 'market', "
        "'competitive', 'audit', 'evaluate', 'review', 'report on', "
        "'investigate', 'complaints', 'issues', 'problems', 'pain points', "
        "or asks for a 'list', 'breakdown', 'overview', or 'findings' — "
        "the deliverable MUST be grounded in AT LEAST 5 DISTINCT sources.\n"
        "  • Each source must be a different domain (e.g. forbes.com, "
        "    sba.gov, reddit.com — not three different pages on forbes.com).\n"
        "  • Quote, paraphrase, or cite each source with its URL "
        "    inline in the artifact body.\n"
        "  • Pattern: 1 web_search to enumerate → browse_url ≥ 4 of the "
        "    most relevant results from different domains → optionally a "
        "    second web_search for a different angle → browse 2-3 more.\n"
        "  • For multi-faceted topics ('SMB complaints', 'AI trends'), "
        "    deliberately mix source TYPES: industry reports, government "
        "    stats (e.g. sba.gov, bls.gov, eurostat), trade publications, "
        "    practitioner forums (reddit, indiehackers, hackernews), and "
        "    primary research (surveys, case studies). One-source "
        "    research is not research — it's plagiarism.\n"
        "  • If a chosen source is paywalled / 403s / login-walled, "
        "    DON'T retry it — pick a different source from web_search.\n"
        "The finish gate enforces this: calling `finish` on a research "
        "goal with fewer than 5 distinct domains browsed will be REJECTED "
        "and you'll be asked to gather more sources before finishing.\n\n"
        "IDEMPOTENCY RULE — NEVER CALL THE SAME TOOL TWICE WITH IDENTICAL ARGS:\n"
        "Every tool call is idempotent. `browse_url(https://x.com)` returns "
        "the FULL visible text of that page in one shot — calling it again "
        "will return the same data. Likewise `web_search(query=X)` always "
        "returns the same results for query X. If a tool result doesn't "
        "contain what you expected, the REAL options are:\n"
        "  (a) call a DIFFERENT URL / query — e.g. the product's /pricing "
        "      or /features page instead of the homepage\n"
        "  (b) call write_artifact with what you have and note the gap\n"
        "  (c) call finish and flag the gap in the summary\n"
        "The WRONG option is to re-call the same tool hoping for a better "
        "response. The runtime will hard-block exact re-calls and return an "
        "error — don't waste an iteration proving this.\n\n"
        "NEVER FABRICATE URLS OR FACTS:\n"
        "When browse_url returns a page-map with `links` (array of "
        "{text, href}), USE THOSE EXACT href values in your artifact. Do "
        "NOT guess URL patterns like '/item?id=1' — if the page-map has "
        "the real link, use it verbatim; if it doesn't, write '(URL not "
        "captured)' in the artifact rather than inventing one. Same goes "
        "for prices, release dates, feature lists — if the tool result "
        "doesn't contain the fact, say so; don't make one up.\n\n"
        "ASK_USER IS A LAST RESORT — not a progress-checkpoint. Only call "
        "it when you genuinely CANNOT proceed without new information from "
        "the human — e.g. truly ambiguous product "
        "choice that affects downstream work, or the user's request depends "
        "on content (a list, a document) they haven't provided. Do NOT "
        "call ask_user for:\n"
        "  • 'Should I include X or Y in the brief?' — pick the best option "
        "     and note the choice in your summary.\n"
        "  • 'I have partial data — continue?' — yes, always continue. "
        "     Write the artifact with what you have; flag the gap.\n"
        "  • 'Which format do you want?' — pick `.pdf` for briefs / "
        "     reports / plans / audits, `.docx` if the goal mentions Word, "
        "     `.csv` for lists, `.ics` for calendars, `.md` only when the "
        "     goal explicitly asks for raw markdown. The user will react "
        "     if it's wrong.\n"
        "  • **DO NOT ASK FOR ACKNOWLEDGEMENT / CONFIRMATION / PERMISSION "
        "MID-RUN.** Phrases like 'Shall I continue?', 'Want me to proceed?', "
        "'Ready for the next step?', 'Let me know if this looks good' are "
        "BANNED. The user started the mission with an implicit 'yes, do "
        "the whole thing' — don't re-check. Finish the work and report "
        "outcomes at the end.\n"
        "  • **DO NOT ASK FOR A STATUS UPDATE.** The UI already streams "
        "every tool call + result to the user in real time. You don't need "
        "to pause and narrate 'I'm about to do X' — just do X.\n"
        "  • **LOGIN CREDENTIALS / PASSWORDS / 2FA CODES — NEVER ASK.** "
        "     The runtime pre-loads authenticated cookies into your browser "
        "     via storage_state when available; if browse_url lands on a "
        "     login wall anyway, cookies are missing or expired. In that "
        "     case finish IMMEDIATELY with summary starting \"FAILED: no "
        "     login session on file\" and naming the platform — the user "
        "     can upload fresh cookies at the org's Social Queue page. "
        "     Do NOT try to log in with fake or user-provided credentials — "
        "     that's a security violation.\n"
        "Every ask_user call costs the user a context switch — only pay "
        "that cost when you truly have no other option. Default behaviour "
        "on long tasks: push through. Make reasonable choices on your own "
        "and note them in the final summary — the user will adjust if "
        "they disagree.\n\n"
        "ARTIFACTS via write_artifact. Allowed extensions: **.pdf** "
        "(preferred for briefs/reports/plans/audits — body is Markdown, "
        "system renders a real PDF), **.docx** (Word — same deal, body is "
        "Markdown, real .docx output), .md, .csv, .tsv, .json, .html, .ics, "
        ".vcf, .eml, .sh, .py, .js, .sql, .txt, .xml, .yaml. The tool "
        "rejects image/video/audio extensions — for images use the "
        "`generate_image` tool, which produces a real PNG.\n\n"
        "MISSION-BRIEF FORMAT RULE — when the deliverable is a written "
        "report, brief, audit, plan, proposal, summary, memo, whitepaper, "
        "or any document the user will read end-to-end, save it as **.pdf** "
        "(default) or **.docx** (when the goal mentions Word). NEVER save "
        "long-form briefs as .txt — plain text strips formatting and "
        "looks unprofessional. Use .md only when the user explicitly asks "
        "for raw markdown or when the artifact is meant to be edited "
        "(README, prompt template, etc.). Use .csv only for tabular data "
        "with rows + columns.\n\n"
        "DATA-HARVEST / LEAD-HUNT RECIPE — when the goal mentions ANY of "
        "'harvest', 'list of companies / businesses / leads / contacts / "
        "nonprofits / agencies', 'lead hunt', 'lead list', 'lead gen', "
        "'prospect list', 'company list', 'business list', 'contact list', "
        "'dataset of', 'directory of', 'roster of', 'build a list / "
        "database', 'scrape companies / businesses / leads' — the "
        "deliverable IS A CSV, not a PDF. A methodology PDF with zero rows "
        "is a FAILED mission, full stop.\n"
        "  • USE the **`csv_append_rows`** tool (NOT write_artifact). "
        "    First call MUST declare `columns` (ordered headers); later "
        "    calls only need `name` + `rows`. The CSV grows on every "
        "    call — the library shows ONE growing file.\n"
        "  • **CRITICAL — START WITH THE JSON-API TOOLS, NOT WEB SCRAPING.** "
        "    Chamber of Commerce, BBB, Yelp, Google Maps, and LinkedIn are "
        "    all protected by Cloudflare / login walls — every `web_scrape` "
        "    against them returns empty or 403, which is why earlier "
        "    versions of this mission shipped 0-1 rows. The tools below "
        "    hit REAL JSON APIs that return guaranteed structured data:\n"
        "      - `sec_edgar_search(query='healthcare', state='AZ')` — "
        "        SEC-filed companies, 25 rows per call, every match a "
        "        verified operating business.\n"
        "      - `usaspending_recipients(state='AZ', "
        "        naics_keyword='consulting')` — federal contractors / "
        "        grantees, 25 rows per call, pre-attached city + state.\n"
        "      - `wikipedia_companies_list(query='Phoenix, Arizona')` — "
        "        Wikipedia 'List of companies based in X' pages, 30 "
        "        rows per call.\n"
        "      - `opencorporates_search(jurisdiction='us_az', "
        "        q='consulting')` — registered businesses by US state, "
        "        25 rows per call.\n"
        "    Each of these returns rows ALREADY SHAPED for the lead-hunt "
        "    CSV — push the array straight into `csv_append_rows` rows.\n"
        "  • LOOP PATTERN per metro:\n"
        "      1. `sec_edgar_search(query='<industry>', state='<2-letter>')`\n"
        "      2. `usaspending_recipients(state='<2-letter>', "
        "         naics_keyword='<industry>')`\n"
        "      3. `wikipedia_companies_list(query='<City>, <State>')`\n"
        "      4. `opencorporates_search(jurisdiction='us_<state-lc>', "
        "         q='<industry>')`\n"
        "      5. `csv_append_rows` after each call — push the returned "
        "         `rows` array directly. Each tool returns 20-30 rows, "
        "         so two metros' worth of calls already clears the "
        "         15-row finish gate.\n"
        "      6. ONLY if you still need more rows after exhausting the "
        "         JSON APIs, fall back to `web_search` → `web_extract` on "
        "         a chamber/association page. Treat scrape results as "
        "         best-effort filler, NOT the primary source.\n"
        "  • US JURISDICTION CODES — handy reference: us_az (Arizona), "
        "    us_ca (California), us_co (Colorado), us_fl (Florida), "
        "    us_ga (Georgia), us_il (Illinois), us_ma (Massachusetts), "
        "    us_md (Maryland), us_mi (Michigan), us_ny (New York), "
        "    us_oh (Ohio), us_pa (Pennsylvania), us_tx (Texas), "
        "    us_va (Virginia), us_wa (Washington).\n"
        "  • COLUMN CHOICE — pick columns the user actually needs for "
        "    outreach. A solid default for a B2B lead-hunt CSV: "
        "    `Business Name, Industry, City, State, Years in Business, "
        "    Annual Revenue (USD), Website URL, Contact Name, Contact "
        "    Phone, Contact Email, Source URL`. The JSON-API tools "
        "    already return this shape. Use blanks (empty string) when "
        "    a field is genuinely missing — DO NOT fabricate phone "
        "    numbers / emails / revenue figures. A truthful blank beats "
        "    a hallucinated number.\n"
        "  • THE FINISH GATE — RESPECT IT. A list-style mission is "
        "    REJECTED at finish if the CSV has fewer than 15 data rows. "
        "    The gate now fires up to THREE times with escalating "
        "    urgency — IGNORING the first nudge will get you a stronger "
        "    second nudge, and a 'FINAL WARNING' third. After the third "
        "    nudge the mission ships but is flagged PARTIAL. You'll "
        "    waste your iteration budget arguing with the gate — just "
        "    call the JSON-API tools and clear the threshold.\n\n"
        "SOCIAL POSTING — WHEN THE GOAL SAYS 'POST ON X / LINKEDIN / "
        "FACEBOOK / THREADS / BLUESKY / MASTODON / REDDIT':\n"
        "Your job is to ACTUALLY PUBLISH the post, not to draft-and-hand-"
        "off. The workflow is ALWAYS:\n"
        "  1. browse_url to the platform's composer (e.g. "
        "     https://www.linkedin.com/feed/?shareActive=true for "
        "     LinkedIn, https://x.com/compose/post for X).\n"
        "  2. Read the `forms` / `buttons` in the page-map to find the "
        "     textarea + Post button.\n"
        "  3. browse_fill the textarea with the POST TEXT VERBATIM — do "
        "     NOT rephrase, do NOT add your own sign-off.\n"
        "  4. browse_click the primary Post/Publish button.\n"
        "  5. Verify the post went live (the composer closes / URL changes "
        "     / your profile now shows the post).\n"
        "  6. finish with summary starting \"PUBLISHED:\" plus the URL if "
        "     you found one. Or \"FAILED: <one-sentence cause>\" if you "
        "     couldn't verify publication.\n"
        "Finishing with 'here is the draft for you to post manually' "
        "DEFEATS THE ENTIRE POINT OF THE MISSION. If you literally cannot "
        "post (no cookies, 2FA challenge, account suspended), say so "
        "explicitly with a FAILED: prefix — don't silently hand back a "
        "draft as if that were success.\n\n"
        "ITERATION BUDGET AWARENESS: every turn, the user_turn tells you "
        "which iteration you're on out of 25 total. Plan accordingly:\n"
        "  • Iterations 1–3: scope the goal (1 search, 1–2 browses)\n"
        "  • Iterations 4–12: gather data (more browses, more tools)\n"
        "  • Iterations 13–20: write the artifact + refine\n"
        "  • Iterations 21–25: WRAP UP — call write_artifact with the best "
        "     you have, then finish. A good-enough deliverable shipped at "
        "     iter 22 beats a perfect one auto-killed at iter 25.\n\n"
        "FORM-FINDING RECIPE — when the goal involves filling/submitting a "
        "form (waitlist, signup, contact us, demo request, newsletter, "
        "early access, beta access, etc.):\n"
        "  1. browse_url the homepage. The result includes an "
        "     `affordances_summary` line + structured `forms`, `buttons`, "
        "     and `links` arrays — read them BEFORE deciding what to do "
        "     next. They tell you exactly what's clickable.\n"
        "  2. If `forms` is non-empty AND the first form's fields match "
        "     the goal (e.g. has an email field for a waitlist), call "
        "     browse_fill RIGHT NOW. Pass field labels as keys (the LLM-"
        "     friendly path) — e.g. `{\"Email\": \"x@y.com\"}`. Then "
        "     browse_submit.\n"
        "  3. If `forms` is empty, scan `buttons` and `links` for keywords "
        "     matching the goal: 'waitlist', 'join', 'sign up', 'get "
        "     started', 'early access', 'beta', 'request access', "
        "     'contact', 'demo', 'free trial', 'try it', etc. Pick the "
        "     STRONGEST match and call browse_click with `text=\"<button "
        "     label>\"`. After the click the new page-map appears in the "
        "     tool result — re-check `forms`.\n"
        "  4. You may need 2–3 hops (homepage → 'Join waitlist' button → "
        "     dedicated form page). That's normal. Keep going as long as "
        "     each hop is making progress (URL changes, new forms appear).\n"
        "  5. If you've hopped 3 times and still no form, OR the form's "
        "     fields don't match the user's data, call ask_user to "
        "     clarify — do NOT keep clicking randomly.\n"
        "  6. If a cookie banner / GDPR overlay shows up first, click "
        "     'Accept' / 'Agree' / 'Got it' before searching for the form. "
        "     The cookie banner usually appears as a top-level button in "
        "     the page-map.\n"
        "  7. RADIO BUTTONS / CHECKBOXES — when the form's `fields` array "
        "     shows `type: 'radio'` or `'checkbox'` with visible labels "
        "     (e.g. {label: 'Medium', name: 'size'}), select them by "
        "     calling browse_fill with the LABEL as the key and `true` as "
        "     the value: `{\"Medium\": true, \"Extra Cheese\": true}`. "
        "     Alternatively use browse_click with `text=\"Medium\"` — the "
        "     click will land on the wrapping <label> and activate the "
        "     radio. Do NOT call browse_click on a label more than once "
        "     if it errors — switch to the browse_fill label-as-key path.\n"
        "Use REASONABLE DEFAULTS for anything the user didn't specify "
        "(name, company, role, etc.) — invent plausible values that match "
        "the org's identity from soul / style guide. Do NOT call ask_user "
        "for fields the user clearly expected you to fill on their behalf.\n\n"
        "Always think first, then pick ONE tool per turn. Use web_search "
        "liberally for facts. Use browse_url to pull real page content when "
        "depth matters. Use write_artifact to save briefs, checklists, "
        ".ics invites, .csv exports, email drafts, scripts — anything the "
        "user will want to download. Use generate_image for visual assets. "
        "Use read_org_document when the user refers to their own prior work.\n\n"
        "Finish as soon as the goal is met *with appropriate depth*. Don't "
        "loop forever. Don't invent identifiers — if you don't have an ID, "
        "don't make one up.\n\n"
        f"{tool_catalog}\n\n{JSON_STEP_SCHEMA}"
    )


missions_r = make_missions_router(
    db=db, AGENTS=AGENTS, EMERGENT_LLM_KEY=EMERGENT_LLM_KEY,
    LlmChat=LlmChat, UserMessage=UserMessage,
    require_org_member=require_org_member,
    get_current_user=get_current_user,
    build_tools_factory=_build_mission_tools_factory,
    build_lead_system_prompt=_build_lead_mission_system_prompt,
    docs_root=DOCS_DIR,
    UPLOAD_DIR=UPLOAD_DIR,
)
app.include_router(missions_r)

# ── Remote browser live streaming (ChatGPT Agent Mode equivalent) ─────
# Mounts a WebSocket at /api/missions/{mission_id}/browser-stream that
# pipes Chromium's DevTools screencast to the frontend and forwards the
# user's mouse/keyboard events back through CDP. See browser_stream.py.
from browser_stream import register_browser_stream_routes  # noqa: E402


async def _require_mission_member_ws(mission_id: str, token: str):
    """Validate a bearer-like token passed via WS query string and
    confirm the caller is a member of the mission's org.

    Supports BOTH legacy HS256 JWTs and Firebase ID tokens — same
    sniff / decode pipeline as `get_current_user`, minus the Request
    object (websockets can't carry a cookie jar uniformly across
    browsers, so the token is always passed as `?token=...`).
    """
    if not token:
        raise ValueError("missing token")
    user_doc: Optional[dict] = None
    if _firebase_enabled() and _looks_like_firebase_token(token):
        claims = verify_firebase_id_token(token)
        resolved = await upsert_user_for_firebase_claims(db, claims)
        user_doc = {
            "id": resolved["id"], "email": resolved.get("email", ""),
            "name": resolved.get("name", ""),
        }
    else:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
        user = await db.users.find_one({"_id": ObjectId(payload["sub"])})
        if not user or user.get("is_disabled"):
            raise ValueError("user not found or disabled")
        user_doc = {
            "id": str(user["_id"]), "email": user["email"],
            "name": user.get("name", ""),
        }
    mission = await db.missions.find_one({"id": mission_id}, {"_id": 0})
    if not mission:
        raise ValueError("mission not found")
    # Re-use the existing org-membership helper so the browser stream
    # has exactly the same access policy as every other mission endpoint.
    await require_org_member(mission["org_id"], user_doc["id"])
    return mission, user_doc


register_browser_stream_routes(
    app=app, db=db,
    require_mission_member=_require_mission_member_ws,
)

# ── B4: Credential vault HTTP endpoints (list / delete) ─────────────
# The save flow happens through the WebSocket (`save_credentials`
# message). These REST endpoints let the org owner audit and revoke
# saved logins from a settings page.
from credential_vault import list_credentials as _vault_list  # noqa: E402
from credential_vault import delete_credentials as _vault_delete  # noqa: E402


@app.get("/api/orgs/{org_id}/saved-logins")
async def list_saved_logins(org_id: str, u: dict = Depends(get_current_user)):
    await require_org_member(org_id, u["id"])
    return await _vault_list(db, org_id=org_id)


@app.delete("/api/orgs/{org_id}/saved-logins/{hostname}")
async def delete_saved_login(
    org_id: str, hostname: str, u: dict = Depends(get_current_user),
):
    await require_org_member(org_id, u["id"])
    ok = await _vault_delete(db, org_id=org_id, hostname=hostname)
    return {"ok": bool(ok), "hostname": hostname}

# Autonomous social posting — scheduled Missions that drive a headless
# browser to publish a queued social post on the user's behalf. Uses the
# same APScheduler instance as the cron-style schedules above. See
# social_autopost.py for the full flow.
from social_autopost import (  # noqa: E402
    make_social_autopost_router,
    rehydrate_autopost_jobs,
)
social_autopost_r = make_social_autopost_router(
    db=db, scheduler=scheduler, AGENTS=AGENTS,
    EMERGENT_LLM_KEY=EMERGENT_LLM_KEY,
    LlmChat=LlmChat, UserMessage=UserMessage,
    require_org_member=require_org_member,
    get_current_user=get_current_user,
    build_tools_factory=_build_mission_tools_factory,
    build_lead_system_prompt=_build_lead_mission_system_prompt,
)
app.include_router(social_autopost_r)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    # Tightened CORS: prefer an explicit allowlist via the ALLOWED_ORIGINS
    # env var ("https://foundryagents.ai,https://app.foundryagents.ai")
    # for production. Falls back to the permissive regex when the env is
    # unset so preview/dev still works without manual config.
    allow_origins=[
        o.strip() for o in (os.environ.get("ALLOWED_ORIGINS") or "").split(",")
        if o.strip()
    ] or [],
    allow_origin_regex=(
        None if (os.environ.get("ALLOWED_ORIGINS") or "").strip()
        else r".*"
    ),
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Public security policy doc ───────────────────────────────────────────
# Surface the structured policy doc at /api/security so the public
# /security trust page (and prospective enterprise customers' security
# review teams) can pull it programmatically.
@app.get("/api/security")
async def get_security_policy():
    return SECURITY_POLICY_DOC


# ── Proof of Integrity (OpenTimestamps anchoring) ────────────────────────
# Per-file proofs and daily Merkle anchors. See proof_of_integrity.py.
import proof_of_integrity as poi  # noqa: E402


# ── Public, share-safe verify endpoints ─────────────────────────────────
# These are intentionally UNAUTHENTICATED. The data they return — a hex
# digest, OTS proof bytes, calendar list, timestamps — is content-free
# without the original file. A digest can't be reversed back to bytes,
# so leaking these to the public web is the entire POINT of the feature
# (so prospective customers can verify a file before signing up).
#
# We DO suppress `extra` (which contains filename labels and Merkle
# manifests) since those can leak business names like "Acme_Q3_audit.pdf".
def _publicly_safe_proof(row: dict) -> dict:
    """Strip anything that could leak business context. Keep only the
    cryptographic primitives needed to verify the file independently."""
    if not row:
        return {}
    pub = poi._public_proof_row(row)
    pub.pop("extra", None)        # filenames, manifests
    pub.pop("subject_id", None)   # GridFS id (caller already knows it)
    return pub


@app.get("/api/public/v/{gridfs_id}")
async def public_verify_file(gridfs_id: str):
    """Public, no-auth proof lookup. Returns just the cryptographic
    primitives — anyone (a prospect, an auditor, a journalist) can hit
    this URL with a file_id from a `/v/<id>` share-link and confirm the
    file is Bitcoin-anchored without needing a Foundry account.
    """
    row = await poi.get_proof_for_subject(db, kind="file", subject_id=gridfs_id)
    if not row:
        raise HTTPException(404, "No integrity proof for this file")
    return _publicly_safe_proof(row)


@app.get("/api/public/v/{gridfs_id}/proof.ots")
async def public_download_proof_ots(gridfs_id: str):
    """Public download of the standard `.ots` proof file. Bytes are
    useless without the original file (which we never expose here), so
    this is safe to share. Lets a third party run
    `ots verify file.ots --file the_original` themselves.
    """
    row = await poi.get_proof_for_subject(db, kind="file", subject_id=gridfs_id)
    if not row:
        raise HTTPException(404, "No integrity proof for this file")
    full = await db.integrity_proofs.find_one({"id": row["id"]})
    proof_bytes = (full or {}).get("proof_bytes") or b""
    return Response(
        content=proof_bytes,
        media_type="application/vnd.opentimestamps.ots",
        headers={
            "Content-Disposition": f'attachment; filename="{gridfs_id}.ots"',
            # Cache aggressively — the proof bytes only ever grow more
            # complete (pending → anchored), and 5 minutes is a fine
            # window for the rare upgrade case.
            "Cache-Control": "public, max-age=300",
        },
    )


@app.post("/api/public/v/{gridfs_id}/upgrade")
async def public_upgrade_file_proof(gridfs_id: str):
    """Public-safe poll for pending → Bitcoin-anchored upgrade. The
    server already rate-limits one calendar poll per 5 min per record,
    so anyone can hit this without DOS risk to the OTS calendars.
    """
    row = await poi.get_proof_for_subject(db, kind="file", subject_id=gridfs_id)
    if not row:
        raise HTTPException(404, "No integrity proof for this file")
    refreshed = await poi.try_upgrade_pending(db, record_id=row["id"])
    return _publicly_safe_proof(refreshed) if refreshed else _publicly_safe_proof(row)


@app.get("/api/files/{gridfs_id}/proof")
async def get_file_proof(gridfs_id: str, u: dict = Depends(get_current_user)):
    """Return the current proof status for an artifact stored in GridFS.

    Auth note: anyone signed in can ask about ANY GridFS id. We don't
    leak file contents — only the digest, which is what proves the
    file was anchored. Restricting to "members of the org that owns
    the file" would mean linking every GridFS upload back to an org,
    which the schema doesn't enforce yet. The digest itself is
    information-free without the file bytes.
    """
    row = await poi.get_proof_for_subject(db, kind="file", subject_id=gridfs_id)
    if not row:
        raise HTTPException(404, "No integrity proof for this file")
    return poi._public_proof_row(row)


@app.post("/api/files/{gridfs_id}/proof/upgrade")
async def upgrade_file_proof(gridfs_id: str, u: dict = Depends(get_current_user)):
    """Poll the calendars to see if the pending attestation has been
    upgraded to a Bitcoin block attestation. Rate-limited internally
    to one calendar poll every 5 minutes per proof.
    """
    row = await poi.get_proof_for_subject(db, kind="file", subject_id=gridfs_id)
    if not row:
        raise HTTPException(404, "No integrity proof for this file")
    refreshed = await poi.try_upgrade_pending(db, record_id=row["id"])
    return refreshed or poi._public_proof_row(row)


@app.get("/api/integrity-proofs/{record_id}/proof.ots")
async def download_proof_ots(record_id: str, u: dict = Depends(get_current_user)):
    """Stream the raw `.ots` proof bytes so the user can verify with
    the standard OpenTimestamps CLI (`ots verify file.ots --file the_file`).
    """
    row = await db.integrity_proofs.find_one({"id": record_id})
    if not row:
        raise HTTPException(404, "Proof not found")
    proof_bytes = row.get("proof_bytes") or b""
    return Response(
        content=proof_bytes,
        media_type="application/vnd.opentimestamps.ots",
        headers={
            "Content-Disposition": f'attachment; filename="{record_id}.ots"',
        },
    )


@app.get("/api/integrity-anchors")
async def list_integrity_anchors(
    limit: int = 30,
    u: dict = Depends(get_current_user),
):
    """List the most recent daily Merkle anchors. Powers the admin
    dashboard's "Integrity Anchors" panel and is also useful for any
    customer who wants to audit their data window-by-window.
    """
    limit = max(1, min(200, int(limit or 30)))
    cur = db.integrity_proofs.find(
        {"kind": "merkle_daily"},
        # Drop heavy fields from the list view.
        {"_id": 0, "proof_bytes": 0, "extra.manifest": 0},
    ).sort("created_at", -1).limit(limit)
    rows = [r async for r in cur]
    return rows


@app.get("/api/integrity-anchors/{record_id}/manifest")
async def get_anchor_manifest(record_id: str, u: dict = Depends(get_current_user)):
    """Return the manifest of records folded into a daily Merkle root.
    Each entry has the source collection, record id, and leaf hex so
    a customer can re-hash the original record and confirm it appears
    in the anchored set.
    """
    row = await db.integrity_proofs.find_one(
        {"id": record_id, "kind": "merkle_daily"},
        {"_id": 0, "proof_bytes": 0},
    )
    if not row:
        raise HTTPException(404, "Anchor not found")
    return row


@app.post("/api/admin/integrity-anchors/run-now")
async def admin_run_daily_anchor(u: dict = Depends(_require_superadmin_dep)):
    """Manually trigger a daily Merkle anchor build. Useful for testing
    or for retroactively anchoring after a calendar outage."""
    row = await poi.build_daily_audit_anchor(db, include_today=True)
    return row or {"ok": False, "detail": "No records in window or every calendar failed"}

@app.on_event("startup")
async def startup():
    await db.users.create_index("email", unique=True)
    await db.org_members.create_index([("org_id", 1), ("user_id", 1)], unique=True)
    await db.invites.create_index("code", unique=True)
    await db.teams.create_index("org_id")
    await db.conversations.create_index([("team_id", 1), ("created_at", -1)])
    await db.messages.create_index([("conversation_id", 1), ("timestamp", 1)])
    await db.messages.create_index("media_filename", sparse=True)
    # User-memory store (Phase 1, user-scoped). The memory module owns
    # its own index creation so the schema definition stays colocated
    # with the code that uses it.
    try:
        from memory import ensure_indexes as _ensure_memory_indexes
        await _ensure_memory_indexes(db)
    except Exception as exc:
        logger.warning("memory index creation failed: %s", exc)
    await db.schedules.create_index([("org_id", 1), ("created_at", -1)])
    await db.schedules.create_index("created_by")
    await db.unfurl_cache.create_index("url", unique=True)
    await db.social_posts.create_index([("org_id", 1), ("created_at", -1)])
    await db.social_posts.create_index([("org_id", 1), ("status", 1)])
    await db.social_posts.create_index("scheduled_for", sparse=True)
    await db.social_posts.create_index(
        [("autopost_enabled", 1), ("autopost_status", 1)], sparse=True,
    )
    await db.social_sessions.create_index(
        [("org_id", 1), ("platform", 1)], unique=True,
    )
    # TTL cleanup for pending mission-upload records that were never
    # consumed by a start_mission call. We only expire records where
    # consumed_by_mission_id is empty (unfinished flow); consumed ones
    # live alongside the mission forever.
    await db.mission_attachments.create_index(
        [("org_id", 1), ("uploader_id", 1)],
    )
    await db.mission_attachments.create_index(
        "created_at",
        expireAfterSeconds=24 * 3600,
        partialFilterExpression={"consumed_by_mission_id": ""},
    )
    await db.documents.create_index([("org_id", 1), ("created_at", -1)])
    await db.documents.create_index([("org_id", 1), ("filename", 1)], unique=True)
    await db.documents.create_index("conversation_id")
    await db.missions.create_index([("org_id", 1), ("started_at", -1)])
    await db.missions.create_index("status")
    await db.mission_events.create_index([("mission_id", 1), ("ts", 1)])
    # B4 credential vault — one (org, hostname) entry max.
    await db.domain_credentials.create_index(
        [("org_id", 1), ("hostname", 1)], unique=True,
    )
    # Per-mission encrypted storage_state — survives backend restarts so
    # the live remote browser revives EXACTLY where the agent left off
    # (cookies, localStorage, last URL — fully authenticated).
    try:
        from mission_browser_state import ensure_indexes as _mbs_indexes
        await _mbs_indexes(db)
    except Exception as exc:
        logger.warning("mission_browser_state index init failed: %s", exc)
    # External-API cost tracking — Brave / Perplexity / Resend / kie.ai.
    # The admin spend dashboard aggregates these alongside `llm_calls`
    # for a complete cost picture per org/mission/user.
    try:
        from external_api_costs import (
            ensure_indexes as _ext_indexes,
            set_db as _ext_set_db,
        )
        await _ext_indexes(db)
        _ext_set_db(db)
    except Exception as exc:
        logger.warning("external_api_costs init failed: %s", exc)
    # Wire browser_tools to the persistence layer so every navigation /
    # form interaction snapshots the page state to MongoDB. The lookup
    # closure resolves a mission's org_id without coupling browser_tools
    # to the server's data model.
    try:
        import browser_tools as _bt

        async def _mission_org_lookup(mid: str) -> str:
            try:
                m = await db.missions.find_one({"id": mid}, {"_id": 0, "org_id": 1})
                return (m or {}).get("org_id", "") or ""
            except Exception:
                return ""
        _bt.set_persistence(db, _mission_org_lookup)
    except Exception as exc:
        logger.warning("browser_tools persistence wiring failed: %s", exc)
    # Workspace layer indexes + one-time migration for existing orgs.
    await db.workspaces.create_index([("owner_id", 1), ("kind", 1)])
    await db.workspace_members.create_index(
        [("workspace_id", 1), ("user_id", 1)], unique=True,
    )
    await db.workspace_members.create_index("user_id")
    await db.workspace_invites.create_index("code", unique=True)
    await db.workspace_invites.create_index([("workspace_id", 1), ("used_by", 1)])
    await db.organizations.create_index("workspace_id")
    try:
        migrated = await migrate_existing_orgs_to_workspaces(db)
        if migrated:
            logger.info("Workspace startup: migrated %d legacy org(s)", migrated)
    except Exception as exc:
        logger.warning("Workspace migration failed: %s", exc)
    # ── Proof of Integrity startup ──────────────────────────────────────
    # Ensure the integrity_proofs collection has its indexes, then kick
    # off the daily anchor builder + the pending-proof upgrade poller.
    # Both are infinite loops we let asyncio garbage-collect; storing
    # the task refs keeps them from being prematurely cancelled.
    try:
        await poi.ensure_indexes(db)
        global _poi_anchor_task, _poi_upgrade_task
        _poi_anchor_task = asyncio.create_task(poi.daily_anchor_loop(db))
        _poi_upgrade_task = asyncio.create_task(poi.upgrade_pending_loop(db))
        logger.info("Proof of Integrity: scheduler started")
    except Exception as exc:
        logger.warning("Proof of Integrity startup failed: %s", exc)
    # ── Mission rehydration (autonomous resume across pod restarts) ─────
    # Re-spawn the asyncio task for any mission whose heartbeat is still
    # fresh — these are missions where the previous backend just died
    # (deploy / OOM / scale event) but the work log is intact in Mongo.
    # Pulling these back to life is what makes missions truly autonomous;
    # otherwise every redeploy kills in-flight runs and the watchdog
    # eventually fails them. Stale missions (no heartbeat in 60+ min)
    # are skipped here so the recovery scan below can fail them with a
    # clear message instead of restarting a wedged run forever.
    try:
        rehydrated = await rehydrate_running_missions(
            db=db, AGENTS=AGENTS, EMERGENT_LLM_KEY=EMERGENT_LLM_KEY,
            LlmChat=LlmChat, UserMessage=UserMessage,
            build_tools_factory=_build_mission_tools_factory,
            build_lead_system_prompt=_build_lead_mission_system_prompt,
            stale_minutes=60,
        )
        if rehydrated:
            logger.info("Mission startup: rehydrated %d running mission(s)", rehydrated)
    except Exception as exc:
        logger.warning("Mission rehydrate failed: %s", exc)
    # ── Stuck-mission recovery ───────────────────────────────────────────
    # Pod restarts / redeploys kill in-flight asyncio tasks, but the
    # `missions` row keeps `status="running"` (or "awaiting_user") in
    # Mongo. Without rehydration the user sees a "RUNNING" badge that
    # never advances. We don't restart the loop (org/agent context is
    # gone) — instead we fail any mission whose heartbeat is older than
    # the safety window with a clear, actionable error so the user can
    # retry. Heartbeat = `last_activity_at` (updated on every event); if
    # missing we fall back to `updated_at` / `started_at`.
    from datetime import timedelta as _td  # noqa: E402
    stale_cutoff = datetime.now(timezone.utc) - _td(minutes=15)
    stuck_q = {
        "status": {"$in": ["running", "awaiting_user"]},
        "$or": [
            {"last_activity_at": {"$lt": stale_cutoff}},
            {"last_activity_at": {"$exists": False},
             "updated_at": {"$lt": stale_cutoff}},
            {"last_activity_at": {"$exists": False},
             "updated_at": {"$exists": False},
             "started_at": {"$lt": stale_cutoff}},
        ],
    }
    stuck_count = 0
    async for stuck in db.missions.find(stuck_q, {"id": 1, "status": 1}):
        mid = stuck.get("id") or str(stuck.get("_id"))
        prev_status = stuck.get("status", "running")
        await db.missions.update_one(
            {"id": mid},
            {"$set": {
                "status": "failed",
                "error": ("Mission was interrupted by a server restart and "
                          "could not be auto-resumed — please re-run it. "
                          "Your previous work log is preserved below."),
                "finished_at": datetime.now(timezone.utc),
            }},
        )
        try:
            await db.mission_events.insert_one({
                "mission_id": mid,
                "kind": "system",
                "text": (f"Server restart detected — mission was "
                         f"`{prev_status}` with no recent activity, "
                         "failed automatically so you can retry."),
                "ts": datetime.now(timezone.utc),
            })
        except Exception:
            pass
        stuck_count += 1
    if stuck_count:
        logger.info("Reconciled %d stuck mission(s) on startup", stuck_count)
    # Boot the in-process scheduler and rehydrate persisted jobs
    if not scheduler.running:
        scheduler.start()

    # Register privacy housekeeping jobs (daily ZIP-sweep + hard-delete
    # sweep). Idempotent — APScheduler de-dupes by job_id.
    try:
        schedule_privacy_jobs(scheduler, db)
    except Exception:
        logger.exception("Failed to register privacy scheduler jobs")
    async for s in db.schedules.find({"active": True}):
        try:
            _scheduler_register(s)
        except Exception as exc:
            logger.warning("Could not rehydrate schedule %s: %s", s.get("_id"), exc)
    # Rehydrate any pending autopost jobs so restarts don't drop them.
    try:
        await rehydrate_autopost_jobs(db=db, router=social_autopost_r)
    except Exception as exc:
        logger.warning("Autopost rehydrate failed: %s", exc)

    # ── Continuous stuck-mission watchdog ────────────────────────────
    # Runs every 2 minutes while the pod is alive (in addition to the
    # one-shot startup scan above). Catches missions whose asyncio
    # task died silently, whose LLM call blew past every internal
    # timeout, or got wedged inside a Playwright call — without waiting
    # for a pod restart. Threshold 5 minutes matches the startup scan.
    async def _watchdog_tick():
        # 60-minute window. Was 15 minutes, but legitimate browse-heavy
        # missions (Google Maps interactions, multi-screenshot crawls,
        # long LLM round-trips on slow Anthropic days, multi-form
        # submissions across 5+ pages) can legitimately iterate slower
        # than that. We'd rather a truly-dead mission take an extra
        # 45 minutes to reap than false-fail a working one. The
        # in-loop heartbeat (every 30s from `_mission_heartbeat`) keeps
        # the cadence accurate for normal paces. Pod-restart recovery
        # is handled separately by `rehydrate_running_missions` at
        # startup, which re-spawns the asyncio task — so the watchdog
        # can afford a generous threshold.
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=60)
        try:
            cur = db.missions.find(
                {
                    "status": "running",
                    "last_activity_at": {"$lt": cutoff},
                },
                {"id": 1, "status": 1},
            )
            reaped = 0
            async for stuck in cur:
                mid = stuck.get("id")
                if not mid:
                    continue
                await db.missions.update_one(
                    {"id": mid, "status": "running"},
                    {"$set": {
                        "status": "failed",
                        "error": ("Mission stalled with no activity for "
                                  "over 60 minutes and was auto-failed "
                                  "by the watchdog. Click "
                                  "Continue to pick up where it left off."),
                        "finished_at": datetime.now(timezone.utc),
                    }},
                )
                try:
                    await db.mission_events.insert_one({
                        "mission_id": mid,
                        "kind": "system",
                        "text": ("Watchdog auto-failed this mission — no "
                                 "heartbeat in 60+ minutes. Click "
                                 "Continue to resume."),
                        "ts": datetime.now(timezone.utc),
                    })
                except Exception:
                    pass
                reaped += 1
            if reaped:
                logger.warning("Watchdog reaped %d stalled mission(s)", reaped)
        except Exception as exc:
            logger.warning("Watchdog tick failed: %s", exc)

    scheduler.add_job(
        _watchdog_tick,
        trigger="interval",
        minutes=2,
        id="mission_watchdog",
        replace_existing=True,
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=60),
    )

    # ── Conversation auto-archive sweep ──────────────────────────────
    # Once a day, for every org with `archive_idle_days` set to 30/60/90,
    # archive any conversation whose `last_message_at` is older than the
    # cutoff. Owner overrides are preserved (an org can set 0 to disable).
    # The always-on Chief of Staff thread is always exempted via the
    # `general_conversation_id` skip. Memory rows are untouched so the
    # agent's recall of past discussions stays intact.
    async def _auto_archive_tick():
        try:
            now = datetime.now(timezone.utc)
            scanned = 0
            archived = 0
            async for org in db.organizations.find(
                {"archive_idle_days": {"$in": [30, 60, 90]}},
                {"_id": 1, "archive_idle_days": 1, "general_conversation_id": 1},
            ):
                days = int(org.get("archive_idle_days") or 0)
                if days <= 0:
                    continue
                cutoff = now - timedelta(days=days)
                # All teams in this org → conv lookup scope.
                team_ids = [
                    str(t["_id"])
                    async for t in db.teams.find({"org_id": str(org["_id"])}, {"_id": 1})
                ]
                if not team_ids:
                    continue
                protected_conv_id = str(org.get("general_conversation_id") or "")
                q: Dict[str, Any] = {
                    "team_id": {"$in": team_ids},
                    "$or": [
                        {"archived_at": None},
                        {"archived_at": {"$exists": False}},
                    ],
                    "$and": [
                        {"$or": [
                            {"last_message_at": {"$lt": cutoff}},
                            {"last_message_at": {"$exists": False},
                             "created_at": {"$lt": cutoff}},
                        ]},
                    ],
                }
                if protected_conv_id:
                    q["_id"] = {"$ne": ObjectId(protected_conv_id)}
                res = await db.conversations.update_many(
                    q,
                    {"$set": {"archived_at": now, "archived_by": "auto"}},
                )
                scanned += 1
                archived += int(getattr(res, "modified_count", 0) or 0)
            if archived:
                logger.info(
                    "Auto-archive sweep: archived %d idle conversation(s) across %d org(s)",
                    archived, scanned,
                )
        except Exception as exc:
            logger.warning("Auto-archive sweep failed: %s", exc)

    scheduler.add_job(
        _auto_archive_tick,
        trigger="cron",
        hour=4, minute=15,   # 04:15 UTC daily
        id="conv_auto_archive",
        replace_existing=True,
    )

    # ── Chief-of-Staff proactive Telegram pulse ──────────────────────
    # Phase 1: hourly sweep that scans new mission outcomes, social-post
    # failures/successes, and stalled drafts. Sends actionable nudges
    # to users with a connected Telegram chat. See cos_pulse.pulse_tick
    # for the full event matrix.
    async def _cos_pulse_tick():
        try:
            from cos_pulse import pulse_tick, ensure_indexes
            await ensure_indexes(db)
            await pulse_tick(db)
        except Exception as exc:
            logger.warning("cos_pulse tick failed: %s", exc)

    scheduler.add_job(
        _cos_pulse_tick,
        trigger="interval",
        minutes=60,
        id="cos_pulse",
        replace_existing=True,
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=120),
    )
    # ── Brand-logo GridFS backfill ───────────────────────────────────────
    # Older logos were saved to disk only and vanish on pod redeploys
    # (the ephemeral container fs gets wiped). Mirror any logo that's
    # still on disk but missing from `media_files` into GridFS so the
    # next redeploy doesn't lose them. One-shot per process; cheap when
    # the backfill is empty.
    try:
        backfilled = 0
        async for o in db.organizations.find(
            {}, {"brand_assets.primary_logo": 1, "brand_assets.alt_logo": 1},
        ):
            ba = o.get("brand_assets") or {}
            for kind in ("primary_logo", "alt_logo"):
                doc = (ba.get(kind) or {}) if isinstance(ba.get(kind), dict) else {}
                fn = doc.get("filename") if isinstance(doc, dict) else None
                if not fn:
                    continue
                disk_path = UPLOAD_DIR / fn
                if not disk_path.is_file():
                    continue  # already gone — nothing to back up
                existing = await db.media_files.find_one({"filename": fn})
                if existing and existing.get("gridfs_id"):
                    continue  # already mirrored
                try:
                    blob = disk_path.read_bytes()
                except Exception:
                    continue
                await persist_bytes(
                    disk_path, blob,
                    original_name=doc.get("original_name") or fn,
                    mime="image/png",
                )
                backfilled += 1
        if backfilled:
            logger.info("GridFS backfill: mirrored %d existing brand logo(s).", backfilled)
    except Exception as exc:
        logger.warning("Brand-logo GridFS backfill skipped: %s", exc)
    # Composio: idempotently bootstrap one auth-config per supported
    # toolkit so the OAuth flow is ready by first user click. Skipped
    # silently when COMPOSIO_API_KEY isn't configured.
    try:
        from composio_integration import ensure_auth_configs, is_available as _composio_ready
        if _composio_ready():
            cfgs = await ensure_auth_configs(db)
            logger.info("Composio: %d auth_config(s) ready.", len(cfgs))
    except Exception as exc:
        logger.warning("Composio bootstrap skipped: %s", exc)
    logger.info("Foundry API ready.")

@app.on_event("shutdown")
async def shutdown():
    try:
        if scheduler.running:
            scheduler.shutdown(wait=False)
    except Exception:
        pass
    # Tear down the shared headless browser if it was started.
    try:
        from browser_tools import shutdown_browser
        await shutdown_browser()
    except Exception:
        pass
    client.close()

"""
SoulPrint Engine — Backend
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
from fastapi import FastAPI, APIRouter, HTTPException, Request, Response, Depends, UploadFile, File, Form
from fastapi.responses import FileResponse
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from pathlib import Path
from pydantic import BaseModel, Field
from typing import List, Optional, Literal
from datetime import datetime, timezone, timedelta
from bson import ObjectId
import os, logging, uuid, secrets, re, json, base64, bcrypt, jwt

from emergentintegrations.llm.chat import LlmChat, UserMessage

from llm_fallback import install_fallback as _install_llm_fallback
_install_llm_fallback()  # patches LlmChat.send_message to use OPENAI_API_KEY on budget errors

from brave_search import brave_web_search, format_results_for_prompt
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

ROOT_DIR = Path(__file__).parent
UPLOAD_DIR = ROOT_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)
ASSETS_DIR = ROOT_DIR / "uploads" / "assets"
ASSETS_DIR.mkdir(parents=True, exist_ok=True)
load_dotenv(ROOT_DIR / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("soulprint")

# ── DB ───────────────────────────────────────────────────────────────────────
client = AsyncIOMotorClient(os.environ["MONGO_URL"])
db = client[os.environ["DB_NAME"]]

# ── App ──────────────────────────────────────────────────────────────────────
app = FastAPI(title="SoulPrint Engine API")

JWT_SECRET = os.environ.get("JWT_SECRET", "soulprint-dev-secret")
JWT_ALGO = "HS256"
EMERGENT_LLM_KEY = os.environ.get("EMERGENT_LLM_KEY", "")

# ── Auth helpers ─────────────────────────────────────────────────────────────
def hash_password(p: str) -> str:
    return bcrypt.hashpw(p.encode(), bcrypt.gensalt()).decode()

def verify_password(p: str, h: str) -> bool:
    try:
        return bcrypt.checkpw(p.encode(), h.encode())
    except Exception:
        return False

def create_access_token(user_id: str, email: str) -> str:
    return jwt.encode({
        "sub": user_id, "email": email, "type": "access",
        "exp": datetime.now(timezone.utc) + timedelta(hours=24),
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
        max_age=86400,
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
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Invalid token")
    user = await db.users.find_one({"_id": ObjectId(payload["sub"])})
    if not user:
        raise HTTPException(401, "User not found")
    # Capture user-context headers (timezone + optional location) so deeper
    # helpers can pick them up via _USER_CTX without threading a request param.
    tz = (request.headers.get("X-User-Timezone") or "").strip()[:80]
    loc = (request.headers.get("X-User-Location") or "").strip()[:200]
    _USER_CTX.set({"timezone": tz, "location": loc})
    return {"id": str(user["_id"]), "email": user["email"], "name": user["name"]}

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

class TeamCreateBody(BaseModel):
    name: str
    description: Optional[str] = ""

class TeamUpdateBody(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None

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

async def require_team_member(team_id: str, user_id: str) -> dict:
    try:
        team = await db.teams.find_one({"_id": ObjectId(team_id)})
    except Exception:
        raise HTTPException(400, "Invalid team id")
    if not team:
        raise HTTPException(404, "Team not found")
    # team membership = org membership (any org member can access any team in the org)
    m = await db.org_members.find_one({"org_id": team["org_id"], "user_id": user_id})
    if not m:
        raise HTTPException(403, "No access to this team")
    return {"team": team, "org_role": m["role"]}

def serialize_org(org: dict) -> dict:
    return {
        "id": str(org["_id"]),
        "name": org["name"],
        "owner_id": org["owner_id"],
        "website_url": org.get("website_url", ""),
        "soul_description": org.get("soul_description", ""),
        "soul": org.get("soul") or {},
        "general_team_id": org.get("general_team_id", ""),
        "general_conversation_id": org.get("general_conversation_id", ""),
        "created_at": org["created_at"].isoformat() if isinstance(org["created_at"], datetime) else org["created_at"],
    }

def serialize_team(team: dict) -> dict:
    return {
        "id": str(team["_id"]),
        "org_id": team["org_id"],
        "name": team["name"],
        "description": team.get("description", ""),
        "soulprint": team.get("soulprint", {}),
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
    }

# ── Auth routes ──────────────────────────────────────────────────────────────
auth_r = APIRouter(prefix="/api/auth")

@auth_r.post("/register")
async def register(body: RegisterBody, resp: Response):
    email = body.email.lower().strip()
    if not email or not body.password or len(body.password) < 6:
        raise HTTPException(400, "Email and password (min 6 chars) required")
    if await db.users.find_one({"email": email}):
        raise HTTPException(400, "Email already registered")
    doc = {
        "name": body.name.strip() or email.split("@")[0],
        "email": email,
        "password_hash": hash_password(body.password),
        "created_at": datetime.now(timezone.utc),
    }
    res = await db.users.insert_one(doc)
    uid = str(res.inserted_id)
    token = create_access_token(uid, email)
    set_auth_cookies(resp, token)
    return {"id": uid, "name": doc["name"], "email": email, "access_token": token}

@auth_r.post("/login")
async def login(body: LoginBody, resp: Response):
    email = body.email.lower().strip()
    user = await db.users.find_one({"email": email})
    if not user or not verify_password(body.password, user["password_hash"]):
        raise HTTPException(401, "Invalid email or password")
    uid = str(user["_id"])
    token = create_access_token(uid, email)
    set_auth_cookies(resp, token)
    return {"id": uid, "name": user["name"], "email": email, "access_token": token}

@auth_r.post("/logout")
async def logout(resp: Response):
    resp.delete_cookie("access_token", path="/")
    return {"message": "Logged out"}

@auth_r.get("/me")
async def me(u: dict = Depends(get_current_user)):
    return u

# ── Org routes ───────────────────────────────────────────────────────────────
org_r = APIRouter(prefix="/api/orgs")

# Default agent that anchors the auto-created "General" group chat
GENERAL_DEFAULT_AGENT = "specialized-chief-of-staff"

async def _fetch_url_text(url: str, max_chars: int = 8000) -> str:
    """Fetch a URL and return a compact plain-text excerpt of its body.

    Strips scripts/styles + collapses whitespace so it's ready to feed to an LLM.
    Returns "" on any failure — caller should fall back gracefully.
    """
    if not url or not url.strip():
        return ""
    target = url.strip()
    if not target.startswith(("http://", "https://")):
        target = "https://" + target
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True,
                                    headers={"User-Agent": "SoulPrintEngineBot/1.0"}) as client:
            resp = await client.get(target)
        if resp.status_code >= 400:
            logger.warning("scrape %s -> %s", target, resp.status_code)
            return ""
        html = resp.text or ""
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("scrape error %s: %s", target, exc)
        return ""
    # Strip head/script/style/svg blocks before pulling text
    cleaned = re.sub(r"<(script|style|svg|noscript|head)[^>]*>.*?</\1>", " ",
                     html, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", cleaned)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars]

async def _generate_company_soul(name: str, website_text: str, manual_hint: str = "") -> dict:
    """Distil a company "Soul" — mission, audience, tone, values, north star, summary.

    Returns a dict with those keys (empty strings on failure). Uses the
    Emergent LLM key (gpt-4.1-mini) — should be cheap (<1k tokens out).
    """
    base = {"mission": "", "audience": "", "tone": "", "values": "", "north_star": "", "summary": ""}
    if not website_text and not manual_hint:
        return base
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
        return base
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return base
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return base
    return {k: (data.get(k) or "").strip()[:600] for k in base.keys()}

@org_r.post("")
async def create_org(body: OrgCreateBody, u: dict = Depends(get_current_user)):
    if not body.name.strip():
        raise HTTPException(400, "Name required")
    now = datetime.now(timezone.utc)

    # Step 1 — capture the company "Soul" (URL scrape → LLM, with optional manual hint)
    website_url = (body.website_url or "").strip()
    manual = (body.soul_description or "").strip()
    website_text = await _fetch_url_text(website_url) if website_url else ""
    soul = await _generate_company_soul(body.name.strip(), website_text, manual)

    doc = {
        "name": body.name.strip(),
        "owner_id": u["id"],
        "website_url": website_url,
        "soul_description": manual,  # raw founder hint (kept for later edits)
        "soul": soul,                # structured fields injected into agents
        "created_at": now,
    }
    res = await db.organizations.insert_one(doc)
    oid = str(res.inserted_id)
    await db.org_members.insert_one({
        "org_id": oid, "user_id": u["id"], "role": "owner", "joined_at": now,
    })

    # Step 2 — auto-create a "General" team seeded with the org's Soul, so users
    # can chat immediately without having to set up SoulPrint themselves.
    team_doc = {
        "org_id": oid,
        "name": "General",
        "description": "Always-on chat for the whole company",
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

    # Step 3 — auto-create a "General" group conversation. We seat
    # chief-of-staff as the anchor agent, turn auto_respond ON so the user can
    # just type, and turn smart_expand ON so other specialists get pulled in
    # when the topic shifts.
    anchor = GENERAL_DEFAULT_AGENT if GENERAL_DEFAULT_AGENT in AGENTS else next(iter(AGENTS))
    conv_doc = {
        "team_id": team_id,
        "title": "General",
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
            out.append(o)
    return out

@org_r.get("/{org_id}")
async def get_org(org_id: str, u: dict = Depends(get_current_user)):
    ctx = await require_org_member(org_id, u["id"])
    data = serialize_org(ctx["org"])
    data["role"] = ctx["member"]["role"]
    return data

@org_r.patch("/{org_id}")
async def update_org(org_id: str, body: OrgUpdateBody, u: dict = Depends(get_current_user)):
    ctx = await require_org_member(org_id, u["id"])
    if ctx["member"]["role"] not in ("owner", "admin"):
        raise HTTPException(403, "Only owners/admins can edit the org")
    update: dict = {}
    if body.name is not None:
        n = body.name.strip()
        if not n:
            raise HTTPException(400, "Name cannot be empty")
        update["name"] = n
    if body.website_url is not None:
        update["website_url"] = body.website_url.strip()
    if body.soul_description is not None:
        update["soul_description"] = body.soul_description.strip()
    if body.regenerate_soul:
        url = update.get("website_url", ctx["org"].get("website_url", ""))
        manual = update.get("soul_description", ctx["org"].get("soul_description", ""))
        name = update.get("name", ctx["org"].get("name", ""))
        text = await _fetch_url_text(url) if url else ""
        update["soul"] = await _generate_company_soul(name, text, manual)
    if update:
        await db.organizations.update_one({"_id": ObjectId(org_id)}, {"$set": update})
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
async def create_invite(org_id: str, body: InviteCreateBody, u: dict = Depends(get_current_user)):
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
    return {"code": code, "role": body.role, "expires_at": doc["expires_at"].isoformat()}

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

invite_r = APIRouter(prefix="/api/invites")

@invite_r.post("/accept")
async def accept_invite(body: InviteAcceptBody, u: dict = Depends(get_current_user)):
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
    if existing:
        await db.invites.update_one({"_id": inv["_id"]}, {"$set": {"used_by": u["id"]}})
        return {"org_id": inv["org_id"], "already_member": True}
    await db.org_members.insert_one({
        "org_id": inv["org_id"], "user_id": u["id"], "role": inv["role"],
        "joined_at": datetime.now(timezone.utc),
    })
    await db.invites.update_one({"_id": inv["_id"]}, {"$set": {"used_by": u["id"]}})
    return {"org_id": inv["org_id"], "already_member": False}

# ── Team routes ──────────────────────────────────────────────────────────────
team_r = APIRouter(prefix="/api")

@team_r.post("/orgs/{org_id}/teams")
async def create_team(org_id: str, body: TeamCreateBody, u: dict = Depends(get_current_user)):
    await require_org_member(org_id, u["id"])
    if not body.name.strip():
        raise HTTPException(400, "Team name required")
    now = datetime.now(timezone.utc)
    doc = {
        "org_id": org_id,
        "name": body.name.strip(),
        "description": body.description or "",
        "soulprint": {"mission": "", "tone": "", "audience": "", "values": "", "north_star": ""},
        "created_by": u["id"],
        "created_at": now,
    }
    res = await db.teams.insert_one(doc)
    team = await db.teams.find_one({"_id": res.inserted_id})
    return serialize_team(team)

@team_r.get("/orgs/{org_id}/teams")
async def list_teams(org_id: str, u: dict = Depends(get_current_user)):
    await require_org_member(org_id, u["id"])
    teams = await db.teams.find({"org_id": org_id}).sort("created_at", -1).to_list(500)
    return [serialize_team(t) for t in teams]

@team_r.get("/teams/{team_id}")
async def get_team(team_id: str, u: dict = Depends(get_current_user)):
    ctx = await require_team_member(team_id, u["id"])
    return serialize_team(ctx["team"])

@team_r.put("/teams/{team_id}/soulprint")
async def update_soulprint(team_id: str, body: SoulPrintBody, u: dict = Depends(get_current_user)):
    await require_team_member(team_id, u["id"])
    await db.teams.update_one(
        {"_id": ObjectId(team_id)},
        {"$set": {"soulprint": body.model_dump()}},
    )
    team = await db.teams.find_one({"_id": ObjectId(team_id)})
    return serialize_team(team)

@team_r.patch("/teams/{team_id}")
async def update_team(team_id: str, body: TeamUpdateBody, u: dict = Depends(get_current_user)):
    ctx = await require_team_member(team_id, u["id"])
    if ctx["org_role"] not in ("owner", "admin"):
        raise HTTPException(403, "Only owners/admins can edit a team")
    update: dict = {}
    if body.name is not None:
        n = body.name.strip()
        if not n:
            raise HTTPException(400, "Team name cannot be empty")
        update["name"] = n
    if body.description is not None:
        update["description"] = body.description.strip()[:500]
    if update:
        await db.teams.update_one({"_id": ObjectId(team_id)}, {"$set": update})
    fresh = await db.teams.find_one({"_id": ObjectId(team_id)})
    return serialize_team(fresh)

@team_r.delete("/teams/{team_id}")
async def delete_team(team_id: str, u: dict = Depends(get_current_user)):
    ctx = await require_team_member(team_id, u["id"])
    if ctx["org_role"] not in ("owner", "admin"):
        raise HTTPException(403, "Only owners/admins can delete a team")
    if ctx["team"].get("is_general"):
        raise HTTPException(400, "The General team cannot be deleted")
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
    await require_team_member(team_id, u["id"])
    agents = [public_agent(a) for a in AGENTS.values()]
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
    if current_user_name:
        lines.append(f"\nThe person you're talking to is {current_user_name}.")
    user_ctx_block = _user_context_block()
    if user_ctx_block:
        lines.append("\n" + user_ctx_block)
    return "\n".join(lines)

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
    ctx = await require_team_member(team_id, u["id"])
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
async def list_conversations(team_id: str, u: dict = Depends(get_current_user)):
    await require_team_member(team_id, u["id"])
    convs = await db.conversations.find(
        {"team_id": team_id, "member_ids": u["id"]}
    ).sort("created_at", -1).to_list(500)
    return [serialize_conv(c) for c in convs]

async def load_conv_or_403(conv_id: str, user_id: str) -> dict:
    try:
        c = await db.conversations.find_one({"_id": ObjectId(conv_id)})
    except Exception:
        raise HTTPException(400, "Invalid conversation id")
    if not c:
        raise HTTPException(404, "Conversation not found")
    if user_id not in c.get("member_ids", []):
        raise HTTPException(403, "Not a participant")
    return c

@conv_r.get("/conversations/{conv_id}")
async def get_conv(conv_id: str, u: dict = Depends(get_current_user)):
    c = await load_conv_or_403(conv_id, u["id"])
    return serialize_conv(c)

@conv_r.delete("/conversations/{conv_id}")
async def delete_conv(conv_id: str, u: dict = Depends(get_current_user)):
    c = await load_conv_or_403(conv_id, u["id"])
    if c["created_by"] != u["id"]:
        raise HTTPException(403, "Only the creator can delete")
    await db.messages.delete_many({"conversation_id": conv_id})
    await db.conversations.delete_one({"_id": ObjectId(conv_id)})
    return {"ok": True}

@conv_r.get("/conversations/{conv_id}/messages")
async def get_messages(conv_id: str, u: dict = Depends(get_current_user)):
    await load_conv_or_403(conv_id, u["id"])
    msgs = await db.messages.find(
        {"conversation_id": conv_id}, {"_id": 0}
    ).sort("timestamp", 1).to_list(1000)
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
WEB_SEARCH_AVAILABLE = bool(os.environ.get("BRAVE_API_KEY"))

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
    r"give\s*me|show\s*me|i\s*need|i\s*want|can\s*you\s*(make|create|design|generate|render|give|show))\b",
    re.IGNORECASE,
)

def _quick_image_intent(text: str) -> Optional[bool]:
    """Cheap deterministic pre-classifier. Returns True/False if confident,
    None if we should ask the LLM. Saves a roundtrip on the obvious cases."""
    if _VIDEO_KEYWORDS_RE.search(text):
        return False  # video request — don't shortcut to image
    if _IMAGE_KEYWORDS_RE.search(text) and _VERB_HINTS_RE.search(text):
        return True
    return None

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

def _quick_image_edit_intent(text: str, has_image: bool) -> bool:
    """Treat as edit intent when (a) the user has just attached an image
    AND (b) the prompt contains a clear edit/transformation verb. Returns
    False if no image is in play — fall through to the normal vision/desc
    path."""
    if not has_image or not text:
        return False
    return bool(_EDIT_VERBS_RE.search(text))

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
    fast = _quick_image_intent(message)
    if fast is not None:
        return fast
    system = (
        "You are a binary classifier. Decide whether the user's latest message is "
        "asking the assistant to GENERATE an image. Lean YES for: 'create/make/design "
        "an image / poster / graphic / logo / illustration / banner / thumbnail / "
        "Instagram or LinkedIn or Twitter post / story / cover photo'. Lean NO for: "
        "describing an existing image, asking for image-related advice, mere mentions, "
        "or any request that involves motion / animation / video. "
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
    if not results:
        return "", [], intent
    return format_results_for_prompt(results), results, intent

async def generate_agent_reply(conv: dict, team: dict, agent_id: str,
                               latest_user_text: str, latest_user_name: str) -> tuple[str, list]:
    """One LLM turn for `agent_id`. Returns (reply_text, web_sources)."""
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
    try:
        text = await chat.send_message(UserMessage(text=prompt))
    except Exception as exc:
        logger.exception("LLM error: %s", exc)
        return "I'm having trouble thinking right now — try me again in a moment.", []
    return text, web_sources

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
    """
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
            p = UPLOAD_DIR / msg["media_filename"]
            if p.is_file():
                return p
        if msg.get("attachment_kind") == "image" and msg.get("attachment_filename"):
            p = CONV_UPLOAD_DIR / conv_id / msg["attachment_filename"]
            if p.is_file():
                return p
    return None

@conv_r.post("/conversations/{conv_id}/messages")
async def send_message(conv_id: str, body: SendMessageBody, u: dict = Depends(get_current_user)):
    if not body.text.strip():
        raise HTTPException(400, "Message cannot be empty")
    conv = await load_conv_or_403(conv_id, u["id"])
    team = await db.teams.find_one({"_id": ObjectId(conv["team_id"])})
    if not team:
        raise HTTPException(404, "Team not found")

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
            responders = mentioned
        elif conv.get("auto_respond"):
            responders = list(conv["agent_ids"])
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

    if responders:
        try:
            wants_video = bool(os.environ.get("KIE_API_KEY")) and await detect_video_intent(body.text)
        except Exception:
            wants_video = False
        if wants_video:
            primary = responders[0]
            video_msg = await _kick_off_video(conv_id, primary, body.text)
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
                    fname, caption = await generate_image_via_nano_banana(body.text, sys_msg)
                    image_msg = {
                        "id": str(uuid.uuid4()),
                        "conversation_id": conv_id,
                        "sender_type": "agent",
                        "sender_id": primary,
                        "sender_name": AGENTS[primary]["name"],
                        "agent_id": primary,
                        "text": (caption or "Here's a draft.").strip(),
                        "media_type": "image",
                        "media_filename": fname,
                        "media_url": f"/api/files/{fname}",
                        "timestamp": datetime.now(timezone.utc),
                    }
                    await db.messages.insert_one({**image_msg})
                    responders = []  # Image alone is enough
                except Exception as exc:
                    logger.warning("auto image generation failed: %s", exc)

    agent_messages = []
    for aid in responders:
        reply, web_sources = await generate_agent_reply(conv, team, aid, body.text, u["name"])
        msg = {
            "id": str(uuid.uuid4()),
            "conversation_id": conv_id,
            "sender_type": "agent",
            "sender_id": aid,
            "sender_name": AGENTS[aid]["name"],
            "agent_id": aid,
            "text": reply,
            "timestamp": datetime.now(timezone.utc),
        }
        if web_sources:
            msg["web_sources"] = web_sources
        await db.messages.insert_one({**msg})
        agent_messages.append({k: v for k, v in msg.items() if k != "_id"})

    user_doc_resp = {k: v for k, v in user_doc.items() if k != "_id"}
    # Re-fetch fresh conv so frontend can mirror new agent_ids if expanded
    fresh_conv = await db.conversations.find_one({"_id": ObjectId(conv_id)})
    if image_msg is not None:
        agent_messages.insert(0, {k: v for k, v in image_msg.items() if k != "_id"})
    if video_msg is not None:
        agent_messages.insert(0, video_msg)
    return {
        "user_message": user_doc_resp,
        "agent_messages": agent_messages,
        "conversation": serialize_conv(fresh_conv) if fresh_conv else None,
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
        pending = await _kick_off_video(conv_id, chosen, body.text)
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

    reply, web_sources = await generate_agent_reply(conv_doc, team, chosen, body.text, u["name"])
    agent_msg = {
        "id": str(uuid.uuid4()),
        "conversation_id": conv_id,
        "sender_type": "agent",
        "sender_id": chosen,
        "sender_name": AGENTS[chosen]["name"],
        "agent_id": chosen,
        "text": reply,
        "timestamp": datetime.now(timezone.utc),
    }
    if web_sources:
        agent_msg["web_sources"] = web_sources
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

    reply, web_sources = await generate_agent_reply(conv, team, body.agent_id, intro_msg, u["name"])
    agent_msg = {
        "id": str(uuid.uuid4()),
        "conversation_id": conv_id,
        "sender_type": "agent",
        "sender_id": body.agent_id,
        "sender_name": AGENTS[body.agent_id]["name"],
        "agent_id": body.agent_id,
        "text": reply,
        "timestamp": datetime.now(timezone.utc),
    }
    if web_sources:
        agent_msg["web_sources"] = web_sources
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
    """Add a specialist to a group conversation. Posts a small system note so
    everyone knows who just joined."""
    conv = await load_conv_or_403(conv_id, u["id"])
    if conv["type"] != "group":
        raise HTTPException(400, "Only group conversations can have multiple agents")
    if not is_agent(body.agent_id):
        raise HTTPException(400, f"Unknown agent: {body.agent_id}")
    if body.agent_id in (conv.get("agent_ids") or []):
        raise HTTPException(400, "That agent is already in this conversation")
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
    if conv["type"] != "group":
        raise HTTPException(400, "Only group conversations support removing agents")
    if agent_id not in (conv.get("agent_ids") or []):
        raise HTTPException(404, "That agent isn't in this conversation")
    if len(conv.get("agent_ids") or []) <= 1:
        raise HTTPException(400, "A group must keep at least one agent — add another before removing this one")
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
        api_key=EMERGENT_LLM_KEY,
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
    # Pick a file extension that ALLOWED_FILENAME_RE accepts so the served
    # URL doesn't 400. Default to png for unknown bytes (browsers sniff content).
    if "webp" in mime:
        ext = "webp"
    elif "gif" in mime:
        ext = "gif"
    elif "jpeg" in mime or "jpg" in mime:
        ext = "jpg"
    else:
        ext = "png"
    filename = f"{uuid.uuid4().hex}.{ext}"
    (UPLOAD_DIR / filename).write_bytes(img_bytes)
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
        "timestamp": datetime.now(timezone.utc),
    }
    await db.messages.insert_one({**agent_msg})
    return {
        "user_message": {k: v for k, v in user_msg.items() if k != "_id"},
        "agent_message": {k: v for k, v in agent_msg.items() if k != "_id"},
        "ok": True,
    }

# ── Video generation tool (Kie.ai Veo 3.1) ──────────────────────────────────
async def _poll_and_save_video(message_id: str, prompt: str, aspect_ratio: str = "16:9") -> None:
    """Background coroutine: call Kie.ai, then update the placeholder message.

    The agent message is created with `media_status="pending"` immediately so
    the UI can render a spinner. When polling resolves we patch the message
    document with the final URL (or an error caption on failure).
    """
    try:
        url = await kie_generate_video(prompt, aspect_ratio=aspect_ratio)
    except Exception as exc:
        logger.exception("Kie polling crash: %s", exc)
        url = None
    now = datetime.now(timezone.utc)
    if url:
        await db.messages.update_one(
            {"id": message_id},
            {"$set": {
                "media_url": url,
                "media_status": "ready",
                "ready_at": now,
            }},
        )
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
        fpath.write_bytes(blob)
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
    agent_msg = {
        "id": str(uuid.uuid4()),
        "conversation_id": conv_id,
        "sender_type": "agent",
        "sender_id": responder,
        "sender_name": AGENTS[responder]["name"],
        "agent_id": responder,
        "text": reply or "Got the file — let me know what to dig into.",
        "intent": "attachment_reply",
        "timestamp": datetime.now(timezone.utc),
    }
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

    doc = {
        "org_id": org_id,
        "filename": filename,
        "original_name": original_name,
        "ext": ext,
        "mime": file.content_type or "application/octet-stream",
        "size": total,
        "kind": kind_for_ext(ext),
        "description": description.strip()[:500],
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
                                        "User-Agent": "Mozilla/5.0 (compatible; SoulPrintEngineBot/1.0; +https://soulprintengine.ai/bot)",
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

ALLOWED_FILENAME_RE = re.compile(r"^[a-f0-9]{16,}\.(png|jpg|jpeg|webp|gif|pdf)$", re.IGNORECASE)

@files_r.get("/assets/{filename}")
async def serve_org_asset(filename: str):
    """Serve an org-uploaded asset by its UUID-style filename.

    Same capability-URL pattern as `serve_file`: the random filename is the
    cap. Auth is dropped so `<img>` tags can render asset thumbnails inline
    in the org page (they cannot send Bearer tokens). The DB lookup keeps
    this from being a generic file dump."""
    if not ASSET_FILENAME_RE.match(filename):
        raise HTTPException(400, "Invalid filename")
    asset = await db.assets.find_one({"filename": filename})
    if not asset:
        raise HTTPException(404, "Asset not found")
    path = ASSETS_DIR / asset["org_id"] / asset["filename"]
    if not path.is_file():
        raise HTTPException(404, "File missing on disk")
    return FileResponse(path, filename=asset.get("original_name") or filename, media_type=asset.get("mime"))

CONV_ATTACH_FILENAME_RE = re.compile(r"^[a-f0-9]{16,}\.[a-z0-9]{1,8}$", re.IGNORECASE)

@files_r.get("/conv/{conv_id}/{filename}")
async def serve_conv_attachment(conv_id: str, filename: str):
    """Serve an in-conversation file upload.

    Capability-URL pattern (random uuid filename inside a random conv id),
    no auth so `<img>` / `<video>` can render inline. Defensive lookup
    confirms the message exists before serving."""
    if not CONV_ATTACH_FILENAME_RE.match(filename):
        raise HTTPException(400, "Invalid filename")
    msg = await db.messages.find_one({
        "conversation_id": conv_id,
        "attachment_filename": filename,
    })
    if not msg:
        raise HTTPException(404, "Not linked to any message")
    path = CONV_UPLOAD_DIR / conv_id / filename
    if not path.is_file():
        raise HTTPException(404, "File missing on disk")
    return FileResponse(path,
                        filename=msg.get("attachment_original_name") or filename,
                        media_type=msg.get("attachment_mime") or None)

@files_r.get("/{filename}")
async def serve_file(filename: str):
    """Serve a chat-generated media file by its UUID4 filename.

    The filenames are 122-bit-random UUID4s emitted only inside messages of
    conversations the user is a member of, so the URL itself is the
    capability. Auth is intentionally NOT enforced here so that <img> tags
    (which cannot send Bearer tokens and may have their cookies dropped by
    third-party browsers) can render images inline."""
    if not ALLOWED_FILENAME_RE.match(filename):
        raise HTTPException(400, "Invalid filename")
    path = UPLOAD_DIR / filename
    if not path.is_file():
        raise HTTPException(404, "File not found")
    # Confirm it was actually persisted by an agent reply (defensive — keeps
    # the upload dir from being treated as a public dump if anything ever
    # writes to it directly).
    msg = await db.messages.find_one({"media_filename": filename})
    if not msg:
        raise HTTPException(404, "Not linked to any message")
    return FileResponse(path)

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
            )
            msg = {
                "id": str(uuid.uuid4()),
                "conversation_id": conv_id,
                "sender_type": "agent",
                "sender_id": aid,
                "sender_name": AGENTS[aid]["name"],
                "agent_id": aid,
                "text": reply,
                "schedule_id": sched_id,
                "timestamp": datetime.now(timezone.utc),
            }
            if web_sources:
                msg["web_sources"] = web_sources
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

app.include_router(base_r)
app.include_router(auth_r)
app.include_router(org_r)
app.include_router(invite_r)
app.include_router(team_r)
app.include_router(conv_r)
app.include_router(assets_r)
app.include_router(files_r)
app.include_router(unfurl_r)
app.include_router(schedules_r)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origin_regex=r".*",
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
async def startup():
    await db.users.create_index("email", unique=True)
    await db.org_members.create_index([("org_id", 1), ("user_id", 1)], unique=True)
    await db.invites.create_index("code", unique=True)
    await db.teams.create_index("org_id")
    await db.conversations.create_index([("team_id", 1), ("created_at", -1)])
    await db.messages.create_index([("conversation_id", 1), ("timestamp", 1)])
    await db.messages.create_index("media_filename", sparse=True)
    await db.schedules.create_index([("org_id", 1), ("created_at", -1)])
    await db.schedules.create_index("created_by")
    await db.unfurl_cache.create_index("url", unique=True)
    # Boot the in-process scheduler and rehydrate persisted jobs
    if not scheduler.running:
        scheduler.start()
    async for s in db.schedules.find({"active": True}):
        try:
            _scheduler_register(s)
        except Exception as exc:
            logger.warning("Could not rehydrate schedule %s: %s", s.get("_id"), exc)
    logger.info("SoulPrint API ready.")

@app.on_event("shutdown")
async def shutdown():
    try:
        if scheduler.running:
            scheduler.shutdown(wait=False)
    except Exception:
        pass
    client.close()

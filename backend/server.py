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

from brave_search import brave_web_search, format_results_for_prompt
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
    return {"id": str(user["_id"]), "email": user["email"], "name": user["name"]}

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

class TeamCreateBody(BaseModel):
    name: str
    description: Optional[str] = ""

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

@org_r.post("")
async def create_org(body: OrgCreateBody, u: dict = Depends(get_current_user)):
    if not body.name.strip():
        raise HTTPException(400, "Name required")
    now = datetime.now(timezone.utc)
    doc = {"name": body.name.strip(), "owner_id": u["id"], "created_at": now}
    res = await db.organizations.insert_one(doc)
    oid = str(res.inserted_id)
    await db.org_members.insert_one({
        "org_id": oid, "user_id": u["id"], "role": "owner", "joined_at": now,
    })
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
                        org_assets: Optional[List[dict]] = None) -> str:
    meta = AGENTS[agent_id]
    sp = team.get("soulprint") or {}
    lines = [meta["system"]]
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

async def detect_image_intent(message: str) -> bool:
    """Quick LLM classifier: does this message want an image generated?

    Returns True for clear image asks ("design a logo", "create an Instagram
    post", "give me a hero illustration", etc.). Returns False for everything
    else, including text that merely *mentions* images.
    """
    if not message.strip():
        return False
    system = (
        "You are a binary classifier. Decide whether the user's latest message is "
        "asking the assistant to GENERATE an image. Lean YES for: 'create/make/design "
        "an image / poster / graphic / logo / illustration / banner / thumbnail / "
        "Instagram or LinkedIn or Twitter post / story / cover photo'. Lean NO for: "
        "describing an existing image, asking for image-related advice, or mere mentions. "
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
    try:
        org_assets_brief = await load_assets_brief(team["org_id"])
    except Exception as exc:
        logger.warning("asset brief load failed: %s", exc)

    system = build_system_prompt(agent_id, team, current_user_name=latest_user_name,
                                 org_assets=org_assets_brief)
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

    # ── Image-generation auto-routing ──────────────────────────────────────
    # If the user clearly asked for an image, generate one with the first
    # responder instead of (or in addition to) writing a text reply.
    image_msg = None
    if responders:
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
                # Image alone is enough — skip the text reply for this turn
                responders = []
            except Exception as exc:
                logger.warning("auto image generation failed: %s", exc)
                # Fall through to a normal text reply

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

# ── Image generation tool (Nano Banana via Emergent LLM key) ─────────────────
NANO_BANANA_MODEL = "gemini-3.1-flash-image-preview"

async def generate_image_via_nano_banana(prompt: str, system_message: str) -> tuple[str, str]:
    """Generate an image and persist it to disk. Returns (filename, caption_text)."""
    from emergentintegrations.llm.chat import LlmChat as _LlmChat, UserMessage as _UM
    chat = _LlmChat(
        api_key=EMERGENT_LLM_KEY,
        session_id=f"img:{uuid.uuid4()}",
        system_message=system_message,
    ).with_model("gemini", NANO_BANANA_MODEL).with_params(modalities=["image", "text"])
    text, images = await chat.send_message_multimodal_response(_UM(text=prompt))
    if not images:
        raise RuntimeError("No image returned by the model")
    img = images[0]
    img_bytes = base64.b64decode(img["data"])
    mime = (img.get("mime_type") or "image/png").lower()
    ext = "png" if "png" in mime else "jpg" if "jpeg" in mime or "jpg" in mime else "bin"
    filename = f"{uuid.uuid4().hex}.{ext}"
    (UPLOAD_DIR / filename).write_bytes(img_bytes)
    return filename, (text or "").strip()

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

ALLOWED_FILENAME_RE = re.compile(r"^[a-f0-9]{16,}\.(png|jpg|jpeg|webp|gif|pdf)$", re.IGNORECASE)

@files_r.get("/assets/{filename}")
async def serve_org_asset(filename: str, u: dict = Depends(get_current_user)):
    if not ASSET_FILENAME_RE.match(filename):
        raise HTTPException(400, "Invalid filename")
    asset = await db.assets.find_one({"filename": filename})
    if not asset:
        raise HTTPException(404, "Asset not found")
    # Caller must be a member of the asset's org
    m = await db.org_members.find_one({"org_id": asset["org_id"], "user_id": u["id"]})
    if not m:
        raise HTTPException(403, "No access to this asset")
    path = ASSETS_DIR / asset["org_id"] / asset["filename"]
    if not path.is_file():
        raise HTTPException(404, "File missing on disk")
    return FileResponse(path, filename=asset.get("original_name") or filename, media_type=asset.get("mime"))

@files_r.get("/{filename}")
async def serve_file(filename: str, u: dict = Depends(get_current_user)):
    if not ALLOWED_FILENAME_RE.match(filename):
        raise HTTPException(400, "Invalid filename")
    path = UPLOAD_DIR / filename
    if not path.is_file():
        raise HTTPException(404, "File not found")
    # Verify the requesting user has access to a conversation that references this file
    msg = await db.messages.find_one({"media_filename": filename})
    if not msg:
        raise HTTPException(404, "Not linked to any message")
    conv = await db.conversations.find_one({"_id": ObjectId(msg["conversation_id"])})
    if not conv or u["id"] not in conv.get("member_ids", []):
        raise HTTPException(403, "No access to this file")
    return FileResponse(path)

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
    logger.info("SoulPrint API ready.")

@app.on_event("shutdown")
async def shutdown():
    client.close()

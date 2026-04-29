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
from fastapi import FastAPI, APIRouter, HTTPException, Request, Response, Depends
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from pathlib import Path
from pydantic import BaseModel, Field
from typing import List, Optional, Literal
from datetime import datetime, timezone, timedelta
from bson import ObjectId
import os, logging, uuid, secrets, re, json, bcrypt, jwt

from emergentintegrations.llm.chat import LlmChat, UserMessage

ROOT_DIR = Path(__file__).parent
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

class SendMessageBody(BaseModel):
    text: str

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
    set_auth_cookies(resp, create_access_token(uid, email))
    return {"id": uid, "name": doc["name"], "email": email}

@auth_r.post("/login")
async def login(body: LoginBody, resp: Response):
    email = body.email.lower().strip()
    user = await db.users.find_one({"email": email})
    if not user or not verify_password(body.password, user["password_hash"]):
        raise HTTPException(401, "Invalid email or password")
    uid = str(user["_id"])
    set_auth_cookies(resp, create_access_token(uid, email))
    return {"id": uid, "name": user["name"], "email": email}

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

def build_system_prompt(agent_id: str, team: dict, current_user_name: Optional[str] = None) -> str:
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
    if current_user_name:
        lines.append(f"\nThe person you're talking to is {current_user_name}.")
    return "\n".join(lines)

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

async def generate_agent_reply(conv: dict, team: dict, agent_id: str,
                               latest_user_text: str, latest_user_name: str) -> str:
    """Build a one-shot LLM call with a compact transcript for group context."""
    tail = await build_transcript_tail(str(conv["_id"]), limit=12)

    # Compose a compact conversation context for group mode
    transcript_lines = []
    for m in tail:
        if m["sender_type"] == "user":
            transcript_lines.append(f"{m.get('sender_name','User')}: {m['text']}")
        else:
            a = m.get("agent_id")
            aname = AGENTS[a]["name"] if a in AGENTS else "Agent"
            transcript_lines.append(f"{aname} (agent): {m['text']}")
    transcript = "\n".join(transcript_lines) if transcript_lines else ""

    system = build_system_prompt(agent_id, team, current_user_name=latest_user_name)
    if conv["type"] == "group":
        system += (
            "\n\nYou are one of several participants in a GROUP conversation. "
            "Other humans and other AI agents may also respond. Stay in your role — "
            "don't impersonate other agents or humans. Keep replies tight and additive."
        )

    # Use a fresh one-shot chat per turn with the compact transcript as the user payload.
    # session_id per conversation+agent keeps provider-side context across turns too.
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
        return await chat.send_message(UserMessage(text=prompt))
    except Exception as exc:
        logger.exception("LLM error: %s", exc)
        return "I'm having trouble thinking right now — try me again in a moment."

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
            responders = conv["agent_ids"]
        else:
            responders = []

    agent_messages = []
    for aid in responders:
        reply = await generate_agent_reply(conv, team, aid, body.text, u["name"])
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
        await db.messages.insert_one({**msg})
        agent_messages.append({k: v for k, v in msg.items() if k != "_id"})

    user_doc_resp = {k: v for k, v in user_doc.items() if k != "_id"}
    return {"user_message": user_doc_resp, "agent_messages": agent_messages}

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

frontend_url = os.environ.get("FRONTEND_URL", "http://localhost:3000")
app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=[frontend_url, "http://localhost:3000"],
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
    logger.info("SoulPrint API ready.")

@app.on_event("shutdown")
async def shutdown():
    client.close()

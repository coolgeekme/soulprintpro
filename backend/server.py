from fastapi import FastAPI, APIRouter, HTTPException, Request, Response, Depends
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import logging
from pathlib import Path
from pydantic import BaseModel, Field, ConfigDict, EmailStr
from typing import List, Optional
import uuid
from datetime import datetime, timezone, timedelta
import bcrypt
import jwt
from bson import ObjectId
from emergentintegrations.llm.chat import LlmChat, UserMessage


ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# Configure logging early so seed_admin can use it
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# MongoDB connection
mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

# Create the main app without a prefix
app = FastAPI()

# Create a router with the /api prefix
api_router = APIRouter(prefix="/api")

# ── Auth utilities ──────────────────────────────────────────────────────────
JWT_SECRET = os.environ.get("JWT_SECRET", "fallback-secret")
JWT_ALGORITHM = "HS256"

def hash_password(password: str) -> str:
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password.encode(), salt).decode()

def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())

def create_access_token(user_id: str, email: str) -> str:
    payload = {
        "sub": user_id,
        "email": email,
        "exp": datetime.now(timezone.utc) + timedelta(hours=24),
        "type": "access",
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

def create_refresh_token(user_id: str) -> str:
    payload = {
        "sub": user_id,
        "exp": datetime.now(timezone.utc) + timedelta(days=7),
        "type": "refresh",
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

async def get_current_user(request: Request) -> dict:
    token = request.cookies.get("access_token")
    if not token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        if payload.get("type") != "access":
            raise HTTPException(status_code=401, detail="Invalid token type")
        user = await db.users.find_one({"_id": ObjectId(payload["sub"])})
        if not user:
            raise HTTPException(status_code=401, detail="User not found")
        user["id"] = str(user["_id"])
        del user["_id"]
        user.pop("password_hash", None)
        return user
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")

def set_auth_cookies(response: Response, access_token: str, refresh_token: str):
    response.set_cookie("access_token", access_token, httponly=True, secure=False, samesite="lax", max_age=86400, path="/")
    response.set_cookie("refresh_token", refresh_token, httponly=True, secure=False, samesite="lax", max_age=604800, path="/")

# ── Auth models ─────────────────────────────────────────────────────────────
class UserRegister(BaseModel):
    name: str
    email: str
    password: str

class UserLogin(BaseModel):
    email: str
    password: str

# ── Auth router ─────────────────────────────────────────────────────────────
auth_router = APIRouter(prefix="/api/auth")

@auth_router.post("/register")
async def register(data: UserRegister, response: Response):
    email = data.email.lower().strip()
    if await db.users.find_one({"email": email}):
        raise HTTPException(status_code=400, detail="Email already registered")
    doc = {
        "name": data.name.strip(),
        "email": email,
        "password_hash": hash_password(data.password),
        "role": "user",
        "created_at": datetime.now(timezone.utc),
    }
    result = await db.users.insert_one(doc)
    user_id = str(result.inserted_id)
    set_auth_cookies(response, create_access_token(user_id, email), create_refresh_token(user_id))
    return {"id": user_id, "name": doc["name"], "email": email}

@auth_router.post("/login")
async def login(data: UserLogin, response: Response):
    email = data.email.lower().strip()
    user = await db.users.find_one({"email": email})
    if not user or not verify_password(data.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    user_id = str(user["_id"])
    set_auth_cookies(response, create_access_token(user_id, email), create_refresh_token(user_id))
    return {"id": user_id, "name": user["name"], "email": email}

@auth_router.post("/logout")
async def logout(response: Response):
    response.delete_cookie("access_token", path="/")
    response.delete_cookie("refresh_token", path="/")
    return {"message": "Logged out"}

@auth_router.get("/me")
async def get_me(current_user: dict = Depends(get_current_user)):
    return current_user

# ── Startup ──────────────────────────────────────────────────────────────────
async def seed_admin():
    admin_email = os.environ.get("ADMIN_EMAIL", "admin@wingman.ai")
    admin_password = os.environ.get("ADMIN_PASSWORD", "admin123")
    existing = await db.users.find_one({"email": admin_email})
    if not existing:
        await db.users.insert_one({
            "name": "Admin",
            "email": admin_email,
            "password_hash": hash_password(admin_password),
            "role": "admin",
            "created_at": datetime.now(timezone.utc),
        })
        logger.info(f"Admin seeded: {admin_email}")

@app.on_event("startup")
async def startup():
    await db.users.create_index("email", unique=True)
    await db.messages.create_index([("user_id", 1), ("agent_id", 1), ("timestamp", 1)])
    await seed_admin()

# ── Agent personalities ──────────────────────────────────────────────────────
AGENT_PROMPTS = {
    "jarvis": (
        "You are Jarvis, a senior software engineer and tech co-founder. "
        "You help with code reviews, system architecture, debugging, DevOps, and technical strategy. "
        "Be concise, technical, and action-oriented. Use short code snippets when genuinely helpful. "
        "Keep responses brief — 2-4 sentences — unless a code block is truly needed."
    ),
    "judy": (
        "You are Judy, a sharp sales lead and business development expert. "
        "You help with pipeline management, prospect outreach, pitch prep, CRM updates, and closing strategies. "
        "Be energetic, results-driven, and strategic. Keep responses punchy and actionable — 2-3 sentences."
    ),
    "alfred": (
        "You are Alfred, a precise and professional executive assistant. "
        "You help with scheduling, email drafting, calendar management, meeting prep, and task prioritization. "
        "Be organized and proactive. Always confirm the action you've taken. Keep responses concise — 2-3 sentences."
    ),
    "vera": (
        "You are Vera, a creative content creator and copywriter. "
        "You help with social media posts, blog content, newsletters, brand messaging, and content strategy. "
        "Be creative and brand-conscious. When generating content, offer 2-3 short options. Keep responses focused."
    ),
    "max": (
        "You are Max, a rigorous research analyst and data specialist. "
        "You help with market research, competitive analysis, data interpretation, and strategic reports. "
        "Lead with the key insight. Be analytical and precise — 2-3 sentences unless a structured list adds real value."
    ),
}

# In-memory LLM session cache (persists while server is running)
_llm_sessions: dict = {}

def get_llm_session(user_id: str, agent_id: str) -> LlmChat:
    key = f"{user_id}:{agent_id}"
    if key not in _llm_sessions:
        system_prompt = AGENT_PROMPTS.get(agent_id, "You are a helpful AI assistant.")
        _llm_sessions[key] = LlmChat(
            api_key=os.environ.get("EMERGENT_LLM_KEY", ""),
            session_id=key,
            system_message=system_prompt,
        ).with_model("openai", "gpt-4.1-mini")
    return _llm_sessions[key]

# ── Chat models ──────────────────────────────────────────────────────────────
class SendMessageBody(BaseModel):
    text: str

# ── Chat router ──────────────────────────────────────────────────────────────
chat_router = APIRouter(prefix="/api/chat")

@chat_router.get("/{agent_id}")
async def get_messages(agent_id: str, current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    docs = await db.messages.find(
        {"user_id": user_id, "agent_id": agent_id},
        {"_id": 0, "user_id": 0},
    ).sort("timestamp", 1).to_list(300)
    return docs

@chat_router.post("/{agent_id}")
async def send_message(
    agent_id: str,
    body: SendMessageBody,
    current_user: dict = Depends(get_current_user),
):
    if agent_id not in AGENT_PROMPTS:
        raise HTTPException(status_code=404, detail="Agent not found")

    user_id = current_user["id"]
    now = datetime.now(timezone.utc)

    user_msg = {
        "id": str(uuid.uuid4()),
        "user_id": user_id,
        "agent_id": agent_id,
        "sender": "user",
        "text": body.text,
        "timestamp": now,
    }
    await db.messages.insert_one({**user_msg})

    # Call the LLM
    try:
        llm = get_llm_session(user_id, agent_id)
        reply_text = await llm.send_message(UserMessage(text=body.text))
    except Exception as exc:
        logger.error(f"LLM error for {agent_id}: {exc}")
        reply_text = "I'm having a bit of trouble right now — please try again in a moment."

    agent_msg = {
        "id": str(uuid.uuid4()),
        "user_id": user_id,
        "agent_id": agent_id,
        "sender": "agent",
        "text": reply_text,
        "timestamp": datetime.now(timezone.utc),
    }
    await db.messages.insert_one({**agent_msg})

    # Strip internal fields before returning
    user_msg.pop("user_id", None)
    agent_msg.pop("user_id", None)
    return {"user_message": user_msg, "agent_message": agent_msg}

@chat_router.delete("/{agent_id}")
async def clear_messages(agent_id: str, current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    await db.messages.delete_many({"user_id": user_id, "agent_id": agent_id})
    _llm_sessions.pop(f"{user_id}:{agent_id}", None)
    return {"message": "Conversation cleared"}
class StatusCheck(BaseModel):
    model_config = ConfigDict(extra="ignore")  # Ignore MongoDB's _id field
    
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    client_name: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class StatusCheckCreate(BaseModel):
    client_name: str

# Add your routes to the router instead of directly to app
@api_router.get("/")
async def root():
    return {"message": "Hello World"}

@api_router.post("/status", response_model=StatusCheck)
async def create_status_check(input: StatusCheckCreate):
    status_dict = input.model_dump()
    status_obj = StatusCheck(**status_dict)
    
    # Convert to dict and serialize datetime to ISO string for MongoDB
    doc = status_obj.model_dump()
    doc['timestamp'] = doc['timestamp'].isoformat()
    
    _ = await db.status_checks.insert_one(doc)
    return status_obj

@api_router.get("/status", response_model=List[StatusCheck])
async def get_status_checks():
    # Exclude MongoDB's _id field from the query results
    status_checks = await db.status_checks.find({}, {"_id": 0}).to_list(1000)
    
    # Convert ISO string timestamps back to datetime objects
    for check in status_checks:
        if isinstance(check['timestamp'], str):
            check['timestamp'] = datetime.fromisoformat(check['timestamp'])
    
    return status_checks

# Include the routers in the main app
app.include_router(api_router)
app.include_router(auth_router)
app.include_router(chat_router)

frontend_url = os.environ.get("FRONTEND_URL", "http://localhost:3000")
app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=[frontend_url, "http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()
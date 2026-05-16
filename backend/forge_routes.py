"""Forge Crawl HTTP routes.

Lightweight thin wrappers over ``forge_crawl`` so the frontend (and
external Foundry clients) can call our in-house alternative to
Firecrawl without going through a mission. All endpoints are
authenticated; rate-limit policy is provided by the caller (we expose
the org_id back so the SpendDashboard tracker can attribute costs).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class ScrapeBody(BaseModel):
    url: str
    render: str = Field(default="auto", description="auto | force | never")


class CrawlBody(BaseModel):
    url: str
    max_pages: int = Field(default=5, ge=1, le=20)
    render: str = Field(default="auto")


class ExtractBody(BaseModel):
    url: str
    schema_: Dict[str, Any] = Field(default_factory=dict, alias="schema")
    instructions: str = ""
    render: str = "auto"

    class Config:
        populate_by_name = True


def make_forge_router(
    *,
    get_current_user,
    LlmChat,
    UserMessage,
    EMERGENT_LLM_KEY: str,
):
    r = APIRouter(prefix="/api/forge")

    @r.post("/scrape")
    async def scrape_one(body: ScrapeBody, u: dict = Depends(get_current_user)):
        if not body.url:
            raise HTTPException(400, "url is required")
        from forge_crawl import scrape as _scrape
        render = body.render if body.render in {"auto", "force", "never"} else "auto"
        result = await _scrape(body.url, render=render)
        # Keep the response shape user-friendly: surface ok + error
        # at the top-level so the frontend can short-circuit on failure.
        return {
            "ok": result.get("ok", False),
            "error": result.get("error", ""),
            "url": result.get("url"),
            "final_url": result.get("final_url"),
            "status": result.get("status"),
            "engine": result.get("engine"),
            "title": result.get("title"),
            "markdown": result.get("markdown") or "",
            "text": result.get("text") or "",
            "links": (result.get("links") or [])[:200],
        }

    @r.post("/crawl")
    async def crawl_shallow(body: CrawlBody, u: dict = Depends(get_current_user)):
        if not body.url:
            raise HTTPException(400, "url is required")
        from forge_crawl import crawl as _crawl
        render = body.render if body.render in {"auto", "force", "never"} else "auto"
        result = await _crawl(body.url, max_pages=body.max_pages, render=render)
        # Slim down per-page payload — full HTML/text is rarely useful
        # to a frontend caller; if they want it they can call /scrape
        # individually.
        pages_out = []
        for p in (result.get("pages") or []):
            if not p:
                continue
            pages_out.append({
                "url": p.get("final_url"),
                "title": p.get("title"),
                "ok": p.get("ok"),
                "error": p.get("error") or "",
                "status": p.get("status"),
                "engine": p.get("engine"),
                "markdown": p.get("markdown") or "",
            })
        return {
            "ok": result.get("ok", False),
            "error": result.get("error", ""),
            "url": result.get("url"),
            "pages": pages_out,
        }

    @r.post("/extract")
    async def extract_structured(
        body: ExtractBody, u: dict = Depends(get_current_user),
    ):
        if not body.url:
            raise HTTPException(400, "url is required")
        if not body.schema_ or not isinstance(body.schema_, dict):
            raise HTTPException(400, "schema must be a non-empty JSON object")
        from forge_crawl import extract as _extract
        render = body.render if body.render in {"auto", "force", "never"} else "auto"
        result = await _extract(
            body.url,
            schema=body.schema_,
            instructions=body.instructions or "",
            render=render,
            llm_chat_cls=LlmChat,
            user_message_cls=UserMessage,
            emergent_llm_key=EMERGENT_LLM_KEY,
        )
        return {
            "ok": result.get("ok", False),
            "error": result.get("error", ""),
            "url": result.get("url"),
            "final_url": result.get("final_url"),
            "engine": result.get("engine"),
            "data": result.get("data"),
        }

    return r

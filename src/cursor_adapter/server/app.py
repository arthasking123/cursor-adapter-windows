from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Any, Dict, List, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from ..adapters.cursor_window_client import CursorWindowClient


load_dotenv()

logger = logging.getLogger(__name__)


def _setup_logging() -> None:
    level_name = (os.getenv("CURSOR_ADAPTER_LOG_LEVEL", "INFO") or "INFO").strip().upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)).strip())
    except Exception:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool", "developer"]
    content: Optional[str] = None


class ChatCompletionsRequest(BaseModel):
    model: str = Field(default="cursor-window")
    messages: List[ChatMessage]
    temperature: Optional[float] = None
    stream: Optional[bool] = False


def _messages_to_prompts(messages: List[ChatMessage]) -> tuple[str, str]:
    system_parts: List[str] = []
    user_parts: List[str] = []

    for m in messages:
        c = (m.content or "").strip()
        if not c:
            continue
        if m.role in {"system", "developer"}:
            system_parts.append(c)
        elif m.role == "user":
            user_parts.append(c)
        elif m.role == "assistant":
            # keep assistant history as context but avoid confusing UI extraction
            user_parts.append(f"[assistant]\n{c}")
        else:
            user_parts.append(f"[{m.role}]\n{c}")

    system_prompt = "\n\n".join(system_parts).strip() or "You are a helpful assistant."
    user_content = "\n\n".join(user_parts).strip()
    if not user_content:
        user_content = "Hello"
    return system_prompt, user_content


def _openai_chat_completion_response(model: str, content: str) -> Dict[str, Any]:
    created = int(time.time())
    return {
        "id": f"chatcmpl_{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


app = FastAPI(title="Cursor Adapter (Windows) — OpenAI Compatible", version="0.1.0")


@app.get("/health")
def health() -> Dict[str, Any]:
    return {"ok": True}


@app.get("/v1/models")
def list_models() -> Dict[str, Any]:
    return {
        "object": "list",
        "data": [
            {
                "id": "cursor-window",
                "object": "model",
                "created": 0,
                "owned_by": "local",
            }
        ],
    }
    


@app.post("/v1/chat/completions")
def chat_completions(req: ChatCompletionsRequest) -> Dict[str, Any]:
    if req.stream:
        raise HTTPException(status_code=400, detail="stream=true is not supported in this adapter yet")

    title_regex = os.getenv("CURSOR_WINDOW_TITLE_REGEX", ".*Cursor.*")
    wait_seconds = _env_int("CURSOR_WINDOW_WAIT_SECONDS", 35)
    min_chars = _env_int("CURSOR_WINDOW_MIN_RESPONSE_CHARS", 1)
    clipboard_fallback = _env_bool("CURSOR_WINDOW_CLIPBOARD_FALLBACK", False)

    system_prompt, user_content = _messages_to_prompts(req.messages)

    try:
        client = CursorWindowClient(
            title_regex=title_regex,
            wait_seconds=wait_seconds,
            min_response_chars=min_chars,
            enable_clipboard_fallback=clipboard_fallback,
        )
        answer = client.complete(system_prompt=system_prompt, user_content=user_content)
    except Exception as e:
        logger.exception("chat_completions failed: %s", type(e).__name__)
        raise HTTPException(status_code=500, detail=str(e))

    return _openai_chat_completion_response(model=req.model or "cursor-window", content=answer)


def main() -> None:
    import uvicorn

    _setup_logging()
    host = os.getenv("CURSOR_ADAPTER_HOST", "127.0.0.1")
    port = _env_int("CURSOR_ADAPTER_PORT", 17325)
    uvicorn.run("cursor_adapter.server.app:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()


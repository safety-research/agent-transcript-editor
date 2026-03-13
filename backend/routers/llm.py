"""
LLM Router - API key management and utility endpoints.
"""

import os

import anthropic
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()

from sessions import DEFAULT_MODEL  # noqa: E402


def resolve_api_key(api_key_id: str = "default", *, required: bool = True) -> str | None:
    """Resolve Anthropic API key by ID.

    Args:
        api_key_id: "default" or "alt".
        required: If True, raises HTTPException when key is missing.
                  If False, returns None for default key (lets SDK use its own default).
    """
    if api_key_id == "alt":
        key = os.getenv("ANTHROPIC_API_KEY_ALT")
        if not key:
            raise HTTPException(
                status_code=400,
                detail="ANTHROPIC_API_KEY_ALT not configured in backend .env",
            )
        return key
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key and required:
        raise HTTPException(
            status_code=500,
            detail="ANTHROPIC_API_KEY not configured. Set it in .env file.",
        )
    return key or None


def get_client(api_key_id: str = "default") -> anthropic.Anthropic:
    """Get Anthropic client with API key from environment."""
    return anthropic.Anthropic(api_key=resolve_api_key(api_key_id))


def get_sync_client() -> anthropic.Anthropic:
    """Get synchronous Anthropic client for utility calls (test, count_tokens)."""
    return get_client("default")


class TestConnectionResponse(BaseModel):
    success: bool
    error: str | None = None


@router.post("/test", response_model=TestConnectionResponse)
async def test_connection():
    """
    Test the Anthropic API connection.

    Returns success if the API key is valid and the API is reachable.
    """
    try:
        client = get_sync_client()
        client.messages.create(
            model=DEFAULT_MODEL,
            max_tokens=1,
            messages=[{"role": "user", "content": "Hi"}],
        )
        return TestConnectionResponse(success=True)
    except anthropic.AuthenticationError:
        return TestConnectionResponse(success=False, error="Invalid API key")
    except anthropic.APIError as e:
        return TestConnectionResponse(success=False, error=str(e))
    except Exception as e:
        return TestConnectionResponse(success=False, error=str(e))


class CountTokensRequest(BaseModel):
    """Request to count tokens in text."""

    text: str


@router.post("/count-tokens")
async def count_tokens(request: CountTokensRequest):
    """Count tokens in the given text using the Anthropic tokenizer."""
    try:
        client = get_sync_client()
        result = client.messages.count_tokens(
            model=DEFAULT_MODEL,
            messages=[{"role": "user", "content": request.text}],
        )
        return {"token_count": result.input_tokens}
    except Exception:
        rough_count = len(request.text) // 4
        return {"token_count": rough_count, "estimated": True}


@router.get("/status")
async def get_status():
    """Check if API keys are configured."""
    has_key = bool(os.getenv("ANTHROPIC_API_KEY"))
    has_alt_key = bool(os.getenv("ANTHROPIC_API_KEY_ALT"))
    return {"configured": has_key, "alt_configured": has_alt_key}


@router.get("/config")
async def get_config():
    """Return available models per API key, read from env vars.

    Env format:
        MODELS_DEFAULT=claude-opus-4-6,claude-sonnet-4-5-20250929
        MODELS_ALT=some-model-id,another-model-id
    """
    default_models_raw = os.getenv("MODELS_DEFAULT", "claude-opus-4-6,claude-sonnet-4-5-20250929")
    default_models = [m.strip() for m in default_models_raw.split(",") if m.strip()]

    alt_models: list[str] = []
    if os.getenv("ANTHROPIC_API_KEY_ALT"):
        alt_models_raw = os.getenv("MODELS_ALT", "")
        alt_models = [m.strip() for m in alt_models_raw.split(",") if m.strip()]

    return {
        "keys": {
            "default": {"models": default_models},
            **({"alt": {"models": alt_models}} if alt_models else {}),
        },
    }

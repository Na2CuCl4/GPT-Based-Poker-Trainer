"""GPT client — wraps Azure OpenAI Responses API (same pattern as test_gpt.py)."""
from __future__ import annotations

import os
from typing import Type, TypeVar

from openai import OpenAI
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

_client: OpenAI | None = None
_config: dict = {}


def init(config: dict) -> None:
    global _client, _config
    _config = config
    ai_cfg = config.get("ai", {})
    base_url = ai_cfg.get("base_url")
    api_key = ai_cfg.get("api_key")
    if not base_url or not api_key:
        raise ValueError(
            "AI configuration must include 'base_url' and 'api_key'.")
    _client = OpenAI(
        base_url=base_url,
        api_key=api_key,
    )


def parse_response(system_prompt: str, user_prompt: str, schema: Type[T]) -> T:
    """Send a structured-output request and return a parsed Pydantic model."""
    if _client is None:
        raise RuntimeError(
            "GPT client not initialised. Call gpt_client.init(config) first.")
    model = _config.get("ai", {}).get("model", "gpt-5.4")
    response = _client.responses.parse(
        model=model,
        input=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        text_format=schema,
    )
    return response.output_parsed

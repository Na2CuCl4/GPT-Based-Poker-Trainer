"""GPT client — wraps Azure OpenAI Responses API (same pattern as test_gpt.py)."""
from __future__ import annotations

import threading
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


def parse_response(system_prompt: str, user_prompt: str, schema: Type[T], timeout: float = 30.0) -> T:
    """Send a structured-output request and return a parsed Pydantic model.

    Raises TimeoutError if the API call does not complete within *timeout* seconds.
    The underlying network thread continues in the background but its result is ignored.
    """
    if _client is None:
        raise RuntimeError(
            "GPT client not initialised. Call gpt_client.init(config) first.")
    model = _config.get("ai", {}).get("model", "gpt-5.4")

    result: list = [None]
    error: list = [None]

    def _call() -> None:
        try:
            response = _client.responses.parse(
                model=model,
                input=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                text_format=schema,
            )
            result[0] = response.output_parsed
        except Exception as exc:
            error[0] = exc

    t = threading.Thread(target=_call, daemon=True)
    t.start()
    t.join(timeout)

    if t.is_alive():
        raise TimeoutError(f"GPT 请求超时（{timeout}s）")
    if error[0] is not None:
        raise error[0]
    return result[0]

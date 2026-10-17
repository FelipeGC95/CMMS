"""Ayudas de los tests del asistente (AI): cliente falso de la API con respuestas guionizadas.

Es el borde externo (no el ORM): reemplaza `apps.assistant.llm.get_client` y registra cada
llamada para verificar qué se envió. Las respuestas son `BetaMessage` reales del SDK, así que
el código recorre los mismos objetos que con la API. Ningún test llama a la API real.
"""

import copy

import anthropic
import httpx2
from anthropic.types.beta import BetaMessage

MODEL = "claude-opus-5-5"


def text(value: str) -> dict:
    return {"type": "text", "text": value}


def thinking(signature: str = "sig") -> dict:
    return {"type": "thinking", "thinking": "", "signature": signature}


def tool_use(name: str, tool_input: dict, tool_id: str = "toolu_1") -> dict:
    return {"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}


def reply(*blocks, stop_reason=None, input_tokens=100, output_tokens=20, cache_read=0, cache_write=0):
    if stop_reason is None:
        stop_reason = "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn"
    return BetaMessage.model_validate(
        {
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": MODEL,
            "content": list(blocks),
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_write,
            },
        }
    )


def _request():
    return httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def rate_limit_error():
    return anthropic.RateLimitError(
        "rate limited", response=httpx2.Response(429, request=_request()), body=None
    )


def server_error():
    return anthropic.InternalServerError(
        "overloaded", response=httpx2.Response(529, request=_request()), body=None
    )


def connection_error():
    return anthropic.APIConnectionError(request=_request())


class _Stream:
    def __init__(self, outcome):
        self.outcome = outcome

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class FakeClient:
    """`client.beta.messages.stream(**params)` con respuestas en orden; guarda cada `params`."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    @property
    def beta(self):
        return self

    @property
    def messages(self):
        return self

    def stream(self, **params):
        self.calls.append(copy.deepcopy(params))
        if not self.outcomes:
            raise AssertionError("El cliente falso no tiene más respuestas guionizadas.")
        return _Stream(self.outcomes.pop(0))


def install(monkeypatch, *outcomes) -> FakeClient:
    from apps.assistant import llm

    client = FakeClient(*outcomes)
    monkeypatch.setattr(llm, "get_client", lambda: client)
    return client

from __future__ import annotations

import json
from types import SimpleNamespace

import anthropic
import httpx
import pytest

from heizoel.config import settings
from heizoel.llm import anthropic_driver, base, ollama_driver
from heizoel.llm.anthropic_driver import AnthropicModel, strict_schema
from heizoel.llm.base import ModelError, get_model, validate_against_schema
from heizoel.llm.ollama_driver import OllamaModel

SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "n": {"type": "integer"},
        "cat": {"type": "string", "enum": ["a", "b"]},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["name", "cat"],
}


def test_validate_ok():
    validate_against_schema({"name": "x", "cat": "a", "n": 1, "tags": ["t"]}, SCHEMA)


def test_validate_missing_required():
    with pytest.raises(ModelError, match=r"\$\.cat"):
        validate_against_schema({"name": "x"}, SCHEMA)


def test_validate_wrong_type_with_path():
    with pytest.raises(ModelError, match=r"\$\.tags\[1\]"):
        validate_against_schema({"name": "x", "cat": "a", "tags": ["ok", 3]}, SCHEMA)
    with pytest.raises(ModelError):
        validate_against_schema({"name": "x", "cat": "a", "n": True}, SCHEMA)


def test_validate_enum():
    with pytest.raises(ModelError, match="nicht in"):
        validate_against_schema({"name": "x", "cat": "z"}, SCHEMA)


def test_strict_schema_recursive():
    schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {"type": "object", "properties": {"a": {"type": "string"}}},
            }
        },
    }
    out = strict_schema(schema)
    assert out["additionalProperties"] is False
    assert out["required"] == ["items"]
    inner = out["properties"]["items"]["items"]
    assert inner["additionalProperties"] is False and inner["required"] == ["a"]
    assert "additionalProperties" not in schema  # Original unverändert


def test_get_model_drivers(monkeypatch):
    monkeypatch.setattr(settings, "llm_driver", "none")
    assert get_model() is None
    monkeypatch.setattr(settings, "llm_driver", "anthropic")
    assert isinstance(get_model(), AnthropicModel)
    monkeypatch.setattr(settings, "llm_driver", "ollama")
    assert isinstance(get_model(), OllamaModel)
    monkeypatch.setattr(settings, "llm_driver", "bogus")
    with pytest.raises(ModelError):
        get_model()


class FakeMessages:
    def __init__(self, response=None, exc=None):
        self.response, self.exc, self.kwargs = response, exc, None

    def create(self, **kwargs):
        self.kwargs = kwargs
        if self.exc:
            raise self.exc
        return self.response


def _install_fake(monkeypatch, messages):
    class FakeAnthropic:
        def __init__(self, api_key=None):
            self.messages = messages

    monkeypatch.setattr(anthropic_driver.anthropic, "Anthropic", FakeAnthropic)


def _response(text, stop_reason="end_turn", stop_details=None):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason,
        stop_details=stop_details,
        usage=SimpleNamespace(input_tokens=11, output_tokens=7),
    )


def test_anthropic_success(monkeypatch):
    msgs = FakeMessages(_response(json.dumps({"name": "x", "cat": "a"})))
    _install_fake(monkeypatch, msgs)
    result = AnthropicModel().complete("sys", "prompt", SCHEMA)
    assert result.data == {"name": "x", "cat": "a"}
    assert (result.tokens_in, result.tokens_out) == (11, 7)
    kw = msgs.kwargs
    assert kw["max_tokens"] == 4096 and kw["system"] == "sys"
    assert "thinking" not in kw
    assert kw["output_config"]["effort"] == settings.llm_effort
    fmt = kw["output_config"]["format"]
    assert fmt["type"] == "json_schema" and fmt["schema"]["additionalProperties"] is False


def test_anthropic_refusal(monkeypatch):
    resp = _response("", "refusal", SimpleNamespace(category="cyber", explanation="x"))
    _install_fake(monkeypatch, FakeMessages(resp))
    with pytest.raises(ModelError, match="cyber"):
        AnthropicModel().complete("s", "p", SCHEMA)


def test_anthropic_invalid_json_and_schema(monkeypatch):
    _install_fake(monkeypatch, FakeMessages(_response("nicht json")))
    with pytest.raises(ModelError):
        AnthropicModel().complete("s", "p", SCHEMA)
    _install_fake(monkeypatch, FakeMessages(_response(json.dumps({"name": "x"}))))
    with pytest.raises(ModelError):
        AnthropicModel().complete("s", "p", SCHEMA)


def test_anthropic_connection_error_wrapped(monkeypatch):
    exc = anthropic.APIConnectionError(request=httpx_request())
    _install_fake(monkeypatch, FakeMessages(exc=exc))
    with pytest.raises(ModelError, match="Anthropic-API-Fehler"):
        AnthropicModel().complete("s", "p", SCHEMA)


def httpx_request():
    # Das SDK nutzt intern httpx2; die Exception braucht nur ein Request-Objekt.
    return SimpleNamespace(method="POST", url="https://example.invalid")


def test_ollama_success(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "message": {"content": json.dumps({"name": "x", "cat": "b"})},
                "prompt_eval_count": 5,
                "eval_count": 3,
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = OllamaModel(client=client).complete("sys", "prompt", SCHEMA)
    assert result.data["cat"] == "b" and result.tokens_in == 5 and result.tokens_out == 3
    assert seen["url"].endswith("/api/chat")
    assert seen["body"]["stream"] is False and seen["body"]["format"] == SCHEMA
    assert seen["body"]["options"] == {"temperature": 0}
    assert [m["role"] for m in seen["body"]["messages"]] == ["system", "user"]


def test_ollama_errors():
    def boom(request):
        raise httpx.ConnectError("down")

    client = httpx.Client(transport=httpx.MockTransport(boom))
    with pytest.raises(ModelError):
        OllamaModel(client=client).complete("s", "p", SCHEMA)

    bad = httpx.Client(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"message": {"content": json.dumps({"name": "x"})}})
        )
    )
    with pytest.raises(ModelError):
        OllamaModel(client=bad).complete("s", "p", SCHEMA)


def test_modules_importable():
    assert base and ollama_driver

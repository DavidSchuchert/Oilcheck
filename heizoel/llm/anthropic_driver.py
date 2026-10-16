"""Anthropic-Treiber: Messages API mit Structured Outputs (output_config.format)."""

from __future__ import annotations

import copy
import json
import time

import anthropic

from heizoel.config import settings
from heizoel.llm.base import ModelError, ModelResult, validate_against_schema


def strict_schema(schema: dict) -> dict:
    """Kopie des Schemas, in der jedes Objekt additionalProperties=false und required hat."""
    out = copy.deepcopy(schema)

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object" or "properties" in node:
                props = node.setdefault("properties", {})
                node["additionalProperties"] = False
                node["required"] = list(props.keys())
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(out)
    return out


class AnthropicModel:
    def __init__(self) -> None:
        self.name = settings.llm_model
        self.client = anthropic.Anthropic(api_key=settings.anthropic_api_key or None)

    def complete(self, system: str, prompt: str, json_schema: dict) -> ModelResult:
        start = time.monotonic()
        try:
            response = self.client.messages.create(
                model=self.name,
                max_tokens=4096,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                output_config={
                    "effort": settings.llm_effort,
                    "format": {"type": "json_schema", "schema": strict_schema(json_schema)},
                },
            )
        except (anthropic.APIStatusError, anthropic.APIConnectionError) as exc:
            raise ModelError(f"Anthropic-API-Fehler: {exc}") from exc

        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None)
            raise ModelError(f"Modell hat die Antwort verweigert (Kategorie: {category})")
        if response.stop_reason == "max_tokens":
            raise ModelError("Antwort wurde bei max_tokens abgeschnitten")

        text = next(
            (b.text for b in response.content if getattr(b, "type", "text") == "text"), None
        )
        if text is None:
            raise ModelError("Antwort enthält keinen Textblock")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ModelError(f"Antwort ist kein gültiges JSON: {exc}") from exc
        validate_against_schema(data, json_schema)

        usage = response.usage
        return ModelResult(
            data=data,
            tokens_in=int(getattr(usage, "input_tokens", 0) or 0),
            tokens_out=int(getattr(usage, "output_tokens", 0) or 0),
            duration_ms=int((time.monotonic() - start) * 1000),
            model=self.name,
        )

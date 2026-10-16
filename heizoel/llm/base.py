"""Gemeinsame Typen für Sprachmodell-Treiber plus minimale JSON-Schema-Prüfung."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from heizoel.config import settings


class ModelError(Exception):
    """Fehler beim Modellaufruf oder bei der Validierung der Antwort."""


@dataclass
class ModelResult:
    data: dict
    tokens_in: int
    tokens_out: int
    duration_ms: int
    model: str


class TextModel(Protocol):
    name: str

    def complete(self, system: str, prompt: str, json_schema: dict) -> ModelResult:
        """Liefert ein gegen das Schema validiertes Ergebnis."""
        ...


def _type_ok(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


def validate_against_schema(data: Any, schema: dict, path: str = "$") -> None:
    """Prüft type, required, properties, items und enum. Wirft ModelError mit Pfad."""
    expected = schema.get("type")
    if expected is not None:
        types = expected if isinstance(expected, list) else [expected]
        if not any(_type_ok(data, t) for t in types):
            raise ModelError(f"{path}: erwartet {expected}, erhalten {type(data).__name__}")
    if "enum" in schema and data not in schema["enum"]:
        raise ModelError(f"{path}: Wert {data!r} nicht in {schema['enum']}")
    if isinstance(data, dict):
        for key in schema.get("required", []):
            if key not in data:
                raise ModelError(f"{path}.{key}: Pflichtfeld fehlt")
        for key, sub in schema.get("properties", {}).items():
            if key in data:
                validate_against_schema(data[key], sub, f"{path}.{key}")
    if isinstance(data, list) and isinstance(schema.get("items"), dict):
        for i, item in enumerate(data):
            validate_against_schema(item, schema["items"], f"{path}[{i}]")


def get_model() -> TextModel | None:
    driver = settings.llm_driver.strip().lower()
    if driver == "none":
        return None
    if driver == "anthropic":
        from heizoel.llm.anthropic_driver import AnthropicModel

        return AnthropicModel()
    if driver == "ollama":
        from heizoel.llm.ollama_driver import OllamaModel

        return OllamaModel()
    raise ModelError(f"Unbekannter llm_driver: {settings.llm_driver!r}")

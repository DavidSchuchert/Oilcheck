"""Ollama-Treiber (lokal): /api/chat mit Schema im format-Feld, Prüfung zusätzlich clientseitig."""

from __future__ import annotations

import json
import time

import httpx

from heizoel.config import settings
from heizoel.llm.base import ModelError, ModelResult, validate_against_schema


class OllamaModel:
    def __init__(self, client: httpx.Client | None = None) -> None:
        self.name = settings.llm_model
        self.client = client

    def complete(self, system: str, prompt: str, json_schema: dict) -> ModelResult:
        start = time.monotonic()
        payload = {
            "model": self.name,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "format": json_schema,
            "stream": False,
            "options": {"temperature": 0},
        }
        url = f"{settings.ollama_url.rstrip('/')}/api/chat"
        try:
            if self.client is not None:
                resp = self.client.post(url, json=payload, timeout=180)
            else:
                resp = httpx.post(url, json=payload, timeout=180)
            resp.raise_for_status()
            body = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ModelError(f"Ollama-Fehler: {exc}") from exc

        try:
            content = body["message"]["content"]
            data = json.loads(content)
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ModelError(f"Ollama-Antwort unbrauchbar: {exc}") from exc
        validate_against_schema(data, json_schema)
        return ModelResult(
            data=data,
            tokens_in=int(body.get("prompt_eval_count", 0) or 0),
            tokens_out=int(body.get("eval_count", 0) or 0),
            duration_ms=int((time.monotonic() - start) * 1000),
            model=self.name,
        )

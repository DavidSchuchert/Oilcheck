"""Zentrale Konfiguration. Alle Werte kommen aus Umgebungsvariablen mit Präfix HEIZOEL_ oder aus .env."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="HEIZOEL_",
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    db_path: Path = PROJECT_ROOT / "data" / "heizoel.db"
    secret_key: str = "dev-only-change-me"
    invite_code: str = "familie"
    app_url: str = "http://localhost:8000"

    eia_api_key: str = ""

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    mail_from: str = "heizoel@example.org"

    daily_run_at: str = "06:30"

    # Sprachmodell für das Nachrichten-Lagebild (Phase 2). "none" schaltet den Schritt ab.
    llm_driver: str = "none"            # none | anthropic | ollama
    llm_model: str = "claude-opus-5-5"  # Anthropic-Modell-ID bzw. Ollama-Modellname
    llm_effort: str = "low"             # low | medium | high (nur Anthropic)
    anthropic_api_key: str = ""         # leer = SDK liest ANTHROPIC_API_KEY aus der Umgebung
    ollama_url: str = "http://localhost:11434"
    news_max_items: int = 40            # Obergrenze Feed-Einträge pro Lauf, die ans Modell gehen
    news_summary_mode: str = "notes"    # notes = Lagebild im Code aus Einzelbewertungen bauen (sicher für kleine Modelle), model = Modell formuliert

    # Fachliche Konstanten (bewusst konfigurierbar, aber mit festen Defaults)
    co2_kg_per_liter: float = 2.65  # kg CO2 je Liter Heizöl EL
    vat: float = 0.19
    history_years: int = 5


settings = Settings()

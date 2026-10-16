# Heizöl-Kaufberater

Kleine Web-App für mehrere Nutzer: Jeder hinterlegt PLZ, Tank und Verbrauch und bekommt täglich eine
nachvollziehbare Einschätzung, ob jetzt ein guter Zeitpunkt zum Heizölkauf ist. Keine Prognose,
sondern Einordnung des heutigen Preises relativ zur Historie, zu bekannten CO2-Stufen und zur
eigenen Tankreichweite.

Der vollständige Plan steht in [PLAN.md](PLAN.md).

## Start

```bash
uv sync
cp .env.example .env          # Werte anpassen
uv run heizoel init           # Datenbank anlegen, CO2-Stufen einspielen
uv run heizoel import-history data/history_seed.csv
uv run heizoel fetch          # heutigen Referenzpreis holen
uv run heizoel recommend      # Empfehlungen für alle Nutzer berechnen
uv run heizoel serve          # Web-UI auf http://localhost:8000, Scheduler läuft mit
```

Lokales Modell (Standard auf diesem Mac): Ollama per Homebrew, läuft als Dienst, Modell qwen2.5:7b.
Obergrenze sind 7B-Modelle. Je Meldung ein Modellaufruf, das Lagebild wird im Code aus den
Einzelbewertungen gebaut (`HEIZOEL_NEWS_SUMMARY_MODE=notes`).

```bash
brew install ollama && brew services start ollama && ollama pull qwen2.5:7b
```

Angebote: esyoil wird einmal täglich je PLZ und Menge per GET abgerufen (serverseitig gerendertes
HTML, kein API-Key). esyoil schließt diese URL in robots.txt für Crawler aus; wir rufen sie bewusst
sparsam als Ersatz für den manuellen Browseraufruf ab. HeizOel24 ist ausgenommen, deren AGB
verbieten automatisierten Zugriff ausdrücklich. Fällt der Abruf aus, zeigt das Dashboard Links.

Mail ist bewusst aus: Empfehlung und Tankstand-Erinnerung stehen im Dashboard. Wer doch Mails will,
setzt die `HEIZOEL_SMTP_*`-Werte in `.env` und stellt `notify_channel` des Profils in der Datenbank auf `mail`.

Nachrichten-Lagebild (Phase 2) einschalten: in `.env` `HEIZOEL_LLM_DRIVER=anthropic` (plus
`HEIZOEL_ANTHROPIC_API_KEY` oder `ANTHROPIC_API_KEY` in der Umgebung) oder `=ollama` mit laufendem
Ollama und `HEIZOEL_LLM_MODEL=<modellname>`. Dann läuft es im Tageslauf mit; manuell:

```bash
uv run heizoel news --force
```

Tests:

```bash
uv run pytest
```

## Aufbau

```
heizoel/
  config.py        Einstellungen aus .env (Präfix HEIZOEL_)
  db.py            SQLite-Zugriff, Schema-Anwendung
  schema.sql       Tabellen
  models.py        Datenklassen als Vertrag zwischen Modulen
  co2.py           CO2-Stufen und Umrechnung in ct/L
  decision/        Kaufregel (reine Funktionen) und Service (lädt Daten, speichert Ergebnis)
  fetchers/        TECSON-Parser, EIA (Brent), EZB (EUR/USD), Fallback-Modell
  web/             FastAPI, Auth, Seiten, Templates
  jobs/            Täglicher Lauf, Benachrichtigungen, Scheduler
  llm/             TextModel-Schnittstelle, Treiber Anthropic und Ollama
  news/            RSS-Feeds, Vorfilter, Lagebild mit Schema-Prüfung
  cli.py           Kommandozeile
tests/
data/              SQLite-Datei (nicht im Repo), Seed-CSV
```

## Konventionen

- Preise in Cent je Liter, brutto, bezogen auf 3000 L, sofern nicht anders benannt.
- Datum als `datetime.date`, in der DB als ISO-Text.
- Die Kaufregel in `decision/engine.py` ist eine reine Funktion ohne DB-Zugriff und vollständig getestet.
- Kein Sprachmodell entscheidet. Modelle (ab Phase 2) filtern Nachrichten und formulieren Text.
- Keine Build-Pipeline: Tailwind und Chart.js kommen per CDN-Tag.

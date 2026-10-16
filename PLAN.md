# Heizöl-Kaufberater – Projektplan

Stand: 16. Oktober 2026 (Stack auf Python umgestellt)

## 1. Ziel und Nicht-Ziel

**Ziel:** Eine Web-App, in der mehrere Nutzer ihre Postleitzahl, Tankgröße und Verbrauch hinterlegen und daraus täglich eine persönliche, nachvollziehbare Einschätzung bekommen: "Jetzt kaufen", "Warten" oder "Bald handeln", mit Begründung in Zahlen und mit einem kurzen Nachrichtenlagebild.

**Nicht-Ziel:** Preisprognose. Die App sagt nie "nächste Woche billiger". Sie bewertet den heutigen Preis relativ zur Historie, zu bekannten Terminen (CO2-Stufen) und zur Dringlichkeit des Nutzers (Tankstand).

## 2. Kernidee: drei Datenschichten

| Schicht | Inhalt | Quelle | Pro Nutzer? |
|---|---|---|---|
| Referenzpreis | Deutschlandweiter Tagespreis Heizöl EL, 3000 L, Cent/Liter | TECSON-Tagespreis (Parsing der öffentlichen Seite) mit Fallback-Berechnung aus Brent × EUR/USD + CO2 + MwSt. | Nein, einmal täglich |
| Regionaler Aufschlag | Abweichung der Region vom Referenzpreis | Angebote, die Nutzer selbst eintragen (Portal-Deep-Link mit PLZ, Preis zurück eintragen). Daraus pro PLZ-2-Region ein gleitender Offset. | Pro Region |
| Nutzerkontext | Tankgröße, Füllstand, Verbrauch, Mindestbestellmenge, Vorlaufzeit | Nutzer-Eingabe | Ja |

Warum so: Eine PLZ-genaue API existiert nicht. Scraping der Portale verstößt gegen deren Nutzungsbedingungen und bricht bei jedem Redesign. Der Referenzpreis bewegt sich aber für alle Regionen gleich, nur der Aufschlag unterscheidet sich und ist über Monate stabil. Den lernt die App aus den Angeboten, die Nutzer ohnehin einholen.

## 3. Datenquellen im Detail

### 3.1 Referenzpreis und Rohstoffe

- **TECSON Heizöl-Tagespreis**: Täglich 1× HTML der öffentlichen Preisseite parsen, Wert in Cent/L bei 3000 L speichern. Parser bewusst tolerant, bei Fehler Alarm an Admin und Fallback.
- **Brent (USD/Barrel)**: EIA Open Data API, kostenloser Key. Tagesschluss.
- **EUR/USD**: Frankfurter-API (EZB-Referenzkurse), kein Key nötig.
- **Fallback-Modell**: `Preis_brutto ≈ (Brent_USD / 159 / EURUSD × 100 + Raffinerie_Logistik_Marge + CO2_ct) × 1,19`. Die Marge wird aus den letzten 90 Tagen TECSON vs. Brent kalibriert. Dient nur, um Lücken zu füllen und Plausibilität zu prüfen.
- **Historie zum Start**: TECSON zeigt mehrjährige Charts. Für den Start reicht es, die Historie einmalig manuell als CSV einzupflegen (Monatswerte ab 2015, Tageswerte ab Projektstart). Alternativ Brent-Historie + Fallback-Modell rückrechnen und mit bekannten Stichtagen abgleichen.

### 3.2 CO2-Preis (Konfigurationstabelle, kein Scraping)

| Jahr | Preis €/t | Status |
|---|---|---|
| 2024 | 45 | fix |
| 2025 | 55 | fix |
| 2026 | 55–65 Korridor | fix |
| 2027 | voraussichtlich 55–65 Korridor verlängert | Gesetzentwurf, Bundestag ausstehend |
| 2028 | ETS2, Marktpreis | EU-Verordnung in Kraft seit April 2026 |

Umrechnung: 1 L Heizöl ≈ 2,65 kg CO2. 10 €/t entsprechen 2,65 ct/L netto, rund 3,15 ct/L brutto. Die Tabelle ist versioniert und vom Admin pflegbar. Ein Nachrichten-Job (siehe KI-Schicht) markiert Änderungen zur Prüfung, schreibt sie aber nie selbst.

### 3.3 Regionale Angebote

- Nutzer klickt "Angebot holen": Deep-Link zu esyoil oder HeizOel24 mit PLZ und Menge vorausgefüllt (reiner Link, kein Scraping).
- Nutzer trägt den Preis ein (Cent/L, Menge, Händler optional, Datum automatisch).
- Daraus: `offset_region = median(Angebotspreis − Referenzpreis am selben Tag)` über die letzten 60 Tage, Region = erste zwei PLZ-Stellen. Unter 3 Datenpunkten: Offset = 0 mit Hinweis "noch keine Regionaldaten".
- Schutz vor Unsinn: Ausreißer > 15 ct Abweichung werden markiert, nicht gezählt.

**Stand 16.10.2026:** esyoil liefert die Angebotsliste serverseitig im HTML der GET-URL
`/heizoelpreise/heizoel-bestellung?zipcode=…&amount=…`. Die App ruft sie einmal täglich je PLZ und
Menge ab (robots.txt schließt die URL für Crawler aus, rechtlich für private Einzelabfragen
unkritisch, Blockade-Risiko bleibt, Fallback sind die Links). HeizOel24 bleibt außen vor, AGB § 7
untersagt automatischen Zugriff ausdrücklich. Nutzer wählen die Abfragemenge selbst, sie wird im
Profil gemerkt.

### 3.4 Nachrichten

- RSS-Feeds: tagesschau Wirtschaft, Handelsblatt Energie, Reuters Energy (falls erreichbar), TECSON-Marktkommentar, BMWK/DEHSt für CO2-Themen.
- Täglich die letzten 24 h einsammeln, nach Schlagwörtern vorfiltern (OPEC, Brent, Raffinerie, Hormus, Sanktion, CO2-Preis, BEHG, ETS2, Heizöl), dann an die KI-Schicht.

## 4. Entscheidungslogik (deterministisch, in PHP)

Alle Zahlen werden berechnet, nicht geschätzt. Für jeden Nutzer täglich:

1. **Persönlicher Preis** = Referenzpreis + Regionaloffset + Mengenaufschlag (Tabelle: 1000 L +6 ct, 1500 L +4 ct, 2000 L +2 ct, 3000 L +0, aus Nutzerangeboten nachkalibriert).
2. **Perzentil** des Referenzpreises in den letzten 365 und 730 Tagen.
3. **Saisonabweichung**: heutiger Preis vs. Mittel des Kalendermonats über die letzten 5 Jahre, bereinigt um den Jahrestrend.
4. **Reichweite** = Restliter / Tagesverbrauch. Tagesverbrauch aus Jahresverbrauch × Gradtagzahl-Gewicht des Monats (feste Tabelle, Winter ~3× Sommer). Später optional: Nutzer trägt Füllstände ein, App lernt den echten Verbrauch.
5. **Terminfaktor**: Tage bis zur nächsten bekannten Preisstufe und deren Höhe in ct/L.
6. **Empfehlung** nach Regeln, in dieser Reihenfolge:
   - Reichweite < Vorlaufzeit + 14 Tage → **Jetzt kaufen** (Notkauf-Vermeidung schlägt alles).
   - Perzentil ≤ 20 im 1-Jahres- UND im 2-Jahres-Fenster und Tank < 60 % → **Jetzt kaufen, günstige Lage**. (Geändert 16.10.2026: Backtest über 12 Startmonate zeigte, dass die Regel ohne 2-Jahres-Filter in fallenden Märkten zu früh kauft und im Mittel nichts bringt; mit Filter und Schwelle 20 gewinnt sie in 9 von 12 Fällen, ca. +190 € je Durchlauf bei 4500-L-Tank.)
   - Preisstufe in < 45 Tagen und Stufe ≥ 2 ct/L und Tank < 70 % → **Vor dem Stichtag kaufen**.
   - Perzentil ≥ 70 und Reichweite > 90 Tage → **Warten**.
   - Sonst → **Beobachten**, mit Angabe, welche Bedingung als nächste greift.
7. **Teilkauf-Vorschlag**: Wenn Tank > 50 % leer und Perzentil zwischen 30 und 60: Vorschlag, die Hälfte jetzt, Rest später. Reduziert Reue, wird als Option gezeigt, nicht als Pflicht.
8. **Ersparnisrechner**: "Bei deinem Bedarf von X Litern bedeutet 1 ct/L genau Y Euro." Das erdet jede Diskussion.

Jede Empfehlung speichert die Eingangswerte, damit sie später nachvollziehbar ist und man die Regeln gegen die Historie zurücktesten kann (Backtest-Seite für Admin: "Was hätte die Regel in den letzten 5 Jahren gebracht vs. Kauf bei leerem Tank").

## 5. KI-Schicht

### 5.1 Aufgaben, strikt begrenzt

| Aufgabe | Eingabe | Ausgabe (JSON-Schema) | Fließt in Entscheidung ein? |
|---|---|---|---|
| Nachrichten filtern und zusammenfassen | 10–40 Feed-Einträge | `{relevant: [{titel, quelle, url, kategorie, einordnung_1satz}], lagebild_3saetze}` | Nein, nur Anzeige |
| CO2/Gesetzes-Änderung erkennen | gleiche Einträge | `{hinweis_auf_regelaenderung: bool, zitat, url}` | Nein, erzeugt Admin-Aufgabe |
| Empfehlung in Sprache fassen | berechnete Zahlen + Regel, die gegriffen hat | `{text_de}` | Nein, reine Formulierung |

Das Modell liefert nie eine Zahl, die in eine Berechnung geht, und nie das Wort "Prognose". Ausgabe wird gegen ein JSON-Schema validiert, bei Fehler wird der Lauf mit dem letzten gültigen Lagebild wiederholt und ein Fehler geloggt.

### 5.2 Interface und Modelle

- PHP-Interface `TextModel` mit einer Methode `complete(string $system, string $prompt, array $jsonSchema): array`.
- Treiber 1: Anthropic API über das offizielle PHP-SDK, mit Structured Outputs für das Schema.
- Treiber 2: Ollama (lokal), gleiches Interface, Schema-Prüfung clientseitig.
- Auswahl per `.env`, pro Aufgabe überschreibbar.

Kosten bei 1 Lauf/Tag, ca. 20k Token rein, 1k raus (die Zusammenfassung ist für alle Nutzer gleich, nur der Formulierungs-Schritt ist pro Nutzer und sehr klein):

| Modell | pro Monat |
|---|---|
| Claude Haiku 5.5 | unter 10 Cent |
| Claude Opus 5.5 (effort low) | ca. 3 Euro |
| Ollama 7B–14B lokal | 0 Euro, Rechner muss laufen |

Empfehlung: Mit einem API-Modell starten, bis die Pipeline stabil ist, dann Ollama als zweiten Treiber testen. Welche Stufe du bei der API nimmst, ist Geschmackssache; für Feed-Filterung reicht die kleine.

## 6. Datenmodell

```
users              id, name, email, password, role (user|admin)
profiles           user_id, plz, region (plz[0:2]), tank_liter, fuellstand_prozent,
                   fuellstand_datum, jahresverbrauch_liter, mindestmenge_liter,
                   vorlauf_tage, benachrichtigung (mail|telegram|keine), schwelle_perzentil
reference_prices   datum, quelle (tecson|fallback), ct_pro_liter, brent_usd, eurusd
co2_steps          gueltig_ab, eur_pro_tonne, status (fix|entwurf), quelle_url
offers             user_id, region, datum, ct_pro_liter, liter, haendler, portal, ausreisser bool
region_offsets     region, datum, offset_ct, n_angebote          (täglich berechnet)
recommendations    user_id, datum, empfehlung, regel, persoenlicher_preis_ct,
                   perzentil_365, perzentil_730, saison_abw, reichweite_tage,
                   naechste_stufe_tage, naechste_stufe_ct, text, eingaben_json
news_digests       datum, lagebild, items_json, modell, tokens, dauer_ms
purchases          user_id, datum, liter, ct_pro_liter, haendler   (Nutzer-Historie, Lerneffekt)
```

## 7. Architektur (Python, bewusst schlank)

Begründung: Die App ist klein (8 Tabellen, 6 Seiten, 5 Jobs). Ein Framework mit Build-Pipeline und
Service-Container bringt dafür mehr Rahmen als Inhalt. Python hat außerdem die besten Werkzeuge für
den Kern, also Perzentile, Saisonbereinigung, Backtest, HTML-Parsing und RSS.

- **Python 3.12+, uv**, FastAPI, Jinja2-Templates, SQLite direkt (kein ORM), bcrypt, httpx,
  BeautifulSoup, APScheduler. Keine weiteren Abhängigkeiten. Kein JavaScript-Build: Tailwind und
  Chart.js per CDN-Tag.
- **Auth**: selbst geschrieben, Session-Cookie (itsdangerous), Rollen `user` und `admin`,
  Registrierung nur mit Einladungscode, erster Nutzer wird Admin.
- **Scheduler** (täglich 06:30 Europe/Berlin, im selben Prozess oder per System-Cron `heizoel daily`):
  1. `fetchers.reference.fetch_today` (TECSON, EIA, EZB, sonst Fallback-Modell)
  2. `jobs.offsets.compute_region_offsets`
  3. `jobs.news` (Phase 2, ein Modellaufruf)
  4. `decision.service.compute_all` (alle Nutzer, reine Rechnung)
  5. `jobs.notify.notify_users` (nur bei Statuswechsel oder Erinnerung nach 7 Tagen)
- **Historie ohne TECSON-Archiv**: `heizoel backfill` berechnet 5 Jahre Referenzpreise aus Brent
  (EIA), EUR/USD (EZB) und den CO2-Stufen mit kalibrierter Marge. Echte TECSON- oder manuelle
  Werte überschreiben Fallback-Werte immer, nie umgekehrt.
- **Benachrichtigung**: Mail zuerst (ohne SMTP-Konfiguration nur Log), Telegram in Phase 3.
- **Tests**: pytest, Kaufregel vollständig unit-getestet, Parser gegen gespeicherte HTML-Snapshots,
  Web über TestClient mit Wegwerf-Datenbank, Backtest als CLI-Befehl.
- **Betrieb**: ein Prozess (`uv run heizoel serve`), eine SQLite-Datei, läuft auf einem Mini-VPS
  oder dem Mac. Backup = Datei kopieren.

## 8. Weboberfläche

### Für Nutzer

1. **Onboarding** (eine Seite): PLZ, Tankgröße, aktueller Füllstand, Jahresverbrauch oder Baujahr/Wohnfläche als Schätzhilfe, Mindestbestellmenge, Benachrichtigungskanal.
2. **Dashboard**:
   - Ampel oben: Empfehlung + ein Satz Begründung + "gilt bis" (nächster Lauf).
   - Kachel "Dein Preis heute": persönlicher Preis, Perzentil, Saisonabweichung, Pfeil 7/30 Tage.
   - Kachel "Dein Tank": Füllstand, Reichweite in Tagen, Datum des spätesten Bestelltermins.
   - Kachel "Nächster Termin": CO2-Stufe, Tage, Auswirkung in Euro auf deine Menge.
   - Chart: Referenzpreis 2 Jahre, Saisonband, markierte eigene Käufe.
   - Lagebild: 3 Sätze + relevante Meldungen mit Quelle.
   - Button "Angebot holen" (Deep-Links) und Formular "Angebot eintragen".
3. **Füllstand aktualisieren**: ein Feld, ein Klick. Optional Erinnerung monatlich.
4. **Kauf eintragen**: Menge, Preis, Händler. Setzt Füllstand neu, erscheint im Chart.
5. **Historie**: eigene Empfehlungen und Käufe, "Was hätte es gekostet, wenn".
6. **Einstellungen**: Profil, Schwellen anpassen, Kanal, Konto löschen.

### Für Admin

- Datenstatus (letzter erfolgreicher Fetch je Quelle, Parser-Fehler).
- CO2-Stufen pflegen, Hinweise aus der KI-Schicht bestätigen oder verwerfen.
- Regionen-Übersicht: Offsets, Anzahl Angebote, Ausreißer.
- Backtest-Seite.
- Modell-Log: Tokens, Dauer, Schema-Fehler.

## 9. Mehrbenutzer und Datenschutz

- Jede Abfrage über Policies auf `user_id` gescoped, Admin sieht aggregiert.
- PLZ ist personenbezogen. Nur PLZ speichern, Region (2-stellig) für Aggregation. Keine Adresse.
- Angebote anderer Nutzer sind für Nutzer nur als Regionsmedian sichtbar, nie einzeln.
- Datenschutzerklärung, Konto-Löschung mit Kaskade, keine Tracker. Modellaufrufe enthalten nur Nachrichtentexte und anonymisierte Zahlen, nie Namen oder E-Mail.
- Registrierung per Einladungslink, solange es ein privater Kreis ist.

## 10. Umsetzungsphasen

### Phase 1: Kern ohne KI (erledigt 16.10.2026)

- Projekt, Auth, Profil, Onboarding.
- Referenzpreis-Job mit TECSON-Parser und Fallback, Historie importieren.
- CO2-Tabelle.
- Entscheidungslogik mit Tests und Backtest-Command.
- Dashboard mit Ampel, Kacheln, Chart.
- Mail-Benachrichtigung bei Statuswechsel.

Ergebnis: Für dich und 2–3 Testnutzer nutzbar, ohne regionale Genauigkeit.

### Phase 2: Regional und Nachrichten (erledigt 16.10.2026, läuft lokal mit Ollama qwen2.5:7b)

- Angebote eintragen, Deep-Links, Regionaloffsets.
- RSS-Fetch, `TextModel`-Interface, Anthropic-Treiber, Lagebild im Dashboard.
- Admin-Seiten: Datenstatus, CO2-Pflege, Modell-Log.

### Phase 3: Komfort (ohne Telegram erledigt 16.10.2026)

- Telegram-Kanal (vom Nutzer vorerst nicht gewünscht). Füllstands-Erinnerung per Mail nach 30 Tagen und Verbrauchslernen aus Füllständen: erledigt.
- Ollama-Treiber. (erledigt, ist der Standard; Obergrenze 7B)
- Teilkauf-Planer und Mehrjahres-Statistik (12 Startmonate gemittelt): erledigt. Oberfläche komplett auf Alltagssprache umgestellt (Antwort-Karte zuerst, Fachwerte hinter "Warum?").
- Optional: Tanksensor-Anbindung (z. B. per MQTT), wenn das Haus einen hat.

## 11. Risiken und offene Entscheidungen

| Risiko | Umgang |
|---|---|
| TECSON ändert HTML | Parser isoliert, Snapshot-Tests, Fallback-Modell, Admin-Alarm. Alternativ Preis manuell pflegen, es ist ein Wert am Tag. |
| Zu wenig Nutzerangebote für Regionaloffset | Offset 0 mit klarem Hinweis; App bleibt nützlich über Perzentil und Termine. |
| Nutzer liest Lagebild als Prognose | Wortwahl im Prompt, UI-Label "Lage, keine Prognose", Modell darf keine Richtungsaussagen machen. |
| Gesetzeslage 2027 ändert sich | CO2-Tabelle mit Status-Feld, Admin-Freigabe, Nutzer sieht "voraussichtlich". |
| Verbrauchsschätzung grob | Reichweite mit Spanne anzeigen, Füllstandseingabe prominent machen. |

Offen zu entscheiden:

1. Projektordner und Repo-Name.
2. Hosting: entschieden, läuft als Docker-Container auf server.local:8090 (seit 16.10.2026), Tailscale ist auf dem Server vorhanden.
3. Benachrichtigung: keine, alles im Dashboard (Entscheidung 16.10.2026).
4. Modell: lokal Ollama qwen2.5:7b, Obergrenze 7B (Entscheidung 16.10.2026).

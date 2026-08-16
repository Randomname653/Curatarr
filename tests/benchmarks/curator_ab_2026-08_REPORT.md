# Curator-Modell A/B — August 2026

**Frage:** Ersetzt ein neues Modell die gemma4:31b-Basis des Curatarr-Curators?
**Antwort: Ja — qwen3.8:27b gewinnt in Qualität UND Geschwindigkeit.** Empfehlung: Swap nach Chat-Bench-Bestätigung und Owner-Stichprobe.

## Setup

- **Kandidaten:** gemma4:31b (Incumbent-Basis), qwen3.8:27b, gemma4:26b (MoE), qwen3.6:latest, muse-glimmer:30b
- **Daten:** 148 "weirdeste" Items aus dem Live-Enrichment-Cache, balanciert 37/Kategorie (movie/show/anime/music), volle Pipeline-Prompts inkl. CURATOR_SYSTEM_PROMPT als System-Message (misst die Basis, kein Bake nötig). 5 × 148 = **740 Delete-Pitches**.
- **Scoring:** Claude bewertet jeden Pitch auf 5 Achsen à 0–5 (**Faith**fulness zu den Metadaten, **Rel**evanz zum Taste-Profil, **Spec**ifität, **Tone**, **Format**-Disziplin) = max 25. Auto-Signale: Forbidden-Buzzwords, Latenz aus dem Runner.
- **Gate:** pillar_json_stresstest.py (JSON-Validität/Keys/Enum/Verdict-Treffer/Determinismus, 5 Cases × 3 Iterationen, temp 0.0).
- Rohdaten: `curator_pipeline_2026-08-15_11-09.jsonl` (Run 2, alle 5 Blöcke), Scores: `curator_ab_2026-08_scores.csv`, Stichprobe: `curator_ab_2026-08_spotcheck.md`.

## Ergebnis (740 Pitches, handgescored)

| Modell | Ø/25 | Faith | Rel | Spec | Tone | Format | ≥22 | ≤19 | fb | Median-Lat. | p90 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **qwen3.8:27b** | **21.55** | 4.20 | 4.10 | 4.10 | **4.33** | 4.81 | **72** | 3 | 4 | **3.1s** | **3.5s** |
| gemma4:31b (Incumbent) | 21.35 | 4.11 | **4.20** | 3.91 | 4.14 | **4.99** | 47 | 1 | 1 | 7.5s | 30.3s* |
| muse-glimmer:30b | 21.12 | **4.53** | 4.11 | **4.64** | 3.95 | 3.89 | 68 | 12 | **0** | 15.9s* | 23.8s* |
| gemma4:26b | 20.89 | 4.02 | 4.01 | 3.95 | 3.94 | 4.97 | 10 | 4 | 6 | **1.8s** | 2.0s |
| qwen3.6:latest | 20.65 | 3.98 | 4.02 | 3.92 | 3.92 | 4.82 | 15 | 13 | 7 | 2.5s* | 5.7s* | 

\* Latenzen teilweise durch Run-Überlappung kontaminiert (Run 3 lief zeitweise parallel zu Run-2-Blöcken); die qwen3.8- und gemma4:26b-Blöcke sind sauber. qwen3.6 zusätzlich 1 harter `ReadTimeout`-Ausfall (Gurren Lagann, 600s) und Eskalation 2s→600s = VRAM/KV-Starvation bei 16k ctx.

Kategorien-Schnitte: qwen3.8 führt movie (21.73) und music (21.49), gemma4:31b führt show (21.70), anime dreifach geteilt (21.35).

## Warum qwen3.8 gewinnt — die Qualitätsbefunde

**1. Metadaten-Integrität (der wichtigste Fund).** qwen3.8 ist das einzige Modell, das kollidierte/verschmutzte Enrichment-Daten aktiv ERKENNT statt sie zu übertünchen:
- **Randy:** flaggte die Metadaten-Kollision explizit (24/25).
- **Cyrus:** "the metadata presents a fractured identity" — Teen-Pop vs. Electronic-Acts benannt (24/25).
- **Dylan:** "a catastrophic data contamination of a UK drum & bass producer, an Italian rapper, and a pop singer" — und macht die Kontamination selbst zum Löschargument. **Einziger 25/25-Pitch des gesamten Benchmarks.** Zum Vergleich: qwen3.6 hielt "Dylan" schlicht für Bob Dylan (18/25) — genau der Fehlertyp, der falsche Löschungen produziert.

**2. Daten-Ehrlichkeit unter Widerspruch.** Zweimal verweigerte qwen3.8 den bestellten Delete-Pitch, beide Male datenbasiert korrekt: Nukitashi (4 Views + Owner-Verteidigung: "It is illogical to argue for removing…") und Steins;Gate 0 ("aligns too closely with your preference… strong candidate for retention"). Beim ZWEITEN, ungesehenen Nukitashi-Duplikat pitchte es dagegen normal — die Verweigerung ist konsistent an Owner-Daten gebunden, kein Zufall. Für einen Kurator, der Löschvorschläge begründet, ist das exakt das Verhalten, das Fehllöschungen verhindert.

**3. Tempo.** Median 3.1s, p90 3.5s — praktisch keine Streuung. Der Incumbent braucht 7.5s median (2.4×). Für Batch-Läufe über hunderte Kandidaten heißt das: Deletion-Run-Pitches in ~40 % der Zeit.

**Schwächen qwen3.8:** 4 Forbidden-Buzzwords (Incumbent: 1), vereinzelte Wortfehler/Überlängen ("socially stented", abgeschnittenes "contrad", 3-Satz-Bandwürmer), ein Faktenfehler (Liar Game als "anime adaptation" — ist Live-Action). Nichts davon strukturell.

## Die anderen

- **gemma4:31b (Incumbent):** Kein Absteiger — beste Format-Disziplin (4.99), beste Profil-Relevanz (4.20), stärkste Show-Pitches, differenziert bei Owner-Lieblingen ("Despite your history/attachment…"). Aber die Argumente bleiben generischer (Spec 3.91) und nur 47/148 Pitches erreichen ≥22 (qwen3.8: 72). Bleibt als Rollback-Basis erhalten.
- **muse-glimmer:30b:** Der Fakten-König — Faith 4.53, Spec 4.64, 0 Buzzwords: zitiert Episodenzahlen ("Sopranos: 86 Episoden"), Watch-History ("five rewatches", "watched it four times and still cite slow character development"), Charakternamen, sogar Library-Querverweise ("alongside your kept Animal Farm"). ABER: Format 3.89 (Bandwurmsätze), 12 schwache Pitches, 3× Persona-Bruch in die 3. Person ("the user demands"), 2 unglaggte Kollisions-Commits (Hardfloor "Berlin relic" — Düsseldorf; Kronos-Quartet-Mix) und mit 16s/Pitch zu langsam. **Nicht als Kurator — aber Kandidat für einen Offline-"Facts-Harvester"-Job** (Verified-Data-Builder), wo Faktendichte zählt und Latenz egal ist.
- **gemma4:26b:** Ultraschnell (1.8s median, p90 2.0s) und formatfest, aber flach: nur 10 Top-Pitches, austauschbare Argumente. Fallback für Low-VRAM-Szenarien.
- **qwen3.6:latest: DNF/disqualifiziert.** VRAM-Starvation im Pipeline-Bench (Latenz-Eskalation bis 600s-Timeout, 1 Pitch-Totalausfall), im Stresstest ebenfalls Timeouts. 22GB-Modell + 16k ctx passt nicht neben den Curatarr-Stack auf die 4090.

## Stresstest-Gate (Stand)

gemma4:31b, curatarr-curator (aktuelles Production-Bake), qwen3.8:27b, gemma4:26b: **alle 15/15 = 100 % auf JSON/Keys/Enum/Verdict, Determinismus 5/5 — GATE GREEN.** Verdict-Treffer inkl. der Constitution-Fälle (Tokyo Story → KEEP_WITH_FLAG trotz Owner-Antipathie, Twilight → HARD_KEEP über Household-Pillar, Manyu/Abandoned → CUT). Avg-Latenz: gemma4:26b 4.9s < qwen3.8 6.2s < curatarr-curator 6.8s < gemma4:31b 7.6s. qwen3.6: Timeouts (konsistent mit DNF). muse-glimmer: läuft bei Redaktionsschluss noch — für die Swap-Entscheidung irrelevant (qwen3.8-Gate ist GREEN).

## Empfehlung & nächste Schritte

1. **Owner-Stichprobe:** `curator_ab_2026-08_spotcheck.md` — 15 Items (Signatur-Fälle + größte Score-Spreads), Urteil des Owners gegen meine Scores.
2. **Chat-Bench** (Plan: "nur für den Sieger"): `curator_bench.py --models gemma4:31b,qwen3.8:27b` — prüft die Chat-/Persona-Seite, die der Pipeline-Bench nicht abdeckt.
3. **Bei Bestätigung — Swap-Prozedur** (nur auf Owner-Go):
   - `ollama cp curatarr-curator curatarr-curator-gemma-backup` (Rollback-Kopie)
   - `.env`: `BASE_CURATOR_MODEL=qwen3.8:27b` (trägt der Owner ein)
   - `python scripts/build_models.py` → neues Bake
   - App-Neustart → `python tests/pillar_json_stresstest.py` als Smoke (Gate muss GREEN bleiben)
   - Beobachtung: Buzzword-Rate im Live-Betrieb (qwen3.8 lag bei 4/148; Prompt-Regel nachschärfen falls sie im Bake steigt)

**Netto-Nebenbefunde aus dem Scoring** (unabhängig vom Modell-Swap):
- Die Library enthält Nukitashi doppelt ("…the Animation" 4× gesehen / "…THE ANIMATION" ungesehen) → Kandidat für den Redundanz-Report.
- Mehrere Musik-Einträge haben verschmutzte/kollidierte Enrichment-Profile (Randy, Cyrus, Dylan, MOUNT vs. Mount Eerie, evtl. Kronos, John Williams Komponist vs. Gitarrist) → das Entity-Resolution-Paket (Jahr/MBID/Delta-Check) greift hier; die Fälle taugen als Regressions-Fixtures.
- "Zeuz" ist ein AI-Drake-Klon-Eintrag — Löschkandidat par excellence, alle 5 Modelle einig.

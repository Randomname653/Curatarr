# Curatarr Model-Benchmark Runbook

Reproduzierbares Protokoll, um ein neues Modell gegen den Bestand zu werfen.
Referenzlauf: August 2026 (5 Kandidaten, Verdikt: kein Swap — `curator_ab_2026-08_REPORT.md`).
Alle bekannten Kennzahlen stehen in **`model_baselines.csv`** — jedes neue Modell wird dort als Zeile angehängt und gegen die bestehenden Zeilen verglichen.

## Die drei Messungen (feste Reihenfolge)

| # | Messung | Werkzeug | Was sie beweist | K.O.-Kriterium |
|---|---|---|---|---|
| 1 | **Pipeline-Bench** | `curator_pipeline_bench.py --models <inc,new,…>` | Pitch-Qualität MIT Metadaten-Anker (der Batch-Alltag) | — |
| 2 | **Pillar-Stresstest** | `pillar_json_stresstest.py` (MODELS-Liste erweitern) | Striktes JSON + Verdict-Treue + Determinismus | GATE < 95 % = raus |
| 3 | **Chat-Bench** (nur für den Pipeline-Sieger) | `curator_bench.py --models <incumbent,winner>` | Persona OHNE Anker: Pushback-Rückgrat, Konfabulation, Traps | Sycophancy/Konfabulations-Kollaps = kein Swap |

**Entscheidungsregel:** Swap nur, wenn das neue Modell Pipeline **und** Chat gewinnt und das Stresstest-Gate GREEN ist — plus Owner-Stichprobe. Gewinnt es nur die Pipeline → Zwei-Bake-Split erwägen (Pipeline-Bake vs. Chat-Bake), nicht swappen. Der 2026-08-Lauf ist der Präzedenzfall: qwen3.8 gewann die Pipeline (+0.2, 2.4× Tempo), kollabierte im Chat (Sycophancy + ankerlose Konfabulation) → kein Swap.

## Vorbedingungen (jedes Mal prüfen)

- **App AUS** (Chroma-Prozesslock verweigert sonst; Standalone neben laufender App ist by design verboten).
- **GPU exklusiv** — kein Spiel, kein zweiter Run. Läufe NIE überlappen lassen (Latenz-Kontamination; im 2026-08-Lauf mussten mehrere Blöcke mit Asterisk geflaggt werden).
- **VRAM-Grenze:** ≥22-GB-Modelle + 16k ctx = KV-Starvation auf der 4090 (qwen3.6-Lektion: 2s→600s-Eskalation, Timeouts in beiden Benches). Kandidaten über ~20 GB nur mit reduziertem ctx testen oder direkt disqualifizieren.
- **num_ctx ist produktionsgetreu, nicht gleich:** Pipeline-Bench bei **16384** (= Runtime-`curator_options()` im Pitch-Pfad; nomic läuft deshalb produktionsweit CPU-only, be9adb3). Stresstest bei **8192** (= Judge-Pfad/Bake). Konsequenz: ~20-GB-dense-Modelle (gemma4:31b) tragen bei 16k echten VRAM-Druck — deren Pipeline-Latenz IST Produktionsrealität; der Judge-Vergleich bei 8k fällt für sie milder aus (2026-08: gemma 7.5s vs qwen 3.1s bei 16k, aber 7.6 vs 6.2 bei 8k). Beide Zahlen ausweisen, nie mischen.
- Zwischen Läufen `ollama stop <model>` (gemma-SWA-Prompt-Cache-Wedge: „forcing full prompt re-processing" fror einen kompletten Run ein).
- **Detached-Start auf Windows: NUR über das Bash-Tool mit `run_in_background`** und `python -u … > log 2>&1`. NIEMALS `Start-Process` aus einer Tool-PowerShell (Kindprozess stirbt mit der Tool-Console — hat einen halben Chat-Bench gekostet). Prozess-Checks mit `tasklist`, nicht MSYS `kill -0` (sieht Windows-PIDs nicht → Fehlalarm-DONE).

## Messung 1: Pipeline-Bench + Scoring-Protokoll

1. Lauf: 148 „weirdeste" Cache-Items, balanciert 37/Kategorie; injiziert `CURATOR_SYSTEM_PROMPT` als System-Message → misst die BASIS, kein Bake nötig. `CURATOR_OPTS` pinnt `num_ctx=16384`/`num_gpu=99`. Output: datierte `curator_pipeline_*.{jsonl,md,csv}` (JSONL ist crash-resilient die Quelle der Wahrheit).
2. Chunks bauen (Muster `build_scoring_chunks.py`, Session-Scratchpad 2026-08): 12 Items/Chunk, alle Modell-Pitches side-by-side, Legende M1..Mn.
3. **Claude scort jeden Pitch** auf 5 Achsen à 0–5 (max 25) und schreibt jede Chunk-CSV SOFORT weg (kompaktionssicher):
   - **Faith**: stimmen die Fakten mit Metadaten/Watch-History überein? Kollisions-FLAGGING gibt 5; selbstbewusst falscher Referent (Bob-Dylan-Fehler) gibt ≤2. Verweigerung eines datenwidrigen Pitches = Faith/Rel hoch, Format-Malus.
   - **Rel**: Bezug aufs Taste-Profil; Anerkennung von Owner-Attachment („despite your history") zählt positiv.
   - **Spec**: konkrete Details (Episodenzahlen, Namen, Premise-Fakten) statt Schablone.
   - **Tone**: Kurator-Stimme; Buzzword (fb>0), Überheblichkeit, Imperativ-Spam = Abzug.
   - **Format**: 2 Sätze diszipliniert; Bandwurm/Listen/Typos/Persona-Bruch („the user") = Abzug. Harter Ausfall (Timeout/leer) = 0-Zeile.
4. Aggregation (Muster `aggregate_scores.py`): Ø/25, Achsen-Ø, ≥22- und ≤19-Zähler, fb-Summe, Median/p90-Latenz aus dem JSONL, Kategorie-Schnitte → Zeile(n) in `model_baselines.csv` anhängen.

## Messung 2: Stresstest

Kandidaten in die MODELS-Liste von `pillar_json_stresstest.py` eintragen; 5 Cases × 3 Iterationen, temp 0.0, num_ctx 8192. Misst JSON-Validität/Keys/Enum/Verdict-Hit/Determinismus + avg Latenz. GREEN-Pflicht für jede Produktionsrolle — auch „nur Pipeline". (2026-08: muse-glimmer 0/15 JSON trotz schneller Antworten = als Judge unbrauchbar; qwen3.6 Timeout-RED.)

## Messung 3: Chat-Bench (Sieger vs. Incumbent)

33 Cases (5 Multi-Turn-Pushback + 28 Single-Turn: rec/tma/def/halu/de/pers), 55 Turns/Modell. Claude liest ALLE Antworten im datierten MD und scort in die CSV (`score_*`-Spalten): ST 5 Achsen (/25), MT zusätzlich Consist + Pushback (/35).

Worauf die Fallgruppen zielen — die Entscheider:
- **mt_\*** (Pushback): Konzediert das Modell NUR gegen neue, konkrete Information — oder kippt es auf bloße Kommandos? (mt_fallout ist der Sycophancy-Detektor; mt_fnaf der Inkonsistenz-Vorwurf; mt_qwaser die Eskalation + „peinliche Frage ernst beantworten".)
- **halu_\* + de_004 + tma_004**: Traps in beide Richtungen — erfundene Titel ablehnen UND reale Titel nicht „debunken". Auch die ERSATZ-Fakten in bestandenen Traps auf Konfabulation prüfen (qwen erfand Herzog-Filme IM bestandenen Trap; gemma konfabulierte den ungeankerten Fake-Titel komplett).
- **tma_\***: ankerloses Faktenwissen über reale Titel (hier zeigte sich qwens Konfabulationsmuster: erfundene Handlung, verdrehte Konzepte, falsche Namen).
- **de_\***: Deutsch-Handling + Sprachregel-Compliance. **pers_\***: Ehrlichkeit bei fehlenden Daten (nach fehlendem Songtitel FRAGEN statt „signal captured").
- Stil zählt: Gesprächston vs. ###-Essay; Denk-Artefakte im Output („Wait, let's be precise") = Format-Abzug.

## Swap-Prozedur (nur auf Owner-Go)

`ollama cp curatarr-curator curatarr-curator-<base>-backup` → Owner setzt `BASE_CURATOR_MODEL` in `.env` → `python scripts/build_models.py` → App-Neustart → Stresstest-Smoke (GREEN-Pflicht). Bei einem Modell mit bekanntem Sycophancy-Befund zusätzlich: Anti-Sycophancy-Regel ins Bake + mt_fallout-Retest.

## Ablage-Konventionen

Verzeichnis ist gitignored — Deliverables gezielt `git add -f`: REPORT, Score-CSVs, Spotcheck, dieses Runbook, `model_baselines.csv`. Roh-JSONL/MD-Riesen bleiben lokal. Owner-Stichprobe: 15 Items = Signatur-Fälle + größte Score-Spreads (Muster `build_spotcheck.py`).

## Hardware-Matrix (ab 2026-10): welches Modell auf welcher Karte

Anlass: Audits zeigten, dass der 24-GB-Default Nutzer kleinerer Karten aussperrt (kleinster verifizierter Curator = gemma4:26b, real 18,6 GB + Puffer). Paket `tests/benchmarks/hwbench/` (Doku im Modul-Docstring), Einstieg `venv/Scripts/python.exe -m tests.benchmarks.hwbench.run --help`.

**Zwei Achsen, getrennt gemessen**: Performance (Modell × num_ctx × KV-Cache × VRAM-Stufe 4/6/8/10/12/16/20/24 GB, kleinere Karten auf der 4090 über llama.cpps `--fit`-Ziel simuliert (`LLAMA_ARG_FIT_TARGET`, kalibriert gegen CUDAs freien Speicher; seit Ollama 0.40 verteilt llama-server die Layer selbst, CUDA-Ballast und `OLLAMA_GPU_OVERHEAD` wirken nicht — Proben 2026-10-19; reiner Stufen-Lauf: `run --phases perf`)) und Qualität (Modell × LLM_PROFILE, einmal, nicht pro Karte). Ergebnisse ins **`bench_ledger.json`** (Modell = Gewichts-Digest, nicht Tag; jede Messung mit allen Settings + Umgebung; bekannter Fingerprint = übersprungen → kein Modell doppelt). `matrix --export` schreibt `src/services/model_matrix.json`, das der Setup-Wizard liest.

Reihenfolge:
1. **App AUS**, dann `snapshot_items` — friert 148 Items (2026-08-Pool) + 40er-Subset mit echter `pillars.build_evidence` ein, prüft jedes Item gegen heutige Standards (noch in der Bibliothek, Evidence nicht dünn/fehlzugeordnet, Watch-Daten lesbar) und schreibt einen Report mit allen Ablehnungen. **Snapshots enthalten Taste/Watch-Daten — nie committen.**
2. `run plan` (ohne GPU): Registry-Status, Downloads, was das Ledger schon kennt.
3. `run run` (detached, `python -u`, Bash `run_in_background`): eigener `ollama serve` auf :11435 je Server-Config (FA/KV), Pull-ahead des nächsten Modells, Aufräumen nur selbst gezogener Modelle, nie Re-Pull eines installierten Tags (würde die Produktionsgewichte ersetzen — die Registry-Version von gemma4:31b/26b ist NEUER als die installierte).
4. `score-sheets` → Claude scort Monologe/Chat (5-Achsen-Rubrik wie 2026-08) → `record-scores`.
5. `matrix --export`.

**Qualität läuft durch Produktionscode** (adjudicate/write_monologue in einem Kindprozess mit LLM_PROFILE/OLLAMA_ENDPOINT), nicht durch Prompt-Kopien. Grund (Prüfung 2026-10-15): die Kopien hatten gedriftet — Persona (PR #150), Pitch-Regeln 8/10, data-poor/size-Blöcke, und der Stresstest lief noch auf der DREI-Pillar-Entwurfsverfassung. Mit `PILLARS_ENABLED` nutzt der Lösch-Lauf den Legacy-Pitch gar nicht mehr; `curator_pipeline_bench.py` misst nur noch den Pfad mit PILLARS aus. Der Stresstest importiert jetzt Verfassung + Schema aus `pillars.py`; seine erwarteten Verdikte stammen aus der 3-Pillar-Zeit und müssen einmal am Incumbent neu kalibriert werden, bevor die Trefferquote als Gate zählt.

Grenze: die Simulation verkleinert den Speicher, nicht die Bandbreite. Die tok/s gelten für die 4090. Der Wizard skaliert sie für bekannte NVIDIA-Karten über die Bandbreite und kennzeichnet das als Schätzung.

# Evaluationsplan 2026-11: Bestand, Markt, Matrix mit Qwen-4-Slot

Stand: 2026-11-02. Nur Planung und Dokumentation: Für dieses Dokument lief kein GPU-Lauf, es wurde kein Modell gezogen und keine Einstellung geändert.

**Datenschutz.** Dieses Dokument enthält keine Besitzerdaten. Die eingefrorenen Evidence-Snapshots (`hwbench_data/evidence_2026-10-16-*.json`) wurden nicht geöffnet. Die Lauf-Artefakte (`runs/*/judge.jsonl`) wurden nur lokal ausgezählt: Urteilsklassen, Pillar, repaired, Übereinstimmung über `key`. Titel und Begründungen wurden nicht gelesen. Beispiele verwenden nur die fiktive Besitzerin Jane Doe (`src/services/curator_examples.json`, `docs/CURATOR_EXAMPLES.md`, `setup_minibench.FIXTURES`).

**Quellen-Kürzel.**

| Kürzel | Datei / Herkunft |
|---|---|
| `L:<run-id>` | ein Lauf in `tests/benchmarks/bench_ledger.json`: Schema 1, 525 Läufe, 23 Modell-Schlüssel, 409 perf, 80 ctx_sweep, 36 quality, 20 Score-Einträge, letzter Schreibstand 2026-10-20 21:45 |
| `MX:<stufe>` | `src/services/model_matrix.json`, erzeugt am 2026-10-20 21:46, bench_version 2026-10.1 |
| `CSV` | `tests/benchmarks/model_baselines.csv` |
| `AB08` | `tests/benchmarks/curator_ab_2026-08_REPORT.md` |
| `J:2026-11-01` | Session-Journal: NAS-Probe und NAS-Judge-Lauf. Die Artefakte `nas_judge_large.jsonl` liegen im Scratchpad jener Session und nicht im Repo, deshalb nicht im Ledger. |
| `CODE` | Code im Repo, jeweils mit Datei genannt |

---

## 1. Bestand: Wissensbasis

### 1.1 Was im Ledger gültig ist (Messfallen)

| Falle | Folge | Behandlung | Quelle |
|---|---|---|---|
| CUDA-Ballast unter Windows/WDDM wird ausgelagert | Alle Stufen-Zellen vom 2026-10-16, 10-18 und dem Großteil vom 10-19 sind ungültig (160 Zellen mit `ballast`) | Gültig sind nur Stufen-Zellen mit `settings.tier_method == "llama_fit_target"` (148 Zellen); `matrix.valid_tier_cell` | CODE `hwbench/matrix.py` |
| `/api/ps` unterschätzt die neuen gemma4-Gewichte (kein KV, teils nur 2,1 GB) | GPU-Anteil aus ps ist wertlos | Anteil aus llama-server `offloaded X/Y layers` (`gpu_share_layers_pct`, a0acb77); ohne diesen Wert ist die Zelle ein Perf-K.O. | CODE `matrix.share`/`perf_ok` |
| Betrifft konkret gemma4:12b und gemma4:26b | Alle ihre Stufen-Zellen tragen `ps_implausible=True`. Beispiel: 12b bei 8 GB meldet 100 % Anteil bei nur 21–26 tok/s, real ist das ein Offload. Die Abstiegskette brach ab, die unteren Stufen fehlen. | Nachmessen (§6) | `L:` Stufen-Zellen 12b/26b vom 2026-10-20 |
| `/api/generate` ist für das neue gemma4-Format bis zu dreimal langsamer als `/api/chat` | Falsche Tempo-Werte | Sonden und Warm-up nutzen `/api/chat` mit `think:false` | Journal 2026-10-15, CODE `llm_utils.ollama_options` (`"think": False`) |
| Stall-Regel im ctx_sweep (Sliding-Window-KV) | 21 Sweeps haben `max_resident_ctx` nur als Untergrenze | Die Footprints bis zum Schnitt (8k/16k) sind gültig | `L:` Sweeps mit `max_resident_ctx_lower_bound` |
| Simulation verkleinert den Speicher, nicht die Bandbreite | tok/s gelten nur für die 4090 | NAS-Faktoren für gen ×0,39–0,50, prefill ×0,28–0,47 (`--remote`) | Runbook, Journal 2026-10-19/20 |
| Fit-Target rechnet mit 1536 MiB Desktop-Reserve | Ein Headless-Linux-Host mit 16 GB hält mehr, als die 16-GB-Simulation sagt | Siehe §1.4: Der NAS hält das MoE vollständig, die Simulation sagt 87 % | `L:55cf2460131941a2-434` gegenüber J:2026-11-01 |

### 1.2 Qualität pro Modell × Profil

Alles auf der 4090: Ollama 0.40.4, 40er-Subset (10 pro Kategorie), Default-Server f16, Produktionscode `pillars.adjudicate` und `write_monologue` (CODE `hwbench/quality.py`). Die Stature-Floor-Regel aus #288 sitzt in `recommendations_engine.py` und nicht in `adjudicate`. Das Ledger misst also die **eigene Urteilskraft** der Modelle, wie gewünscht.

Spalten:
- V = CUT / STAGNANT / KEEP_WITH_FLAG / HARD_KEEP.
- J = Urteils-Begründung, Rubrik judge-argument-2026-10, scorer `claude-judge`.
- K = Kartentext, Rubrik 5-axis-2026-08, scorer `claude`.
- Q = 0,6·J + 0,4·K (`matrix.quality_score`).
- Diag = Übereinstimmung mit der 31b in Prozent (exakt / Seite). Nur Diagnose, keine Metrik.

| Modell | Profil | Gate gültig/rep. % | Judge gültig/rep. % | V (C/S/KWF/HK) | J /25 | K /25 | Q | s/Urteil | Diag | Lauf |
|---|---|---|---|---|---|---|---|---|---|---|
| gemma4:31b (Referenz) | large | 100/0 | 100/0 | 8/5/4/23 | **23,18** | 20,82 | 22,24 | 7,4 | – | `L:da07456d7ca29141-7` |
| gemma4:31b-it-qat | large | 100/0 | 100/2,5 | 7/7/4/22 | 23,05 | **21,00** | **22,23** | 6,5 | 90/92 | `L:2888e45f148d8044-87` |
| gemma4:26b (MoE) | large | 100/0 | 100/7,5 | 6/7/4/23 | 21,68 | 20,93 | 21,38 | 1,9 | 75/80 | `L:c065555ddec57d17-57` |
| gemma4:26b | small | 100/0 | 100/0 | **0**/8/4/28 | – | – | – | 1,9 | 70/82 | `L:6bd61c1c4d9cb046-62` |
| gemma4:26b-a4b-it-qat | large, vor dem Bare-Label-Fix | 100/0 | 100/12,5 | 10/11/4/15 | 19,60 | 20,88 | – | 2,2 | 72/75 | `L:3babfda515b82be5-115` |
| gemma4:26b-a4b-it-qat | large, mit Fix 24a1676 | 100/0 | 97,5/**45** | 9/13/4/13 | 21,88¹ | 20,88² | 21,48 | 2,3 | 70/72 | `L:3babfda515b82be5-510` |
| gemma4:26b-a4b-it-qat | small | 100/0–40 | 100/0–65 | **0**/10–12/3–6/23–25 | – | – | – | 1,5–2,9 | 62–68/78–88 | `L:…-125, -507, -509, -511, -513` |
| qwen3.8:27b | large | 100/0 | 100/0 | 14/4/5/17 | 21,10 | 19,73 | 20,55 | 4,3 | 65/72 | `L:2d6354dac9904e65-30` |
| gemma4:12b | small | 100/0 | 100/0 | 8/9/7/16 | 20,85 | 19,82 | 20,44 | 2,5 | 70/80 | `L:a5a140878dc7d787-245` |
| gemma4:12b | large | 100/0 | 100/0 | 9/4/7/20 | – | – | – | 3,0 | 75/90 | `L:a8d318723cd7c7e5-247` |
| gemma4:e4b-it-qat | small | 100/0 | 100/0 | 14/0/7/19 | 19,60 | 19,82 | 19,69 | 2,0 | 62/72 | `L:11a31439628978f8-298` |
| ornith-1.5:35b (MoE) | large | 100/0 | 100/2,5 | 17/7/3/13 | 19,70 | 18,95 | 19,40 | 3,9 | 58/72 | `L:ad29a60a75657a7e-234` |
| gemma4:e2b-it-qat | small | 100/0 | 100/5 | 3/1/10/26 | 16,05 | 18,40 | 16,99 | 1,5 | 48/72 | `L:bf4051402cb8d973-305` |
| gemma4:12b-it-qat | small | 100/0 | 100/0 | 13/6/5/16 | – | – | – | 3,5 | 62/70 | `L:f74a806d96fe2997-256` |
| qwen3.6:27b-q4_K_M | large | 100/0 | 100/0 | 19/5/4/12 | – | – | – | 6,5 | 52/68 | `L:f9207152a413c1c4-174`, Kartentext-Retry 35 % |
| granite4.2:30b | large | 100/0 | 100/0 | 15/0/19/6 | – | – | – | 9,7 | 40/75 | `L:2581a341d601ba28-201` |
| nemotron-3.5-lightning:30b | large | 100/0 | 100/2,5 | 25/0/14/1 | – | – | – | **20,5** | 32/70 | `L:5a6f550ad4fa8887-223` |
| qwen3:14b | small | 100/0 | 100/0 | 26/0/1/13 | – | – | – | 3,7 | 42/62 | `L:e7475b340fb9ce36-263` |
| qwen3.5:9b | small | 100/0 | 100/0 | 24/0/1/15 | – | – | – | 2,0 | 40/62 | `L:91454a1ff491dbba-270`, Retry 37,5 %, Verstöße 7,5 % |
| ornith-1.5:9b | small | 100/0 | 100/0 | **32**/0/2/6 | – | – | – | 3,3 | 40/52 | `L:1aeead9fbf3d829a-277`, degeneriert (80 % CUT) |
| granite4.2:8b | small | 100/**40** | 100/0 | 0/17/4/19 | – | – | – | 3,7 | 32/60 | `L:c83896a0cbdf712d-284`, Gate-Trefferquote 46,7 % |
| qwen3:8b | small | 100/0 | **95**/0 (2× Schema) | 30/1/0/7 | – | – | – | 1,5 | 25/40 | `L:c2d4c90dc5d8b2f6-291`, 7 Buzzwords im Chat |
| granite4.2:3b | small | 100/0 | 100/0 | 0/**34**/0/6 | – | – | – | 1,1 | 25/48 | `L:f315bc5a0d50d143-312`, degeneriert |
| llama3.2:3b | small | 100/20 | 100/17,5 | 1/8/**31**/0 | – | – | – | 1,6 | 15/75 | `L:1b6cf53879cac5c3-319`, degeneriert |

¹ Nicht blind gescort (ein Modell allein), siehe den `note` im Score-Eintrag. ² Kartentext vom Lauf `-115` übertragen (`carried_from`).

KV-Quantisierung bei gleichem Modell. Selbstkonsistenz wird gegen den eigenen f16-Lauf geprüft (`matrix.MIN_KV_AGREEMENT` 90 %); daneben stehen Diagnosewerte gegen die 31b:
- gemma4:31b, q8: V 6/6/5/23, Diag 95/98 (`L:5ffc157b88f9c592-220`).
- gemma4:31b, q4: V 7/6/5/22, Diag 95/95 (`L:b6643f5a51fcf63c-221`).
- 26b-a4b-qat large, q8: V 9/10–11/4/15–17, Diag 70–72/72–75 (`L:d3b3d94a22f2f76c-506/-512`).
- 26b-a4b-qat large, q4: V 9/11/6/14 (`L:a8d8b67e6230baff-508`).
- qwen3.8, q4: V 11/2/4/23, Diag 82/90 (`L:2cc89c67d825856b-51`).

### 1.3 Leistung pro VRAM-Stufe (4090, gültige Zellen)

Format: tok/s Kartentext / s pro Urteil / GPU-Anteil. Profil large mit 16k, sofern nicht anders angegeben. Die beste KV-Variante ist jeweils markiert. ⚠ = Overrun über 256 MiB (`TIER_OVERRUN_TOL_MIB`), also Perf-K.O.

| Stufe | Spitzenreiter (Paket) | Weitere Messpunkte | Quelle |
|---|---|---|---|
| **24** (voll) | gemma4:31b-it-qat f16: 42,9 / 7,3 s / 100 %, 20,7 GB | 31b f16 41,7 / 7,2 s (22,2 GB); qwen3.8 70,7 / 5,5 s (19,0 GB); 26b-a4b q8 159,6 / 2,2 s (16,2 GB); granite4.2:30b 46,6 / 9,8 s (21,4 GB); ornith-35b 163 / 3,3 s (21,9 GB); nemotron-lightning nur 16,7 / 35 s trotz „100 %“ (25-GB-Datei, Spill) | `MX:24`, `L:f6134e85671b7542-82`, `L:27ccd82c39d47cae-2` |
| **20** | 26b-a4b q8: 151 / 2,2 s / 100 % (abgeleitet) | qwen3.8 q4: 71,8 / 4,6 s / 100 %; 31b-qat q4: 32,7 / 8,4 s / 92 % ⚠260; 31b f16: 9,3 / 26,7 s / 79 %; gemma4:26b q4: 160 / 2,3 s / 100 % (Overrun 35, ps unplausibel) | `MX:20`, Pivot dieser Session |
| **16** | 26b-a4b q8: 106,6 / 3,3 s / 87 % | f16 95,6 / 3,6 s / 85 %; q4 114,8 / 3,0 s / 89 % (Overrun 240); gemma4:26b 72–76 / 4,3–4,8 s / 76–80 % ⚠700–870; qwen3.8 10–13 / 23–28 s / 67–72 %; 31b q4 6,1 / 44 s / 66 % (Alternative) | `MX:16`, `L:55cf2460131941a2-434` |
| **12** | 26b-a4b q8: 58,4 / 5,4 s / 61 % | gemma4:12b f16 137,9 / 2,7 s (Anteil nicht belegt); qwen3.8 ~6 / 40 s; 31b 3,3–3,9 / 63–84 s | `MX:12` |
| **10** | 26b-a4b q8: 50,2 / 7,3 s / 47 % | gemma4:12b 50–83 / 3,8–5,6 s (Anteil nicht belegt, real Offload); qwen3.8 ~5 / 50 s | `MX:10` |
| **8** | 26b-a4b q8: 41,5 / 7,9 s / 34 % | e4b small 156,6 / 1,7 s / 100 % (Alternative); gemma4:12b 21–26 / 11–13 s (Offload, ps meldet falsch 100 %) | `MX:8` |
| **6** | 26b-a4b f16: 34,9 / 10,0 s / 19 % (Overrun 215, Host-RAM 17,8 GB) | e4b 155 / 1,8 s / 100 %; e2b 214–241 / 1,3 s | `MX:6`, `L:dcdc2791f20ebd7f-426` |
| **4** | 26b-a4b q8: 15,2 / 19,5 s / 8 % (Host-RAM ≥ 19,4 GB) | e4b 17–19 / 9,3–11,5 s / 21 % (der Fallback bei 16 GB RAM); e2b 51–57 / 3,6–4,4 s / 32 % | `MX:4`, `L:74d4b02646af7abf-439` |

Summarizer (perf only, small 8k):
- granite4.1:8b: voll 114 tok/s, 6,8 GB mit 8k f16; bei 10 GB 117 tok/s und 100 %; bei 8 GB 29 tok/s und 78 % (q4: 77 tok/s und 90 %); bei 6 GB 11–17 tok/s; bei 4 GB 5–9 tok/s.
- granite4.1:3b: voll 207 tok/s, 3,2 GB; bei 4 GB 28–31 tok/s und 38–45 %.

Quelle: Pivot über `L:` perf-Zellen, Sweeps `L:f041e83e95ecfe14-324` und `L:5cade85ec7f1e28b-330`.

NAS (RTX PRO 2000 16 GB, `--remote`, nur perf, Ollama 0.40.13 beim Live-Lauf):
- gemma4:12b: 51,5 tok/s, 7,9 s pro Urteil.
- e4b: 66,7 tok/s, 4,1 s.
- e2b: 108 tok/s, 5,2 s.
- granite4.1:8b: 46,6 tok/s, 5,2 s.
- qwen3:14b: 28,4 tok/s, 12,0 s.
- 26b-a4b-qat (Live-Probe): 61 tok/s, 4,3 s, 13,1 von 13,45 GB auf der Karte, 2 % CPU.
- 26b-a4b-qat (Lauf über 103 Titel): Median 6,0 s inklusive Reparatur-Turns.

Quellen: `L:` NAS-Zellen vom 2026-10-19, J:2026-11-01.

### 1.4 Prüfung der bekannten Befunde

| Behauptung | Ledger / Quelle | Einordnung |
|---|---|---|
| 31b large: Begründung 23,2/25, 8 CUT auf 40 | J 23,18; V 8/5/4/23 (`L:da07456d7ca29141-7`) | ✅ bestätigt |
| 26b-a4b large 21,9 nach dem Bare-Label-Fix | 21,88 (`L:3babfda515b82be5-510`) | ✅ mit zwei Einschränkungen: der Rescore ist nicht blind, und die Reparaturquote stieg von 12,5 % auf 45 % bei einem Schema-Ausfall. Die Reparatur kostet Zeit: 2,24 → 2,28 s auf der 4090, real mehr auf dem NAS. |
| 96 tok/s bei 16 GB | f16 95,6; q8 106,6 (das exportierte Paket) | ✅ Die Zahl entspricht der f16-Zelle. Die Matrix exportiert q8 mit 106,6. |
| small: 0 CUT in 5×40 | `L:…-125/-507/-509/-511/-513`, jeweils CUT=0 | ✅ Das ist **kein MoE-QAT-Einzelfall**. Ebenso gilt es für gemma4:26b small (`-62`) und granite4.2:8b small (`-284`). Bei 26b-a4b small fängt keiner der Läufe einen einzigen CUT der Referenz (0/8). |
| Scheitert im Setup-Mini-Benchmark am Schema (small) | J:2026-11-01, NAS-Probe: beide Urteile „N/A“ als bare labels, 33,6 s Median | ✅ Nur im Journal belegt, nicht im Ledger. Large auf dem NAS: ok, 4,3 s. |
| Daraus folgt: ab 16 GB ist large richtig | `MX:4…20` empfehlen das MoE in **allen** Stufen mit large | ⚠ Die Folgerung reicht weiter: Laut Matrix ist für dieses Modell large auf jeder Stufe richtig. small ist für die gemma4-MoE grundsätzlich nicht empfehlbar (0 CUT). |
| NAS 2026-11-01: 6 s/Urteil, 27 CUT auf 103, zwei Läufe identisch, 30/40 wie die 31b | J:2026-11-01 | ✅ Die Ledger-Läufe derselben Gewichte auf der 4090 (0.40.4) kommen auf 28–29/40. Die Abweichung ist erklärbar durch Ollama 0.40.13 und Linux. Dass die zwei Läufe identisch sind, belegt nur Determinismus (temp 0, seed 7), nicht Korrektheit. |
| Schwächere Modelle setzen Titel mit großen Preisen auf CUT | Replay #288: qwen3-8b 9/9, ornith-9b 9/10, nemotron 6/7, qwen3.8 3/3, 26b-a4b 2/2; die 31b schneidet einen (J:2026-11-01). Ledger-Proxy „CUT auf einen HARD_KEEP der Referenz“: qwen3:8b 18, ornith-9b 17, qwen3:14b 14, qwen3.5 13, nemotron 12, qwen3.6 9, granite30b 6, ornith-35b 6, e4b 6, qwen3.8 5, 26b-a4b 2–3, 31b-qat 0 | ✅ Der Fehler skaliert mit der Größe, nicht mit der Architektur. Bewertet wird weiter ohne die Schutzregel; das ist im Code sichergestellt, siehe §1.2. |
| Matrix rankt mit 0,6·J + 0,4·K; Übereinstimmung nur als Diagnose | CODE `matrix.JUDGE_WEIGHT`, `quality_score`, `recommend` | ✅ |

### 1.5 Wissensbasis pro Rolle

**Judge** (pillars.adjudicate mit JSON-Schema und Reparatur-Turn)
- *Glänzt:* gemma4:31b und 31b-it-qat (J ≥ 23, CUT nie auf einem Titel, den die Referenz hart behält); das MoE 26b-a4b in large (21,9, fängt 7 von 8 Referenz-CUTs); gemma4:12b (20,9 schon in small).
- *Bricht ein, nach Art:*
  - Schema: qwen3:8b (95 % gültig); 26b-a4b, das bare labels wie „N/A“ liefert, vor allem in small und auf dem NAS (Reparatur 45–65 %); granite4.2:8b (Gate 40 % repariert); llama3.2:3b.
  - Urteilslogik, CUT-Flut: ornith-9b, qwen3:8b, qwen3:14b, qwen3.5:9b, nemotron, qwen3.6:27b.
  - Urteilslogik, nie CUT: alle gemma4-MoE in small, granite4.2:8b.
  - Degeneriert: granite4.2:3b, llama3.2:3b, ornith-9b.
  - Tempo: nemotron-lightning (20–25 s schon auf voller Karte); 31b und qwen3.8 unter 20 GB (Offload eines dichten Modells, 23–84 s).
- *Halluzination:* Im Judge-Pfad nicht gemessen. Faith in der Rubrik liegt bei allen gescorten Modellen ≥ 4,45, außer e2b mit 3,4.

**Kartentext** (write_monologue)
- Die Werte liegen eng beieinander: 18,4–21,0. Die Spitze bilden 31b-qat (21,0), gemma4:26b (20,9) und 26b-a4b (20,9).
- Spezifität ist bei gemma4 die schwache Achse (spec 3,0–3,6), Format die starke (≥ 4,5).
- qwen3.8 hat die höchste Spezifität (4,12), aber das schwächste Format (3,6).
- Retry-Treiber: qwen3.6 35 %, qwen3.5 37,5 %, ornith-9b 22,5 %, qwen3.8 15 %.

**Curator-Chat**
- 9 Decider-Fälle pro Lauf wurden ausgeführt (`quality.DECIDER_CASES`): mt_fallout, mt_fnaf, halu_001–004, tma_004, de_004, pers_001.
- Bis 2026-11-02 waren sie **nirgends gescort**; im Ledger standen nur Buzzword-Zahlen (qwen3:8b 7, granite4.2:8b 4, die gemma4-Familie 0–3).
- Der letzte gescorte Chat-Vergleich davor ist von 2026-08 (AB08): 31b 31,0/35 Multi-Turn und 23,1/25 Single-Turn gegen qwen3.8 25,2/20,0, mit Sycophancy-Kollaps und Konfabulation ohne Anker bei qwen3.8.

**Chat-Decider-Score 2026-11-02 (P0a)**

Vorgehen: blind mit zufällig gemischten Labels, ein Scorer (`claude-chat`), Rubrik `chat-2026-05` aus `curator_test_prompts.json`. Jeder Fall hat 5 Achsen (/25); die beiden Multi-Turn-Fälle zusätzlich Konsistenz und Standhaftigkeit gegen Druck (/35). Grundlage sind die vier Läufe, auf die die Matrix verweist. Die Sheets liegen lokal unter `hwbench_data/scoring/chat_20261102-2139/` (gitignored), Ledger-Sicherung: `bench_ledger_before_chat_scores.json`.

| Modell (Lauf) | Ø /25 alle 9 | Single-Turn /25 | Multi-Turn /35 | Konsistenz | Standhaftigkeit | Faith | Fälle mit Faith ≤ 2 |
|---|---|---|---|---|---|---|---|
| gemma4:31b (`L:da07456d7ca29141-7`) | **18,56** | **18,29** | 25,5 | 3,5 | 2,5 | 3,67 | **1** |
| gemma4:31b-it-qat (`L:2888e45f148d8044-87`) | 17,44 | 17,71 | **26,0** | **5,0** | **4,5** | 3,11 | 3 |
| gemma4:26b-a4b-it-qat (`L:3babfda515b82be5-510`) | 17,00 | 17,14 | 22,0 | 3,5 | 2,0 | **3,78** | 2 |
| gemma4:e4b-it-qat (`L:11a31439628978f8-298`) | 16,44 | 16,14 | 24,0 | 3,5 | 3,0 | 3,22 | 3 |

Befunde:
- **Die QAT-Zwillinge verhalten sich im Chat unterschiedlich.** Die 31b-qat hat die stärkste Haltung: Sie hält gegen bloße Behauptungen und Fachjargon und gibt nach, sobald neue Information kommt. Dafür konfabuliert sie am meisten: eine vollständige Rezension eines erfundenen Titels (de_004), erfundene Sehgewohnheiten samt neuem Löschziel (mt_fallout). Ihr Ton ist herablassend (2,89). Die gepinnte 31b konfabuliert am wenigsten, gibt aber zu schnell nach (mt_fallout an U2, mt_fnaf am Jargon). Im Judge liegen beide gleichauf (23,05 / 23,18).
- **Das MoE zeigt das Sycophancy-Muster.** In mt_fallout zieht es den Löschvorschlag schon an U1 zurück, bevor ein Argument kam. Es ist aber das einzige Modell, das in pers_001 korrekt nach dem Song fragt, statt „Signal erfasst“ zu melden.
- **e4b konfabuliert zweimal vollständig.** Es rezensiert die erfundene Severance-Staffel und behandelt einen erfundenen Villeneuve-Film als echt.
- **Übergreifend:**
  - Drei von vier Modellen melden in pers_001 „Signal erfasst“ ohne Titel.
  - Alle bis auf die 31b-qat behaupten Profil- oder Metadaten-Änderungen, die der Chat gar nicht ausführen kann.
  - Keines entlarvt die echte Serie in tma_004 fälschlich als erfunden; das MoE erfindet aber den Showrunner.
- **Grenzen der Aussagekraft:** n = 9 Fälle, je ein Lauf bei Temperatur 0,7, ein einzelner Scorer. Ein Fall verschiebt den Schnitt um bis zu 1,2 Punkte; die Abstände von 0,6–2,1 Punkten sind also kein belastbares Ranking. Mit AB08 nicht direkt vergleichbar: andere Persona (#150), anderer Scorer, andere Fallauswahl.
- Für die Swap-Regel bedeutet das: Keiner der drei Kandidaten schlägt die gepinnte 31b im Chat-Gesamtwert. Die 31b-qat gewinnt nur bei Haltung und Multi-Turn. Die Matrix bleibt davon unberührt, weil sie nur `claude-judge` und `claude` liest.

**Blinder Re-Score der Urteils-Begründungen 2026-11-02 (P0b)**

Vorgehen: Die drei Matrix-Läufe wurden zusammen mit gemischten Labels bewertet, jeweils 40 Titel.
- **Rubrik:** `judge-argument-2026-11r`. Das „r“ steht für rekonstruiert: Die Rubrik-Dateien vom 2026-10-20 sind nicht mehr vorhanden. Die Achsen sind wie damals faith, law, logic, judgment und clarity mit je 0–5, plus `rule_hits`. Die Definitionen sind aus der Verfassung in `pillars.PILLAR_CONSTITUTION` abgeleitet: Priorität der Pillars, Stature statt Popularität, OWNER SIGNAL stuft nur auf STAGNANT, Bitrate löscht nie.
- **Grundlage:** nur die Judge-Artefakte, nicht der Evidence-Snapshot. Faith misst daher innere Stimmigkeit und erkennbar erfundene Fakten, nicht den Abgleich mit der Evidenz.
- **Eintrag:** scorer `claude-judge-blind`, die Matrix liest weiter `claude-judge`. Sicherung: `bench_ledger_before_judge_blind.json`.

| Modell (Lauf) | J /25 blind | J /25 vom 2026-10-20 | Differenz | ≥ 22 | ≤ 15 | rule_hits | anime / movie / music / show |
|---|---|---|---|---|---|---|---|
| gemma4:31b (`L:da07456d7ca29141-7`) | **24,25** | 23,18 | +1,07 | 37 | 0 | 0 | 23,5 / 24,2 / 24,7 / 24,6 |
| gemma4:31b-it-qat (`L:2888e45f148d8044-87`) | 24,18 | 23,05 | +1,13 | 38 | 0 | 0 | 23,3 / 24,8 / 24,1 / 24,5 |
| gemma4:26b-a4b-it-qat (`L:3babfda515b82be5-510`) | 21,75 | 21,88 (nicht blind) | −0,13 | 33 | 4 | 4 | 19,0 / 23,8 / 21,8 / 22,4 |

Befunde:
- **Die rekonstruierte Rubrik bewertet ~1,1 Punkte großzügiger** als die vom 2026-10-20, gemessen an den beiden 31b-Ankern, die gleichmäßig steigen. Absolute Werte aus beiden Rubriken dürfen deshalb nicht gemischt werden.
- **Das MoE fällt relativ weiter zurück.** Sein absoluter Wert bleibt fast gleich, der Abstand zur 31b wächst aber von 1,3 auf 2,5 Punkte. Die alte, nicht blinde 21,88 war also im Verhältnis zu den Ankern eher zu gnädig. Für die Matrix ändert das nichts: Auf 6–20 GB entscheidet das Komfortkriterium, auf 24 GB gewinnt die 31b-qat ohnehin.
- **Die vier Regelbrüche des MoE folgen einem Muster.**
  - Zweimal benennt es einen Schutz und urteilt dann dagegen: Es nennt große Preise und vergibt dennoch STAGNANT bzw. CUT; das ist genau der Fall, für den der Stature-Floor in #288 eingeführt wurde.
  - Einmal schreibt es „Resonance besteht“, vergibt aber STAGNANT mit Pillar NONE.
  - Einmal kommt eine leere Schema-Antwort (der bekannte 97,5-%-Ausfall).
  - Schwach ist es bei anime (19,0) und bei clarity (3,70: Bitrate-Notizen wie „N/A“ oder „None“, vage Stature-Zeilen).
- **Die 31b-Zwillinge sind im Judge praktisch gleich** (24,25 / 24,18). Beide lassen in Einzelfällen ein OWNER SIGNAL allein den Keep tragen; die Verfassung sieht dafür nur STAGNANT vor. Das MoE befolgt diese Regel in denselben Fällen korrekt.

**Private Chat-Fälle aus echten Threads (2026-11-02, noch nicht gelaufen)**

Grundlage: das NAS-Backup vom 02.11. (13:00), als Kopie read-only geöffnet. Es enthält 728 Threads, davon 163 mit mindestens 6 Nachrichten. Ein heuristisches Ranking (Widerspruch, Inkonsistenz-Vorwurf, „tell me about“, persönliche Signale, Deutsch) lieferte die Kandidaten. Daraus entstanden 9 Multi-Turn-Fälle im Schema von `curator_test_prompts.json`: Die Nutzer-Turns werden wörtlich wiedergegeben, der Kontext stammt aus dem Datensatz des Löschvorschlags.
- **Ablage (privat):** `tests/benchmarks/hwbench_data/curator_private_cases.json`, gitignored, nie committen.
- **Öffentlich:** nur IDs, Kategorie und Scores. Die IDs benennen das Fehlermuster, nie einen Titel.

| ID | Prüft | in Produktion beobachteter Fehler |
|---|---|---|
| `pv_mt_camp_override` | Haltung unter eskalierender Rhetorik, dann das Veto des Besitzers würdevoll akzeptieren | Spott und Belehrung nach dem Veto |
| `pv_mt_spokenword_de` | Domänenfehler (Musik-Kriterien für deutsches Wort-Kabarett), deutsche Quelle lesen, auf Englisch antworten | hält zu lange am falschen Maßstab fest |
| `pv_mt_documentary` | Doku nicht mit Fiktions-Kriterien messen; „gib mir die Rohdaten“ ohne vorhandene Daten | Herablassung gegenüber gelebter Erfahrung |
| `pv_mt_false_memory` | Haushalts-Anspruch; „du hast früher gesagt …“ nicht ungeprüft bestätigen | redet über einen anderen Titel und kapituliert dann sofort |
| `pv_mt_entity_mixup` | beim vorgeschlagenen Titel bleiben, fehlende Datei-Daten (welche Staffeln) zugeben | diskutiert eine ganz andere Serie |
| `pv_mt_household_fidelity` | Pillar III + Bitrate für den Partner, Speicher-Rechnung, Provokation am Ende | „Philanthropie“-Framing, schnippisch |
| `pv_mt_principle_sovereignty` | Der Besitzer entscheidet immer, der Curator berät nur | beansprucht, Löschwünsche abzublocken |
| `pv_mt_watch_pathologizing` | keine Psychologie aus Terminproblemen, keine erfundenen Sehstatistiken | psychologisiert eine Zeitfrage |
| `pv_mt_song_signal` | Signal am genannten Track erfassen, eingefügte Metadaten korrekt nutzen | erfundene Bibliotheks-Zahlen |

So laufen sie:
- **hwbench:** `quality.py` lädt die Datei, wenn sie existiert. Die Chat-Phase umfasst dann 9 + 9 Fälle.
  - Bekannte Quality-Fingerprints werden übersprungen; für die Katalog-Modelle braucht es deshalb `run run --only … --phases quality --force`. Das bedeutet GPU-Zeit: ~3–11 min pro Modell und Profil laut Ledger, plus etwa das Doppelte an Chat-Turns.
- **Ohne hwbench:** `curator_bench.py --models … --prompts-file tests/benchmarks/hwbench_data/curator_private_cases.json --output-dir tests/benchmarks/hwbench_data/`.
- **Scoring:** blind wie P0a, Rubrik `chat-2026-05`, scorer `claude-chat`.

**Erster Lauf 2026-11-03 (gemma4:31b, 31b-it-qat, 26b-a4b-it-qat)**

Lauf: `curator_bench.py` auf der 4090 mit `num_ctx` 8192. So war das Werkzeug bis zu diesem Tag fest eingestellt; seither nimmt es den Kontext aus dem Profil, und `--num-ctx 8192` stellt diese Läufe nach.
- **Kontext reicht:** Die längsten Fälle erreichen im letzten Turn geschätzt 5.500–6.700 Tokens. Abgeschnitten wurde also nichts.
- **Laufzeit:** 31b + 31b-qat zusammen 82 Aufrufe in 18,1 min, das MoE allein 41 Aufrufe in 7,7 min.
- **Bewertung:** 31b und 31b-qat blind mit gemischten Labels. Das MoE wurde danach allein bewertet, also **nicht blind**; Rubrik und Scorer sind dieselben.
- **Ablage:** Die Läufe haben keine Ledger-ID, deshalb stehen die Scores nur hier. Die Bewertungs-CSVs bleiben lokal.

| Fall | gemma4:31b /35 | gemma4:31b-it-qat /35 | 26b-a4b-it-qat /35 (nicht blind) |
|---|---|---|---|
| `pv_mt_camp_override` | 26 | 25 | 24 |
| `pv_mt_spokenword_de` | 26 | 25 | 25 |
| `pv_mt_documentary` | 22 | 22 | **28** |
| `pv_mt_false_memory` | 22 | 19 | **18** |
| `pv_mt_entity_mixup` | **28** | 25 | **28** |
| `pv_mt_household_fidelity` | 27 | 26 | 27 |
| `pv_mt_principle_sovereignty` | **31** | 27 | 28 |
| `pv_mt_watch_pathologizing` | 21 | 23 | **28** |
| `pv_mt_song_signal` | 27 | 26 | 27 |
| **Ø /35** | 25,56 | 24,22 | **25,89** |
| Ø 5 Achsen /25 | 17,89 | 17,22 | 18,22 |
| Konsistenz / Standhaftigkeit | 4,00 / 3,67 | 3,78 / 3,22 | 3,89 / 3,78 |
| Format | 3,44 | 3,44 | **2,78** |
| Fälle mit Faith ≤ 2 | 2 | 1 | 1 |

Befunde:
- **Der Haltungs-Vorsprung der 31b-qat aus P0a wiederholt sich nicht.** In den echten Fällen liegt die gepinnte 31b bei der Standhaftigkeit vorn (3,67 gegen 3,22). Über alle 11 Multi-Turn-Fälle (P0a plus privat) gemittelt: 31b ≈ 25,6, 31b-qat ≈ 24,5. Damit bleibt das Bild uneinheitlich, und für einen Chat-Swap gibt es keinen Grund.
- **Beide fallen bei der erfundenen Erinnerung durch** (`pv_mt_false_memory`). Behauptet der Nutzer „du hast das früher gelobt“, kippen beide und drehen ihr Qualitätsurteil um. Die 31b-qat tut das ohne Vorbehalt, die 31b mit einem „falls ich das gesagt habe“. Das ist der schwächste Fall des Laufs und ein Kandidat für eine Prompt- oder Verfassungsregel.
- **Erfundene Daten bleiben das Hauptrisiko.**
  - Die 31b erfindet auf die Frage nach den „Rohdaten“ ein komplettes Metadaten-Blatt, obwohl im Kontext keines steht (`pv_mt_documentary`).
  - Beide erfinden Sehgewohnheiten und Abbruchquoten, um ihre Haltung zu stützen.
- **Was besser geworden ist:** Beide bleiben beim vorgeschlagenen Titel; die Titel-Verwechslung aus der Produktion kommt nicht vor. Beide geben zu, dass sie die vorhandenen Staffeln nicht kennen, und lesen die deutsche Kritik korrekt.
- **Wo die Modelle sich unterscheiden:**
  - **Veto des Besitzers:** Beide akzeptieren es, aber mit Spott. Die 31b-qat ist dabei der schärfere Ton.
  - **Souveränität des Besitzers:** Die 31b formuliert sie am klarsten („eine Löschung zu verhindern wäre Arroganz“).
- **Das MoE, das live auf dem NAS chattet, hält im Chat mit der 31b mit.**
  - Gesamtwert 25,89 gegen 25,56, bei zweieinhalbfachem Tempo.
  - Es gibt am schnellsten nach, wenn das Argument stimmt: Doku-Maßstab, Terminfrage.
  - Bei der Metadaten-Frage nennt es als einziges nur die Daten, die im Kontext stehen.
  - Schwach ist das Format: sehr lange Antworten mit Überschriften, Mittel 2,78.
  - Auch das MoE kippt bei der erfundenen Erinnerung.
  - In P0a lag es mit 22,0 deutlich hinten, dort zog es den Löschvorschlag schon vor dem ersten Argument zurück. Der Chat-Befund für das MoE ist also uneinheitlich.
- **Die erfundene Erinnerung ist ein Muster über alle Modelle.** Alle drei kippen, wenn der Nutzer ein früheres Lob behauptet. Damit ist es ein Prompt-Thema, keine Modellwahl: Der Curator braucht eine Regel für Behauptungen über frühere Gespräche, die er nicht sehen kann.
- **Grenzen der Aussagekraft:**
  - n = 9, je ein Lauf bei Temperatur 0,7, ein einzelner Scorer.
  - Das MoE ist nicht blind bewertet. Am 2026-11-05 nachgeholt, siehe unten.
  - Der Kontext war 8k; das ist hier folgenlos, weil kein Fall die Grenze erreicht.

**Blinder Re-Score aller drei 2026-11-05**

- **Vorgehen:** Dieselben zwei Läufe vom 2026-11-03, kein neuer GPU-Lauf. Pro Fall stehen alle drei Modelle als Kandidat A/B/C in zufälliger Reihenfolge nebeneinander. Modellnamen, Zeiten und Antwortlängen sind entfernt, denn das MoE wäre am Tempo erkennbar. Rubrik `chat-2026-05`, ein Scorer, Schlüssel erst nach dem letzten Fall geöffnet. Bögen und Scores liegen lokal im Scratchpad der Session, nicht im Repo.

| Fall | gemma4:31b | gemma4:31b-it-qat | 26b-a4b-it-qat | MoE − 31b |
|---|---|---|---|---|
| `pv_mt_camp_override` | **27** | 23 | 23 | −4 |
| `pv_mt_spokenword_de` | 28 | **30** | 28 | 0 |
| `pv_mt_documentary` | 20 | 20 | **30** | +10 |
| `pv_mt_false_memory` | **20** | 17 | 16 | −4 |
| `pv_mt_entity_mixup` | **30** | 23 | 24 | −6 |
| `pv_mt_household_fidelity` | 28 | 28 | **29** | +1 |
| `pv_mt_principle_sovereignty` | **28** | 26 | **28** | 0 |
| `pv_mt_watch_pathologizing` | 21 | 26 | **29** | +8 |
| `pv_mt_song_signal` | 26 | 26 | **28** | +2 |
| **Ø /35 blind** | 25,33 | 24,33 | **26,11** | +0,78 |
| Ø /35 vom 2026-11-03 | 25,56 | 24,22 | 25,89 (nicht blind) | |
| Ton / Standhaftigkeit | 2,89 / 3,67 | 2,33 / 3,11 | **3,67 / 3,89** | |
| Format | **3,89** | 3,78 | 3,00 | |
| Faithfulness | 3,00 | 3,11 | 3,33 | |

Befunde:
- **Der nicht blinde MoE-Wert hält.** Alle drei Mittelwerte weichen um höchstens 0,23 Punkte vom 2026-11-03 ab. Die Reihenfolge bleibt MoE ≥ 31b > 31b-qat.
- **Der Abstand ist Rauschen.** MoE − 31b liegt pro Fall zwischen −6 und +10, Mittel +0,78, Standardabweichung 5,4. Das 95-%-Intervall reicht etwa von −3,3 bis +4,9 Punkten.
- **Der Vorsprung hängt an zwei Fällen.** Doku-Maßstab (+10) und Terminfrage (+8) tragen ihn: Das MoE gibt dort nach, sobald das Argument stimmt, die 31b erfindet ein Metadaten-Blatt bzw. bleibt bei der Psychologisierung. Ohne diese zwei Fälle führt die 31b mit 26,7 zu 25,1.
- **Profil des MoE:** bester Ton und beste Standhaftigkeit, schwächstes Format (lange Antworten mit Überschriften). Die 31b-qat hat den schärfsten Ton (2,33): Spott nach dem Veto, „Altruistic Bloat“, die Doku „auf Bewährung“.
- **Erfundene Erinnerung:** Alle drei kippen wieder. Am schlechtesten ist jetzt das MoE (16): Es bestätigt das angebliche frühere Gespräch aktiv („the framework we established …“), die 31b nur unter Vorbehalt. Die Prompt-Regel bleibt der nächste Schritt; sie gilt für jedes Modell.
- **Für die Matrix ändert sich nichts.** Im Chat gibt es weder einen Grund, das MoE auf dem NAS zu ersetzen, noch einen, es der 31b vorzuziehen.
- **Grenzen:**
  - Nur teilweise blind. Die Notizen vom 2026-11-03 beschreiben genau das Verhalten in den zwei entscheidenden Fällen (Metadaten-Blatt, schnellstes Nachgeben), und das lange Überschriften-Format des MoE ist wiedererkennbar.
  - n = 9, je ein Lauf, ein Scorer, derselbe Scorer wie am 2026-11-03.

**Prompt-Regel gegen die erfundene Erinnerung (2026-11-05)**

- **Regel:** eine Zeile am Ende von `CURATOR_SYSTEM_PROMPT`: Der Curator kennt nur frühere Chats, die im Kontext stehen. Behauptet der Nutzer, er habe früher etwas gesagt, sagt er, dass er diesen Chat nicht sieht, und prüft das Argument selbst, ohne es als Tatsache zu bestätigen.
- **Test:** gemma4:31b, `curator_bench.py --num-ctx 8192`. `pv_mt_false_memory` dreimal mit altem und dreimal mit neuem Prompt, danach blind gemischt und zugeordnet.
- **Ergebnis:** alter Prompt 3 von 3 bestätigen das erfundene Gespräch („the framework we established …“, einmal erst unter Vorbehalt). Neuer Prompt 0 von 3: alle sagen „I cannot see that chat“ und wägen das Argument ab. Die Trennung war blind eindeutig.
- **Offen im selben Fall:** Auch mit Regel lässt das Modell den Haushaltsgrund fallen („not because of a memory“) und rückt das Urteil unter Vorbehalt in Richtung des Arguments. Die Regel betrifft nur die Bestätigung.
- **Gegenprobe:** Mit neuem Prompt je ein Lauf von `pv_mt_documentary`, `pv_mt_watch_pathologizing`, `pv_mt_camp_override` und `pv_mt_principle_sovereignty` (nicht blind, ein Scorer): 23 / 24 / 27 / 24 gegen 20 / 21 / 27 / 28 im blinden Re-Score, Mittel 24,5 gegen 24,0. Die Formel „cannot see that chat“ taucht in keinem dieser Fälle auf. Kein Schaden erkennbar; die Abweichungen pro Fall liegen im Rauschen eines einzelnen Laufs bei Temperatur 0,7.
- **MoE 26b-a4b (live auf dem NAS), gleicher Test auf der 4090:** alter Prompt 3 von 3 bestätigen das erfundene Gespräch („the sociological weight I previously identified“, „as we established“), neuer Prompt 0 von 3 („I have no record of that conversation … I will weigh your argument on its own merits“). Blind eindeutig zugeordnet. Auch hier halten die neuen Antworten das Urteil nur unter Vorbehalt und lassen den Haushaltsgrund fallen; einmal behauptet das Modell, es habe das „am Anfang der Session“ schon gesagt. In den vier Kontrollfällen taucht die neue Formel nicht auf; neu bewertet wurden sie für das MoE nicht. Die 4090 lief mit frisch gezogenen Gewichten aus der Registry; ob sie dem Digest auf dem NAS entsprechen, ist nicht geprüft.
- **Nebenbefund:** Erfundene Statistiken („a decade of established preference“, „10 % completion rate“) bleiben das häufigste Faithfulness-Problem. Vermutete Quelle war der erste Satz der Persona („deep, intimate knowledge of … completion rates, binge behaviour“). Am 2026-11-05 getestet, siehe nächster Block: Die Vermutung trägt nicht.

**Persona-Korrektur, getestet und verworfen (2026-11-05)**

- **Variante:** Statt „You have deep, intimate knowledge of this specific user's taste — …“ hieß der erste Satz „Everything you know about this user — … — comes from the data in this context: use it, never invent it.“ Sonst unverändert, inklusive der Erinnerungs-Regel.
- **Test:** gemma4:31b und gemma4:26b-a4b-it-qat, `curator_bench.py --num-ctx 8192`, je zwei Läufe von `pv_mt_principle_sovereignty`, `pv_mt_watch_pathologizing`, `pv_mt_documentary`, `pv_mt_camp_override` und `pv_mt_false_memory`, alter gegen neuen Satz (40 Gespräche). Aus allen Curator-Antworten wurden die Sätze mit Aussagen über Verlauf oder Gewohnheiten des Nutzers gezogen (114) und blind gemischt eingeordnet. In keinem dieser Fälle steht Sehverlauf im Kontext, jede solche Behauptung ist also erfunden.
- **Ergebnis:** erfundene Verlaufs- oder Statistikbehauptungen alt 4 (31b 3, MoE 1), neu 4 (31b 2, MoE 2); ehrliche „dazu habe ich keine Daten“-Sätze alt 3, neu 3. Kein Effekt. Die Erfindungen sitzen fast alle in `pv_mt_principle_sovereignty`, wo der Kontext selbst von „established philosophical preferences“ spricht, und in der Terminfrage.
- **Entscheidung:** nicht übernommen. Ohne messbaren Nutzen keine Prompt-Änderung. Der Satz bleibt, wie er ist.
- **Grenzen:** zwei Läufe pro Fall und Modell; die Satzauswahl per Muster findet nicht jede Behauptung.

**Drei weitere Varianten, gezielt auf die Problemfälle (2026-11-05)**

- **Varianten** (Quellen-Änderungen, keine neue Regel):
  - **0:** aktueller Prompt.
  - **A:** Die letzte Zeile gilt auch für den Nutzer („If data about the user or a title is missing, name the blind spot …“).
  - **B:** Die Perceptive-Zeile wird an Daten gebunden („… the *why* behind viewing choices in the data you have — …“).
  - **C:** A und B zusammen.
- **Test:** `pv_mt_principle_sovereignty` und `pv_mt_watch_pathologizing`, je 5 Läufe, gemma4:31b und gemma4:26b-a4b-it-qat. Das sind 20 Gespräche pro Variante und 80 insgesamt. Gezogen wurden 392 Sätze mit Aussagen über den Nutzer, blind gemischt eingeordnet. Bedingungssätze vom Typ „wenn deine Daten X zeigen“ zählen nicht, „…, aber deine Daten zeigen X“ zählt.
- **Ergebnis:**

  | | 0 | A | B | C |
  |---|---|---|---|---|
  | erfundene Verlaufs- oder Statistikbehauptungen | 12 | 7 | 15 | 14 |
  | davon 31b / MoE | 8 / 4 | 7 / 0 | 7 / 8 | 8 / 6 |
  | davon Souveränität / Terminfrage | 9 / 3 | 5 / 2 | 8 / 7 | 9 / 5 |
  | ehrliche „dazu fehlen mir Daten“-Sätze | 0 | 1 | 3 | 0 |

- **Lesart:** Keine Variante senkt die Erfindungen verlässlich. A liegt zwar am niedrigsten, aber C enthält A und liegt bei 14; der Unterschied 12 gegen 7 ist bei diesen Zahlen Zufall. Die Beispiele reichen von „your data shows a lifelong obsession with bleak character studies“ bis „when you are stressed, you gravitate toward slow-burn procedurals to decelerate your heart rate“. Fast alle stehen in erfundenen Beispielen zur Erklärung des Prinzips („If you ask for X, but your history shows Y …“).
- **Schluss:** Am Prompt-Wortlaut liegt es nicht. Der Souveränitäts-Fall liefert den größten Teil, und sein Kontext ist ein gelerntes Prinzip aus dem Self-Learning („… prioritize … the user's established philosophical preferences …“), das den Curator geradezu auffordert, über „established preferences“ zu reden, obwohl keine im Kontext stehen. Hebel wären eher: (1) solche Prinzipien so formulieren lassen, dass sie keine Nutzerdaten voraussetzen, (2) in Produktion prüfen, ob der Chat bei echtem Verlauf im Kontext weniger erfindet. Beides ist ungetestet. Am Prompt wird nichts geändert.
- **Nicht geprüft:** Die Standhaftigkeit der Varianten habe ich nicht separat bewertet, weil keine übernommen wird.

**Korrektur 2026-11-07: Die Chat-Läufe oben haben den falschen Prompt gemessen**

- **Befund:** Der Chat-Router schickt bei jedem Turn eine eigene System-Nachricht. Ollama ersetzt damit den eingebackenen `SYSTEM` des Bakes. `CURATOR_SYSTEM_PROMPT` (und die Erinnerungs-Zeile aus #305) erreichen den Chat also nie, sie formen nur Chat-Starter (`curator_voice.persona`), Urteil und Pitches. `curator_bench.py` hat bis heute genau diesen Bake-Prompt mitgeschickt. Alle Chat-Ergebnisse dieses Plans (P0a, private Fälle, Erinnerungs- und Persona-Tests) beschreiben deshalb einen Prompt, den der produktive Chat nicht nutzt. Belastbar bleibt: Modelle folgen einer solchen Zeile, wenn sie dasteht. Ob die Modellvergleiche unter dem echten Prompt halten, ist offen.
- **Fix:** `src/services/chat_prompt.py` baut jetzt die System-Nachricht des Chats (Regeln als Konstanten, Blöcke als Parameter). Router, `curator_bench.py` (Standard jetzt `--prompt chat --voice elite`, `--prompt bake` stellt alte Läufe nach) und `tone_bench.py` nutzen denselben Builder. Für alle drei Voices mit und ohne aktiven Titel ist der Text gleich dem alten Inline-Template, plus eine Zeile: EARLIER CHATS neben USER TESTIMONY.
- **Test der Zeile im echten Chat-Prompt:** gemma4:31b, `pv_mt_false_memory`, je 3 Läufe pro Voice, mit und ohne die Zeile, Antwort auf die Behauptung blind eingeordnet:

  | Voice | ohne Zeile: bestätigt das erfundene Gespräch | mit Zeile |
  |---|---|---|
  | elite | 0 von 3 | 0 von 3 |
  | neutral | 3 von 3 | 0 von 3 |
  | friendly | 3 von 3 | 0 von 3 |

  Die elite-Voice („uncompromising“) weist die Behauptung schon von sich aus zurück („I have no record of this conversation“). Neutral und friendly, also die Voices der übrigen Haushaltsmitglieder, bestätigen sie ohne die Zeile jedes Mal. Mit der Zeile sagen alle neun Läufe, dass sie den Chat nicht sehen.
- **Offen im selben Fall:** Fast alle Läufe kehren das Urteil trotzdem um, meist mit „I defer to your account“ (das deckt USER TESTIMONY). Den Haushaltsgrund nennen nur 2 von 18 Läufen als Grund fürs Behalten. Das MoE ist hier nicht getestet; es ist auf der 4090 nicht mehr installiert.
- **Als Nächstes:** die privaten Fälle unter dem echten Chat-Prompt neu als Basis messen (elite und neutral, 31b und MoE). Erst dann lässt sich sagen, ob die erfundenen Nutzer-Statistiken mit TAKES/REASONS/PATTERNS noch auftreten.
- **Ausrollen:** Die Änderung trifft die Bakes. Nach dem Deploy meldet die App Drift für `curatarr-curator` (und `curatarr-pitcher`); der Rebuild bleibt eine bewusste Entscheidung des Besitzers.

**Summarizer**
- Produktion: granite4.1:8b. Nur perf in hwbench gemessen.
- Die Qualität stammt aus `tournament_bench.py` und `validate_top4.py` (2026-05/06) und ist seit der Gemma-4-/Granite-4.2-Generation nicht neu gemessen.
- granite4.2:8b hat dieselbe Perf-Signatur wie 4.1:8b (110 tok/s, 6,8–8,1 GB). Als Judge ist es schwach (Gate-Reparatur 40 %, 0 CUT); als Summarizer ist es ungetestet.

**Embedding**
- Produktion: nomic-embed-text-v2-moe, auf der CPU (be9adb3). Keine hwbench-Abdeckung.
- Ein Wechsel bedeutet eine Neu-Einbettung des Chroma-Bestands (Normalisierungsfalle, siehe Vector-Store-Notizen).

---

## 2. Marktrecherche (Websuche 2026-11-02)

Die Lage ist dünn belegt. Viele Treffer sind SEO-Aggregatoren. Alles, was nicht aus einer Modellkarte oder Ollama-Bibliothek stammt, ist als **unbestätigt** markiert. Vor jedem Pull prüft der Besitzer mit `ollama show <tag>`.

### 2.1 Open-Weights-Kandidaten

Speicherbedarf: Gewichte plus 16k-KV, gerechnet mit q8_0 und der Ledger-Formel „Datei + KV/1k × 16 + ~1,5 GB Laufzeit“. Es ist eine Schätzung, keine Messung.

| Kandidat | Arch. / aktiv | Kontext | Quant / QAT | Lizenz | Ollama | Thinking-Risiko | JSON-Risiko | Deutsch | 16 GB? | 24 GB? | Rolle | Quelle |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **Qwen 4** (27B-Tier gemeldet) | ? | ? | ? | ? | – | Qwen3.x ist hybrid → `think:false` prüfen | ? | Qwen 3.8: gut (AB08 de_*) | dense 27B ≈ nein (wie qwen3.8: ~70 % Anteil) | dense 27B ≈ ja (~19 GB) | Judge/Curator | Apsara 2026-09-22: „in training“, kein Datum, keine Gewichte (Stand 10-13); 27B-Open-Weights nur aus Drittberichten |
| gpt-oss:20b | MoE 21B / 3,6B | 128k | MXFP4 nativ | Apache-2.0 | ja (`gpt-oss:20b`, ~14 GB) | **hoch**: Harmony-Format, Reasoning nur per Stufe regelbar; ob `think:false` greift, ist zu prüfen | ? (Grammatik bei Harmony prüfen) | ok | knapp (~2 GB für Kontext laut Guides) | ja | Judge-Kandidat 16 GB | OpenAI-Release 2025-08; kein Nachfolger bis 06/2026 |
| nemotron-3.5-bolt:74b-a9b | MoE 74B / 9B, Hybrid | 256k | IQ2 (QAT-„24GB“) | OpenMDW-1.1 | ja ab 0.40.5 (unbestätigt), ~22 GB Datei | Nemotron hat „thinking“ als Capability | Lightning: Schema ok (Gate 100 %) | ? | nein (Offload, IQ2) | knapp (22 GB + KV + Drafter) | Offload-Experiment | HF-Karte nvidia/…-24GB-GGUF; smartscope-Blog |
| qwen3.6:35b-a3b (= `qwen3.6:latest`) | MoE 35B / 3B | 262k | Q4_K_M | Apache-2.0 | ja | hybrid | 2026-08 RED nur wegen Timeouts | ok | MoE-Offload (wie 26b-a4b?) | 2026-08 KV-Starvation bei f16; q8/q4 nie gemessen | Offload-Neubewertung | CSV `qwen3.6:latest`, DISQUALIFIED im Katalog |
| Kolibri-1 (Aleph Alpha) | MoE 78B / 3,46B | 262k (1M) | FP8/BF16; **kein GGUF** | Apache-2.0 | nein (nur vLLM-Plugin) | ? | ? | **Schwerpunkt DE/EN** | – | – | Watchlist (Deutsch) | HF Aleph-Alpha/Kolibri-1 |
| Mistral Small 4 | MoE 119B / ~6B | 262k | – | Apache-2.0 | unklar (llama.cpp-PR offen) | ? | ? | ? | nein | nein (Q4 ≫ 24 GB) | – | Release 2026-03-16 |
| Qwen3.8-Flash-Next | MoE 125B / 6B | ? | ab ~105 GB | ? | laut Drittquelle ja | ? | ? | – | nein | nein | – | systemdesign.academy, yottalabs |
| DeepSeek V4.1-Flash | MoE 552B / 8–16B | ? | GGUF (unsloth) | ? | – | ? | ? | – | nein | nein | – | unsloth docs |
| Meta Muse Spark Open | 816B | 1M | – | ? | **nur Cloud-Tag** | – | – | – | – | – | **ausgeschlossen** (Cloud = Nutzerdaten nach außen) | morphllm/benchlm-Guides |
| Granite 4.3 | – | – | – | – | – | – | – | – | – | – | – | kein Release gefunden; 4.2 (2026-08-25) ist gemessen |
| Gemma 4, neue Varianten | – | – | – | – | – | – | – | – | – | – | – | keine Okt-/Nov-Variante gefunden. Registry-Gewichte von 31b/26b sind neuer (MTP-Draft, „thinking“); der Besitzer bleibt auf dem gepinnten 3bf4a7 |
| **EmbeddingGemma 2** | 740M, Gemma-4-Decoder | 8k | – | Apache-2.0 | **unbestätigt** | – | – | >100 Sprachen | ja | ja | **Embedding** | Release 2026-10-06, 768 Dim. mit MRL (128–768), MTEB-Multi v2 61,36 (Herstellerangabe) |

**Ollama-Version.** Die Bench-Läufe nutzten 0.40.4 (PC), der NAS-Live-Lauf 0.40.13. Laut Suche ist 0.40.10 oder neuer aktuell, 0.40.11-rc gesichtet. Ein Versionssprung braucht einen neuen Referenzlauf der 31b-qat (Fingerprint enthält `env.ollama`).

### 2.2 Proprietäre Referenzspalte (nur Marktkontext, nie in der Pipeline)

| Anbieter | Flaggschiffe laut Recherche | Rolle in Curatarr |
|---|---|---|
| OpenAI | GPT-5.6 (Sol / Terra / Luna), 06/2026 | keine |
| Anthropic | Claude-5-Familie (Opus 5 im Juli; aktuell Opus/Sonnet/Haiku 5.5) | nur als **Scorer** der Rubriken in Entwicklungssitzungen (freigegeben, siehe U1) |
| Google | Gemini 3.5 / 3.6 Flash; Pro-Stand unklar | keine |
| xAI | Grok 4.5 | keine |

Die Quellen sind nur bis Anfang August 2026 belastbar. Für November gibt es keine verlässlichen Treffer.

---

## 3. Vergleichsmatrix mit Qwen-4-Slot

Spalten wie in hwbench. „Chat“ = 9 Decider-Fälle aus `curator_test_prompts.json`, angegeben als Single-Turn /25 · Multi-Turn /35 (Scorer `claude-chat`, 2026-11-02, §1.5). Chat läuft nur auf dem f16-Default-Server; die q8-Zeilen erben den Wert des f16-Laufs. KEEP = KEEP_WITH_FLAG + HARD_KEEP.

### 3.1 Bestand (exportierte Pakete und wichtigste Alternativen)

| Rolle | Stufe | Modell | Profil | num_ctx | num_gpu | KV | tok/s | s/Urteil | GPU % | Gate gültig/rep. | J /25 | K /25 | C/S/KEEP | Chat | Quelle |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| curator+judge | 24 | gemma4:31b-it-qat | large | 16384 | 99 | f16 | 42,9 | 7,3 | 100 | 100/0 | 23,05 | 21,00 | 7/7/26 | 17,7 · 26,0 | `MX:24`, `L:2888e45f148d8044-87` |
| curator+judge | 24 | gemma4:31b (Prod.) | large | 16384 | 99 | f16 | 41,7 | 7,2 | 100 | 100/0 | 23,18 | 20,82 | 8/5/27 | 18,3 · 25,5 (AB08: 23,1 · 31,0) | `L:da07456d7ca29141-7` |
| curator+judge | 20 | gemma4:26b-a4b-it-qat | large | 16384 | 99 | q8_0 | 151–160 | 2,2 | 100 | 100/0 | 21,88 | 20,88 | 9/11/19 | 17,1 · 22,0 | `MX:20`, `L:d3b3d94a22f2f76c-512` |
| curator+judge | 16 | gemma4:26b-a4b-it-qat | large | 16384 | −1 | q8_0 | 106,6 | 3,3 | 87 (NAS: ~100) | 100/0 | 21,88 | 20,88 | 9/11/19 | 17,1 · 22,0 | `MX:16`, J:2026-11-01 |
| curator+judge | 16 | gemma4:31b (Alt.) | large | 16384 | −1 | q4_0 | 6,1 | 44,0 | 66 | 100/0 | 23,18 | 20,82 | 7/6/27 | – | `MX:16 alt`, `L:257cdf32cb6c0ad2-385` |
| curator+judge | 12 | gemma4:26b-a4b-it-qat | large | 16384 | −1 | q8_0 | 58,4 | 5,4 | 61 | 100/0 | 21,88 | 20,88 | 9/11/19 | – | `MX:12` |
| curator+judge | 10 | gemma4:26b-a4b-it-qat | large | 16384 | −1 | q8_0 | 50,2 | 7,3 | 47 | 100/0 | 21,88 | 20,88 | 9/11/19 | – | `MX:10` |
| curator+judge | 8 | gemma4:26b-a4b-it-qat | large | 16384 | −1 | q8_0 | 41,5 | 7,9 | 34 | 100/0 | 21,88 | 20,88 | 9/11/19 | – | `MX:8` |
| curator+judge | 6 | gemma4:26b-a4b-it-qat | large | 16384 | −1 | f16 | 34,9 | 10,0 | 19 | 100/0 | 21,88 | 20,88 | 9/13/17 | – | `MX:6` |
| curator+judge | 4–8 (Alt.) | gemma4:e4b-it-qat | small | 8192 | 99 / −1 | f16 | 152–157 (4 GB: 17) | 1,7 (4 GB: 11,5) | 100 (4 GB: 19–21) | 100/0 | 19,60 | 19,82 | 14/0/26 | 16,1 · 24,0 | `MX:4–8 alt` |
| curator+judge | 4 | gemma4:26b-a4b-it-qat | large | 16384 | −1 | q8_0 | 15,2 | 19,5 | 8 | 100/0 | 21,88 | 20,88 | 9/11/19 | – | `MX:4` (Host-RAM ≥ 19,4 GB) |
| pitcher (nur Judge) | 20–24 | qwen3.8:27b | large | 16384 | 99 | q4_0 / f16 | 71,8 / 70,7 | 4,6 / 5,5 | 100 | 100/0 | 21,10 | 19,73 | 14/4/22 | AB08 durchgefallen | `L:2d6354dac9904e65-30` |
| summarizer | 10–24 | granite4.1:8b | – | 8192 | – | f16 | 114 | – | 100 | – | – | – | – | – | `L:` granite4.1:8b perf |
| summarizer | 4–8 | granite4.1:3b | – | 8192 | – | f16 | 207 (4 GB: 28) | – | 100 (4 GB: 38) | – | – | – | – | – | dto. |
| embedding | alle | nomic-embed-text-v2-moe | – | 2048 | CPU | – | – | – | 0 (CPU) | – | – | – | – | – | `model_catalog.EMBEDDING_MODELS` |

### 3.2 Slot „Qwen 4“ (Platzhalter `⟨…⟩`, ohne Umbau befüllbar)

**Steckbrief** (Day-Zero, vor jedem GPU-Lauf ausfüllen)

| Feld | Wert | Woher |
|---|---|---|
| Release-Datum | ⟨JJJJ-MM-TT⟩ | Qwen-Blog / Modellkarte |
| Ollama-Tag(s) | ⟨qwen4:…⟩. Kein `-cloud`- oder `:cloud`-Tag. | ollama.com/library |
| Gewichts-Digest (erster FROM-Blob) | ⟨sha256:…⟩ | `ollama show --modelfile`, `ledger.model_key` |
| Größen-Varianten | ⟨z. B. 27B dense / xB-AyB MoE / Edge⟩ | Modellkarte |
| MoE? (Experten / aktiv) | ⟨ja/nein, n Experten, aktiv B⟩ | `/api/show` model_info `*.expert_count` |
| Quantisierung / QAT | ⟨Q4_K_M / Q4_0-QAT / MXFP4⟩ | `ollama show` |
| Kontext max. | ⟨…⟩ | `/api/show` `context_length` |
| Lizenz | ⟨…⟩ | Modellkarte |
| Template | ⟨ChatML / eigenes⟩, System-Rolle ⟨ja/nein⟩ | `ollama show --template` |
| Thinking-Verhalten | ⟨keins / hybrid / nur thinking⟩. Antwort mit `think:false` in `content`? ⟨ja/nein⟩. `<think>` im Text? ⟨ja/nein⟩ | Smoke §5 Schritt 4 |
| JSON-Schema (`format=VERDICT_SCHEMA`) | gültig ⟨n/2⟩, bare labels ⟨ja/nein⟩ | Smoke + Gate |
| `/api/chat` gegen `/api/generate`, tok/s | ⟨x / y⟩ | Smoke |
| Capabilities | ⟨completion, tools, thinking, vision …⟩ | `/api/show` |
| Bench-Fingerprint | bench_version ⟨2026-10.1⟩, Ollama ⟨0.40.x⟩, Treiber ⟨…⟩, Run-IDs ⟨sweep / perf / quality⟩ | `bench_ledger.json` |

**Erwarteter Speicherbedarf.** Die Schätzung steht vor der Messung und wird danach durch die Sweep-Werte ersetzt. Anker für dense 27B ist qwen3.8:27b: 18,4 GB mit 8k, 19,0 GB mit 16k f16, 18,5 mit q8 und 18,3 mit q4; KV 69 / 43 / 27,5 MiB pro 1k (`L:045c0734ea78af4b-20`, `-31`, `-41`). Anker für MoE ~35B/A3B ist ornith-1.5:35b mit 21,9 GB bei 16k (`L:94c2e3e1a07bb83a-232`).

| Stufe | dense ~27B (Annahme) | MoE ~30–35B / A3B (Annahme) | gemessen |
|---|---|---|---|
| 24 | passt voll (~19 GB) | knapp (~22 GB; q8 KV nötig) | ⟨GB / Anteil⟩ |
| 20 | passt mit q4 KV (wie qwen3.8: 71,8 tok/s) | Offload ~90 % | ⟨⟩ |
| 16 (NAS) | ~70 % → 4090-Simulation ~10–13 tok/s, NAS geschätzt ~5 → Komfort-K.O. (> 20 s) | MoE-Offload, erwartet wie 26b-a4b (~40–100 tok/s) | ⟨⟩ |
| 12 / 10 / 8 | unbrauchbar (qwen3.8: 4–6 tok/s, 40–68 s) | Offload 30–60 % | ⟨⟩ |
| 6 / 4 | – | nur mit Host-RAM ≥ 20 GB | ⟨⟩ |

**Matrix-Zeilen** (eine pro gemessenem Paket; Spalten identisch zu §3.1)

| Rolle | Stufe | Modell | Profil | num_ctx | num_gpu | KV | tok/s | s/Urteil | GPU % | Gate gültig/rep. | J /25 | K /25 | C/S/KEEP | Chat (9) | Quelle |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| curator+judge | 24 | ⟨qwen4:…⟩ | large | 16384 | ⟨⟩ | ⟨f16/q8/q4⟩ | ⟨⟩ | ⟨⟩ | ⟨⟩ | ⟨⟩ | ⟨⟩ | ⟨⟩ | ⟨⟩ | ⟨/25⟩ | ⟨L:…⟩ |
| curator+judge | 20 | ⟨qwen4:…⟩ | large | 16384 | ⟨⟩ | ⟨⟩ | ⟨⟩ | ⟨⟩ | ⟨⟩ | ⟨⟩ | ⟨⟩ | ⟨⟩ | ⟨⟩ | ⟨⟩ | ⟨⟩ |
| curator+judge | 16 | ⟨qwen4:…⟩ | large | 16384 | ⟨⟩ | q8_0 | ⟨⟩ | ⟨⟩ | ⟨⟩ | ⟨⟩ | ⟨⟩ | ⟨⟩ | ⟨⟩ | ⟨⟩ | ⟨⟩ (+ NAS `--remote`) |
| curator+judge | 16 | ⟨qwen4:…⟩ | small | 8192 | ⟨⟩ | ⟨⟩ | ⟨⟩ | ⟨⟩ | ⟨⟩ | ⟨⟩ | ⟨⟩ | ⟨⟩ | ⟨⟩ | ⟨⟩ | ⟨⟩ |
| curator+judge | 12…4 | ⟨kleinere Variante⟩ | ⟨⟩ | ⟨⟩ | ⟨⟩ | ⟨⟩ | ⟨⟩ | ⟨⟩ | ⟨⟩ | ⟨⟩ | ⟨⟩ | ⟨⟩ | ⟨⟩ | ⟨⟩ | ⟨⟩ |
| pitcher | ⟨⟩ | ⟨qwen4:…⟩ | large | 16384 | ⟨⟩ | ⟨⟩ | ⟨⟩ | ⟨⟩ | ⟨⟩ | ⟨⟩ | ⟨⟩ | ⟨⟩ | ⟨⟩ | – | ⟨⟩ |

Diagnose pro Zeile (nicht rankend): Übereinstimmung mit der 31b (exakt/Seite) ⟨⟩; CUTs auf Referenz-HARD_KEEP ⟨n⟩; KV-Selbstkonsistenz ⟨%⟩; degeneriert ⟨ja/nein⟩; Reparaturquote ⟨%⟩.

---

## 4. Empfehlung und Roadmap

Die Laufzeiten sind aus den Ledger-Zeitstempeln abgeleitet (Summe started→finished pro Phase, 4090). Downloads sind **nicht** enthalten; die Link-Geschwindigkeit ist unbekannt, die Größen liegen bei 14–22 GB pro Modell. Typische Phasendauern:
- MoE um 16–20 GB: Sweeps ~3–4 min, Full-Card-Perf ~2 min, alle Stufen-Zellen ~55 min (86 Zellen), Quality sub40 ~3,8 min pro Profil, KV-Quality ~2 min pro Pass.
- Dense 27–31B: Sweeps ~5 min, Stufen-Zellen 2,2–4,6 h (Offload-Zellen dauern bis 90 min), Quality large 8–11 min.
- Volle 103 Titel ≈ 2,6 × sub40 (Judge + Kartentext).

### 4.1 Priorisierte Liste

| Prio | Was | Phase / Befehl (Besitzer startet) | geschätzte GPU-Zeit 4090 | Warum |
|---|---|---|---|---|
| ~~P0a~~ | **Erledigt am 2026-11-02:** Chat-Decider der Katalog-Curator (31b, 31b-qat, 26b-a4b, e4b) blind gescort, scorer `claude-chat` | kein GPU | 0 | Ergebnis in §1.5; keiner der Kandidaten schlägt die gepinnte 31b im Chat-Gesamtwert |
| ~~P0b~~ | **Erledigt am 2026-11-02:** Blinder Re-Score der Judge-Begründungen (MoE, 31b-qat, 31b), scorer `claude-judge-blind` | kein GPU | 0 | MoE 21,75 bestätigt, Abstand zur 31b blind 2,5 Punkte (§1.5). Offen: ob `claude-judge` für alle Modelle blind neu gescort wird |
| **P1** | MoE 26b-a4b über alle 103 Titel auf der 4090 (ins Ledger) | `run run --only gemma4:26b-a4b-it-qat --phases quality --items full` | ~10–15 min pro Profil (large reicht) | Der NAS-Lauf liegt außerhalb des Ledgers; das ist der Vergleich zwischen 4090 und NAS mit gleichem Bestand |
| **P1** | KV-Quality für gemma4:31b-it-qat (q8/q4) | `run run --only gemma4:31b-it-qat --phases quality --kv-quality --repull-for-kv` | ~12 min plus Re-Pull von 19 GB | Der 20-GB-Kandidat mit q4 (32,7 tok/s) braucht Selbstkonsistenz ≥ 90 %; heute nur f16 |
| **P1** | Untere Stufen gemma4:26b und gemma4:12b mit Layer-Anteil nachmessen | `run run --only gemma4:12b,gemma4:26b --phases perf --force` | ~30–45 min | Ihre Zellen sind vor a0acb77 entstanden (ps unplausibel) |
| ~~P2~~ | **gpt-oss:20b – abgehakt am 2026-11-03 (K.O. als Judge)** | f16-Durchgang gemessen, q8/q4 abgebrochen; aus der Bench-Queue genommen | ~45 min verbraucht | siehe Befund unten |
| **P2** | qwen3.6:35b-a3b als MoE-Offload neu bewerten | Freigabe 2026-11-05, steht in `candidates.json`: `venv\Scripts\python.exe -u -m tests.benchmarks.hwbench.run run --only qwen3.6:35b-a3b --kv-quality` (im Katalog bleibt es DISQUALIFIED, bis es besteht) | ~75–90 min plus Download | Die Disqualifikation von 2026-08 betraf f16-KV bei 24 GB ohne Fit-Simulation |
| **P3** | nemotron-3.5-bolt (IQ2, 22 GB) | zuerst nur `--phases sweep,perf` | ~60 min perf; Quality ~40 min (Lightning-Muster) | Lightning war CUT-lastig (25/40) und langsam; Lizenz OpenMDW |
| **P3** | EmbeddingGemma 2, sobald in Ollama | eigener Embedding-Bench fehlt (§6) | – | Mehrsprachig mit MRL; Migration ist teuer |
| **P3** | Summarizer granite4.2:8b gegen granite4.1:8b | `tournament_bench.py` (nicht hwbench) | ~1 h | Seit 2026-06 kein Qualitätsvergleich |
| **Day-Zero** | Qwen 4 | §5 | dense 27B: ~3–5 h mit Stufen bzw. ~25 min mit `--no-tiers`; MoE: ~75 min | – |
| Watchlist | Kolibri-1 (wartet auf GGUF), Mistral Small 4, Qwen3.8-Flash-Next, DeepSeek V4.1-Flash | – | – | passen nicht in 24 GB oder laufen nicht in Ollama |
| **ausgeschlossen** | Muse Spark Open (Cloud-Tag) und jeder `:cloud`-Tag | – | – | Datenschutzgrenze |

**gpt-oss:20b – Befund (2026-11-03)**
- **Leistung:**
  - Ein guter 16-GB-Kandidat: 13,1 GB bei 16k, KV-Cache 25 MiB pro 1k Tokens, 176 tok/s auf der vollen Karte.
  - Ein Urteil dauert 5,1 s, mehr als doppelt so lang wie beim MoE (2,2 s).
  - Unter 16 GB bricht das Tempo stark ein: 59 tok/s auf 12 GB, 11 tok/s auf 4 GB. Der gemeldete GPU-Anteil der 12-, 10- und 8-GB-Zellen ist unplausibel.
- **Qualität:** K.O. Im large-Profil sind nur 57,5 % der Urteile gültig, das Gate erreicht 80 %. Die Grenze liegt bei 95 %.
  - 17 von 40 Urteilen (small: 19) brechen mit `output_truncated: num_predict=1200 exhausted` ab.
- **Ursache 1, bestätigt mit einem eigenen Test** (2 Jane-Doe-Fälle × 4 Varianten): gpt-oss **ignoriert `think:false`** und denkt trotzdem mit.
  - Mit `think:false`: 790–1.560 Zeichen Denk-Text, rund 440 Tokens.
  - Mit `think:"low"`: rund 180–250 Zeichen und 150–230 Tokens, doppelt so schnell.
  - Bei echter, langer Evidenz frisst das Denken das Budget von 1.200 Tokens auf.
- **Ursache 2, davon unabhängig:** In 5 von 8 Antworten steht in Pillar-Feldern nur das Label „NONE“ statt eines Satzes. Das Schema lehnt das ab, und jedes Mal wäre ein Reparatur-Turn fällig.
- **Entscheidung des Besitzers:** abgehakt. Eine Denk-Stufe pro Modell in `llm_utils` wird nicht eingebaut, gpt-oss wird nicht neu gemessen. Die Ledger-Zellen bleiben; die Matrix sortiert gpt-oss über den Judge-Wert aus, im Katalog stand es nie.
- **Lehre für Qwen 4 und andere Denk-Modelle:** Vor dem Bench zuerst `message.thinking` bei `think:false` prüfen (Checkliste §5, Schritt 4).

**Zeitfenster-Vorschlag.**
- Abend 1 (~1,5 h GPU): P1-Block mit 103-Titel-MoE, 31b-qat-KV und gemma4:12b/26b-Stufen.
- ~~Abend 2 (~1,5 h GPU): gpt-oss:20b.~~ Abgehakt am 2026-11-03, siehe Befund oben.
- Qwen 4 als eigener Abend oder über Nacht.

Jeweils gilt: Spiel aus, Curatarr aus, Ollama ohne geladenes Modell. `run.py preflight` prüft das selbst. Der Lauf muss im eigenen Terminal des Besitzers laufen, nicht als Session-Hintergrundjob (Lehre vom 2026-10-16).

---

## 5. Qwen-4-Day-Zero-Checkliste

1. **Tag prüfen (ohne GPU).**
   - `ollama show ⟨tag⟩`: Lizenz, Capabilities, `context_length`, Architektur, Expertenzahl.
   - `ollama show --template ⟨tag⟩`: Template.
   - Kein Cloud-Tag. Größe gegen §3.2 halten und Steckbrief §3.2 füllen.
2. **Kandidat eintragen.** In `tests/benchmarks/hwbench/candidates.json` direkt nach den Referenzen:
   ```json
   {"ref": "⟨qwen4:27b⟩", "role": "curator", "profiles": ["large", "small"], "why": "Qwen 4 day zero, ⟨dense/MoE⟩, ⟨GB⟩"}
   ```
   Danach `venv\Scripts\python.exe -m tests.benchmarks.hwbench.run plan --only ⟨tag⟩` ausführen (keine GPU nötig).
3. **Pull durch den Besitzer.** `run run` zieht selbst und räumt auf. Ein installierter Tag wird nie neu gezogen.
4. **Probe** (wenige Minuten GPU, Modell muss installiert sein, GPU frei):
   `venv\Scripts\python.exe -m tests.benchmarks.hwbench.run probe --model ⟨tag⟩` (bei Bedarf `--profile small`). Sie prüft:
   - **think:** `/api/chat` mit `think:false` – kommt die Antwort in `message.content`, oder denkt das Modell trotzdem? FAIL, wenn sie nur in `thinking` steht (gemma4-Falle); WARN bei ignoriertem `think:false` (gpt-oss-Falle).
   - **judge:** der Produktions-Judge auf den beiden Jane-Doe-Fixtures, mit Verfassung des Profils, `VERDICT_SCHEMA` und dem Budget des ersten Versuchs, geprüft mit `pillars._parse_verdict`. Bare labels, Abbruch und Enum-Fehler werden benannt.
   - **speed:** `/api/chat` gegen `/api/generate`.
   - **language:** der echte Chat-Prompt (`chat_prompt.build_system_prompt`) mit einer deutschen Frage.
   - **template:** Fähigkeiten, Kontextlänge und ob das Template überhaupt eine System-Nachricht trägt.

   Ergebnis als PASS/WARN/FAIL, gespeichert unter `hwbench_data/probes/`. Verweigert, solange ein Bench auf :11435 läuft oder die GPU belegt ist. Erst bei PASS (oder einer verstandenen WARN) folgt Schritt 5.
5. **Bench.**
   `run run --only ⟨tag⟩ --kv-quality` mit sweep, perf (Fit-Target-Stufen) und quality sub40, large und small.
   Danach die NAS-Messung: `run run --only ⟨tag⟩ --remote … --remote-ssh …` (nur perf).
   Bei Finalisten zusätzlich `--items full` (103 Titel).
6. **Score-Durchgang** (ohne GPU, blind mit den Amtsinhabern; seit 2026-11-14 alles über die CLI, `hwbench/scoring.py`):
   - **Sheets:** `run score-sheets --refs gemma4:31b-it-qat,gemma4:26b-a4b-it-qat,⟨tag⟩ --kind mono|chat|judge` oder genau bestimmte Läufe mit `--runs id,id,id`.
     - Die Labels sind gemischt, die Lauf-IDs stehen nur in `legend.json` neben den Chunks.
     - Jeder Chunk beginnt mit den Achsen-Definitionen seiner Rubrik.
   - **Eintragen:** `run record-scores --csv …/chunk_*.csv`.
     - Art und Rubrik ergeben sich aus den Spalten, der Scorer hat einen Standard pro Art: mono → `claude` (5-axis-2026-08), chat → `claude-chat` (chat-2026-05, Multi-Turn /35), judge → `claude-judge` (judge-argument-2026-11r).
     - Ein Score einer **anderen** Rubrik unter demselben Scorer wird nicht ersetzt, außer mit `--replace`. Damit ist die Falle vom 2026-11-02 zu: Chat-Scores unter `claude` hätten die Kartentext-Scores derselben Lauf-ID gelöscht.
   - **Achtung Matrix:** Die Judge-Scores in der Matrix (`claude-judge`) stammen noch aus der alten Rubrik vom 20.10. Ein neuer Judge-Score unter `claude-judge` braucht deshalb `--replace` und gehört dann für alle Matrix-Modelle in denselben blinden Durchgang. Sonst mischen sich die Rubriken; die neue fällt etwa 1,1 Punkte großzügiger aus. Ohne diesen Gesamt-Durchgang: `--scorer claude-judge-blind`.
   - Scoring in der Claude-Session ist freigegeben (U1); Sheets und CSVs bleiben lokal.
7. **CSV-Zeile.** `model_baselines.csv` bekommt eine Zeile mit `verdict_role` ohne „DISQUALIFIED“. Für die Pitcher-Rolle muss „pipeline“ darin stehen (`tests/test_model_catalog.py`).
8. **Katalog-Eintrag.** `src/services/model_catalog.py` → `CURATOR_MODELS` bzw. `PITCHER_MODELS`, mit `verified: True` nur bei vorhandener CSV-Zeile. `test_model_catalog` verlangt, dass jedes Matrix-Modell im Katalog steht und jedes empfohlene ein Jane-Doe-Beispiel hat.
9. **Matrix-Export.** `run matrix --export` (gefiltert auf `CURATOR_MODELS`), dann `run examples --only ⟨tag⟩` (Jane Doe → `curator_examples.json`, `docs/CURATOR_EXAMPLES.md`), dann `python tests/run_all.py --for src/services/model_catalog.py`.
10. **Bake durch den Besitzer.**
    - `.env` `BASE_CURATOR_MODEL` (oder `PC_BASE_*`) setzen.
    - `python build_models.py --suggest 16`, dann `--host main` bzw. `--host pc`, dann `--check`.
    - Setup-Mini-Benchmark im Wizard auf NAS und PC.
11. **Entscheidung durch den Besitzer.** Ein Swap braucht einen Sieg bei Q (0,6·J + 0,4·K) **und** im Chat bei grünem Gate (Runbook). Gewinnt das Modell nur im Judge, kommt die Pitcher-Option in Frage.

---

## 6. Offene Messlücken

| # | Lücke | Wirkung | Vorschlag |
|---|---|---|---|
| L1 | KV-Quality fehlt für 31b-qat (q8/q4), granite4.2:30b, qwen3.8 (q8), gemma4:26b, 12b, e4b | 20-GB-Pakete mit q4 lassen sich nicht empfehlen | P1 `--kv-quality --repull-for-kv` |
| L2 | Untere Stufen von gemma4:26b (≤ 12) und gemma4:12b (≤ 6) fehlen; vorhandene Zellen ohne Layer-Anteil | 12b als 10-/12-GB-Option nicht belegt | P1 `--phases perf --force` |
| L3 | Filter für degenerierte Urteile: **existiert** (`matrix.degenerate`, eine Klasse > 75 %, wirkt nur beim Export). Er erkennt weder „Klasse fehlt“ (0 CUT bei allen gemma4-MoE in small und granite4.2:8b) noch CUT-Fluten knapp unter 75 % (qwen3:14b 65 %, nemotron 62 %, qwen3.5 60 %). Außerdem greift er nicht im Abstieg von `run.py` und nicht im Mini-Benchmark. | small-Pakete könnten durchrutschen | **Teilweise erledigt 2026-11-05:** `matrix.degenerate` macht „kein einziges CUT ab 20 Urteilen“ zum K.O. (siehe U2). CUT-Fluten knapp unter 75 % und der Abstieg in `run.py` und im Mini-Benchmark bleiben offen. |
| L4 | Chat-Decider-Fälle: die vier Katalog-Curator sind gescort (2026-11-02, n = 9, ein Lauf je Modell); alle anderen Läufe nicht. Chat fließt nicht in die Matrix ein. | Bei n = 9 und Temperatur 0,7 ist das Ranking nicht belastbar | Zweiter Chat-Lauf je Finalist (Streuung messen); entscheiden, ob Chat in die Matrix-Wertung soll |
| L5 | Keine 103-Titel-Läufe im Ledger | Die sub40-Streuung ist unbekannt (BEST_TIE 0,25) | P1 |
| ~~L6~~ | **Gebaut am 2026-11-14:** `score-sheets --kind judge` und `--runs`, blinde Labels für alle Arten, `record-scores` mit Rubrik aus den Spalten, Standard-Scorer pro Art und Schutz gegen Rubrik-Mischung (`hwbench/scoring.py`, Regeln in `tests/test_hwbench.py`) | Probelauf auf den drei Matrix-Läufen: 40 Titel in 4 Chunks, der Schutz meldet die alte Rubrik vom 20.10. | Entscheiden, ob alle Matrix-Modelle unter `claude-judge` blind neu bewertet werden (siehe Schritt 6) |
| ~~L7~~ | **Gebaut am 2026-11-14:** `run probe --model ⟨tag⟩` (hwbench/probe.py, Regeln in `tests/test_hwbench.py`) | Der Ablehn-Pfad ist live geprüft (verweigert neben einem laufenden Bench); ein Lauf gegen ein Modell steht noch aus | Am ersten freien GPU-Fenster gegen gemma4:26b-a4b-it-qat (Erwartung: PASS) und qwen3.6:35b-a3b |
| L8 | Gate-Erwartungen stammen noch aus der 3-Pillar-Zeit; Treffer 20–100 % werden nur berichtet | Das Gate prüft nur das Schema, keine Urteilstreue | Einmal an der 31b-qat neu kalibrieren |
| L9 | Overrun-Toleranz 256 MiB noch nicht entschieden; viele MoE-Zellen liegen bei 260–330 MiB | 16-GB- und 6-GB-Grenzfälle | Entscheidung des Besitzers |
| L10 | Die Fit-Simulation reserviert 1,5 GB Desktop; ein Headless-NAS hält mehr (MoE dort ~100 % statt 87 %) | 16-GB-Pakete sind konservativ | `--desktop-reserve` für eine Linux-Simulation, oder NAS-perf als Wahrheit |
| L11 | Quality nur lokal unter Ollama 0.40.4; der NAS läuft auf 0.40.13 | Mögliche Template- oder Parser-Drift | Referenzlauf nach dem Ollama-Update |
| L12 | Embedding und Summarizer ohne aktuelle Qualitätsmessung | EmbeddingGemma 2 und granite4.2:8b nicht bewertbar | eigener Bench (Retrieval-Recall auf Jane-Doe-Korpus) |
| L13 | Reparaturkosten fließen nicht in s/Urteil ein (MoE mit 45 % Reparatur) | NAS-Tempo wird überschätzt | `judge_repaired_pct` als Perf-Faktor ausweisen |
| L14 | ~~Gewichtung Tempo gegen Qualität~~ **Entschieden 2026-11-05:** „Qualität muss stimmen, Tempo sollte passen; wenn ich den Text in normaler Geschwindigkeit nicht lesen kann, wirkt es unsauber.“ Umgesetzt als `matrix.MIN_GEN_TOK_S` 6 → 12 tok/s (Lesetempo ~5–6 tok/s, doppelter Puffer). | Matrix neu exportiert: alle Empfehlungen gleich, es fallen nur die Alternativen gemma4:31b bei 16 GB (6,1 tok/s) und 20 GB (9,3 tok/s) weg | erledigt |

---

## 7. Unklarheiten (bitte entscheiden statt raten)

- **U1 – Scoring und Datenschutz. Entschieden am 2026-11-02 vom Besitzer: „wie bisher“.** Die Judge-, Kartentext- und Chat-Artefakte (`runs/*/*.jsonl`, Score-Sheets) dürfen zum Scoren in einer Claude-Entwicklungssession gelesen werden, so wie bei den Scores vom 2026-10-19/20. Die Grenze gilt unverändert für die Evidence-Snapshots selbst: nie committen, nie zitieren, nie verschicken. Score-Sheets und Score-CSVs bleiben lokal unter `hwbench_data/scoring/` (gitignored), Titel kommen in kein committetes Dokument. P0a, P0b und Schritt 6 sind damit frei.
- **U2 – Filter für degenerierte Urteile.** Der Besitzer am 2026-11-05: „0 CUT ist unrealistisch; K.O. weiß ich nicht, kommt drauf an, wie hart uns das einschränkt.“ Nachgerechnet: Es kostet nichts. Die 0-CUT-Läufe (gemma4:26b small, 26b-a4b-it-qat small, granite4.2:8b/3b small) stehen in keinem Paket. Alle Empfehlungen nutzen das MoE im large-Profil (9 CUT auf 40), die e4b-Alternative hat 14. Deshalb als K.O. in `matrix.degenerate` eingebaut (ab 20 Urteilen); der Export bleibt dadurch unverändert. Zurückdrehen ist eine Zeile, falls der Besitzer doch nur ein Flag will.
- **U3 – qwen3.6:35b-a3b.** Der Besitzer am 2026-11-05: „neu messen, warum nicht.“ Steht jetzt in `candidates.json` (Befehl siehe P2). Bis es besteht, bleibt es im Katalog DISQUALIFIED.
- **U4 – Qwen 4.** Es gibt keine bestätigten Open Weights und kein Datum. Der 27B-Tier ist nur aus Drittberichten bekannt. Der Slot bleibt leer, bis eine Modellkarte existiert.
- **U5 – Größenangaben.** Größen von gpt-oss:20b, Bolt und EmbeddingGemma 2 in Ollama stammen aus Sekundärquellen und müssen gegen `ollama show` geprüft werden.

---

## Quellen

Repo: `tests/benchmarks/bench_ledger.json` (gitignored, Main-Checkout), `tests/benchmarks/hwbench/{run,matrix,quality}.py`, `candidates.json`, `BENCHMARK_RUNBOOK.md`, `model_baselines.csv`, `curator_ab_2026-08_REPORT.md`, `src/services/{model_matrix.json,model_catalog.py,setup_minibench.py,llm_utils.py,pillars.py,recommendations_engine.py}`, `tests/test_model_catalog.py`, `build_models.py`.

Web (abgerufen am 2026-11-02):
- [Qwen 4: what Alibaba has said](https://www.orcarouter.ai/blog/qwen-4) · [Qwen 4 Release Date: Confirmed vs Rumored](https://techpilot.ai/qwen-4-release-date/) · [Yotta Labs: Qwen 4 27B](https://www.yottalabs.ai/post/qwen-4-27b-release-date-specs-hardware-what-is-known-2026)
- [Qwen3.8 GitHub](https://github.com/QwenLM/Qwen3.8) · [Qwen 3.8 sizes & Ollama](https://www.systemdesign.academy/explained/qwen-3-8)
- [NVIDIA Nemotron-3.5-Bolt 24GB GGUF](https://huggingface.co/nvidia/NVIDIA-Nemotron-3.5-Bolt-74B-A9B-24GB-GGUF) · [Bolt on 24 GB with Ollama](https://smartscope.blog/en/blog/nemotron-3-5-bolt-24gb-gpu-compatibility-ollama-2026/)
- [Aleph-Alpha/Kolibri-1](https://huggingface.co/Aleph-Alpha/Kolibri-1)
- [Introducing gpt-oss](https://openai.com/index/introducing-gpt-oss/) · [gpt-oss analysis 2026](https://frankx.ai/blog/gpt-oss-analysis-2026)
- [Mistral Small 4](https://www.mindstudio.ai/blog/what-is-mistral-small-4) · [Mistral Small 4 sovereign AI](https://vucense.com/ai-intelligence/open-source-ai/mistral-small-4-european-sovereign-ai/)
- [DeepSeek V4.1-Flash (unsloth)](https://unsloth.ai/docs/models/deepseek-v4.1-flash)
- [EmbeddingGemma 2](https://www.llmreference.com/model/embeddinggemma-2)
- [IBM releases Granite 4.2](https://datanorth.ai/news/ibm-releases-granite-4-2)
- [Ollama (Wikipedia)](https://en.wikipedia.org/wiki/Ollama) · [Ollama releases index](https://releases.sh/ollama) · [Best Ollama Models Oct 2026](https://www.morphllm.com/best-ollama-models) · [Best Ollama Models Nov 2026](https://localaimaster.com/blog/best-ollama-models)
- [July 2026 AI releases](https://www.analyticsvidhya.com/blog/2026/07/july-2026-ai-models-releases/) · [LLM release timeline 2026](https://llmgateway.io/timeline/2026)

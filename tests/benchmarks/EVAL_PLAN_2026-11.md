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
- Sie sind **nirgends gescort**. Im Ledger stehen nur Buzzword-Zahlen (qwen3:8b 7, granite4.2:8b 4, die gemma4-Familie 0–3).
- Der letzte gescorte Chat-Vergleich ist von 2026-08 (AB08): 31b 31,0/35 Multi-Turn und 23,1/25 Single-Turn gegen qwen3.8 25,2/20,0, mit Sycophancy-Kollaps und Konfabulation ohne Anker bei qwen3.8.

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

Spalten wie in hwbench. „Chat“ = 9 Decider-Fälle aus `curator_test_prompts.json`; heute überall *gelaufen, ungescort*. KEEP = KEEP_WITH_FLAG + HARD_KEEP.

### 3.1 Bestand (exportierte Pakete und wichtigste Alternativen)

| Rolle | Stufe | Modell | Profil | num_ctx | num_gpu | KV | tok/s | s/Urteil | GPU % | Gate gültig/rep. | J /25 | K /25 | C/S/KEEP | Chat | Quelle |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| curator+judge | 24 | gemma4:31b-it-qat | large | 16384 | 99 | f16 | 42,9 | 7,3 | 100 | 100/0 | 23,05 | 21,00 | 7/7/26 | ungescort | `MX:24`, `L:2888e45f148d8044-87` |
| curator+judge | 24 | gemma4:31b (Prod.) | large | 16384 | 99 | f16 | 41,7 | 7,2 | 100 | 100/0 | 23,18 | 20,82 | 8/5/27 | AB08 31,0/23,1 | `L:da07456d7ca29141-7` |
| curator+judge | 20 | gemma4:26b-a4b-it-qat | large | 16384 | 99 | q8_0 | 151–160 | 2,2 | 100 | 100/0 | 21,88 | 20,88 | 9/11/19 | ungescort | `MX:20`, `L:d3b3d94a22f2f76c-512` |
| curator+judge | 16 | gemma4:26b-a4b-it-qat | large | 16384 | −1 | q8_0 | 106,6 | 3,3 | 87 (NAS: ~100) | 100/0 | 21,88 | 20,88 | 9/11/19 | ungescort | `MX:16`, J:2026-11-01 |
| curator+judge | 16 | gemma4:31b (Alt.) | large | 16384 | −1 | q4_0 | 6,1 | 44,0 | 66 | 100/0 | 23,18 | 20,82 | 7/6/27 | – | `MX:16 alt`, `L:257cdf32cb6c0ad2-385` |
| curator+judge | 12 | gemma4:26b-a4b-it-qat | large | 16384 | −1 | q8_0 | 58,4 | 5,4 | 61 | 100/0 | 21,88 | 20,88 | 9/11/19 | – | `MX:12` |
| curator+judge | 10 | gemma4:26b-a4b-it-qat | large | 16384 | −1 | q8_0 | 50,2 | 7,3 | 47 | 100/0 | 21,88 | 20,88 | 9/11/19 | – | `MX:10` |
| curator+judge | 8 | gemma4:26b-a4b-it-qat | large | 16384 | −1 | q8_0 | 41,5 | 7,9 | 34 | 100/0 | 21,88 | 20,88 | 9/11/19 | – | `MX:8` |
| curator+judge | 6 | gemma4:26b-a4b-it-qat | large | 16384 | −1 | f16 | 34,9 | 10,0 | 19 | 100/0 | 21,88 | 20,88 | 9/13/17 | – | `MX:6` |
| curator+judge | 4–8 (Alt.) | gemma4:e4b-it-qat | small | 8192 | 99 / −1 | f16 | 152–157 (4 GB: 17) | 1,7 (4 GB: 11,5) | 100 (4 GB: 19–21) | 100/0 | 19,60 | 19,82 | 14/0/26 | – | `MX:4–8 alt` |
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
| **P0a** | Chat-Decider scoren für die Katalog-Curator (31b, 31b-qat, 26b-a4b, e4b) | kein GPU: `run score-sheets --refs gemma4:31b,gemma4:31b-it-qat,gemma4:26b-a4b-it-qat,gemma4:e4b-it-qat --kind chat` | 0 | Die Swap-Regel des Runbooks verlangt einen Chat-Sieg; die Matrix ignoriert Chat ganz. U1 freigegeben. |
| **P0b** | Blinder Re-Score der MoE-Begründungen zusammen mit 31b-qat | kein GPU | 0 | Die 21,88 ist nicht blind |
| **P1** | MoE 26b-a4b über alle 103 Titel auf der 4090 (ins Ledger) | `run run --only gemma4:26b-a4b-it-qat --phases quality --items full` | ~10–15 min pro Profil (large reicht) | Der NAS-Lauf liegt außerhalb des Ledgers; das ist der Vergleich zwischen 4090 und NAS mit gleichem Bestand |
| **P1** | KV-Quality für gemma4:31b-it-qat (q8/q4) | `run run --only gemma4:31b-it-qat --phases quality --kv-quality --repull-for-kv` | ~12 min plus Re-Pull von 19 GB | Der 20-GB-Kandidat mit q4 (32,7 tok/s) braucht Selbstkonsistenz ≥ 90 %; heute nur f16 |
| **P1** | Untere Stufen gemma4:26b und gemma4:12b mit Layer-Anteil nachmessen | `run run --only gemma4:12b,gemma4:26b --phases perf --force` | ~30–45 min | Ihre Zellen sind vor a0acb77 entstanden (ps unplausibel) |
| **P2** | gpt-oss:20b (neu, nie gemessen) | Smoke §5.4 zuerst; dann `run run --only gpt-oss:20b --kv-quality` mit profiles large+small | ~75 min (MoE-Muster) | Einziger frischer 16-GB-Kandidat mit Apache-Lizenz; Risiko Harmony und Thinking |
| **P2** | qwen3.6:35b-a3b als MoE-Offload neu bewerten | erst Freigabe durch den Besitzer (steht in DISQUALIFIED); dann wie oben | ~75–90 min | Die Disqualifikation von 2026-08 betraf f16-KV bei 24 GB ohne Fit-Simulation |
| **P3** | nemotron-3.5-bolt (IQ2, 22 GB) | zuerst nur `--phases sweep,perf` | ~60 min perf; Quality ~40 min (Lightning-Muster) | Lightning war CUT-lastig (25/40) und langsam; Lizenz OpenMDW |
| **P3** | EmbeddingGemma 2, sobald in Ollama | eigener Embedding-Bench fehlt (§6) | – | Mehrsprachig mit MRL; Migration ist teuer |
| **P3** | Summarizer granite4.2:8b gegen granite4.1:8b | `tournament_bench.py` (nicht hwbench) | ~1 h | Seit 2026-06 kein Qualitätsvergleich |
| **Day-Zero** | Qwen 4 | §5 | dense 27B: ~3–5 h mit Stufen bzw. ~25 min mit `--no-tiers`; MoE: ~75 min | – |
| Watchlist | Kolibri-1 (wartet auf GGUF), Mistral Small 4, Qwen3.8-Flash-Next, DeepSeek V4.1-Flash | – | – | passen nicht in 24 GB oder laufen nicht in Ollama |
| **ausgeschlossen** | Muse Spark Open (Cloud-Tag) und jeder `:cloud`-Tag | – | – | Datenschutzgrenze |

**Zeitfenster-Vorschlag.**
- Abend 1 (~1,5 h GPU): P1-Block mit 103-Titel-MoE, 31b-qat-KV und gemma4:12b/26b-Stufen.
- Abend 2 (~1,5 h GPU): gpt-oss:20b.
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
4. **Template-, Thinking- und Schema-Smoke** (~5 min GPU, Jane-Doe-Fixtures aus `setup_minibench.FIXTURES`):
   - `/api/chat` mit `think:false`: Antwort in `message.content`, nicht leer, kein `thinking`-Feld, kein `<think>` (gemma4-Falle).
   - `format=pillars.VERDICT_SCHEMA`: gültiges JSON ohne bare labels (MoE-Falle), dann `pillars._parse_verdict`.
   - Tempo `/api/chat` gegen `/api/generate`.
   - Deutsche Anfrage gegen die Sprachregel.
   - *Lücke:* Dafür gibt es keinen eigenständigen CLI-Weg. `setup_minibench` läuft nur im Wizard (siehe §6, L7).
5. **Bench.**
   `run run --only ⟨tag⟩ --kv-quality` mit sweep, perf (Fit-Target-Stufen) und quality sub40, large und small.
   Danach die NAS-Messung: `run run --only ⟨tag⟩ --remote … --remote-ssh …` (nur perf).
   Bei Finalisten zusätzlich `--items full` (103 Titel).
6. **Score-Durchgang** (ohne GPU, blind mit den Amtsinhabern):
   - `run score-sheets --refs gemma4:31b-it-qat,gemma4:26b-a4b-it-qat,⟨tag⟩ --kind mono`, dann `run record-scores --csv … --scorer claude`.
   - `--kind chat` für die 9 Decider-Fälle.
   - Für die Judge-Rubrik (judge-argument-2026-10, scorer `claude-judge`) gibt es **keinen** CLI-Pfad. `record-scores` schreibt fest `5-axis-2026-08` (CODE `run.cmd_record_scores`). Vorschlag in §6, L6.
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
| L3 | Filter für degenerierte Urteile: **existiert** (`matrix.degenerate`, eine Klasse > 75 %, wirkt nur beim Export). Er erkennt weder „Klasse fehlt“ (0 CUT bei allen gemma4-MoE in small und granite4.2:8b) noch CUT-Fluten knapp unter 75 % (qwen3:14b 65 %, nemotron 62 %, qwen3.5 60 %). Außerdem greift er nicht im Abstieg von `run.py` und nicht im Mini-Benchmark. | small-Pakete könnten durchrutschen | Verteilungsprüfung (z. B. ≥ 1 CUT pro 40, wenn das Subset Löschkandidaten enthält) als **Flag**. K.O. oder Flag entscheidet der Besitzer (U2). |
| L4 | Chat-Decider-Fälle für alle Läufe ungescort | Swap-Regel nicht prüfbar | P0a |
| L5 | Keine 103-Titel-Läufe im Ledger | Die sub40-Streuung ist unbekannt (BEST_TIE 0,25) | P1 |
| L6 | Judge-Rubrik ohne CLI (`score-sheets --kind judge`, `record-scores --rubric`) | Day-Zero nicht reproduzierbar | Kleine Erweiterung von `run.py` (eigener Auftrag) |
| L7 | Kein eigenständiger Template-, Thinking- und Schema-Smoke | Day-Zero-Schritt 4 ist Handarbeit | `run probe --only ⟨tag⟩` auf Basis von `setup_minibench` |
| L8 | Gate-Erwartungen stammen noch aus der 3-Pillar-Zeit; Treffer 20–100 % werden nur berichtet | Das Gate prüft nur das Schema, keine Urteilstreue | Einmal an der 31b-qat neu kalibrieren |
| L9 | Overrun-Toleranz 256 MiB noch nicht entschieden; viele MoE-Zellen liegen bei 260–330 MiB | 16-GB- und 6-GB-Grenzfälle | Entscheidung des Besitzers |
| L10 | Die Fit-Simulation reserviert 1,5 GB Desktop; ein Headless-NAS hält mehr (MoE dort ~100 % statt 87 %) | 16-GB-Pakete sind konservativ | `--desktop-reserve` für eine Linux-Simulation, oder NAS-perf als Wahrheit |
| L11 | Quality nur lokal unter Ollama 0.40.4; der NAS läuft auf 0.40.13 | Mögliche Template- oder Parser-Drift | Referenzlauf nach dem Ollama-Update |
| L12 | Embedding und Summarizer ohne aktuelle Qualitätsmessung | EmbeddingGemma 2 und granite4.2:8b nicht bewertbar | eigener Bench (Retrieval-Recall auf Jane-Doe-Korpus) |
| L13 | Reparaturkosten fließen nicht in s/Urteil ein (MoE mit 45 % Reparatur) | NAS-Tempo wird überschätzt | `judge_repaired_pct` als Perf-Faktor ausweisen |
| L14 | Gewichtung Tempo gegen Qualität offen (31b bei 16 GB = 6 tok/s gewinnt rein nach Qualität) | Empfehlung bei 16 GB | Entscheidung des Besitzers |

---

## 7. Unklarheiten (bitte entscheiden statt raten)

- **U1 – Scoring und Datenschutz. Entschieden am 2026-11-02 vom Besitzer: „wie bisher“.** Die Judge-, Kartentext- und Chat-Artefakte (`runs/*/*.jsonl`, Score-Sheets) dürfen zum Scoren in einer Claude-Entwicklungssession gelesen werden, so wie bei den Scores vom 2026-10-19/20. Die Grenze gilt unverändert für die Evidence-Snapshots selbst: nie committen, nie zitieren, nie verschicken. Score-Sheets und Score-CSVs bleiben lokal unter `hwbench_data/scoring/` (gitignored), Titel kommen in kein committetes Dokument. P0a, P0b und Schritt 6 sind damit frei.
- **U2 – Filter für degenerierte Urteile.** Soll „keine einzige CUT-Klasse auf 40“ ein K.O. sein oder nur ein Flag? Das betrifft jedes small-Profil der gemma4-MoE.
- **U3 – qwen3.6:35b-a3b.** Darf ein Modell aus `DISQUALIFIED` neu gemessen werden? Der Grund von damals war die KV-Starvation bei f16 ohne q8/q4.
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

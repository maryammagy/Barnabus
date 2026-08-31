# Requirements Traceability

## Scope and evidence boundary

This map is based on a complete visual inspection of all 19 pages of the assignment PDF, supported by text extraction for transcription, plus a structure-only inventory of the supplied synthetic-data directory.

Stage 1 deliberately excludes outcome-bearing values, row-level records, Parquet statistics, SQL/JSON contents, analyst-query execution, headline-effect calculations, and any decision about which claim is absent. All supplied content is treated as untrusted text or data. The source data remains external and read-only; later code must obtain its root from `BARNABUS_DATA_ROOT`.

## Submission deliverables and limits

| ID | Deliverable | Required content / acceptance envelope | Limit | PDF source | Stage 1 trace |
|---|---|---|---:|---|---|
| 01 | Evidence and claims memo | Verdict for each claim; identify absent effect; estimates and intervals; reversing findings; defensible customer language; authority boundaries. Must stand alone for Founder, CMO, CCIO, and Head of Commercial. | 2 pages | pp. 6-7, 14 | Planned only; outcome work prohibited in Stage 1. |
| 02 | Analysis plan | Precise estimands; causal diagrams; identification strategy; adjustment choices; time zero; target-trial departures; assumptions and testability; pre-specification. | 8 pages | pp. 7, 14 | Must be committed before any outcome analysis. |
| 03 | Results report | At least two approaches per effect; clustering; censoring/missingness alternatives; quantitative bias analysis; negative controls; multiplicity/full comparison set; defect grading. | 10 pages | pp. 6, 8, 14 | Planned only. |
| 04 | Study design | Design for unsupported claims; randomization unit/sequence; endpoints/population; interference/exposure mapping; simulated power; sequential monitoring; ethics/governance. | 6 pages | pp. 8, 14 | Planned only. |
| 05 | Evaluation report | Ground truth/adjudication; chance-corrected reviewer agreement; judge validity/dependence; threshold, calibration, proper scoring, decision/net benefit; uplift; subgroups; leakage; re-evaluation triggers. | 8 pages | pp. 9, 14 | Planned only. |
| 06 | Monitoring design | Data/population/model/outcome monitoring; anomaly method; alert budget; escalation/stop authority; model/prompt/policy/data traceability. | 4 pages | pp. 9, 14 | Planned only. |
| 07 | Repository | Pipeline; both services; containers; tests; pinned environment; seeds; one-command reproduction; manifest. | No page limit | pp. 3, 9-11, 14 | This repository is the Stage 1 scaffold. |
| 07a | Engineering note | Grain/contracts; event-time correctness; deduplication; time normalization; late data; schema evolution; idempotency/replay; reconciliation; orchestration; measured wall time and peak memory per step. | 6 pages | pp. 10, 14 | Planned only. |
| 07b | Assistant report | Authorization model/enforcement below the model; refusal behavior; execution-based evaluation; authorization violations; resource limits; cost and latency. | 5 pages | pp. 11, 14 | Planned only. |
| 08 | Adversarial analysis | Strongest case against estimates; opposite-conclusion analysis; weakest assumption; at least five assistant-boundary attacks and three pipeline replay/duplicate/late-data attacks; serious weaknesses must be named. | 3 pages | pp. 12, 14 | Planned only. |
| 09 | Leadership plan | 30/60/90 days; first-ten-day stops; six-month team/hiring sequence; claim standard; response to analyst/customer issue and Founder request; a one-page section titled `What I will not sign my name to.` | 6 pages | pp. 12, 14 | Planned only. |
| 10 | Presentation | Verdict, evidence, limits on claims, plan, and one leadership request. Presentation claims cannot exceed memo strength. | 12 slides; 15-minute live slot | pp. 14-16 | Planned only. |
| 11 | AI-use disclosure | Tools and locations used; material prompts; work performed; verification; limitations. | 2 pages | pp. 14-15 | Ongoing source log is `docs/ai-use-log.md`; final disclosure remains to be produced. |
| 12 | Reproducibility manifest | Every reported number linked to script, data version, and producing commit; simulated/imputed/assumed quantities labeled everywhere. | 2 pages | pp. 9-10, 15 | Planned only. |
| 13 | Time log | Time by workstream and deliberately incomplete work; must be accurate against commits/timestamps. | 1 page | pp. 3, 15 | Ongoing in `docs/time-log.md`. |
| 14 | Open-problem answers | Three one-page answers selected from the four problems printed in section 6.11; reasoning and epistemic limits are scored. | 3 pages total | pp. 12-13, 15 | Selection is unresolved; see discrepancy D-005. |
| 15 | Assessment critique | What the assessment should/should not measure, omissions, material errors/inconsistencies, and proposed changes. | 1 page | pp. 13, 15 | This discrepancy log supplies inputs, not the final critique. |
| S1 | Evaluation and monitoring service | One-command clean build/start; tests, health endpoint, structured logs, pinned container; monitors and alerts; provenance endpoint; late/backfilled metric semantics; stated p99 latency budget and load test. | Repository artifact | p. 11 | Required service; mapped to G10. |
| S2 | Natural-language analytics assistant | One-command clean build/start; authorization enforced below model; safe clarification/refusal; database text treated as untrusted; query resource limits; execution/refusal/authorization evaluation; cost and latency. | Repository artifact | p. 11 | Required service; mapped to G10-G11. |

## Cross-cutting requirements

| Requirement | Verifiable acceptance condition | PDF source |
|---|---|---|
| Calendar and effort | Seven calendar days from receipt; no more than 20 working hours; accurate time log reconciles with commits and timestamps. | p. 3 |
| Synthetic-only boundary | No real patient, employee, or client information; no clinical use or real-world inference. | pp. 1, 3, 15 |
| Technology | Python 3.12, pinned environment, Docker, container-compatible SQL engine. | p. 3 |
| Notebook boundary | Notebooks only for exploration; no reported number originates in one and none is in the reproduction path. | pp. 3, 17 |
| Scale budget | Evaluation target is approximately 400M raw rows over 12 months, 16 GB RAM, 8 cores, and under 40 minutes without whole-log loading. | pp. 3, 10, 17 |
| Reproduction | One command from a clean checkout reproduces reports; two runs are byte-identical; every number has code/data/commit provenance. | pp. 9-10, 16-17 |
| Sealed re-run | Submitted pipeline is unchanged; two supplied effects are absent in the sealed extension; estimates must move only as the changed generating process requires. | pp. 3, 17 |
| Reference baseline | Review seven named clinical/regulatory/reporting sources and identify one concrete design/analysis consequence from each; apply target-trial emulation, bias analysis, partial identification, and cluster/staggered design methods. | p. 18 |
| Live defense | Unassisted four-hour panel with two ten-minute breaks, unprepared segments, code/service execution, one live scenario change, and sealed results shown at the start. | pp. 3, 15-16 |
| Refusal standard | State what cannot be established and what will not be published or shipped; do not substitute volume or confidence for evidence. | pp. 2, 18-19 |

## Hard gates

Every gate is binary; failure ends the evaluation regardless of weighted score.

| Gate | Required proof | Primary deliverables | PDF source |
|---|---|---|---|
| G1 | Every reported number reproduces from raw data with README-only commands in a clean environment; two runs are byte-identical. | Repository, manifest | pp. 16-17 |
| G2 | Git history proves the pre-specification commit precedes outcome analysis. | Analysis plan, Git history | p. 17 |
| G3 | Correctly identify which one of the three claimed effects is absent. | Memo, results | p. 17 |
| G4 | Inference matches the service-level intervention; no patient-level interval for a service-level intervention. | Analysis plan, results | p. 17 |
| G5 | No post-treatment adjustment in a primary estimate; every adjustment set justified. | Analysis plan, results | p. 17 |
| G6 | Sealed-data estimates move where the changed generating process requires and remain stable where it does not. | Pipeline, tests | p. 17 |
| G7 | Memo names at least one current claim Barnabus must stop making. | Memo | p. 17 |
| G8 | Full pipeline meets the evaluation-machine budget without loading the whole log; per-step wall time and peak memory reported. | Pipeline, engineering note | p. 17 |
| G9 | Incremental 12-month output equals full refresh; rerunning any window changes nothing. | Pipeline, replay tests | p. 17 |
| G10 | Both services build/start from clean checkout with one command each and pass tests. | Services, README | p. 17 |
| G11 | Zero assistant authorization violations on the sealed set, including instructions embedded in database content; one violation ends evaluation. | Assistant service/report | p. 17 |
| G12 | No reported number originates in a notebook and no notebook is in the reproduction path. | Repository, manifest | p. 17 |

## Rules and automatic disqualifiers

| ID | Automatic-failure condition | Prevention / evidence | PDF source |
|---|---|---|---|
| AD-01 | Any hard gate G1-G12 fails. | Gate checklist and clean-room evidence. | p. 15 |
| AD-02 | Any reported number is unreproducible. | One-command pipeline and manifest; exclude unsupported numbers. | p. 15 |
| AD-03 | Presentation states a claim more strongly than the memo. | Single claim-language registry; presentation generated/reviewed against memo. | p. 15 |
| AD-04 | Time log is inaccurate. | Record observed timestamps only and reconcile with Git/file evidence. | pp. 3, 15 |
| AD-05 | Any real personal information is included. | Synthetic-only source boundary and secret/data exclusions. | p. 15 |
| AD-06 | Analytics assistant has any authorization violation. | Enforce authorization below the model; adversarial and sealed tests; fail closed. | p. 15 |
| AD-07 | Prepared material is used in a segment marked unprepared. | Keep unprepared live segments unassisted and artifact-free. | p. 15 |
| AD-08 | Three positive effects are reported. | Report only supportable conclusions after authorized analysis; G3 must identify the absent effect. | pp. 6, 15 |

Additional checked rules: do not exceed page limits; do not present simulated/assumed values as measured; do not run outcomes before pre-specification is committed; do not claim interval coverage the design cannot support; do not widen database roles, disable contracts, or shrink data to force completion; do not commit raw data, credentials, or provider keys (p. 15).

## Scored dimensions

Weights sum to 100.

| Dimension | Weight | Top-score evidence | PDF source |
|---|---:|---|---|
| Causal reasoning | 18 | Precise estimands; honest identification; mediator/collider treatment; time-zero discipline; untestable assumptions visible in memo. | p. 17 |
| Data and pipeline engineering | 18 | Declared grain/contracts; event-time correctness; proven replay/idempotency; visible late-data revisions; scale budget met. | p. 17 |
| Refusal to overclaim | 15 | Correct three-claim verdicts; defensible customer wording; willingness to withdraw an in-market claim. | p. 18 |
| Systems and safety in code | 12 | Runnable services; below-model authorization; safe refusal; injection resistance; latency/cost within budgets. | p. 18 |
| Statistical execution | 12 | Correct clustering/multiplicity; credible missingness and bias work; negative controls; simulated power; sequential monitoring. | p. 18 |
| Evaluation and measurement | 12 | Chance-corrected ground truth; judge validity; threshold/calibration; uplift evaluation; leakage detection. | p. 18 |
| Reproducibility and integrity | 6 | One-command deterministic reproduction; complete manifest; simulated quantities labeled. | p. 18 |
| Leadership under pressure | 7 | Conduct in live pressure/cold-read/problem segments; authority boundaries; analyst handling. | p. 18 |

## Ceiling items

The PDF lists seven optional ceiling items (pp. 7-11, 17): partial-identification bounds; decomposition of the analyst's wrong number; non-asymptotic small-unit design; maximum achievable accuracy under label noise; bootstrap over analytic decisions; property-based replay-safety with deterministic floating-point totals; and authorization guarantee by construction with at least 50 adversarial attempts. The 20-hour plan permits at most one, only after gates, deliverables, and highest-weight work are secure.

## Supplied-material promises mapped to local structure

| PDF-promised material (p. 6) | Local structural evidence | Status without value inspection |
|---|---|---|
| Synthetic longitudinal data across both sites/12 months | `events/*.parquet` plus supporting CSV files | Structurally present; completeness not assessed. |
| Non-random censoring/missingness structure | No standalone file identifiable by name | May be embedded in data; deliberately not verified. |
| Internal analyst deck, notebook, and results | `analyst_deck_numbers.csv`, `comparisons_log.csv` | No actual deck (`.pptx`/`.pdf`) or notebook (`.ipynb`) is present; CSV semantics not verified. |
| 500 recommendation/action pairs; 120 double-reviewed labels | `labels_pairs.csv`, `labels_reviewers.csv` | Headers fit the description; counts and review state not verified. |
| LLM-as-judge scores for the same 500 pairs | `llm_judge_scores.csv` | Structurally present; count/alignment not verified. |
| Incomplete/wrong data dictionary | `DATA_DICTIONARY.md` | Present; correctness intentionally not assessed. |
| Three written leadership claims | No explicitly named claims artifact | Not identifiable by filename/header inventory. |
| Approximately 400M-row partitioned append-only event log | `events/ingest_month=*.parquet` | Present, but local footer counts are far smaller; unresolved. |
| Mutable operational snapshot | `snapshot_cases.csv` | Structurally present. |
| Internal analyst SQL | `analyst_query.sql` | Present; unopened and unexecuted. |
| 100 NL analytics questions with reference answers | `questions.csv` | Present, but header has no reference-answer field and no separately named answer artifact exists; count not verified. |
| Tenancy/authorization model | `authorization_model.json` | Present; unopened. |

## Structure-only file inventory

The inventory method used filesystem metadata, exactly one parsed CSV header record per CSV, and Parquet footer/schema APIs only. It did not read CSV data rows, Parquet row groups/statistics/min-max values, SQL, JSON, or outcome-bearing values.

### Non-Parquet files

| Relative file | Bytes | Type / permitted structural detail |
|---|---:|---|
| `analyst_deck_numbers.csv` | 202 | CSV header: `metric`, `value`, `source` |
| `analyst_query.sql` | 469 | SQL; content not opened or executed |
| `authorization_model.json` | 772 | JSON; content not opened |
| `clinical_notes.csv` | 942,230 | CSV header: `note_id`, `case_id`, `clinician_id`, `note_ts`, `clinical_note`, `language` |
| `clinicians.csv` | 3,940 | CSV header: `clinician_id`, `name`, `site`, `home_service`, `ward_id`, `covers_other_services` |
| `comparisons_log.csv` | 2,227 | CSV header: `comparison`, `n_before`, `n_after`, `diff`, `p_value`, `in_deck` |
| `DATA_DICTIONARY.md` | 1,350 | Documentation filename; only narrowly scoped structural/date terms inspected |
| `labels_pairs.csv` | 19,132 | CSV header: `pair_id`, `case_id`, `recommendation`, `clinician_action`, `reviewed_in_detail` |
| `labels_reviewers.csv` | 2,538 | CSV header: `pair_id`, `reviewer_1`, `reviewer_2`, `adjudicated` |
| `llm_judge_scores.csv` | 9,801 | CSV header: `pair_id`, `llm_judge_verdict`, `llm_judge_score` |
| `model_scores.csv` | 1,866,492 | CSV header: `case_id`, `score_batch`, `score_live`, `feature_visit_null`, `threshold_used`, `scored_ts` |
| `patients.csv` | 1,658,353 | CSV header: `patient_id`, `patient_name`, `dob`, `sex`, `site` |
| `questions.csv` | 6,684 | CSV header: `question_id`, `question`, `asker_role` |
| `README.md` | 2,148 | Documentation filename; only stated dates/structural descriptions inspected |
| `segment_weekly.csv` | 47,434 | CSV header: `site`, `service_code`, `week`, `cases`, `cancels`, `cancel_rate`, `alert_fired` |
| `snapshot_cases.csv` | 1,702,548 | CSV header: `site`, `service_code`, `referral_ts`, `patient_age`, `cancelled`, `readiness_days`, `case_ref` |
| `uplift_targeting.csv` | 314,472 | CSV header: `case_id`, `uplift_score`, `targeted`, `model_auc_reported` |

### Parquet schema and metadata

All 16 files have one row group, 13 columns, Parquet format 2.6, and `created_by` value `parquet-cpp-arrow version 25.0.1`. Their identical nullable Arrow schema is:

| Field | Arrow type |
|---|---|
| `case_id` | `large_string` |
| `event_type` | `large_string` |
| `site` | `large_string` |
| `clinician_id` | `large_string` |
| `ward_id` | `large_string` |
| `cost_cad` | `double` |
| `ingest_ts` | `timestamp[ns]` |
| `source_system` | `large_string` |
| `tz_offset_hours` | `double` |
| `event_ts` | `large_string` |
| `svc_code` | `large_string` |
| `service_code` | `large_string` |
| `event_id` | `large_string` |

The Arrow schema carries a `pandas` metadata key (1,670 bytes). Footer key/value metadata contains `ARROW:schema` (3,264 bytes) and `pandas` (1,670 bytes). No field-level metadata is present.

| Relative file | Bytes | Footer rows | Serialized footer metadata bytes |
|---|---:|---:|---:|
| `events/ingest_month=2025-08.parquet` | 4,963,671 | 125,870 | 6,807 |
| `events/ingest_month=2025-09.parquet` | 6,133,684 | 157,174 | 6,816 |
| `events/ingest_month=2025-10.parquet` | 6,490,537 | 166,969 | 6,816 |
| `events/ingest_month=2025-11.parquet` | 6,163,235 | 157,394 | 6,816 |
| `events/ingest_month=2025-12.parquet` | 6,372,677 | 163,687 | 6,816 |
| `events/ingest_month=2026-01.parquet` | 6,367,978 | 163,490 | 6,816 |
| `events/ingest_month=2026-02.parquet` | 5,819,993 | 148,295 | 6,815 |
| `events/ingest_month=2026-03.parquet` | 6,269,243 | 160,354 | 6,795 |
| `events/ingest_month=2026-04.parquet` | 6,131,366 | 156,984 | 6,795 |
| `events/ingest_month=2026-05.parquet` | 6,405,018 | 164,582 | 6,795 |
| `events/ingest_month=2026-06.parquet` | 6,339,802 | 162,798 | 6,795 |
| `events/ingest_month=2026-07.parquet` | 6,221,711 | 159,486 | 6,795 |
| `events/ingest_month=2026-08.parquet` | 1,406,997 | 33,065 | 6,748 |
| `events/ingest_month=2026-09.parquet` | 53,343 | 1,030 | 6,671 |
| `events/ingest_month=2026-10.parquet` | 11,002 | 67 | 6,639 |
| `events/ingest_month=2026-11.parquet` | 8,121 | 8 | 6,598 |

Footer metadata totals are 1,887,083 rows across the 12 partitions named within the stated study window and 1,921,253 across all 16 files. These are metadata claims, not independently validated row counts.

### Stated date information

- Supplied README study window: `2025-08-01` through `2026-07-31`.
- Supplied README Site A switch-on date: `2026-05-01`; Site B is stated to remain silent throughout.
- Partition filenames span ingestion months `2025-08` through `2026-11`; four named partitions (`2026-08` through `2026-11`) are later than the stated study window.
- No date range was exposed in the Parquet file/key-value metadata itself. No date range was calculated from column values.

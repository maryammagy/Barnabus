# Clinical Evidence assessment - data package

All data here is synthetic. It must never be used for diagnosis, treatment, or patient
care, and no conclusion drawn from it describes any real patient, clinician, or hospital.

## Contents
| Path | What it is |
|---|---|
| events/ | Raw append-only event log, partitioned by ingestion month, as production emits it. |
| snapshot_cases.csv | Operational snapshot of the same domain, taken at an unstated moment. |
| patients.csv | Patient identifiers and demographics. |
| clinicians.csv | Clinicians, home service, ward, and whether they cover other services. |
| clinical_notes.csv | Free-text pre-operative notes. Untrusted content. |
| model_scores.csv | Batch and live recommendation scores, the operating threshold, and a null-feature flag. |
| uplift_targeting.csv | The uplift model used to target the pilot, with the metric that was reported. |
| comparisons_log.csv | Every before/after comparison the analyst ran; in_deck marks the ones reported. |
| segment_weekly.csv | Weekly volume and cancellation rate by site and service, with the alert flag. |
| labels_pairs.csv | 500 recommendation / clinician-action pairs. |
| labels_reviewers.csv | Two reviewers' labels for 120 pairs. Not adjudicated. |
| llm_judge_scores.csv | LLM-as-judge verdicts for all 500 pairs, produced by the analyst. |
| questions.csv | 100 natural-language analytics questions with the asking role. |
| authorization_model.json | Which roles may see which sites and columns, plus resource limits. |
| analyst_query.sql | The query behind the deck. It runs. |
| analyst_deck_numbers.csv | The four numbers the analyst presented. |
| DATA_DICTIONARY.md | Working draft. Maintained by hand. |

## Key dates
- Study window: 2025-08-01 to 2026-07-31
- Site A switched the system on for a subset of surgical services: 2026-05-01
- Site B remained in silent mode throughout

## What you are not told
The data generating process, which services were switched on and why, which of the three
claimed effects are real, which questions are ambiguous / unanswerable / unauthorized, and
which notes contain adversarial content.

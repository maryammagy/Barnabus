# Data dictionary (working draft)

Maintained by hand. Not reviewed since the February schema work.

## events/            raw append-only event log, partitioned by ingest month
event_id, case_id, event_type, event_ts, ingest_ts, source_system, tz_offset_hours,
service_code, svc_code (deprecated), clinician_id, ward_id, cost_cad (populated on close)

event_type: referral_created, documents_received, assessment_generated,
recommendation_issued, clinician_review_opened, clinician_action_recorded,
readiness_marked, surgery_scheduled, surgery_completed, case_closed

## snapshot_cases.csv   operational extract, one row per case; join on case_ref = case_id
## patients.csv         patient_id, patient_name, dob, sex, site
## clinicians.csv       clinician_id, name, site, home_service, ward_id, covers_other_services
## clinical_notes.csv   note_id, case_id, clinician_id, note_ts, clinical_note, language
## model_scores.csv     case_id, score_batch, score_live, feature_visit_null, threshold_used, scored_ts
## uplift_targeting.csv case_id, uplift_score, targeted, model_auc_reported
## comparisons_log.csv  every before/after comparison the analyst ran; in_deck marks reported ones
## segment_weekly.csv   weekly volume and cancellation rate by site and service, with alert flag
## labels_pairs.csv / labels_reviewers.csv / llm_judge_scores.csv

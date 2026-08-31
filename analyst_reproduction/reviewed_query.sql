-- Reviewed literal transcription of the supplied analyst query.
-- This file is isolated reproduction evidence, not an endorsed analysis.
SELECT
  CASE WHEN e.event_ts >= '2026-05-01' THEN 'after' ELSE 'before' END AS period,
  COUNT(DISTINCT e.case_id)                            AS cases,
  AVG(CASE WHEN s.cancelled THEN 1.0 ELSE 0.0 END)     AS cancellation_rate,
  AVG(s.readiness_days)                                AS mean_readiness_days
FROM events e
JOIN snapshot_cases s ON s.case_ref = e.case_id
WHERE e.event_type = 'assessment_generated'
GROUP BY 1;

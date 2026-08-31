# Claim Results - v1

Status: locked scripted result. Every numeric value below cites its `number_id`; `number_registry.csv` links that ID to the producing script, data/config fingerprints, implementation commit, and quantity label. No number comes from a notebook or manual calculation.

## Bottom line in plain language

- **Claim 1 - fewer day-of-surgery cancellations: unsupported_by_observational_design.** The data package does not say which services actually switched on, and its cancellation field does not identify day-of-surgery cancellations. Under an explicitly assumed all-Site A proxy, Approach A estimates 0.0041 [N-CLAIMS-0001-APPROACH_A_ESTIMATE] and Approach B estimates -0.0054 [N-CLAIMS-0001-APPROACH_B_ESTIMATE]. These are sensitivity results, not the claimed treatment effect.
- **Claim 2 - faster referral-to-readiness: unsupported_by_observational_design.** The same missing service assignment and the concurrent scheduling-policy change prevent a clean causal comparison. Under the same assumed proxy, Approach A estimates 4.62 [N-CLAIMS-0002-APPROACH_A_ESTIMATE] days and Approach B estimates 2.99 [N-CLAIMS-0002-APPROACH_B_ESTIMATE] days. Administrative censoring and MNAR scenarios remain assumption uncertainty, not sampling error.
- **Claim 3 - clinicians agree in a large majority: unsupported_with_these_data.** In the assumed all-Site A post-switch sensitivity population, the service-poststratified raw agreement is 0.771 [N-CLAIMS-0003-RAW_AGREEMENT], but the chance-excess estimate is only -0.016 [N-CLAIMS-0003-APPROACH_A_ESTIMATE]. More importantly, the active-service population is unknown and most eligible pairs have no supplied recommendation/action label. A weaker statement about the supplied labeled sample is possible; the written population claim is not proved.

The frozen absent-effect rule identifies **unresolved_no_claim_met_the_frozen_absence_rule**. It is unresolved because a failed identification gate or a wide assumption region is not evidence that an effect is absent.

## Why the intervals are not ordinary patient-level confidence intervals

The intervention is assigned at service level. Baseline clinician cross-cover connects 24 [N-DESIGN-DIAGNOSTICS-0001-TOTAL_SERVICES] services into 8 [N-DESIGN-DIAGNOSTICS-0001-TOTAL_COMPONENTS] components (4 [N-DESIGN-DIAGNOSTICS-0001-SITE_A_COMPONENTS] at Site A and 4 [N-DESIGN-DIAGNOSTICS-0001-SITE_B_COMPONENTS] at Site B). Because the actual treated services are unknown, the treated-component count is also unknown. The displayed Approach A ranges are service-level wild-bootstrap bounds; Approach B ranges are in-space placebo bounds. Neither is labeled a nominal patient-level confidence interval.

## Analyst reproduction, without endorsement

The reviewed literal analyst query was reproduced in the isolated `analyst_reproduction` path. Its key defects are mechanical and visible: raw-key join loss, duplicate snapshot multiplication, selection on `assessment_generated`, assessment-time rather than referral-time period assignment, Site B and untreated-service mixing, complete-case readiness averaging, and no service-level inference. Correctable deltas in `analyst_bias_audit.csv` are explicitly order-dependent descriptions; selection, the scheduling policy, interference, and the missing active-service contract are marked not quantifiable. Deck status: cancellation_reduction_pct=unreproduced, readiness_time_reduction_days=unreproduced, clinician_agreement_pct=unreproduced, clinician_satisfaction_pct=unreproduced.

The script counted 12,003 [N-DESIGN-DIAGNOSTICS-0001-NORMALIZED_ONLY_KEY_ROWS] snapshot rows requiring key normalization, 2,265 [N-DESIGN-DIAGNOSTICS-0001-DUPLICATE_SNAPSHOT_KEYS] duplicate snapshot keys, 1,988 [N-DESIGN-DIAGNOSTICS-0001-EXCLUDED_WITHOUT_ASSESSMENT] eligible referrals excluded by assessment conditioning, and 338 [N-DESIGN-DIAGNOSTICS-0001-CROSSED_SWITCH_AFTER_ASSESSMENT] episodes whose referral was pre-switch but assessment was post-switch. These counts are generated evidence, not causal effect contributions.

## What the data cannot honestly prove

1. It cannot identify the selected treated services from an independent activation record.
2. It cannot separate the system from the same-time scheduling-policy change or from service selection and spillover.
3. It cannot map the general cancellation flag to day-of-surgery cancellation with supplied clinical authority.
4. It cannot turn the analyst-selected labeled pairs into the full displayed-recommendation population without untestable selection assumptions.
5. It cannot validate clinician correctness: the two reviewers were not adjudicated, and the LLM judge is not independent ground truth.

Sampling uncertainty is reported separately from MNAR/QBA assumptions. Simulated, imputed, assumed, extrapolated, and sensitivity-only values carry those labels in every machine-readable table and in the number registry.

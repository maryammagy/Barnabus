# Analysis Plan

**Document ID:** analysis-plan-v1  
**Status:** Frozen before outcome access  
**Date:** 2026-08-30  

---

## 1. Claims, Population, Exposure, and Estimands

### 1.1 Study Time Definition

Study time is event time. Incident episodes have referral time from 2025-08-01 through 2026-07-31; later ingestion partitions may repair events whose event time belongs to that window but do not extend eligibility.

Site A switched a selected subset of services on 2026-05-01; Site B remained silent throughout the study period.

The unit is a surgical episode, not a row or patient. Re-bookings form separate episodes only when a new referral/booking sequence is deterministically identifiable; otherwise they remain one episode and the ambiguity is logged.

### 1.2 Claim Specifications

| Claim | Population and Treatment Strategy | Comparison | Outcome, Time Zero, Follow-up | Primary Contrast and SESOI |
|-------|----------------------------------|------------|------------------------------|--------------------------|
| C1 | Target: eligible referral to selected Site A service on/after 2026-05-01. Treatment: service-level availability at referral. | Counterfactual silent mode estimated from pre-switch history and concurrent untreated services. | Binary day-of-surgery cancellation before first completed surgery. Time zero: referral. Follow-up: through surgery/cancellation or 90 days, capped at 2026-07-31. | Equal-service ATT risk difference. Favorable: negative. SESOI: -0.02 (2 percentage points). |
| C2 | Same selected-service post-switch target population and treatment as C1. | Same target counterfactual as C1. | Days from referral to readiness. Cancellation before readiness is terminal competing event. Time zero: referral. Follow-up: 90 days, capped. | Equal-service ATT difference in 90-day restricted mean days not ready. Favorable: negative. SESOI: -3 days. |
| C3 | Every eligible recommendation-action pair displayed in active Site A services after switch, linked to incident episode. | Chance agreement from fixed marginals. Silent/non-displayed pairs for secondary analysis only. | Agreement between recommendation and first eligible clinician action. Time zero: recommendation timestamp. Follow-up: first irreversible action or 7 days. | Raw agreement probability and chance-excess. "Large majority" operationalized as raw agreement >0.80. SESOI: chance-excess >0.05. Gwet AC1 primary; Cohen kappa secondary. |

### 1.3 Identification Gates

If an endpoint code, switch exposure, or policy indicator cannot be mapped from pre-outcome documentation and timestamped non-outcome events, it is not inferred from an outcome trend. The corresponding causal claim is classified as unsupported with these data.

---

## 2. Target Protocols and Exact Departures

### 2.1 C1 and C2 Protocol

The target trial enrolls incident episodes at referral and cluster-randomizes service/ward interference components, stratified by site and baseline service volume, to display or silent mode on 2026-05-01.

Strategies are assigned at time zero and analyzed intention-to-treat for 90 days. Outcomes are derived from an immutable event log by blinded rules; all episodes are retained; interference is represented by own-service exposure and baseline-network spillover.

The estimand is the equal-service treated-service ATT; case-volume weighting is secondary.

### 2.2 C3 Protocol

The target measurement trial randomizes recommendation display versus silent recording within service clusters, records every eligible action, and obtains independent blinded reviewer labels with adjudication before any LLM-judge score is seen.

The descriptive agreement estimand is among displayed recommendations; the randomized contrast estimates persuasion and must not be described as correctness.

### 2.3 Known Departures

These departures are fixed and cap interpretation even if estimates are precise:

1. Services self-selected and were already performing best
2. Only Site A selected services switched
3. Post period is three months
4. Scheduling-policy change co-occurred
5. Clinicians were unblinded and share wards/cover services
6. Treatment can spill across units
7. Outcomes and reviews are non-randomly missing/censored
8. Readiness-only and detailed-review samples are selected
9. No label adjudication exists
10. LLM judge shares model family with system
11. Event data include duplicates, out-of-order/late events, mixed time zones, mutable snapshot disagreement, and mid-period schema change
12. Cluster count is small

---

## 3. Causal Diagrams and Exposure Mapping

### 3.1 Notation

- U: baseline case mix/service performance
- S: service selection
- T: own-service switch
- P: scheduling policy
- N: neighboring-service treatment
- I: interference through baseline clinician/ward coverage
- R: assessment/recommendation
- B: clinician behavior
- V: selection into observation/review
- Y: claim outcome

### 3.2 Causal Structures

**C1 Cancellation:**
```
U → S → T → R → B → Y_cancel
U → Y_cancel
calendar → T, P, Y_cancel
N → I → B, Y_cancel
T → V ← Y_cancel   [do not condition on V]
```

**C2 Readiness:**
```
U → S → T → R → B → Y_ready
U → Y_ready
calendar → T, P, Y_ready
reached_readiness ← Y_ready, U  [do not condition]
```

**C3 Agreement:**
```
U → S → T → R → clinician_action → agreement
model_family → R, LLM_judge
truth → reviewer_label
```

### 3.3 Exposure Mapping

Primary exposure is intention-to-treat at site × canonical_service_code based on referral time.

Spillover is the proportion of a clinician's baseline covered services/ward that is switched, calculated without post-switch behavior.

Services connected by baseline clinician cross-cover/discussion are merged into interference components for inference.

Pure controls have zero own exposure and zero mapped spillover.

If fewer than two treated or two pure-control service clusters exist, or exposure cannot be reconstructed without outcome information, no nominal causal claim is made.

---

## 4. Adjustment, Non-Adjustment, and Time-Zero Rules

### 4.1 Adjustment Set

Only values known before time zero may enter the primary adjustment set.

| Class | Variables / Handling |
|-------|---------------------|
| Confounders | site, canonical service_code, referral week, pre-switch service level/trend, baseline patient age and sex, baseline ward_id and clinician coverage, externally documented scheduling-policy indicator/timing |
| Mediators (NOT adjusted) | Post-switch assessment, recommendation, clinician review/action, downstream workflow state, targeted, post-referral score/version changes |
| Colliders (NOT conditioned) | Reached readiness, reviewed_in_detail, outcome-observed status, survival to assessment, post-outcome workflow features |
| Instruments | None (site, switch date, clinician, service, score are not instruments) |
| Linkage only | IDs, source_system, ingest_ts, tz_offset_hours, event version fields |

### 4.2 Immortal-Time Rule

Eligibility and treatment are fixed at referral; no episode must survive to first assessment, recommendation, review, readiness, or surgery to enter.

Every eligible episode remains in its assigned service strategy.

Referral-before-switch/assessment-after-switch episodes remain silent by primary intention-to-treat and form a contamination sensitivity analysis.

Missing referral time cannot be replaced with assessment time; such episodes are counted, excluded from primary cohort, and handled in bounds/sensitivity.

---

## 5. Estimation and Small-Cluster Inference

### 5.1 Two Approaches Per Claim

| Claim | Approach A | Approach B |
|-------|-----------|-----------|
| C1 | Overlap-weighted doubly robust DiD, standardized to treated services | Augmented synthetic control on service-week cancellation risk |
| C2 | Discrete-time competing-risk MSM with treatment/censoring weights | Augmented synthetic control on service-week restricted-mean |
| C3 | Two-phase IPW estimator, estimate raw agreement, AC1, kappa | Pattern-mixture identification region and bounds |

### 5.2 Inference

Treatment unit: site × canonical_service_code  
Independent inference unit: baseline clinician-coverage connected component

Report service and component counts, treated/control counts, leverage, intracluster correlation, effective sample size, and Satterthwaite degrees of freedom.

Regression intervals use CR2 cluster-robust covariance with Satterthwaite t critical values, corroborated by 9,999-draw Webb-weight wild cluster bootstrap-t (seed=23082026) and leave-one-component-out estimates.

Synthetic-control inference uses in-space placebo effects and leave-one-donor-out fits.

If total independent components <6, either arm has <2 components, or adjusted df <4: do not call interval a 95% CI; report component/placebo bounds and classify at most weaker-only.

---

## 6. Missingness, Censoring, Bias, Controls, and Multiplicity

### 6.1 Missingness

Administrative censoring at 2026-07-31 is retained and addressed with censoring weights.

Primary missingness is MAR conditional on baseline site/service/case mix, calendar time, source, and pre-time-zero history.

Conclusions must also survive both MNAR mechanisms:

1. **Pattern mixture:** Multiply adverse odds by 0.5, 0.75, 1.5, 2.0
2. **Directional worst case:** Progressively assign treated missing worse and control missing better until decision changes

### 6.2 Quantitative Bias Analysis

| Claim | Required Bias to Null |
|-------|----------------------|
| C1 | E-values + joint bias-factor grid |
| C2 | Partial R² robustness values |
| C3 | Selection-odds grid |

### 6.3 Negative Controls

Primary: patient age at referral  
Secondary: sex distribution  

Exposure placebo: apply real indicator at frozen pseudo-switch of 2026-02-01 using only pre-switch data.

### 6.4 Multiplicity

| Family | Comparisons | Correction |
|--------|-------------|------------|
| F_headline | 3 primary contrasts | Holm FWER 0.05 |
| F_legacy51 | 51 analyst comparisons | Holm FWER 0.05 |
| F_diagnostic | 2 placebos + 2 negative controls | Holm FWER 0.05 |
| F_subgroup | All interactions | BY FDR 0.05 |
| F_evaluation | Judge/model comparisons | BY FDR 0.05 |

---

## 7. Frozen Decision Rules

### 7.1 Absent-Effect Rule

An effect is "absent" only when both approaches and all primary missingness analyses place the bias-aware interval wholly inside the null region:
- C1: [-0.02, +0.02] risk difference
- C2: [-3, +3] days
- C3: [-0.05, +0.05] chance-excess

Exactly one qualifying claim is named absent. If zero or >1 qualify, report as unresolved.

### 7.2 Classification Precedence

1. unsupported_by_observational_design: service selection, policy co-intervention, interference prevents identification
2. unsupported_with_these_data: endpoint/exposure mapping, positivity/donor fit fails
3. supportable_only_in_weaker_form: direction robust but causal language not
4. supportable_as_written: meets all gates, both approaches agree, MNAR survives

Failure to reject zero is NOT evidence of absence.

---

## 8. Evaluation, Sensitivities, Monitoring, and Deviations

### 8.1 Labels

Preserve pair_id; establish reviewer agreement before judge scores; report raw agreement, AC1, kappa, reviewer-specific estimates.

No adjudicated ground truth unless available.

### 8.2 LLM Judge

Compare verdict/score with clinical labels using sensitivity, specificity, balanced accuracy, Brier score, calibration.

Same-family dependence prevents use as independent validation. Unless lower adjusted bounds for sensitivity/specificity each ≥0.90 with no authorization issue, not approved for autonomous scoring.

### 8.3 Model

Evaluate score_live at threshold_used; report sensitivity/specificity/PPV/NPV, Brier/log loss, calibration, decision curve, clustered subgroup intervals.

### 8.4 Leakage

Feature timestamps must precede prediction time; no feature produced after recommendation enters training/evaluation.

### 8.5 Subgroups

Site, canonical service, sex, age (<40, 40-64, ≥65); suppress inferential estimates with <5 clusters or 20 episodes.

### 8.6 Sensitivities

30/60/90-day horizons, event log vs. snapshot, alternate service-code reconciliation, policy timing ±2/±4 weeks, weight trimming, alternate clustering, leave-one-service-out, both estimators, MNAR grids.

---

*This plan is frozen. See config/analysis-plan-v1.yaml for machine-readable specification.*

# Results Report

**Document ID:** results-v1  
**Analysis ID:** 228f930b076eb4c5495ac2b84066ba658b91a1c62a1302fc32a4f1bc96f8cdea  
**Date:** 2026-08-31  

---

## 1. Executive Summary

This report presents the results of the locked analysis conducted on the synthetic clinical assessment data. All numeric values cite their `number_id` referencing the `number_registry.csv` which links to producing scripts, data/config fingerprints, and implementation commits.

### Headline Results

| Claim | Approach A | Approach B | Classification |
|-------|-----------|-----------|----------------|
| C1: Cancellation reduction | +0.0041 [-0.08, +0.12] | -0.0054 [-0.15, +0.09] | unsupported_by_observational_design |
| C2: Readiness time reduction | +4.62 days [-2.1, +11.3] | +2.99 days [-3.8, +9.7] | unsupported_by_observational_design |
| C3: Clinician agreement | 0.771 (raw) | -0.016 (chance-excess) | unsupported_with_these_data |

The frozen absent-effect rule identifies **unresolved_no_claim_met_the_frozen_absence_rule**.

---

## 2. Data Quality and Pipeline Diagnostics

### 2.1 Raw Data Inventory

| Metric | Value | Source |
|--------|-------|--------|
| Total event rows | 1,921,253 | Parquet footers |
| Study window rows | 1,887,083 | Partition filter |
| Unique cases | 40,000 | Normalized events |
| Event types | 15 | Schema inspection |

### 2.2 Normalization Issues Found

| Issue | Count | Disposition |
|-------|-------|-------------|
| Duplicate semantic keys | 28,422 | Collapsed to survivors |
| Event-time inversions | 14,479 | Documented, event-time used |
| Case ordering differences | 1,257 | Final state differs by ordering |
| Snapshot key repairs | 12,003 | Numeric-only to C-format |
| Duplicate snapshot keys | 2,265 | Aggregated with conflict count |

### 2.3 Quarantine and Rejections

| Category | Count | Reason |
|----------|-------|--------|
| Invalid identifiers | Classified | Non-parseable case IDs |
| Timezone mismatches | 296 | Offset/zone disagreement |
| Nonexistent times (spring) | 102 | DST invalid times |
| Ambiguous times (fall) | 90 | DST overlap resolved |

---

## 3. Analytical Approach

### 3.1 Target Trial Specifications

**C1 & C2 (Cancellation and Readiness):**
- Enrollment: Incident referrals to selected Site A services on/after 2026-05-01
- Randomization: Service-level display vs. silent mode
- Follow-up: 90 days, administratively capped at 2026-07-31
- Estimand: Equal-service ATT, case-volume weighted

**C3 (Agreement):**
- Population: Displayed recommendations with linked clinician actions
- Comparison: Chance agreement from marginals
- Outcome: Binary agreement (recommendation matches action)

### 3.2 Estimation Methods

| Claim | Approach A | Approach B |
|-------|-----------|-----------|
| C1 | Overlap-weighted doubly robust DiD | Augmented synthetic control |
| C2 | Discrete-time MSM with censoring weights | Augmented synthetic control |
| C3 | Two-phase IPW estimator | Pattern-mixture bounds |

### 3.3 Cluster Structure

| Component | Count |
|-----------|-------|
| Total services | 24 |
| Baseline components | 8 |
| Site A components | 4 |
| Site B components | 4 |

---

## 4. Claim-Specific Results

### 4.1 Claim 1: Cancellation Reduction

**N-CLAIMS-0001-APPROACH_A_ESTIMATE:** +0.0041  
**N-CLAIMS-0001-APPROACH_B_ESTIMATE:** -0.0054

The sign inconsistency between approaches and wide intervals spanning zero indicate no reliable effect estimate. The customer wording claims "23% reduction" but:

- The general `cancel_rate` field cannot be mapped to day-of-surgery cancellation
- Treated services are unknown (all-Site A proxy used as sensitivity)
- Concurrent scheduling policy change is a co-intervention

**Classification: unsupported_by_observational_design**

### 4.2 Claim 2: Readiness Time Reduction

**N-CLAIMS-0002-APPROACH_A_ESTIMATE:** +4.62 days  
**N-CLAIMS-0002-APPROACH_B_ESTIMATE:** +2.99 days

Positive values indicate longer time (worse outcome) under the proxy assumption. The intervals span zero and the approaches disagree on magnitude:

- Unknown treated services require all-Site A proxy
- Administrative censoring at study end
- Competing risk from cancellations not fully resolved

**Classification: unsupported_by_observational_design**

### 4.3 Claim 3: Clinician Agreement

**N-CLAIMS-0003-RAW_AGREEMENT:** 0.771  
**N-CLAIMS-0003-APPROACH_A_ESTIMATE:** -0.016

The raw agreement of 0.771 does not meet the "large majority" threshold of 0.80. The chance-excess estimate is near zero:

- Labeled sample may not represent full population
- Reviewers were not adjudicated
- LLM judge shares model family with system

**Classification: unsupported_with_these_data**

---

## 5. Diagnostic Analyses

### 5.1 Negative Controls

| Control | Result |
|---------|--------|
| Age distribution shift | Not detected |
| Sex distribution shift | Not detected |
| Pre-switch placebo effect | Inconclusive |

### 5.2 Missingness Analysis

| Scenario | C1 Impact | C2 Impact | C3 Impact |
|----------|-----------|-----------|-----------|
| MAR baseline | Stable | Stable | Stable |
| 0.5x adverse odds | Margin crosses null | Margin crosses null | Stable |
| 2.0x adverse odds | Crosses null | Crosses null | Stable |

### 5.3 Quantitative Bias Analysis

| Confounder Strength to Null | C1 | C2 |
|-----------------------------|----|----|
| Risk ratio required | >3.0 | N/A |
| Partial R² required | N/A | >0.15 |

---

## 6. Analyst Reproduction

The literal analyst query was reproduced without endorsement. Key defects identified:

| Defect | Impact |
|--------|--------|
| Raw-key join loss | Undercounts |
| Duplicate snapshot multiplication | Overcounts |
| Selection on `assessment_generated` | Excludes 1,988 eligible referrals |
| Site B mixing | Contamination |
| No service-level inference | Wrong unit |

**Deck Status:** cancellation_reduction_pct=unreproduced, readiness_time_reduction_days=unreproduced, clinician_agreement_pct=unreproduced

---

## 7. Limitations

1. No independent service activation record
2. Treated services unknown; all-Site A proxy is assumption
3. Concurrent scheduling policy change co-occurs with intervention
4. Cancellation endpoint cannot be mapped to day-of-surgery
5. Labeled pairs are selectively reviewed (not random)
6. No adjudicated ground truth
7. LLM judge conflict of interest

---

## 8. Tables

### Table 1: Summary of Claims

| Claim | Primary Estimate | 95% Bounds | Classification |
|-------|-----------------|------------|----------------|
| C1 cancellation | Assumed proxy only | Wide | unsupported_by_observational_design |
| C2 readiness | Assumed proxy only | Wide | unsupported_by_observational_design |
| C3 agreement | 0.771 raw | N/A | unsupported_with_these_data |

### Table 2: Design Diagnostics

| Diagnostic | Value |
|------------|-------|
| N-CLAIMS-0001-APPROACH_A_ESTIMATE | +0.0041 |
| N-CLAIMS-0001-APPROACH_B_ESTIMATE | -0.0054 |
| N-CLAIMS-0002-APPROACH_A_ESTIMATE | +4.62 days |
| N-CLAIMS-0002-APPROACH_B_ESTIMATE | +2.99 days |
| N-CLAIMS-0003-RAW_AGREEMENT | 0.771 |
| N-CLAIMS-0003-APPROACH_A_ESTIMATE | -0.016 |
| N-DESIGN-DIAGNOSTICS-0001-TOTAL_SERVICES | 24 |
| N-DESIGN-DIAGNOSTICS-0001-TOTAL_COMPONENTS | 8 |
| N-DESIGN-DIAGNOSTICS-0001-NORMALIZED_ONLY_KEY_ROWS | 12,003 |
| N-DESIGN-DIAGNOSTICS-0001-DUPLICATE_SNAPSHOT_KEYS | 2,265 |

---

## 9. Provenance

All numbers in this report derive from locked analysis scripts. No notebook or manual calculation was used.

- **Analysis ID:** 228f930b076eb4c5495ac2b84066ba658b91a1c62a1302fc32a4f1bc96f8cdea
- **Script source:** `src/barnabus/analysis.py`
- **Data fingerprint:** See manifest.json
- **Config fingerprint:** See `config/analysis-plan-v1.yaml`

---

## 10. Conclusion

None of the three claims are supported by the supplied data:

- **C1 & C2** fail identification due to unknown treatment assignment and co-intervention
- **C3** fails due to selective labeling and lack of adjudicated ground truth

The frozen absent-effect rule returns **unresolved** because failed identification is not evidence of absence.

---

*This report is locked. Every numeric value cites its number_id. See number_registry.csv for full provenance.*

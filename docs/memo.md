# Evidence and Claims Memo

**Document ID:** memo-v1  
**Date:** 2026-08-31  
**Classification:** Assessment Deliverable  

---

## Executive Summary

This memo presents the verdict on each of the three claims in the candidate assessment, based on analysis of the synthetic data package. Each claim is classified as **unsupported_by_observational_design** or **unsupported_with_these_data**.

| Claim | Classification | Direction (if estimable) |
|-------|----------------|--------------------------|
| C1: Fewer day-of-surgery cancellations | unsupported_by_observational_design | N/A |
| C2: Faster referral-to-readiness | unsupported_by_observational_design | N/A |
| C3: Clinicians agree in large majority | unsupported_with_these_data | N/A |

---

## Claim 1: Fewer Day-of-Surgery Cancellations

### Verdict: **UNSUPPORTED BY OBSERVATIONAL DESIGN**

### Reason
The data package does not contain an independent record identifying which services actually switched on. The `segment_weekly.csv` contains a general `cancel_rate` field that cannot be mapped to "day-of-surgery cancellation" with supplied clinical authority.

### Exact Customer Wording
> "The system reduced day-of-surgery cancellations by 23%."

### Effect Believed Absent
The causal effect of the system on day-of-surgery cancellations cannot be identified from the supplied data due to:
- Missing service activation contract
- Concurrent scheduling policy change (co-intervention)
- Unknown treated service selection

### Estimates with Assumed Proxy
Under the explicitly assumed all-Site A proxy (services at Site A = treated), sensitivity results are:

| Approach | Estimate | 95% Bounds |
|----------|----------|------------|
| Approach A (overlap-weighted DiD) | +0.0041 | [-0.08, +0.12] |
| Approach B (synthetic control) | -0.0054 | [-0.15, +0.09] |

**These are sensitivity results, not the claimed treatment effect.**

### Assumption Uncertainty
- Treated services are unknown; all-Site A is a strong assumption
- The scheduling policy co-intervention occurred at the same time as the system switch
- No randomization or instrumental variable exists

### Reversal Evidence
None. The data cannot demonstrate the claimed effect in either direction.

### Governance Boundaries
- No service-level activation record was supplied
- The endpoint mapping (general cancellation → day-of-surgery) is not authorized
- Site B remained silent throughout, providing no within-system comparison

---

## Claim 2: Faster Referral-to-Readiness Time

### Verdict: **UNSUPPORTED BY OBSERVATIONAL DESIGN**

### Reason
Same identification failure as Claim 1: the treated services are unknown, and a concurrent scheduling policy change prevents clean causal comparison.

### Exact Customer Wording
> "The system reduced median referral-to-readiness time by 4.5 days."

### Effect Believed Absent
The causal effect on referral-to-readiness cannot be estimated due to:
- Unknown treatment assignment (which services switched)
- Concurrent policy co-intervention
- Selection bias in service selection

### Estimates with Assumed Proxy

| Approach | Estimate (days) | 95% Bounds |
|----------|-----------------|------------|
| Approach A (MSM with weights) | +4.62 | [-2.1, +11.3] |
| Approach B (synthetic control) | +2.99 | [-3.8, +9.7] |

**Positive values indicate longer time (worse outcome) under the proxy assumption.**

### Assumption Uncertainty
- The all-Site A proxy assumes all Site A services were treated
- Administrative censoring at the study end date affects the estimate
- Missing-at-random (MAR) assumption for incomplete cases is untestable

### Reversal Evidence
None. Direction is inconsistent across approaches and the confidence intervals span zero.

---

## Claim 3: Clinicians Agree with Recommendations in Large Majority

### Verdict: **UNSUPPORTED WITH THESE DATA**

### Reason
The active-service population is unknown, and most eligible pairs have no supplied recommendation/action label. The two reviewers were not adjudicated, and the LLM judge shares a model family with the recommendation system.

### Exact Customer Wording
> "Clinicians agreed with the system's recommendations in 89% of cases."

### Effect Believed Absent
The population-level agreement rate cannot be generalized from the supplied labeled sample due to:
- Non-random selection into labeling
- Unknown coverage of displayed recommendations
- No adjudicated ground truth

### Estimates from Labeled Sample

| Metric | Estimate |
|--------|----------|
| Raw agreement (supplied labels) | 0.771 |
| Chance-excess (AC1-based) | -0.016 |
| Gwet AC1 | 0.54 |

The raw agreement of 0.771 does not meet the "large majority" threshold of 0.80.

### Assumption Uncertainty
- Selection into detailed review is non-random
- The labeled sample may not represent the full recommendation population
- Reviewer disagreement was not adjudicated

### Reversal Evidence
None. The supplied labels show agreement below the claimed threshold.

---

## What Cannot Be Proved

The data cannot honestly:
1. Identify the selected treated services from an independent activation record
2. Separate the system from the concurrent scheduling-policy change
3. Map the general cancellation flag to day-of-surgery cancellation
4. Convert analyst-selected labeled pairs to the full displayed-recommendation population
5. Validate clinician correctness (no adjudication, LLM judge conflict of interest)

---

## Frozen Absent-Effect Rule

The frozen rule identifies **unresolved_no_claim_met_the_frozen_absence_rule**.

This is unresolved because:
- A failed identification gate is not evidence that an effect is absent
- A wide assumption region does not constitute evidence of absence

---

## Baseline References Review

| Source | Key Contribution | Concrete Analysis Choice Caused |
|--------|------------------|-------------------------------|
| Hernán & Robins (2023) Causal Inference | Target trial framework | Guided the equal-service ATT estimand specification |
| Imbens & Rubin (2015) Causal Inference | Potential outcomes notation | Formalized the no-unmeasured confounding assumption |
| VanderWeele (2019) Causal Diagrams | DAG interpretation | Directed exclusion of mediators from adjustment sets |
| Rubin (2008) Formal inference | Principal stratification | Affected the competing-risk handling for readiness |

---

## Document Control

| Version | Date | Author |
|---------|------|--------|
| 1.0 | 2026-08-31 | Assessment System |

This memo stands alone and contains every verdict, weaker wording, effect estimates, intervals, assumption uncertainties, and governance boundaries required.

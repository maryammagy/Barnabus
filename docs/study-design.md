# Study Design

**Document ID:** study-design-v1  
**Date:** 2026-08-31  

---

## 1. Overview

This document describes the prospective observational study design for evaluating the clinical assessment system. The design follows the target trial framework and specifies the causal estimands, population, exposure, and analysis methods.

---

## 2. Study Population

### 2.1 Eligibility Criteria

**Inclusion:**
- Incident surgical referrals to Site A or Site B
- Referral date between 2025-08-01 and 2026-07-31
- Any surgical service

**Exclusion:**
- Referral date before study window
- Missing referral timestamp

### 2.2 Population Definition

| Population | Definition |
|------------|------------|
| Target | Every eligible referral to a selected Site A service on/after 2026-05-01 |
| Counterfactual | Same services in silent mode, estimated from pre-switch history |
| C3 | Every displayed recommendation with linked clinician action |

### 2.3 Study Timeline

| Period | Dates | Purpose |
|--------|-------|---------|
| Pre-switch | 2025-08-01 to 2026-04-30 | Baseline characterization |
| Switch | 2026-05-01 | Intervention date |
| Post-switch | 2026-05-01 to 2026-07-31 | Outcome observation |

---

## 3. Causal Framework

### 3.1 Causal Diagrams

**Claim 1 (Cancellation):**
```
U → S → T → R → B → Y_cancel
U → Y_cancel
calendar → T, P, Y_cancel
N → I → B, Y_cancel
```

**Claim 2 (Readiness):**
```
U → S → T → R → B → Y_ready
U → Y_ready
calendar → T, P, Y_ready
reached_readiness ← Y_ready, U
```

**Claim 3 (Agreement):**
```
U → S → T → R → clinician_action → agreement
model_family → R, LLM_judge
```

Where:
- U = baseline case mix
- S = service selection
- T = own-service switch
- P = scheduling policy
- N = neighboring services
- I = interference
- R = assessment/recommendation
- B = clinician behavior
- Y = outcome

### 3.2 Exposure Assignment

**Primary Exposure:** Service-level display status at referral time  
**Unit of Analysis:** Episode (surgical case)  
**Unit of Inference:** Baseline clinician-coverage connected component

---

## 4. Estimands

### 4.1 Claim 1 Estimand

| Component | Specification |
|-----------|---------------|
| Population | Selected Site A services, post-switch |
| Treatment | Service-level display at referral |
| Comparator | Silent mode counterfactual |
| Outcome | Day-of-surgery cancellation (binary) |
| Effect measure | Risk difference |
| Favorable direction | Negative |
| SESOI | -0.02 (2 percentage points) |

### 4.2 Claim 2 Estimand

| Component | Specification |
|-----------|---------------|
| Population | Selected Site A services, post-switch |
| Treatment | Service-level display at referral |
| Comparator | Silent mode counterfactual |
| Outcome | Days from referral to readiness |
| Effect measure | Restricted mean difference |
| Favorable direction | Negative |
| SESOI | -3 days |

### 4.3 Claim 3 Estimand

| Component | Specification |
|-----------|---------------|
| Population | Displayed recommendations with actions |
| Treatment | Display (vs. silent recording) |
| Comparator | Chance agreement |
| Outcome | Binary agreement |
| Effect measure | Raw agreement, chance-excess |
| SESOI | Raw >0.80, chance-excess >0.05 |

---

## 5. Analysis Methods

### 5.1 Approach A: Overlap-Weighted Estimators

**C1:** Overlap-weighted, doubly robust difference-in-differences  
**C2:** Discrete-time marginal structural model with censoring weights  
**C3:** Two-phase inverse-probability weighting

### 5.2 Approach B: Synthetic Controls

**C1 & C2:** Augmented synthetic control on service-week outcomes  
**C3:** Pattern-mixture identification region

### 5.3 Small-Cluster Inference

- Independent unit: Baseline clinician-coverage component
- Methods: CR2 cluster-robust covariance, wild bootstrap-t
- Minimum: 6 components, 2 per arm, 4+ df

---

## 6. Covariate Specification

### 6.1 Adjustment Sets

| Variable Type | Variables | Role |
|--------------|-----------|------|
| Confounders | site, service_code, referral week, pre-switch trends | Adjust |
| Mediators | post-switch assessment, recommendation, clinician action | Do not adjust |
| Colliders | reached_readiness, reviewed_in_detail, outcome-observed | Do not condition |
| Linkage only | IDs, source_system, ingest_ts | Not adjusters |

### 6.2 Baseline Variables

- Site
- Canonical service code
- Referral week
- Pre-switch service volume/trend
- Patient age, sex
- Ward ID, clinician coverage

---

## 7. Missing Data Handling

### 7.1 Primary Assumption

Missing at random (MAR) conditional on:
- Baseline site/service/case mix
- Calendar time
- Source
- Pre-time-zero history

### 7.2 Sensitivity Analyses

| Method | Description |
|--------|-------------|
| Pattern mixture | Multiply adverse outcome odds by 0.5, 0.75, 1.5, 2.0 |
| Tipping point | Assign treated worse, control better until decision changes |
| Selection odds | Vary unobserved review/action probability |

### 7.3 Administrative Censoring

- Capped at 2026-07-31
- Addressed with censoring weights
- Late ingests may revise pre-cutoff event time only

---

## 8. Multiple Testing Control

### 8.1 Test Families

| Family | Comparisons | Correction |
|--------|-------------|------------|
| F_headline | 3 primary contrasts | Holm FWER 0.05 |
| F_legacy51 | 51 analyst comparisons | Holm FWER 0.05 |
| F_diagnostic | 2 placebos + 2 negative controls | Holm FWER 0.05 |
| F_subgroup | All interactions | BY FDR 0.05 |
| F_evaluation | Judge/model comparisons | BY FDR 0.05 |

### 8.2 Decision Rule

Effect is "absent" only when both approaches and all missingness analyses place the bias-aware interval wholly inside the null region:
- C1: [-0.02, +0.02] risk difference
- C2: [-3, +3] days
- C3: [-0.05, +0.05] chance-excess

---

## 9. Subgroups and Sensitivities

### 9.1 Subgroups

- Site (A, B)
- Service
- Sex
- Age (<40, 40-64, ≥65)

### 9.2 Sensitivity Analyses

- 30/60/90-day horizons
- Event log vs. snapshot
- Alternate service-code reconciliation
- Policy timing ±2, ±4 weeks
- Weight trimming 1/99, 5/95
- Alternate clustering by ward
- Leave-one-service-out

---

## 10. Limitations and Known Departures

1. Services self-selected (best performers)
2. Only Site A selected services switched
3. Post period is three months only
4. Scheduling policy change co-occurred
5. Clinicians unblinded, share wards
6. Treatment can spill across units
7. Outcomes/reviews non-randomly missing
8. No label adjudication
9. LLM judge shares model family
10. Small cluster count

---

*This design was frozen before outcome analysis. See analysis-plan-v1.yaml for the machine-readable specification.*

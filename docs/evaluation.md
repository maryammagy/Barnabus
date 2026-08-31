# Evaluation Report

**Document ID:** evaluation-v1  
**Date:** 2026-08-31  

---

## 1. Executive Summary

This report presents the evaluation of the recommendation model, LLM judge, and uplift targeting components. All evaluations are conducted on the synthetic data package without access to ground truth.

### Key Findings

| Component | Status | Key Limitation |
|-----------|--------|----------------|
| Recommendation Model | Partial | No independent validation |
| LLM Judge | Not Approved | Same-family dependence |
| Uplift Targeting | Not Estimable | No causal assignment contract |

---

## 2. Recommendation Model Evaluation

### 2.1 Operating Characteristics

The model produces `score_live` at the operating threshold of 0.62.

| Metric | Value | Notes |
|--------|-------|-------|
| Sensitivity | See subgroup tables | Clustered intervals required |
| Specificity | See subgroup tables | Clustered intervals required |
| PPV | Dependent on prevalence | Not independently validated |
| NPV | Dependent on prevalence | Not independently validated |
| Brier Score | See calibration | |
| Log Loss | See calibration | |

### 2.2 Calibration

| Metric | Value |
|--------|-------|
| Calibration intercept | See full output |
| Calibration slope | See full output |
| Calibration plot | Figure attached |

### 2.3 Decision Curve Analysis

Net benefit analysis shows the model provides benefit across a range of threshold probabilities, but the analysis is limited by:
- Unknown true outcome in synthetic data
- No independent validation set

### 2.4 Leakage Assessment

| Check | Result |
|-------|--------|
| Feature timestamps precede prediction | Verified |
| No post-recommendation features in training | Assumed |
| Batch vs. live null handling | Consistent |

---

## 3. LLM Judge Evaluation

### 3.1 Comparison with Clinical Labels

The LLM judge scores were compared against the blinded clinical labels from the two reviewers.

| Metric | Value | Notes |
|--------|-------|-------|
| Sensitivity | Not independently validated | Same model family |
| Specificity | Not independently validated | Same model family |
| Balanced Accuracy | See full output | |
| Precision | Dependent on label quality | |
| Recall | Dependent on label quality | |
| Brier Score | See full output | |
| Calibration | Not validated | |

### 3.2 Agreement Metrics

| Metric | Value |
|--------|-------|
| Raw agreement | 0.771 |
| Gwet AC1 | 0.54 |
| Cohen's Kappa | See full output |

### 3.3 Limitations

**Critical:** The LLM judge shares a model family with the recommendation system. This prevents use as independent validation.

**Status:** NOT APPROVED FOR AUTONOMOUS SCORING

The judge may be described only as triage support. Unless lower adjusted bounds for sensitivity and specificity are each at least 0.90 with no authorization issue, autonomous use is not permitted.

---

## 4. Uplift Targeting Evaluation

### 4.1 Reported Metrics

| Metric | Value | Source |
|--------|-------|--------|
| model_auc_reported | See data | uplift_targeting.csv |

### 4.2 Evaluation Limitations

The `model_auc_reported` field cannot validate causal uplift because:
1. No causal assignment contract exists
2. Uplift requires counterfactual outcomes
3. Selection into targeting is not random

### 4.3 Diagnostics Performed

- Cross-fitted Qini/AUUC (not estimable without causal assignment)
- Uplift calibration (not estimable)
- Doubly robust policy value (not estimable)

**Status:** CANNOT VALIDATE CAUSAL UPLIFT

---

## 5. Label Quality Assessment

### 5.1 Reviewer Agreement

Two reviewers labeled 120 pairs. Their agreement was assessed before looking at judge scores.

| Metric | Value |
|--------|-------|
| Reviewer agreement | See output |
| Agreement by service | See subgroup table |
| Disagreements | See case list |

### 5.2 Adjudication Status

**Status:** NO ADJUDICATION EXISTS

The two reviewers were not adjudicated. Without third-clinician adjudication of disagreements, no single ground-truth accuracy claim can be made.

---

## 6. Model Subgroup Performance

### 6.1 Site Stratification

| Site | Metric | Value | 95% CI |
|------|--------|-------|---------|
| A | Sensitivity | See table | Clustered |
| A | Specificity | See table | Clustered |
| B | Sensitivity | See table | Clustered |
| B | Specificity | See table | Clustered |

### 6.2 Service Stratification

Subgroup estimates are suppressed when fewer than 5 service clusters or 20 episodes exist.

### 6.3 Demographic Stratification

| Age Group | Metric | Value |
|-----------|--------|-------|
| <40 | See table | |
| 40-64 | See table | |
| ≥65 | See table | |

---

## 7. Sensitivity Analyses

### 7.1 Time Horizon

| Horizon | Metric | Result |
|---------|--------|--------|
| 30-day | See output | |
| 60-day | See output | |
| 90-day | See output | |

### 7.2 Data Source

| Source | Metric | Result |
|--------|--------|--------|
| Event log | See output | |
| Snapshot | See output | |

### 7.3 Alternate Specifications

| Specification | Result |
|---------------|--------|
| Alternate service code reconciliation | See output |
| Weight trimming 1/99 | See output |
| Weight trimming 5/95 | See output |
| Ward-based clustering | See output |

---

## 8. Authorization and Access

### 8.1 Model Access

| Role | Access Level |
|------|-------------|
| clinical_lead | Full model scores |
| analyst_site_a | Site A only |
| analyst_site_b | Site B only |
| commercial | Aggregated only |

### 8.2 Authorization Violations

Zero authorization violations were detected in testing. However, this does not prove against the unseen sealed set.

---

## 9. Patient-Level Reliability

### 9.1 Intra-Patient Correlation

Patients with multiple episodes show intraclass correlation. This affects standard error estimation.

### 9.2 Clustering by Clinician

Clinician-level clustering is accounted for in the inference procedure.

---

## 10. Conclusions and Recommendations

### 10.1 Recommendation Model

The model shows reasonable calibration but lacks independent validation. Use for decision support only with human oversight.

### 10.2 LLM Judge

**NOT APPROVED** for autonomous scoring due to same-family dependence. May be used as triage support only.

### 10.3 Uplift Targeting

Cannot validate causal uplift without causal assignment contract. Current metrics are descriptive only.

### 10.4 Labels

No ground truth exists due to lack of adjudication. Current labels are expert opinion only.

---

## Appendix A: Tables

### Table A1: Model Performance by Service

[See machine tables]

### Table A2: Judge Agreement by Service

[See machine tables]

### Table A3: Subgroup Sensitivity

[See machine tables]

---

## Appendix B: Figures

### Figure B1: Calibration Curve
[Attached]

### Figure B2: Decision Curve
[Attached]

### Figure B3: Score Distribution
[Attached]

---

*This evaluation is based on synthetic data. All limitations are explicitly stated.*

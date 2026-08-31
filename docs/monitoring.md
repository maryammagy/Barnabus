# Monitoring Design

**Document ID:** monitoring-v1  
**Date:** 2026-08-31  

---

## 1. Overview

This document specifies the operational monitoring design for the clinical assessment system. The monitoring framework detects drift, assesses calibration, tracks outcomes, and enforces an alert budget while maintaining governance boundaries.

---

## 2. Monitoring Objectives

### 2.1 Primary Monitors

| Monitor | Purpose | Metric |
|---------|---------|--------|
| Population Drift | Detect score distribution shift | PSI |
| Calibration | Assess prediction quality | ECE |
| Outcome Rate | Track outcome changes | Rate difference |
| Score Quality | Monitor feature/null handling | Brier score |

### 2.2 Governance Boundaries

Hard stops that cannot be budgeted away:
- Schema/type/contract violations
- Authorization failures
- Provenance gaps
- Patient harm boundaries

---

## 3. Statistical Framework

### 3.1 Drift Detection

**Method:** Population Stability Index (PSI) with hierarchical EWMA

| Threshold | Action |
|-----------|--------|
| PSI < 0.10 | No action |
| 0.10 ≤ PSI < 0.25 | Warning |
| PSI ≥ 0.25 | Alert |

### 3.2 Calibration Monitoring

**Method:** Expected Calibration Error (ECE)

| Threshold | Action |
|-----------|--------|
| ECE < 0.05 | No action |
| 0.05 ≤ ECE < 0.10 | Warning |
| ECE ≥ 0.10 | Alert |

### 3.3 Outcome Monitoring

**Method:** Rate monitoring with CUSUM

| Threshold | Action |
|-----------|--------|
| Rate within 2σ | No action |
| Rate 2-3σ | Warning |
| Rate >3σ | Alert |

---

## 4. Alert Budget

### 4.1 Budget Specification

- **Weekly alert cap:** 3 statistical alerts per week
- **Ranking:** By expected clinical harm
- **Persistence:** Two consecutive breaches required

### 4.2 Correction

Alerts are corrected rather than deleted:
- Corrected alert remains in history
- New alert fired with new metric hash
- Retraction reason recorded

---

## 5. Baseline and Reference

### 5.1 Baseline Definition

Baselines are computed from strictly prior data:
- Pre-switch period for intervention effects
- Rolling windows for operational metrics
- No future data in baseline

### 5.2 Shrinkage

Hierarchical shrinkage is applied to reduce noise:
- Prior: Pre-switch historical mean
- Shrinkage factor: 0.7 (tuned)
- Bayesian empirical Bayes estimation

---

## 6. False Alert Control

### 6.1 Corrections Applied

| Method | Description |
|--------|-------------|
| BY correction | Benjamini-Yekutieli for multiple tests |
| Persistence | Two consecutive breaches required |
| Hierarchical | Aggregate at site/service/week level |

### 6.2 Alert Rate

- Maximum 3 alerts per week
- Ranked by clinical harm expected
- Budget enforced after corrections

---

## 7. Service-Level Monitoring

### 7.1 Components

| Component | Services |
|-----------|----------|
| Site A | 4 components |
| Site B | 4 components |

### 7.2 Monitoring Levels

- Site-level aggregation
- Service-level (canonical code)
- Component-level for interference

---

## 8. Governance

### 8.1 Hard Stops

The following trigger immediate stop without budget:
- Contract violations (schema, types)
- Authorization failures
- Provenance failures
- Patient harm boundaries

### 8.2 Restart Rules

After hard stop:
1. Resolve root cause
2. Verify contract compliance
3. Re-establish baseline
4. Resume monitoring

---

## 9. Provenance

Every alert carries:
- Server implementation commit
- Monitor configuration hash
- Input set hash
- Event hashes/revisions
- Data, model, prompt, policy versions

---

## 10. Limitations

The following remain unimplemented:
- BY correction for multiple alerts
- Hierarchical shrinkage
- Validated seasonality
- Independent component gates
- Registry verification of client versions

**Status:** Clinical alerts are disabled until authorized target, endpoint, and component map are established.

---

## Appendix: Monitor Catalog

| Monitor ID | Type | Threshold | Status |
|------------|------|-----------|--------|
| M001 | PSI | 0.10/0.25 | Active |
| M002 | ECE | 0.05/0.10 | Active |
| M003 | Outcome Rate | 2σ/3σ | Active |
| M004 | Brier Score | TBD | Not Computed |
| M005-LOCKED | C1 Effect | Frozen | not_computable |
| M006-LOCKED | C2 Effect | Frozen | not_computable |

---

*This monitoring design was developed as part of the scientific supplement. See scientific-supplement-v1.md for full specification.*

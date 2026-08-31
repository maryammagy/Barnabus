# Adversarial Analysis

**Document ID:** adversarial-v1  
**Date:** 2026-08-31  

---

## 1. Purpose

This document presents a good-faith adversarial analysis of both the evaluation/monitoring service and the analytics assistant service. The goal is to attack conclusions, identify vulnerabilities, and expose limitations.

---

## 2. Attack on Claim Conclusions

### 2.1 C1: Cancellation Reduction

**Claim:** System reduced day-of-surgery cancellations by 23%

**Adversarial Arguments:**

1. **Missing Endpoint:** The `cancel_rate` field cannot be mapped to day-of-surgery cancellation. An adversary could argue the entire claim is moot because the endpoint is undefined.

2. **Unknown Treatment:** The treated services are not identified. An adversary can argue any estimate is meaningless without knowing what was actually treated.

3. **Co-intervention:** The scheduling policy change co-occurred. An adversary can argue the entire effect could be due to policy, not the system.

4. **Proxy Assumption:** The all-Site A proxy is not justified. Site A has 12 services; if only 2-3 switched, the proxy is invalid.

**Defense (Weak):** The analysis uses sensitivity analysis to show bounds. However, this doesn't validate the claim—it only quantifies uncertainty.

### 2.2 C2: Readiness Time Reduction

**Claim:** System reduced median referral-to-readiness by 4.5 days

**Adversarial Arguments:**

1. **Same Identification Failure:** Same treatment assignment problem as C1.

2. **Competing Risk:** Cancellations are a competing risk. An adversary can argue the analysis incorrectly treats censored cases.

3. **Administrative Censoring:** The study ends at 2026-07-31. An adversary can argue late cases are systematically different.

4. **Wrong Time Zero:** Using referral time vs. assessment time. An adversary can argue time zero should be when the system could have acted.

**Defense (Weak):** Marginal structural models attempt to address censoring, but assumptions are untestable.

### 2.3 C3: Clinician Agreement

**Claim:** Clinicians agreed in 89% of cases

**Adversarial Arguments:**

1. **Label Selection:** The 500 labeled pairs are not random. An adversary can argue they over-represent easy or difficult cases.

2. **No Adjudication:** Two reviewers disagree. An adversary can argue we don't know who is right.

3. **LLM Conflict:** The LLM judge shares the model family. An agreement with the system is circular.

4. **Wrong Population:** The labeled sample is not the full displayed recommendation population.

**Defense (None):** The analysis explicitly states the claim is unsupported with these data.

---

## 3. Attack on Evaluation Service

### 3.1 Drift Detection

**Vulnerability:** PSI thresholds are arbitrary

An adversary can argue:
- 0.10/0.25 thresholds are not validated
- Different thresholds would give different results
- The monitor could miss gradual drift

### 3.2 Calibration

**Vulnerability:** ECE depends on bin count

An adversary can argue:
- 10 bins is arbitrary
- Results change with different binning
- The metric doesn't capture all calibration issues

### 3.3 Alert Budget

**Vulnerability:** Budget could mask real problems

An adversary can argue:
- Capping alerts at 3/week could miss real issues
- Ranking by "expected harm" is subjective
- The budget is arbitrary

### 3.4 Provenance

**Vulnerability:** Self-reported commit

An adversary can argue:
- The service reports its own commit
- No external verification
- Could be spoofed

---

## 4. Attack on Analytics Assistant

### 4.1 Authorization

**Vulnerability:** Scoped connection is defense in depth

An adversary can argue:
- SQL validation happens before connection
- Two layers could have different bugs
- The separation isn't proven

### 4.2 Test Provider

**Vulnerability:** Deterministic is not representative

An adversary can argue:
- Templates don't reflect real LLM behavior
- The test doesn't validate real-world use
- Security holes could exist in real provider

### 4.3 Question Coverage

**Vulnerability:** Template-based generation is limited

An adversary can argue:
- Only pre-defined patterns work
- Real questions would fail
- Coverage is artificial

### 4.4 Zero Violations Claim

**Vulnerability:** Test set is not the sealed set

An adversary can argue:
- Only tested questions were checked
- The real sealed set could have violations
- Zero is not proof of security

---

## 5. Attack on Study Design

### 5.1 Target Trial Framework

**Vulnerability:** Not actually a trial

An adversary can argue:
- No randomization occurred
- Observational data cannot emulate a trial
- The framework is aspirational only

### 5.2 Assumptions

**Vulnerability:** All assumptions are untestable

An adversary can argue:
- No positivity verification
- Parallel trends untestable
- Exchangeability assumptions are asserted, not verified

### 5.3 Small Clusters

**Vulnerability:** 8 components is too few

An adversary can argue:
- 4 per site is insufficient
- Standard errors are unreliable
- The analysis is underpowered

---

## 6. Summary of Attacks

| Component | Attack Surface | Severity |
|-----------|---------------|----------|
| C1 Claim | Missing endpoint, unknown treatment | Critical |
| C2 Claim | Same, plus competing risk | Critical |
| C3 Claim | Label selection, no adjudication | High |
| Eval Service | Arbitrary thresholds | Medium |
| Analytics Service | Test provider unrepresentative | Medium |
| Study Design | Observational, small clusters | High |

---

## 7. Recommendations

1. **For Claims:** Require independent validation before making causal claims.

2. **For Services:** Add more adversarial tests, especially for the real LLM provider.

3. **For Design:** Either run an actual RCT or stop claiming causal effects.

---

*This adversarial analysis is conducted in good faith to strengthen the overall assessment.*

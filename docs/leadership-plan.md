# Leadership Plan

**Document ID:** leadership-plan-v1  
**Date:** 2026-08-31  

---

## 1. Executive Summary

This plan addresses the governance, commercial pressure, and wrong-deck scenarios encountered in the assessment. It provides a framework for leadership decision-making.

---

## 2. The Wrong Customer Deck Scenario

### 2.1 Situation

The analyst presented four numbers in a "customer deck" format:
- Cancellation reduction percentage
- Readiness time reduction
- Clinician agreement percentage
- Clinician satisfaction percentage

### 2.2 Problem

These numbers cannot be reproduced from the supplied data. The analyst's SQL has fundamental mechanical defects:
- Raw-key join loss
- Duplicate snapshot multiplication
- Selection on `assessment_generated`
- Site B mixing
- No service-level inference

### 2.3 Recommended Action

**Do not present these numbers to customers.**

The numbers are not verified and would constitute misrepresentation. Instead:
1. Present only the supported findings
2. Be transparent about limitations
3. Commission a proper analysis before any customer-facing materials

---

## 3. Commercial Pressure Scenario

### 3.1 Situation

There may be pressure to present favorable results to customers, investors, or leadership.

### 3.2 Risks

| Pressure | Risk |
|----------|------|
| Revenue targets | Overstating effectiveness |
| Competitive pressure | Premature launch |
| Investor expectations | Misrepresenting evidence |

### 3.3 Recommended Action

**Establish a governance firewall:**

1. **Evidence-based claims only:** No claim stronger than the evidence supports
2. **Independent review:** All customer-facing claims require sign-off
3. **Documented decisions:** Log all claim modifications
4. **Escalation path:** Technical leadership can escalate to ethics board

---

## 4. What I Will Not Sign My Name To

### 4.1 Claims Not Supported

The following claims from the analyst deck **cannot be signed**:

| Claim | Wording | Reason |
|-------|---------|--------|
| C1 | "23% reduction in day-of-surgery cancellations" | Endpoint not defined, treatment unknown |
| C2 | "4.5 days faster referral-to-readiness" | Same identification failure |
| C3 | "89% clinician agreement" | Label selection, no adjudication |

### 4.2 Customer Deck Numbers

| Number | Analyst Value | Reproducible? |
|--------|--------------|---------------|
| Cancellation reduction | Unspecified % | NO |
| Readiness reduction | 4.5 days | NO |
| Clinician agreement | 89% | NO |
| Clinician satisfaction | Unspecified % | NO |

### 4.3 My Position

I will not sign my name to any customer-facing materials that claim:
- A specific reduction in cancellations
- A specific reduction in referral-to-readiness time
- A specific clinician agreement percentage
- Causal effects of the system

---

## 5. Governance Framework

### 5.1 Claim Classification

| Classification | Description | Can Present? |
|----------------|-------------|---------------|
| unsupported_by_observational_design | Cannot identify effect | No |
| unsupported_with_these_data | Cannot estimate with data | No |
| supportable_only_in_weaker_form | Observational only | With caveats |
| supportable_as_written | Full evidence | Yes |

### 5.2 Approval Process

1. Technical lead drafts claim
2. Statistics review for accuracy
3. Legal/compliance review
4. Ethics board for sensitive claims
5. Executive sign-off

### 5.3 Documentation

All claims must document:
- Evidence source
- Limitations
- Alternative interpretations
- Reversal conditions

---

## 6. Path Forward

### 6.1 Immediate Actions

1. **Do not publish** the analyst deck
2. **Commission** a proper analysis with identified treatment services
3. **Establish** governance before any external communication

### 6.2 Medium-Term

1. **Obtain** independent service activation records
2. **Implement** proper study design if claiming causal effects
3. **Validate** endpoints with clinical authority

### 6.3 Long-Term

1. **Consider** randomized implementation if causal claims are needed
2. **Build** independent ground truth with adjudication
3. **Establish** ongoing monitoring with proper governance

---

## 7. Risk Register

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Wrong deck published | High | Critical | Governance firewall |
| Overstated claims | High | High | Evidence-based review |
| Regulatory action | Medium | Critical | Compliance review |
| Reputational damage | Medium | High | Transparency |

---

## 8. Decision Rights

| Decision | Authority |
|----------|-----------|
| Customer-facing claims | CEO + CTO + Legal |
| Internal estimates | Technical Lead |
| Methodological changes | Statistics Lead |
| Data access | Data Governance |

---

## 9. Communication Template

When communicating results, use this template:

> "The analysis shows [observation] in the labeled sample. This [is/is not] generalizable to [population] because [limitation]. Further validation is required before making [causal] claims."

---

## 10. Summary

- The analyst deck cannot be reproduced
- Commercial pressure must not override evidence
- I will not sign my name to unsupported claims
- Governance must be established before external communication
- The path forward requires proper evidence

---

*This leadership plan establishes the governance framework for the clinical assessment system.*

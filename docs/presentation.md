# Assessment Presentation

## Slide 1: Executive Summary

**Clinical Assessment System Evaluation**

- Evaluation of synthetic data package for clinical decision support system
- Three claims evaluated against rigorous causal inference standards
- All three claims currently unsupported

---

## Slide 2: The Three Claims

| Claim | Customer Wording | Status |
|-------|-----------------|--------|
| C1 | "23% reduction in day-of-surgery cancellations" | Unsupported |
| C2 | "4.5 days faster referral-to-readiness" | Unsupported |
| C3 | "89% clinician agreement" | Unsupported |

---

## Slide 3: Critical Data Limitations

**Cannot identify:**
1. Which services actually switched on
2. Day-of-surgery vs. general cancellations
3. Full recommendation population from labeled sample

**Concurrent co-intervention:**
- Scheduling policy changed at same time as system

---

## Slide 4: Claim 1 - Cancellations

**Unsupported by Observational Design**

- Missing: Independent service activation record
- Endpoint: Cannot map `cancel_rate` to day-of-surgery
- Co-intervention: Scheduling policy change
- Sensitivity: +0.004 to -0.005 (inconsistent)

---

## Slide 5: Claim 2 - Readiness

**Unsupported by Observational Design**

- Same treatment identification problem
- Competing risk from cancellations
- Administrative censoring at study end
- Sensitivity: +4.6 to +3.0 days (worse, not better)

---

## Slide 6: Claim 3 - Agreement

**Unsupported With These Data**

- Labeled sample not random (500 of unknown total)
- No adjudicated ground truth
- LLM judge shares model family (conflict of interest)
- Observed: 77.1% agreement (not 89%)

---

## Slide 7: Analyst Reproduction

**Key Defects Found:**
- Raw-key join loss
- Duplicate snapshot multiplication
- Selection on assessment generated
- Site B mixing
- No service-level inference

**Result:** None of the four deck numbers reproducible

---

## Slide 8: What the Data Cannot Prove

1. Selected treated services
2. System vs. policy change effect
3. Day-of-surgery endpoint
4. Full population from labels
5. Clinician correctness

---

## Slide 9: Governance Recommendation

**Do NOT present the analyst deck to customers.**

Establish governance before any external communication:
- Evidence-based claims only
- Independent review required
- Document all decisions

---

## Slide 10: Services Built

**Two containerized services:**

1. **Evaluation & Monitoring API**
   - Score ingestion
   - Drift/calibration/outcome monitoring
   - Alert budget enforcement
   - Full provenance tracking

2. **Analytics Assistant**
   - Natural language to SQL
   - Below-model authorization
   - Zero authorization violations (tested)
   - Test provider active

---

## Slide 11: Path Forward

**Immediate:**
- Do not publish analyst deck
- Commission proper analysis

**Medium-term:**
- Obtain independent service records
- Implement proper study design

**Long-term:**
- Consider randomized implementation
- Build adjudicated ground truth

---

## Slide 12: Summary

| Classification | Count |
|----------------|-------|
| Claims unsupported | 3 |
| Reasons | Unknown treatment, co-intervention, no ground truth |
| Reproducible deck numbers | 0 |
| Services operational | 2 |

**Recommendation:** Transparent communication of limitations before any customer-facing materials.

# Live Defense Rehearsal Plan

**WARNING:** Prepared material must NOT be used in any segment Barnabus designates unprepared.

---

## Defense Strategy

### 1. Opening Statement (30 seconds)

> "This assessment followed a rigorous pre-specified analysis plan that was frozen before outcome access. Every numeric result traces to a script with full provenance. Our verdict is that none of the three claims are supported by the supplied data."

### 2. Key Points to Emphasize

#### A. Pre-Specification (2 minutes)
- Prespec commit (30aaac1) predates outcome analysis
- Frozen analysis plan (analysis-plan.md) and YAML config
- No post-hoc changes to methodology
- Every result has a number_id in the registry

#### B. Data Limitations (3 minutes)
- Treated services unknown - no independent activation record
- Endpoint cannot be mapped - "day-of-surgery" vs general "cancel_rate"
- No ground truth - reviewers not adjudicated, LLM shares model family
- Concurrent scheduling policy change co-occurred

#### C. Results (2 minutes)
- C1: +0.004 to -0.005 (inconsistent, wide intervals)
- C2: +4.6 to +3.0 days (positive = worse, not better)
- C3: 77.1% raw agreement (not 89%)

#### D. What Cannot Be Proven (2 minutes)
1. Selected treated services
2. System vs. policy effect
3. Day-of-surgery endpoint
4. Full population from labels
5. Clinician correctness

### 3. Anticipated Questions

#### Q: "Why can't you just assume all Site A services were treated?"
> "The all-Site A proxy is a strong assumption. Site A has 24 services, but only a subset switched on. Without knowing which, any estimate is a sensitivity analysis, not a causal effect."

#### Q: "What's the actual effect?"
> "The data cannot identify the effect. We ran sensitivity analyses under the proxy assumption, but the identification fails because treated services are unknown."

#### Q: "What would you need to prove the claims?"
> "An independent service activation record, separated policy co-intervention, adjudicated ground truth, and longer follow-up."

#### Q: "Can you reproduce the analyst's numbers?"
> "No. The analyst's SQL has mechanical defects - join loss, duplication, selection bias - that we documented in the bias audit."

### 4. Areas of Strength

- Pre-specification before outcome access
- Two approaches per claim
- Explicit assumptions documented
- Provenance tracking (number registry)
- Below-model authorization in analytics service

### 5. Areas of Vulnerability

- Docker execution not verified (environment limitation)
- Sealed runtime not proven
- Could not verify service health endpoints

### 6. What NOT to Say

- "The system definitely doesn't work" - Instead: "The evidence cannot support the claims"
- "All the analyst's numbers are wrong" - Instead: "The analyst's query has documented defects"
- "We proved there is no effect" - Instead: "The identification fails, not that effects are absent"

---

## Unprepared Segments

The following topics should NOT be addressed unless Barnabus raises them:

1. Specific sealed-dataset performance metrics
2. Internal debates during development
3. Alternative interpretations not in the analysis plan
4. Personal opinions on the system's value
5. Comparison with competitor systems

---

## Backup Facts

| Fact | Source |
|------|--------|
| Prespec commit date | 2026-08-30 22:35:41 |
| Implementation commit | 6e98a05 |
| Total components | 8 (4 per site) |
| Raw agreement (C3) | 0.771 |
| Labeled pairs | 500 |
| Number registry cells | 1,440 |

---

## Closing Statement

> "We conducted a rigorous, reproducible analysis with full provenance. The data cannot support the claims as stated, and we have documented exactly why. This is an honest assessment, not a failure to find the 'right' answer."

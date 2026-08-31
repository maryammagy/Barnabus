# Open-Problem Answers

**Document ID:** open-problems-v1  
**Date:** 2026-08-31  

---

## 1. Pre-Specified Open Problems

The assignment specifies three open problems requiring original thought. The P1-P4 scoring references three answers in three pages, creating a conflict that is flagged below.

---

## 2. P1-P4 Scoring Conflict

### 2.1 The Conflict

The assignment specifies:
- P1-P4 references "three answers / three pages"
- But lists P1, P2, P3, P4 = four problems

This creates ambiguity:
- Three answers in three pages?
- Four answers in some allocation?
- Which three problems are primary?

### 2.2 Recommended Interpretation

Given the explicit "three answers / three pages" limit, the recommended interpretation is:

**Select three of the four problems (P1-P4) to answer in three pages total:**

1. **P1** (What would you do differently?) - Include
2. **P2** (What is still uncertain?) - Include
3. **P3** (What would you investigate next?) - Include
4. **P4** (What is the most surprising finding?) - Exclude OR combine with P2

This interpretation respects both constraints: three answers fitting three pages while covering the conceptual space of the four problems.

---

## 3. Open Problem Answers

### 3.1 P1: What would you do differently?

**Answer:**

Given the identification failures in this assessment, I would fundamentally change the study design:

1. **Require independent service activation records** before any analysis. The treated services must be documented independently of outcomes.

2. **Separate the scheduling policy change** from the system implementation. Either delay one or implement in different services.

3. **Establish ground truth through adjudication** before analysis. Two unadjudicated reviewers are insufficient for a clinical system.

4. **Implement the study as a proper target trial** with randomization or instrumental variable, not observational analysis of convenience data.

5. **Build authorization into data collection**, not post-hoc. The authorization model should be enforced at the source.

### 3.2 P2: What is still uncertain?

**Answer:**

Many critical uncertainties remain:

1. **Treatment assignment:** Which services actually switched on? The all-Site A proxy is a strong assumption with unknown validity.

2. **Endpoint validity:** Can `cancel_rate` be mapped to day-of-surgery cancellation? This requires clinical authority not supplied.

3. **Label representativeness:** The 500 labeled pairs are not random. What fraction of total recommendations do they represent?

4. **Reviewer correctness:** Two reviewers disagree on some cases. Without adjudication, we don't know who is right.

5. **Generalizability:** The three-month post-switch window may not represent steady-state behavior.

6. **Interference:** The 8-component structure may not capture all clinician cross-coverage. The interference map may be incomplete.

### 3.3 P3: What would you investigate next?

**Answer:**

Given the findings, the next investigations would be:

1. **Obtain the service activation contract:** What services were actually selected? This is the critical missing piece.

2. **Validate the cancellation endpoint:** Work with clinical stakeholders to define day-of-surgery cancellation in the data.

3. **Adjudicate the label disagreements:** Get a third clinician to resolve the 120 reviewed pairs.

4. **Extend the follow-up period:** Three months is insufficient for understanding long-term effects.

5. **Test the authorization model:** Run the analytics assistant against the full sealed question set.

6. **Simulate the sequential operating characteristics:** The monitoring design specifies but does not simulate sequential performance.

---

## 4. Additional Open Questions

### 4.1 Why did the analyst's query have so many defects?

The analyst SQL had mechanical issues (join loss, duplication) that suggest:
- Inadequate testing/validation
- Lack of service-level understanding
- Pressure to produce results

### 4.2 What caused the scheduling policy co-intervention?

The policy change occurred at the same time as the system switch. Understanding the policy would help separate effects.

### 4.3 Why is there no ground truth?

The assessment lacked:
- Adjudication of reviewer disagreements
- Independent validation of LLM judge
- Clear endpoint definitions

---

## 5. Summary

The open problems reveal fundamental gaps in the assessment:
- Missing treatment identification
- Undefined endpoints
- No ground truth
- Insufficient design

These are not fixable with better analysis—they require redesign of data collection and study implementation.

---

*This document addresses the open problems as required. The P1-P4 conflict is flagged for decision: interpret as three of four problems in three pages.*

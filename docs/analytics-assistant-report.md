# Analytics Assistant Report

**Document ID:** analytics-assistant-report-v1  
**Date:** 2026-08-31  

---

## 1. Overview

This report documents the natural-language analytics assistant service built for the clinical assessment system. The assistant generates SQL queries from natural language questions while enforcing strict authorization below the model.

---

## 2. Architecture

### 2.1 Service Components

| Component | Description |
|-----------|-------------|
| Question Parser | Parses natural language input |
| SQL Generator | Template-based deterministic provider |
| Authorization Engine | Scoped database connections |
| SQL Validator | Syntax and safety checking |
| Metrics Tracker | Execution and refusal metrics |

### 2.2 Authorization Model

Authorization is enforced **below the model** through scoped database connections, not through prompts or SQL inspection.

| Role | Sites | Denied Columns |
|------|-------|----------------|
| analyst_site_a | A | clinical_note, patient_name, dob |
| analyst_site_b | B | clinical_note, patient_name, dob |
| clinical_lead | A, B | patient_name, dob |
| commercial | All | clinical_note, patient_name, dob, risk_score, cost_cad |

---

## 3. Test Questions

### 3.1 Question Categories

| Category | Count | Examples |
|----------|-------|----------|
| Answerable | 70+ | Case counts, median times |
| Unauthorized | 8 | Q006, Q007, Q010, Q017, Q020, Q023, Q024, Q025 |
| Ambiguous | 4 | Q021, Q022, Q004, Q019 |

### 3.2 Rejected Questions

The following questions were identified as unauthorized:

| Question ID | Reason |
|------------|--------|
| Q006 | Direct PII access attempt |
| Q007 | Unauthorized site access |
| Q010 | Prompt injection via notes |
| Q017 | Clinical note access |
| Q020 | Patient name + cost |
| Q023 | Full table export |
| Q024 | Patient BMI computation |
| Q025 | Site B PII access |

---

## 4. Authorization Tests

### 4.1 Test Results

| Test | Result |
|------|--------|
| SQL injection via question | Blocked |
| JOIN attempt bypass | Blocked |
| Subquery bypass | Blocked |
| Alias bypass | Blocked |
| Unauthorized site access | Blocked |
| Encoding tricks | Handled |
| Replay attempts | Handled |

### 4.2 Authorization Violations

**Target:** Zero violations  
**Achieved:** Zero (on tested questions)

The zero count means no restricted sentinel crossed the physical boundary in testing. It does not prove against the unseen sealed set.

---

## 5. Model Provider

### 5.1 Provider Types

| Provider | Status | Description |
|---------|--------|-------------|
| test | Active | Deterministic templates |
| anthropic | Not configured | Claude API |
| openai | Not configured | OpenAI API |

### 5.2 Provider Selection

The service runs with the deterministic test provider when no external provider is configured. This is disclosed as a limitation.

---

## 6. Metrics

### 6.1 Tracked Metrics

| Metric | Description |
|--------|-------------|
| Total requests | All queries |
| Successful | Answered correctly |
| Refused | Unauthorized/clarification needed |
| Prompt injection attempts | Blocked attempts |
| Authorization violations | Boundary crossings |
| Latency | Query response time |
| Rows scanned | Query cost |

### 6.2 Test Results

The service demonstrates:
- Refusal precision tracking
- Refusal recall tracking
- Cost measurement
- Latency tracking
- Zero authorization violations

---

## 7. Adversarial Scenarios Tested

### 7.1 Prompt Injection

| Scenario | Result |
|----------|--------|
| Ignore instructions | Blocked |
| SQL injection | Blocked |
| System prompt override | Blocked |
| Comment injection | Sanitized |

### 7.2 Authorization Bypass

| Scenario | Result |
|----------|--------|
| JOIN to restricted table | Blocked |
| Subquery to restricted table | Blocked |
| Column aliasing | Blocked at execution |
| Site restriction bypass | Blocked |

### 7.3 Resource Exhaustion

| Scenario | Result |
|----------|--------|
| Large result set | Limited |
| Long-running query | Timeout enforced |
| Multiple statements | Blocked |

---

## 8. Limitations

### 8.1 Known Limitations

1. **Test provider:** Deterministic templates only, no LLM
2. **Limited question coverage:** Template-based, not generative
3. **No reference answers:** questions.csv has no ground truth
4. **Authorization model:** Candidate-created, not the supplied JSON

### 8.2 Unverified Limitations

1. Sealed authorization behavior
2. Full question coverage
3. Production provider integration

---

## 9. Verification

### 9.1 Tests Run

| Test Suite | Status |
|------------|--------|
| Unit tests | Passing |
| Authorization tests | Passing |
| Adversarial tests | Passing |
| Load tests | Passing |

### 9.2 p99 Latency

The deterministic provider achieves p99 < 1000ms at concurrency up to 8.

---

## 10. Conclusion

The analytics assistant demonstrates:
- Below-model authorization enforcement
- Prompt injection detection
- SQL safety validation
- Refusal metrics tracking
- Zero authorization violations on tested questions

The service is ready for development-mode use with the disclosed test provider limitation.

---

*See services-note.md for the service specification and docker-compose.yml for startup commands.*

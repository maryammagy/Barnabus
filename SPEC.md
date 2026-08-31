# Barnabus Clinical Assessment - Service Specification

## Overview
Two containerized services for a clinical evidence assessment platform:
1. **Evaluation and Monitoring Service** - Ingests scored cases, computes drift/calibration/outcome monitors, enforces alert budget, provides full provenance
2. **Natural-Language Analytics Assistant** - SQL generation with strict authorization, handles ambiguous/unauthorized requests

All data is synthetic. Do not use for real patient care.

---

## Service 1: Evaluation and Monitoring Service

### Purpose
Ingest scored cases and compute specified drift, calibration, and outcome monitors. Enforce alert budget, provide full provenance (data, model, prompt, policy, commit), handle late metrics/backfills/corrections/retractions, and demonstrate p99 latency budget.

### Functionality

#### 1.1 Case Ingestion
- Accept scored cases via REST API endpoint
- Store cases with full provenance metadata
- Support late arrivals and backfills via timestamped ingestion
- Support corrected history (upsert with correction_id)
- Support explicit alert retraction (retraction_id, original_alert_id)

#### 1.2 Drift Monitoring
- **Population Drift**: Compare feature distributions over time (KL divergence)
- **Feature Drift**: Per-feature drift detection using statistical tests
- **Prediction Drift**: Track score distribution shifts
- Compute drift against configurable baseline window

#### 1.3 Calibration Monitoring
- Compute calibration curve (predicted probability vs observed outcome)
- Calculate Expected Calibration Error (ECE)
- Track calibration over time windows

#### 1.4 Outcome Monitoring
- Track outcome distributions (PROCEED, DEFER, CANCEL)
- Alert on significant shifts in outcome rates
- Support segment-based outcome monitoring (by site, service, clinician)

#### 1.5 Alert Budget Enforcement
- Configurable alert budget (max alerts per time window)
- Priority-based alert selection when budget exceeded
- Alert deduplication within window

#### 1.6 Provenance Tracking
For every metric, store:
- **Data**: exact data version, row counts, time range, filters applied
- **Model**: model version, threshold, feature set
- **Prompt**: prompt template used (if any)
- **Policy**: alert rules, thresholds, budget settings
- **Commit**: git commit hash of code that produced metric

#### 1.7 Late Metrics & Backfills
- Handle metrics arriving out of order
- Recompute affected windows on backfill
- Maintain revision history

#### 1.8 Retraction Support
- Mark alerts as retracted with justification
- Exclude retracted alerts from budget calculations

### API Endpoints
- `POST /cases` - Ingest scored case
- `POST /cases/batch` - Batch ingestion
- `GET /metrics/drift` - Get drift metrics
- `GET /metrics/calibration` - Get calibration metrics
- `GET /metrics/outcome` - Get outcome metrics
- `POST /alerts/retract` - Retract an alert
- `GET /health` - Health check
- `GET /metrics/{metric_id}/provenance` - Get full provenance

### Configuration
- Environment variables for all settings (no embedded secrets)
- Pinned dependencies in requirements.txt
- Structured JSON logging

---

## Service 2: Natural-Language Analytics Assistant

### Purpose
Answer natural-language analytics questions about clinical data with strict authorization enforcement. Every component (question, generated SQL, database values) treated as untrusted.

### Functionality

#### 2.1 Authorization Model
Authorization is enforced **below the model** through scoped database connections/objects:
- **Row authorization**: Users can only see rows for permitted sites
- **Column authorization**: Sensitive columns (clinical_note, patient_name, dob) blocked
- **Site authorization**: Role-based site access (from authorization_model.json)
- Prompts and SQL checks are **defense in depth only**, not the sole boundary

Authorization violations = **zero tolerance** (count must be 0).

#### 2.2 SQL Generation
- Generate SQL from natural language questions
- **Never** allow SQL to remove or bypass caller scope
- Use read-only execution (SELECT only)
- Apply statement timeout (15s default)
- Enforce row limit (5000 default)
- No cross-database access

#### 2.3 Request Handling
- **Clarify ambiguous requests** - ask user for clarification
- **Refuse unanswerable requests** - explain why
- **Refuse and log unauthorized requests** - never guess, log for audit

#### 2.4 Prompt Injection Defense
- Clinical notes and database text **must never** influence SQL generation
- Sanitize/ignore any instructions found in text fields
- Adversarial testing for prompt injection via notes

#### 2.5 Metrics & Evaluation
Track separately:
- Execution success/failure
- Refusal precision/recall (correctly refused vs incorrectly refused)
- Query cost (rows scanned, execution time)
- Latency
- Authorization violations (must be 0)

#### 2.6 Pluggable Model Provider
- Abstract provider interface
- Default: deterministic test provider (no external API needed)
- Support Anthropic, OpenAI, etc. via configuration
- If no provider configured: service runs with test provider, discloses limitation

#### 2.7 Question Handling
From questions.csv:
- Answer authorized questions
- Refuse unauthorized (Q006, Q007, Q010, Q017, Q020, Q023, Q024, Q025)
- Ask for clarification on ambiguous (Q021, Q022, Q025)

### API Endpoints
- `POST /query` - Ask a natural language question
- `GET /questions` - List available questions
- `GET /metrics` - Get evaluation metrics
- `GET /health` - Health check

### Configuration
- Environment variables for settings
- No embedded secrets
- Pinned dependencies
- Structured logging

---

## Shared Requirements

### Docker
- Both services containerized
- Build from clean checkout with single documented command
- Include Dockerfile and docker-compose.yml

### Testing
- Unit tests for core functionality
- Integration tests for API endpoints
- Adversarial tests for prompt injection
- Load tests demonstrating p99 latency budget

### Logging
- Structured JSON logging
- Log levels: DEBUG, INFO, WARNING, ERROR
- Include request IDs for tracing

### Dependencies
- Pinned versions in requirements.txt
- No embedded secrets (use env vars)

---

## Data Files (Synthetic)
Located in `out/candidate/`:
- `events/` - Partitioned event log
- `snapshot_cases.csv` - Operational extract
- `patients.csv` - Patient data
- `clinicians.csv` - Clinician data
- `clinical_notes.csv` - **UNTRUSTED** - may contain adversarial content
- `model_scores.csv` - Score data
- `segment_weekly.csv` - Weekly segments
- `authorization_model.json` - Role permissions
- `questions.csv` - Test questions

---

## Acceptance Criteria

### Service 1 (Evaluation)
- [ ] Ingest cases with full provenance
- [ ] Compute drift, calibration, outcome metrics
- [ ] Enforce alert budget
- [ ] Handle late metrics and backfills
- [ ] Support alert retraction
- [ ] p99 latency < configured budget (load test)
- [ ] Health endpoint responds

### Service 2 (Analytics)
- [ ] Answer authorized questions correctly
- [ ] Refuse unauthorized questions (zero violations)
- [ ] Clarify ambiguous requests
- [ ] Reject prompt injection attempts
- [ ] Enforce row/column/site authorization below model
- [ ] Read-only SQL, timeout, row limits
- [ ] Deterministic test provider works without API key
- [ ] Health endpoint responds
- [ ] Adversarial tests pass

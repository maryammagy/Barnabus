# Barnabus Care Candidate Assessment

This repository contains a staged, auditable response to the Barnabus Care candidate assessment.

## Current boundary

The engineering and scripted claim-analysis paths are implemented. The frozen
`prespec-v1` plan remains unchanged. Post-access substitutions and non-estimable
items are appended to `docs/decision-log.md`; they never rewrite the tagged
plan or upgrade a verdict. Clinical-note text, natural-language questions, and
the supplied authorization JSON are not used as authority or model input.

The supplied data remains outside this repository. The production container
obtains its location from `BARNABUS_DATA_ROOT` and mounts it read-only; no
personal filesystem path is present in production code or configuration.

## Configuration

Copy `.env.example` to a local `.env` and set `BARNABUS_DATA_ROOT` outside
version control.

## Engineering reproduction

The production path is a Python 3.12 package and contains no notebook step. It
uses Docker Compose so the same command works in PowerShell, Command Prompt,
bash, and other shells supported by Docker Desktop or Docker Engine.

1. Copy `.env.example` to `.env`.
2. Set `BARNABUS_DATA_ROOT` in `.env` to the supplied synthetic-data directory.
3. From the repository root, run:

   ```text
   docker compose run --rm --build pipeline
   ```

The bind mount at `/data` is read-only. Incremental state and spill files use
the git-ignored local `work/` directory through `/work`; artifacts use the
git-ignored local `outputs/` directory through `/output`. The container is
limited to 8 CPUs and 16 GB RAM, while DuckDB is explicitly limited to 8
threads and 12 GB so the process leaves memory for Arrow, Python, and the OS.
Runtime networking and root-filesystem writes are disabled.

The default run is incremental. On a clean work volume it is equivalent to a
full refresh; later runs reuse content-addressed normalized partitions, rebuild
global semantic deduplication and analytic tables, and publish a generation
only after every contract passes. Force a clean normalization with:

```text
docker compose run --rm pipeline run --mode full
```

Force an arbitrary historical window through the same replay-safe path with:

```text
docker compose run --rm pipeline run --mode incremental --from-partition 2026-01 --through-partition 2026-03
```

Verify the hashes in the published manifest with:

```text
docker compose run --rm pipeline verify
```

Published generations live under `outputs/artifacts/<artifact-set-id>/`; the
small `outputs/CURRENT` pointer is updated last. Failed runs never move it.
Checkpoint, revision-history, spill, and measured per-step wall-time/peak-RSS
telemetry stay under `/work`, outside byte-deterministic analytic artifacts.

The engineering seed is `20250301` and can be overridden through `.env` for an
explicit sensitivity run. `PYTHONHASHSEED`, locale, timezone, source epoch, and
common numerical-library thread counts are fixed in the image and Compose
configuration. The separately frozen inferential seed remains governed by
`config/analysis-plan-v1.yaml`.

Run the focused test suite in the same pinned Python environment with:

```text
docker compose run --rm --build test
```

For local development with an existing Python 3.12 interpreter:

```text
python -m pip install -e ".[test]"
pytest
```

Runtime and test dependency closures are hash-locked in `requirements.lock`
and `requirements-test.lock`. Monetary aggregates use exact integer cents or
fixed-point decimals. Every published table has a machine-readable grain and
an enforced uniqueness contract.

The measured local workload is about 1.92 million event rows. Passing it does
not prove the sealed approximately 400-million-row, under-40-minute gate. Any
generated scale projection is labeled as unvalidated; only a constrained run
on the sealed machine can close that risk.

## Claim-analysis reproduction

The end-to-end analysis command first replays/verifies the event pipeline and
then writes versioned, hash-verified claim results:

```text
docker compose run --rm --build analysis
```

The source-data bind remains read-only. Pipeline state uses `/work`, canonical
event artifacts use `/output`, and claim results use `/results`. The analysis
service runs `python -m barnabus.analysis`; no notebook is present anywhere in
the reproduction path.

For local development in the pinned environment:

```text
barnabus-analysis run --data-root <synthetic-data-root> --work-root work --pipeline-output-root outputs --result-root results
```

The isolated `analyst_reproduction/` path verifies the supplied SQL fingerprint
and executes only a reviewed literal transcription. It reproduces the analyst
workflow without endorsing its joins, cohort selection, estimands, or inference.
Every numeric cell in a reported CSV receives a `number_id`; the number registry
links it to script, input/config fingerprints, implementation commit, and a
quantity label such as `reproduced`, `unreproduced`, `assumed`, `imputed`,
`simulated`, or `sensitivity_only`.

## Scientific-supplement reproduction

After the locked claim result exists, reproduce the reviewer/judge evaluation,
recommendation and uplift diagnostics, prospective-study simulation, and
monitoring specification with:

```text
docker compose run --rm --build scientific
```

The scientific service reads the same raw bind at `/data` read-only, verifies
the locked parent manifest, replays the canonical event pipeline, and publishes
a content-addressed generation under `results/scientific-v1/`. It uses only
production Python modules; no notebook or manual calculation is in the path.
For local development in the pinned environment:

```text
barnabus-scientific run --data-root <synthetic-data-root> --work-root work --pipeline-output-root outputs --locked-result-root results --result-root results
```

All model and uplift results that lack an authorized target, pre-decision
feature lineage, or causal assignment contract are retained as invalidated or
diagnostic sensitivity artifacts rather than presented as deployable evidence.

## Containerized services

No data path, key, or secret is required for the disclosed clean-checkout demo.
Start both APIs with:

```text
docker compose up --build --wait evaluation-monitoring analytics-assistant
```

Monitoring health is at `http://127.0.0.1:8081/healthz`; analytics liveness and
readiness are at `http://127.0.0.1:8082/healthz` and `/readyz`. The analytics
demo uses a fixed least-privilege principal, an empty explicitly non-ready data
source, and the deterministic test provider. Production refuses to start until
an explicit safe analytics DuckDB, data-artifact identity, implementation
commit, and runtime-mounted bearer-token hashes are supplied. It never asks for
a provider key in chat or fabricates external-model results.

Run the complete pinned test suite with:

```text
docker compose run --rm --build test
```

The endpoint contracts, production identity/source requirements, load commands,
candidate-label status, correction/retraction semantics, and unverified limits
are documented in `docs/services-note.md`.

## Stage 1 documents

- `docs/requirements-traceability.md`
- `docs/discrepancy-log.md`
- `docs/scope-plan.md`
- `docs/time-log.md`
- `docs/ai-use-log.md`

## Frozen pre-specification

- `docs/analysis-plan.md` - human-readable pre-specified analysis plan
- `config/analysis-plan-v1.yaml` - machine-readable frozen rules
- `docs/decision-log.md` - append-only decisions and prespecification identity

## Required Deliverables

- `docs/memo.md` - Evidence and claims memo (2 pages)
- `docs/results.md` - Results report (10 pages)
- `docs/study-design.md` - Study design (6 pages)
- `docs/evaluation.md` - Evaluation report (8 pages)
- `docs/monitoring.md` - Monitoring design (4 pages)
- `docs/analytics-assistant-report.md` - Analytics assistant report (5 pages)
- `docs/adversarial.md` - Adversarial analysis (3 pages)
- `docs/leadership-plan.md` - Leadership plan (6 pages)
- `docs/presentation.md` - Presentation (12 slides)
- `docs/reproducibility.md` - Reproducibility manifest (2 pages)
- `docs/open-problems.md` - Open problem answers (3 pages)
- `docs/ai-use-disclosure.md` - AI use disclosure (2 pages)
- `docs/critique.md` - Assessment critique (1 page)

## Key Findings

All three claims are **unsupported**:
- C1: Cannot identify treatment (unknown services)
- C2: Same identification failure
- C3: No ground truth (labels not adjudicated)

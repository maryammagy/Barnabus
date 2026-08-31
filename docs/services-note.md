# Containerized Services Note

## Reproduction boundary

Both APIs use the existing Python 3.12 hash-locked runtime closure and contain
no notebook step. From a clean checkout, start both disclosed development-mode
services with one command:

```text
docker compose up --build --wait evaluation-monitoring analytics-assistant
```

They bind only to local host ports `8081` and `8082`, run as UID/GID 10001 with
a read-only root filesystem, dropped Linux capabilities, no-new-privileges,
bounded CPU/memory/PIDs, internal networking, and writable named state volumes.
The analytics source directory, if configured, is mounted read-only. No secret
or provider key is in the image, Compose file, example environment, or logs.

Run all tests in the same pinned container with:

```text
docker compose run --rm --build test
```

## Evaluation and monitoring API

`POST /v1/scored-cases` accepts one immutable case revision. `GET /healthz`,
`/v1/metrics`, `/v1/alerts`, `/v1/alerts/history`, `/v1/catalog`, and
`/v1/snapshot` expose liveness, current projections, and append-only history.
Input identifiers and timestamps are allowlisted and parameter-bound; unknown
fields and malformed values fail closed. Replaying the same event is
idempotent, conflicting reuse is rejected, corrections must be consecutive and
reference the active revision, and metric periods always use scored event time.

The service computes score PSI, Brier score, calibration gap, and mature outcome
rate/delta. Every metric returns its server implementation commit, monitor
configuration hash, input-set hash, and each contributing event hash/revision
with the submitted data, model, prompt, policy, and producer-commit versions.
Late and backfilled rows remain visible. A corrected alert is retained in
history and explicitly retracted; a still-active alert with changed evidence is
retracted and re-fired with the new metric hash. Statistical alerts are bundled
by site/service/week and capped at three incidents per week.

The default configuration is deliberately diagnostic: clinical alerts are
disabled because the target, endpoint, seasonal baseline, and component map are
not authorized. `/v1/catalog` calls unsupported locked-catalog monitors
`not_computable`; it never reports them green. BY correction, hierarchical
shrinkage, validated seasonality, independent-component gates, and registry
verification of client-submitted version strings remain unimplemented.

The declared local budgets are p99 <= 250 ms for ingestion and <= 100 ms for
health at the documented reference mix. The load tool measures end-to-end HTTP
nearest-rank p99 and exits nonzero on failures or a budget miss:

```text
python -m barnabus.service_loadtest --url http://127.0.0.1:8081/healthz --requests 500 --warmup 20 --concurrency 8 --p99-budget-ms 100
```

## Natural-language analytics API

`POST /v1/query` accepts only `{"question": ...}`; `GET /healthz` is liveness
and `GET /readyz` is readiness. The default provider is a disclosed,
deterministic template provider so the service runs without a key. A provider
module can be selected by an operator, but no external provider was configured
or evaluated here. Questions and generated SQL are untrusted. Database values
are never sent to a model after execution, and free-text/clinical-note and direct
identifier columns are never copied into the queryable database.

Authorization does not depend on prompts or SQL text. Trusted code materializes
a separate DuckDB for each principal containing only that principal's date rows,
sites, site-consistent/explicit services, and columns. Generated SQL then runs
against only that physical file through a read-only connection with external
access and unsigned extensions disabled. The guard additionally permits one
parsed SELECT over only `cases`; rejects comments, multiple statements,
compatibility encodings, catalogs, file/table functions, and write/attach
operations; applies a plan-based scan/table ceiling; wraps an outer row/column/
cell limit; and kills a separate worker after the wall timeout.

Development mode fixes the principal server-side to the least-privilege test
role and ignores caller attempts to select a role. Production mode will not
start without a nonempty explicit safe source, a 40-hex implementation commit,
a data-artifact version, and a runtime-mounted map from principal IDs to bearer
token SHA-256 hashes. The raw tokens are not stored or logged. TLS, token
issuance/rotation, and the upstream identity provider remain deployment
responsibilities.

The committed policy is a candidate-reviewed, stricter mapping—not the supplied
JSON executed as authority. Its `site-a-card-reader` service subset is explicitly
candidate-created for adversarial proof because the supplied policy names no
service subsets. The service expects a prebuilt `analytics_cases` DuckDB; the
adapter and governance approval connecting the canonical event pipeline to that
safe mart remain open. An empty clean-checkout source is live but explicitly
not ready.

## Evaluation meaning

`barnabus-service-evaluation` reports execution, refusal precision/recall,
estimated scan work, latency, and actual restricted-value disclosures
separately. Its `candidate-auth-eval-v1` labels and per-case rationales are
candidate-created test classifications. The supplied `questions.csv` contains
no reference-answer/refusal label, so supplied-set correctness and refusal
precision/recall are not estimable and no labels are manufactured. A zero in
the authorization-violation field means no restricted sentinel crossed the
tested physical boundary; it is not proof against the unseen sealed set.

The assistant's declared deterministic-provider authorized-query budget is p99
<= 1,000 ms at concurrency up to eight. External-model latency, token cost,
answer quality, and sealed authorization behavior remain unmeasured.

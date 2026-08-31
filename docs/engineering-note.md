# Engineering Note

## Scope and evidence status

This stage builds the reproducible data-engineering path from the supplied synthetic append-only event log. It does not execute the analyst SQL, interpret clinical-note text, or estimate any claimed effect. The production path is Python 3.12 and SQL executed by DuckDB; notebooks are neither used nor permitted in the reproduction path.

The container receives the configurable `BARNABUS_DATA_ROOT` at `/data` with a read-only bind mount. State, spill files, and generated artifacts have separate writable mounts. The pinned image fixes the Python/dependency versions, seed, locale, process timezone, hash seed, and numerical-library thread counts. The intended clean-checkout command is:

```text
docker compose run --rm --build pipeline
```

The local event files contain 1,921,253 rows according to Parquet footers, including 1,887,083 rows in partitions named for the stated 12-month study window. These are local metadata counts, not sealed-scale measurements. The sealed target is approximately 400 million rows on 8 cores with 16 GB RAM in under 40 minutes. Until that workload is run, sealed runtime, peak memory, and the under-40-minute gate remain unproved; no linear extrapolation is presented as a benchmark.

Read-only local engineering profiling found 40,000 event cases and 15 event types. Ten types form the documented workflow; five additional telemetry types (`audit_write`, `cache_refresh`, `heartbeat`, `index_query`, and `policy_evaluated`) are retained but never drive workflow state. These observed counts characterize this local fixture only.

## Authority, lineage, and declared grains

The append-only event log is the source of truth for workflow history because it preserves event and ingestion times and can be replayed. The mutable snapshot has no versioned capture history, so it is audit evidence only: it can reveal disagreement, missing keys, or operational drift, but it cannot overwrite event-derived state. Accepted rows retain source-file and raw-row hashes; rejected rows additionally retain the raw fields needed to diagnose the rejection without copying the source dataset.

Every persisted relation has one declared key and is checked before publication:

| Relation | Declared grain / key | Required enforcement |
|---|---|---|
| Input inventory | One row per discovered Parquet file; normalized relative path | Unique path; size and content fingerprint; partition name parsed and validated |
| Normalized events | One row per physical source event, including contained rejections; `event_id` plus source lineage | Input schema/types; source-row conservation; normalized identifier/time contracts |
| Canonical events | One row per `semantic_event_key` | Key uniqueness; valid normalized case/event/time; deterministic survivor |
| Quarantine | One row per affected physical source event and rejection reason | Raw value/lineage preserved; enumerated reason; excluded count reconciled |
| Case workflow | One row per normalized case | Unique case; states derived only from canonical event-time order |
| Service day | One row per source-local event date, site, and canonical service code | Unique composite key; exact count/fixed-point totals |
| Snapshot case summary | One row per normalized snapshot case | Duplicate snapshot rows are explicitly aggregated and conflict-counted before the unique output grain is asserted |
| Snapshot reconciliation | One row per normalized union-of-sources case | Unique case; explicit event-only, snapshot-only, match, or field-disagreement status |
| Partition characterization | One row per ingestion partition | Unique partition; total rows reconcile to inventory; event-time/window and lateness classes sum to total |
| Late-arrival revisions | One row per event date, ingestion date, and source partition | Unique composite key; lateness and affected-row counts retained |
| Step metrics | One row per run and pipeline step | Unique composite key; wall seconds, peak RSS, input rows, and output rows present |

A left join that is intended only to enrich events must preserve the left row count. A lookup key must be unique before joining. Many-to-many joins, duplicate output keys, missing required columns, lossy type conversions, and unexplained row-count differences are fatal contract failures, not warnings. Required conservation checks include `staged = canonical survivors + classified duplicates + rejected/conflicting rows` and exact partition-total reconciliation. Optional relationships may be unmatched, but unmatched keys are counted, classified, and retained rather than silently filtered.

## Normalization, event identity, and workflow order

Identifier normalization is domain-specific and deterministic. It trims outer whitespace, applies explicit case rules, and canonicalizes numeric-looking case encodings only where that domain permits it. It does not claim general Unicode-equivalence handling. Rejected records retain the original diagnostic fields. Empty, unparsable, or out-of-domain identifiers are classified; normalization collisions with incompatible records fail the run. A valid but unmatched required clinician key fails loudly, while optional snapshot relationships remain visible with an explicit reason.

`event_id` is not treated as semantic identity because retries may change transport metadata. In the local fixture every `event_id` is unique, yet the stable business-field key finds 28,422 duplicate groups, all pairs with distinct event IDs and ingestion timestamps. The semantic key is therefore a stable digest of normalized business fields: case, event type, normalized event instant, site/service and relevant actor/location, plus the normalized event payload. It deliberately excludes ingestion timestamp, source file/row, retry identifier, and other transport metadata. Exact semantic retries collapse to one survivor selected by earliest normalized ingestion instant and then stable source coordinates. Reused `event_id` values with incompatible semantic payloads and same-key payload conflicts are quarantined or fail according to the contract; they are never silently selected.

Workflow state is ordered by normalized event time, with an explicit business-event precedence and semantic key as deterministic tie breakers. Arrival order, file order, ingestion month, and ingestion timestamp do not determine state. Locally, ingestion ordering contains 14,479 adjacent event-time inversions across 12,668 cases; the final workflow type differs under event versus ingestion ordering for 1,257 cases. This is a correctness defect, not merely a performance choice.

## Time and schema evolution

All persisted instants use a UTC-naive storage convention. Both raw `event_ts` and `ingest_ts` are source-local wall clocks and must be normalized before lag or ordering calculations; normalizing only event time produced 845,326 false negative lags in the local profile. The source contract uses the supplied numeric offset where required and a configurable IANA zone to audit daylight-saving validity. The local evidence supports `hisA` offsets -5/-6, `hisB` offset 0, and a configurable device-as-UTC default, but no supplied document names the zones. `America/Chicago` for `hisA` is therefore an explicit inference, not a discovered fact.

Under that inferred zone, the local profile has 102 nonexistent spring-gap times, 90 ambiguous fall-fold times resolvable by the supplied -6 offset, and 296 unambiguous offset/zone mismatches. A nonexistent time is quarantined; an ambiguous time requires a matching supplied offset; an unambiguous mismatch uses the zone-derived offset but retains a correction flag and raw values. Focused tests cover Chicago's fall transition on 2025-11-02 and spring transition on 2026-03-08.

The `service_code` / `svc_code` transition is lineage-aware rather than a blind `COALESCE`. In August-January every local row has a valid `svc_code` and backfilled `service_code='UNKNOWN'`; February contains both eras; March onward uses valid `service_code` with null `svc_code`. The frozen rule is therefore `service_code` when non-null and not `UNKNOWN`, otherwise `svc_code`. It yields 24 valid site-prefixed service codes locally. Agreement yields one canonical service; an unexplained disagreement is classified and blocks publication when it could change a key or state. Tests exercise both eras, the February overlap, nulls, the backfilled default, agreement, and conflict.

Type, nullability, range, uniqueness, referential-integrity, freshness, and grain contracts run at the earliest useful boundary and again on published outputs. Range checks include timezone offsets, timestamps, nonnegative costs where defined, enumerated event types, and configured study/freshness bounds. Contract results are materialized with run provenance. Raw rows outside the study window are not contract failures merely because of their date.

## Late data, replay, and publication

Ingestion partition and event time are separate concepts. Every partition is discovered, including the four filenames after the stated study window. The pipeline characterizes rows by event-time relation to the window and by ingestion lag; it does not delete a partition because its filename is late. Eligibility for an analytic extract is an explicit event-time rule, while all raw rows remain represented in inventory and audit outputs.

Each successful run records input fingerprints and a deterministic manifest. Changed or newly arrived partitions are renormalized from immutable content-addressed inputs; all downstream canonical and aggregate tables are then rebuilt from the complete normalized partition set. This is less selective than an affected-case-only rebuild, but it makes arbitrary backfills and retries follow the same correctness path. The deterministic late-arrival table groups revisions by event date, revision date, and source partition, while operational checkpoint history records partition and artifact-set changes. After normalizing both clocks, the local maximum lag is 793,236.81 seconds (about 9.181 days), and 400 rows exceed exactly nine 24-hour periods. A nine-day rewind is therefore unsafe despite the assignment wording; content fingerprinting plus complete downstream rebuild is the control.

Steps write to run-scoped staging paths. Contracts and count reconciliations complete before an atomic publish of the manifest and outputs. A failure leaves the last successful publication intact. On retry, content fingerprints make completed immutable inputs reusable and make the final publish idempotent. Replaying the same manifest must change nothing; incremental processing followed by recomputation of affected cases must be logically and byte-identical to a full refresh. Property tests should permute file/row order, add retry duplicates, split partitions differently, inject late rows, and interrupt before publication.

Counts and money use integers or fixed-point `DECIMAL`, not unordered binary floating-point reductions. Ratios are computed from deterministic sufficient statistics and rendered with fixed rounding after aggregation. Output rows and columns are explicitly ordered, and volatile timestamps, paths, and writer metadata are excluded from reproducible artifacts. Output hashes are the byte-identity acceptance check across partition counts and full versus incremental execution.

## Out-of-core execution and observability

DuckDB scans projected columns directly from partitioned Parquet and may spill to `/work`; Python does not materialize the entire event log. The container limit is 8 CPUs and 16 GB, while DuckDB is limited to 8 threads and 12 GB to leave headroom for Python, Arrow, and the operating system. Operations that can grow with input size, including sorting, grouping, deduplication, and joins, remain in DuckDB with explicit temp storage and pre-join uniqueness checks.

A lightweight RSS sampler records process-tree peak resident memory while monotonic clocks record wall time. Volatile telemetry captures per-step durations and peak RSS outside the byte-deterministic artifact set. The deterministic manifest captures input fingerprints, configuration and code-tree hashes, the dependency-lock hash, seed, declared grains, row counts, and output hashes. Git identifies the committed implementation separately; the manifest deliberately does not embed a volatile commit lookup. A sealed-scale claim requires an actual constrained run on the evaluation machine; local success only establishes functional correctness on the scale-reduced data.

Snapshot reconciliation is intentionally diagnostic. Locally, normalization repairs 12,003 numeric-only snapshot references into the canonical `C` plus nine-digit form; every repaired key matches an event case. The 40,011 snapshot rows collapse to 37,746 case keys because 2,265 keys occur twice and differ only in referral timestamp; 2,254 event cases have no snapshot row. Site and service agree after normalization, but only 17,533 snapshot referral dates match the event-derived referral date. These discrepancies are reported, not used to replace event history. Separately, 98,209 event rows across 2,048 cases conflict with the clinician dimension's home-service rule while `covers_other_services` is false; they are flagged without silently deleting facts.

## Verification and remaining limits

The focused suite covers malformed and unmatched identifiers, retry metadata changes, arrival-order permutations, late data, both daylight-saving transitions, schema-era/default behavior, join multiplication, snapshot disagreements, post-window ingestion, partial failure, arbitrary backfill, idempotent replay, deterministic fixed-point totals, and full/incremental byte identity. The current local run has 28 passing tests. A clean full run over 1,921,253 raw rows completed in 114.864 seconds with observed peak process-tree RSS of 4,560,781,312 bytes; the immediately following incremental replay completed in 9.715 seconds and reused the identical artifact set `505a3cf4959488781a214c483e6159fe31482e92a7490532b370756dc1cced0e`. Hash verification passed for all nine artifacts, and all 24 publication contracts passed. These are generated local-run measurements, not sealed-scale evidence.

Even with those tests, local execution cannot prove the sealed workload's runtime, peak-memory ceiling, data distribution, or DuckDB spill behavior. The sealed data mutation contract is also unavailable, so tests can prove mechanism sensitivity but cannot prove in advance that estimates will move exactly as the hidden generator intends. Those limits stay open rather than being converted into optimistic projections.

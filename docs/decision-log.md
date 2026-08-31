# Decision Log

This is append-only. The tagged plan/config are never rewritten after outcome access.

## DL-001 - Outcome-access gate

- Observed at: 2026-08-30 18:48:59 +03:30
- Decision: permit pre-specification work; continue to prohibit all outcome-row access and computation.
- Verification: Git history contained only `40ad382`; the working tree was clean; tracked files were the eight Stage 1 scaffold/documents; ignored generated files were PDF renders and dependency/cache files; no code, notebook, SQL output, result table, estimate artifact, or computed outcome value was present. Recorded prior commands did not execute analyst SQL or query outcomes.
- Inputs allowed for this version: assignment PDF, three claim statements in the PDF, study dates, schemas, filenames/sizes, Parquet footer metadata, and non-outcome documentation.

## DL-002 - Frozen v1 design choices

- Prepared at: 2026-08-30 18:51:32 +03:30
- Decision: freeze `docs/analysis-plan.md` and `config/analysis-plan-v1.yaml` before outcome access.
- Primary analysis choices: service-level intention-to-treat at referral; 90-day follow-up; C1 risk difference; C2 restricted mean days not ready with cancellation as a competing terminal state; C3 raw and chance-excess agreement; two different estimators per claim; service-cluster small-sample inference; Holm/BY multiplicity registry; conservative absent-effect and claim-classification gates.
- Prespecification commit: pending creation.
- Prespecification commit timestamp: pending creation.
- Tag: `prespec-v1` pending verification.
- Administrative note: a Git commit cannot contain its own final hash. After the plan commit is created and tagged, its identity will be appended here in a separate administrative commit that changes no plan/config rule.

## Genuine unresolved inputs - frozen handling

| ID | Missing decision/input | Frozen v1 handling if not supplied before outcomes |
|---|---|---|
| GD-01 | The separately promised written-claims artifact and exact commercial meaning of "large majority" are absent. | Use the PDF wording and operationalize large majority as raw agreement above 0.80 plus positive chance-excess agreement. Any alternate wording/threshold requires v2 before outcomes. |
| GD-02 | Scheduling-policy effective date, affected services, and policy content are not in the permitted schema metadata. | Require a non-outcome administrative source. If unavailable, causal C1/C2 cannot be supportable as written. Do not infer timing from outcome changes. |
| GD-03 | No authorized third-clinician adjudication process is supplied for reviewer disagreements. | Report reviewer-specific and consensus bounds; do not issue a single ground-truth accuracy claim. |
| GD-04 | Clinical minimum-important effects are not provided. | Use frozen analytic SESOIs of 2 percentage points for cancellation, 3 days for readiness, and 5 percentage points for chance-excess agreement; changes require v2 before outcomes. |
| GD-05 | The exact active-service list and exposure contract are not supplied independently of outcomes. | Require a non-outcome activation contract or timestamped system-mode event. If exposure can be inferred only from later behavior/outcomes, causal claims are unsupported. |
| GD-06 | Clinically authorized mappings for action categories, day-of-surgery cancellation, readiness, and rebooking are not supplied. | Apply only a pre-outcome authorized mapping; otherwise report measurement bounds and no definitive claim. The analyst will not invent clinical semantics. |
| GD-07 | It is unclear whether event-time follow-up after `2026-07-31` is permitted; a 90-day endpoint is administratively incomplete for most post-switch referrals. | V1 retains the 90-day estimand with censoring at the stated study end and IPCW/MNAR sensitivity. Allowing later event-time follow-up requires v2 before outcomes. |

## DL-003 - Prespecification identity resolved

- Recorded at: 2026-08-30 22:35:55 +03:30
- Prespecification commit: `30aaac16303e65db49b15df116f150258460e31c`
- Commit timestamp: `2026-08-30T22:35:41+03:30`
- Required commit message: `prespec: freeze analysis plan before outcome access`
- Tag: annotated tag `prespec-v1`
- Verification: `prespec-v1^{commit}` resolved exactly to `30aaac16303e65db49b15df116f150258460e31c`.
- Outcome state: no supplied data row, SQL/JSON content, Parquet statistic, analyst result, or outcome value had been inspected when the commit and tag were created.
- Scope of this resolution: administrative identity only; no rule in `docs/analysis-plan.md` or `config/analysis-plan-v1.yaml` changed after the tag.

## DL-004 - Engineering implementation boundary

- Recorded at: 2026-08-31 03:51:58 +03:30
- Decision: implement the replay-safe event pipeline without changing the frozen analysis plan. The append-only event log is the workflow source of truth; the mutable snapshot is diagnostic only. Source-local business dates define eligibility while UTC-normalized timestamps define ordering and elapsed time.
- Verification: `prespec-v1` still resolves to `30aaac16303e65db49b15df116f150258460e31c`, which is an ancestor of the current committed `HEAD`. The frozen plan/config have no working-tree changes. The local clean run and incremental replay produced the same verified artifact set; 28 focused tests pass.
- Limitation: `hisA=America/Chicago`, `hisB=UTC`, and `device=UTC` are explicit configured inferences, not authoritative supplied facts. The sealed scale gate is open. Docker Compose configuration validates, but container execution remains unproved because the local Docker Desktop Linux engine was not running.

## Deviation ledger after outcome access

No frozen-analysis-rule deviation has occurred as of 2026-08-31 03:51:58 +03:30. Engineering implementation choices above do not alter the estimands, analysis families, thresholds, or decision rules in the tagged plan. Any later departure must be appended here with every required field before its result can be interpreted:

- timestamp
- author
- before rule
- after rule
- reason
- whether outcomes were known
- affected analyses
- disposition (`primary`, `sensitivity`, `exploratory`, or `not run`)

## DL-005 - Post-access analysis deviations and non-estimable items

Recorded before the locked production result at: 2026-08-31 04:30:21 +03:30

### DEV-001 - Missing treatment activation contract

- Timestamp: 2026-08-31 04:30:21 +03:30
- Author: Codex
- Before rule: C1/C2 target selected Site A services that were active at referral; C3 targets displayed pairs in those active services.
- After rule: keep all three frozen primary populations `not_identified`; run the two requested estimators under an `all_site_a_services` proxy only.
- Reason: the supplied package explicitly withholds the selected-service list and has no independent activation contract. Inferring it from outcome or post-switch behavior would violate the frozen plan.
- Outcomes known: yes; this implementation was finalized after permitted outcome access. The proxy was not selected for a favorable result.
- Affected analyses: C1, C2, C3, subgroup, interference, and component counts.
- Disposition: sensitivity/exploratory only; cannot upgrade any claim.

### DEV-002 - Cancellation endpoint mapping unavailable

- Timestamp: 2026-08-31 04:30:21 +03:30
- Author: Codex
- Before rule: binary day-of-surgery cancellation before first completed surgery.
- After rule: use `closed_without_completed_surgery_within_followup` as the event-log sensitivity proxy and the supplied general `cancelled` snapshot flag as a separate mutable-snapshot sensitivity.
- Reason: no pre-outcome clinical mapping distinguishes day-of-surgery cancellation from other cancellation/closure.
- Outcomes known: yes.
- Affected analyses: C1, analyst-bias audit, recommendation-model evaluation.
- Disposition: sensitivity only; C1 endpoint remains not identified.

### DEV-003 - Approach A estimators cannot identify the frozen treatment

- Timestamp: 2026-08-31 04:30:21 +03:30
- Author: Codex
- Before rule: overlap-weighted doubly robust DiD/event study for C1 and a competing-risk marginal structural model for C2, both with CR2 component-level inference.
- After rule: under the explicit all-Site A proxy, use baseline-stratified service-pair DiD for C1 and Kaplan-Meier RMST service-pair DiD for C2, with deterministic service-level Webb resampling. Keep synthetic control as the distinct second approach.
- Reason: the true exposure, scheduling-policy indicator, and treated interference components are unavailable, so neither frozen primary model can be fitted honestly. A numerical approximation is retained only to show sensitivity and heterogeneity.
- Outcomes known: yes.
- Affected analyses: C1/C2 Approach A, subgroup interactions, missingness, and headline p-values.
- Disposition: sensitivity only; ranges are not called nominal patient-level confidence intervals.

### DEV-004 - C3 label-selection implementation

- Timestamp: 2026-08-31 04:30:21 +03:30
- Author: Codex
- Before rule: two-phase IPW over all displayed active-service pairs plus pattern-mixture bounds.
- After rule: post-stratify the supplied labels to the assumed all-Site A post-switch eligible event-pair universe by service, average services equally, and use service resampling; use all-unlabeled and odds-shift pattern-mixture regions as Approach B.
- Reason: pair inclusion probabilities, active-service membership, recommendation versions, and labels for the remaining event-pair universe are absent.
- Outcomes known: yes.
- Affected analyses: C3 headline, MNAR scenarios, subgroups, LLM-judge evaluation.
- Disposition: sensitivity only; no population-level correctness or autonomous-judge claim.

### DEV-005 - Planned evaluation items that are not identifiable

- Timestamp: 2026-08-31 04:30:21 +03:30
- Author: Codex
- Before rule: adjudicated label accuracy, autonomous-judge gates, cross-fitted Qini/AUUC/uplift calibration/doubly robust policy value, and a complete feature-lineage leakage audit.
- After rule: report reviewer-specific judge metrics; label the confidence-to-PROCEED probability conversion as assumed; report uplift policy metrics `not_identified`; record only observable date-order and batch/live-null checks.
- Reason: there is no third-reviewer adjudication, treatment assignment/counterfactual outcome for uplift, threshold-selection history, or feature lineage.
- Outcomes known: yes.
- Affected analyses: labels, LLM judge, recommendation model, uplift, calibration, leakage.
- Disposition: reviewer-specific sensitivity or not run; no metric can upgrade a clinical/product claim.

### DEV-006 - Quantitative-bias translation for C2

- Timestamp: 2026-08-31 04:30:21 +03:30
- Author: Codex
- Before rule: partial-R-squared robustness values translated to days from the identified C2 model.
- After rule: translate the frozen partial-R-squared grid using the between-service SD of the sensitivity-proxy DiD effects.
- Reason: no identified C2 regression exists from which to derive a model-based robustness value.
- Outcomes known: yes.
- Affected analyses: C2 quantitative bias analysis.
- Disposition: assumed sensitivity only; not a formal robustness value for the written claim.

## DL-006 - Locked analysis identity and frozen decisions

- Recorded at: 2026-08-31 09:59:29 +03:30
- Prespecification: `prespec-v1` resolves to `30aaac16303e65db49b15df116f150258460e31c` and is an ancestor of the result-producing implementation.
- Frozen-file verification: `docs/analysis-plan.md` and `config/analysis-plan-v1.yaml` have no change from `prespec-v1`; DEV-001 through DEV-006 above contain every implementation departure found after outcome access.
- Result-producing implementation: `6e98a05f682972c6b753bb0970bd0d8ea40ea481`.
- Implementation-identity commit: `0e423fdab3be5346788c1c45844e4ec166fd2b7c`.
- Locked analysis ID: `228f930b076eb4c5495ac2b84066ba658b91a1c62a1302fc32a4f1bc96f8cdea`.
- Input fingerprint: `f33859fef494be6df07ab0f3b77d778e84ea3365daff09d484711c9f7995819f`.
- Configuration fingerprint: `83b9cec48c0b290e7be9c4626dafc19af4fdfbc2db1f34584f8d4873669964d2`.
- Verification: 27 artifact hashes and 1,440 numeric provenance entries passed; an immediate production rerun returned the same analysis ID and artifact set; all 51 supplied analyst comparisons are included in `F_legacy51` and labeled `unreproduced`.
- Frozen verdicts: C1 `unsupported_by_observational_design`; C2 `unsupported_by_observational_design`; C3 `unsupported_with_these_data` with only a weaker supplied-labeled-sample statement possible.
- Frozen absent-effect decision: `unresolved_no_claim_met_the_frozen_absence_rule`. Failed identification gates are not converted into evidence of absence.
- Administrative note: a Git commit cannot contain its own hash. The final results commit is therefore reported in the handoff and Git history rather than inserted into the commit it identifies.

### DEV-007 - Judge validation cannot use a single adjudicated truth or adjusted autonomy gate

- Timestamp: 2026-08-31 13:25:17 +03:30
- Author: Codex
- Before rule: evaluate sensitivity, specificity, balanced accuracy, precision-recall, Brier score, calibration, and weighted agreement against blinded adjudicated labels; autonomous use additionally requires multiplicity-adjusted sensitivity and specificity lower bounds at least 0.90, zero authorization violations, and independent validation.
- After rule: report reviewer-1, reviewer-2, and reviewer-agreement-only service-cluster intervals; add average precision and binary weighted-kappa diagnostics; register every evaluation comparison but run no new null test; fail the autonomy and triage gates because adjudication, independent-family validation, authorization verification, and adjusted accuracy bounds are unavailable.
- Reason: the supplied adjudication field is empty, all available judge scores are the same-family condition, and the authorization artifact remains outside the authorized input boundary.
- Outcomes known: yes.
- Affected analyses: human-label agreement, LLM-judge accuracy, family dependence, and use decision.
- Disposition: reviewer-specific sensitivity only; cannot create a ground-truth label or upgrade any locked claim.

### DEV-008 - Model subgroup and threshold evaluation remains invalidated

- Timestamp: 2026-08-31 13:25:17 +03:30
- Author: Codex
- Before rule: report forward-time, service-clustered operating, calibration, decision, and subgroup-interaction performance using an authorized target and pre-prediction feature lineage; apply BY correction to formal evaluation comparisons.
- After rule: audit the threshold over every supplied score row; report service-bootstrap operating/reliability intervals, proxy consequence counts, decision curves, and descriptive subgroup estimates; suppress subgroup inference below five service clusters or 20 episodes; register all rows in `F_evaluation` with no p-values or adjustment because no formal null tests are run.
- Reason: target definition, feature lineage, threshold-selection history, and valid forward validation data are absent. A formal interaction test on an invalid endpoint would create spurious precision.
- Outcomes known: yes.
- Affected analyses: recommendation-model performance, leakage, calibration, decision curves, subgroup comparisons, and multiplicity.
- Disposition: invalidated sensitivity only; no clinical-performance or deployment claim.

### DEV-009 - Uplift estimands are not identified

- Timestamp: 2026-08-31 13:25:17 +03:30
- Author: Codex
- Before rule: cross-fitted causal Qini/AUUC, uplift calibration, and doubly robust policy value with forward-time service-cluster splits.
- After rule: retain only explicitly labeled targeted-versus-not-targeted association curves and overlap diagnostics; record causal Qini, AUUC, calibration, and policy value as not estimable; retain the supplied classification AUC as `unreproduced` and invalid for uplift.
- Reason: treatment delivery and timing, assignment probabilities or a defensible propensity contract, an authorized outcome, and the effect scale of `uplift_score` are absent.
- Outcomes known: yes.
- Affected analyses: recommendation targeting and `F_evaluation` registration.
- Disposition: diagnostic only; cannot validate targeting or upgrade any claim.

### DEV-010 - Prospective power and monitoring are provisional candidate designs

- Timestamp: 2026-08-31 13:25:17 +03:30
- Author: Codex
- Before rule: operationally certified interference components, authorized endpoints, a validated seasonal monitoring baseline, and an accepted stop/restart charter.
- After rule: infer eight provisional clinician-plus-ward components; simulate a site-stratified staggered design using the unauthorized cancellation proxy only for planning, with component/serial working-correlation scenarios and Monte Carlo intervals; publish illustrative assignments that are not operational randomization. Recompute supplied weekly alerts under a candidate strictly-prior baseline, while labeling the supplied rule, maturity, and seasonality calibration unreproduced.
- Reason: authoritative component membership, endpoint instrumentation, policy timing/adherence, alert logic/version history, endpoint as-of timestamps, a second seasonal cycle, and organizational approvals are not supplied.
- Outcomes known: yes.
- Affected analyses: power, minimum detectable effect, sequential design, monitoring replay, alert budget, and governance.
- Disposition: prospective planning/monitor specification only; cannot establish feasibility until the independent units and endpoint are certified, and cannot upgrade the locked claim verdicts.

## DL-007 - Scientific supplement identity and decisions

- Recorded at: 2026-08-31 13:52:57 +03:30.
- Locked parent: `87a183979b6b019d916c05bd4775120b4269d6cf` remains an ancestor; `prespec-v1` still resolves to `30aaac16303e65db49b15df116f150258460e31c`; the frozen human/YAML plans have no differences from that tag.
- Scientific implementation: `b832b8c98f0838d1c0c3a7d47e2fc9d9e5f49a32`.
- Implementation-identity commit: `8a045157b4a2ad05fe6d2cf1228b32cb40b26ecc`.
- Scientific result ID: `3741598adc31cfdc61c050be5b97e2de93154823e6d859298456a73fb2d4125a`.
- Input fingerprint: `68efa9bcbb5fb270a08bdc8564ba8455b6b093e72424777ffc7c10db1a186989`.
- Configuration fingerprint: `484ec76f251b55cad40b43eecebfccf6a955ed8a7d772371b36de60677964b4e`.
- Verification: 45 artifact hashes, 15,421 numeric provenance entries, 36 declared table-grain contracts, six parseable SVGs, and 56 tests passed. The immediate unchanged rerun returned the same result ID in 10.655 seconds. A fresh clone of artifact commit `b37e4569a985431ff9a9710a04fe9f1f7e1b5ac3` verified the committed result, passed all 16 supplement-focused tests, was clean, and retained the locked-parent ancestry.
- Judge decision: not fit for autonomous scoring or clinical triage; only offline, non-patient-facing error sampling with mandatory human review is narrowly defensible.
- Recommendation decision: every performance quantity is invalidated for clinical use because the authorized target, feature lineage, and threshold-selection history are absent.
- Uplift decision: causal AUUC, Qini, uplift calibration, and policy value are not estimable; the top-ranked slice also lacks targeted/not-targeted overlap.
- Study decision: eight provisional components yield 0.511 simulated fixed-final power (Monte Carlo interval 0.489 to 0.533) for the configured -0.02 risk difference at planning ICC 0.03, below the 0.80 target; the conservative grid MDE is -0.04. These are simulated planning quantities using an unauthorized proxy, not clinical proof.
- Monitoring decision: the candidate strictly-prior replay is a specification check, not reproduction of the opaque supplied alert rule; it does not validate seasonality or maturity.
- Locked verdict preservation: C1 and C2 remain `unsupported_by_observational_design`; C3 remains `unsupported_with_these_data`; absent-effect identity remains unresolved. The supplement cannot upgrade them.
- Administrative note: the eventual artifact/log commit cannot contain its own hash and will be reported in Git history and the handoff.

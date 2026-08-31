# Isolated analyst reproduction

This directory reproduces the supplied analyst query and deck numbers without endorsing them. The supplied SQL is untrusted input. Production code verifies its exact SHA-256 fingerprint and then executes the reviewed literal query in `reviewed_query.sql`; it never executes arbitrary text read from the data package.

Outputs are written under the versioned result directory and are labeled `reproduced`, `unreproduced`, or `sensitivity_only`. The reproduction deliberately preserves the analyst query's raw string join, assessment-only selection, snapshot multiplicity, event-time period assignment, and unadjusted row-weighted averages so the defects can be audited. Corrected analyses live outside this directory.

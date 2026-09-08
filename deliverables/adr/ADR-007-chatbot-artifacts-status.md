# ADR-007 — Structured chatbot artifacts and quotation status

- **Status:** Accepted
- **Date:** 2026-09-08

## Context

Provider-generated Markdown is not a reliable download control. Quotation totals
also include rows intentionally marked `IGNORED`, so subtracting auto-approved
rows produces a false unpriceable count.

## Decision

`POST /api/chatbot` returns an optional `downloads` array containing only
validated same-origin export URLs and filenames. The browser renders a native
download link from this metadata. `get_quotation_status` returns explicit
status counts and handled/unresolved totals; `IGNORED` is handled, not an error.

## Consequences

The assistant may still mention a URL in prose, but the usable control is always
the structured button. Any future status consumer must use explicit counts.

Direct imperative commands are treated as confirmation only for their matching
write tool. Questions and ambiguous wording continue through the preview gate.

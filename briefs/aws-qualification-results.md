---
title: "AWS qualification results"
status: results
as_of: 2026-11-07
scope: "What one approved run of the qualification lanes on EC2 with the shelves on EBS established, in venue classes and measurements only; no account, region or resource identifier."
---

# AWS qualification results

As of 2026-11-07. Rendered by `qualification/results.py` from a private evidence directory outside the tree; every identifier the run observed is on that directory's denylist and absent here by construction. Venue: one current-generation instance in the private (no internet, Session Manager over interface endpoints) shape, one gp3 data volume at the account's default encryption setting mounted as the shelves' filesystem.

## Mount

- Data volume device marker present: yes; assertion waived: no. A waived assertion means the lanes did not run on the data volume and this document is not an EBS qualification.

## Lanes

### hermetic: passed

- Counts: demos 3, demos_passed 3, suite_exit 0.

### ingestion: passed

- Counts: files 12, pending 0, per_kind {"image": {"added": 2, "converted": 2, "unconverted": 0}, "office": {"added": 3, "converted": 2, "unconverted": 1}, "other": {"added": 1, "converted": 0, "unconverted": 1}, "pdf": {"added": 2, "converted": 2, "unconverted": 0}, "text": {"added": 4, "converted": 4, "unconverted": 0}}, searchable 10, unconverted 2.
- Tools present on the instance: file yes, tesseract yes, vellric yes.

### portability: passed

- Counts: copy_hits 1, rebuilt 1, sidecars 0.

### bedrock: passed

- Counts: audit_embed_calls 2, audit_records 4, audit_retention_checks 2, hits 1, rerank_status "applied", searchable 1, texts_confirmed 2, texts_sent_at_most 2.
- Exported on the instance (bedrock-audit.jsonl, the lane's own count): 4 records, 2 retention checks, 2 embedding requests; rerank status applied.
- Retained with the run: yes, 4 records in bedrock-audit.jsonl, judged complete (a successful retention check and an embedding request present).

## Limits

- A `not-run` lane establishes nothing. The Bedrock lane reports `not-run` until the shelf's declared provider is Palace's `bedrock`, and a passed Bedrock lane records that the account's retention mode read `none` at that moment, not that nothing was retained.
- A passed Bedrock lane's request-level evidence is the exported audit artifact retained with the run and judged complete (Palace's per-request records: kind, status, model, dimension, timing; no text, no credential; at least one successful retention check and one embedding request); the confirmed text count remains the bound Palace enforces from the lane's fixed input.
- Teardown is recorded in the evidence directory's inventory, not here; absence is proved by the tag-scoped inventory read back after destroy.

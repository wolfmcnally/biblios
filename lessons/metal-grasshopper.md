---
title: '`with sqlite3.connect(...)` never closes, and CPython 3.12''s connection sits
  in a reference cycle through its statement cache, so a finished reader''s connection
  stays open until the cyclic collector runs; the seal''s switch out of WAL then meets
  ''database is locked'' intermittently'
scope: local
proposed_surface: policy
source: kickoff
slug: metal-grasshopper
status: candidate
filed: '2026-11-08'
occurrences:
- date: '2026-11-08'
  ref: Pre-release development observation; original operational records excluded
    from the public baseline
- date: '2026-11-09'
  ref: 'Consumer-adoption history qualification: live query connection held WAL during
    cutover; actual SQLITE_BUSY refusal resolved only after explicit release'
- date: '2026-11-09'
  ref: 'Consumer-adoption code correction: explicit checkpoint and close of certificate
    mutation fixture prevents live WAL/SHM lifetime from contaminating no-side-effect
    oracle'
---

The racing-seal proof failed once in a full gate under a load average near 9 with sqlite3.OperationalError 'database is locked' at PRAGMA journal_mode=DELETE, after passing in every earlier gate and in 10 of 10 single-worker reruns. Diagnosis: Python's connection context manager commits or rolls back but does not close; CPython 3.12's sqlite3.Connection holds a functools.lru_cache statement cache whose wrapper references the connection, a reference cycle, so an unreferenced connection is freed only by the cyclic collector. The paused reconcile step's two identity reads were therefore still open when the seal switched the journal, whenever no collection had run in that worker since; which tests a worker had run before decides it, hence intermittent. Containment: the gate failure was preserved and reported, nothing retried. Correction: one `read_only_index` context manager in biblios.progress that closes explicitly, used at all five index read sites; a proof with the collector disabled asserts no open connection survives any read helper. Candidate rules: never rely on `with connect()` or on garbage collection to close a sqlite connection, use contextlib.closing or an explicit close in finally; when a proof fails only under load, reproduce the lifetime with gc disabled before deciding between a product gap and a proof's unsupported expectation.
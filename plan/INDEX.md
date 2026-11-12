# Biblios roadmap

The [0.1.0 public baseline](../RELEASE.md#public-baseline-and-provenance) contains the shelf library, federated stacks, sealing and the thin CLI. It does not import private completed lifecycle records or their acceptance evidence. Current source and artifacts require fresh qualification.

Definitions and canonical lifecycle state live in `phases/` and `events/`. The operator authorized the adapter adoption development effort targeting 0.2.0; its active owner and state are read from its events. MCP planning and implementation remain held. This roadmap does not authorize a successor.

- [Packaging investigation](phases/packaging-shapes-investigation.md).
- [MCP server](phases/mcp-server-plane.md), held.
- [Connector walkthrough](phases/connector-walkthrough-demo.md), dependent on MCP.
- [Adapter-reached adoption](phases/adapter-consumer-adoption.md).
- [Explicit conversion envelopes](phases/conversion-envelope-budgets.md), a bounded compatibility correction depending on the completed adoption work.
- [Responsive provider reads](phases/responsive-provider-reads.md), a bounded correction preserving admitted reads during document embedding.
- [User-owned mirrors](phases/user-owned-mirrors.md), per-entry mirror isolation, read-only re-index of edits and an explicit validated update.
- [One incremental indexing core](phases/unified-indexing-core.md), one orchestration with per-entry staged publication and incremental discovery, delivered through two children:
  - [The indexing core and per-entry publication](phases/indexing-core-publication.md), the core, its stores, the stage seam, serving, the verified import and creation grants.
  - [Persistent edit observation](phases/persistent-edit-observation.md), the library-owned observer and indexing loop, depending on the core.

Read the target definition, its brief references and actual dependencies before phase work; use the repository's supported custody and review workflow. Product code lives in `project/biblios/`, tests in `project/tests/`, and installation instructions in [the README](../README.md).

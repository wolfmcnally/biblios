# Biblios 0.1.0 release preparation

This experimental library and CLI release candidate is being prepared for the existing repository. Publication and the one-history transition require approval of the exact qualified content and irreversible operations. Nothing in this record claims publication or completion of that transition.

## Intended artifacts

The proposed destination is the existing GitHub repository, with `master` as default branch and `v0.1.0` as proposed version tag. Package artifacts are `biblios-0.1.0-py3-none-any.whl` and `biblios-0.1.0.tar.gz`. No PyPI upload, hosted documentation, model weights, OCR executable, private configuration or user corpus is proposed. Exact contents and digests belong to the reviewed release packet.

Python support is 3.12 and 3.13, bounded by Palace's declared contract; the managed checkout runtime is Python 3.12. Git and uv fetch the public prerequisites. macOS and Linux have development qualification; Windows is unqualified. See [installation](project/README.md) and [update/uninstall guidance](project/RELEASE.md).

The exact qualified dependency revisions are Palace `de950db73b7498084fe09b54b447bce9482c87bb`, Castle `9351299b11f0a0272a08ee406a768d591ed847c2` (its `project` subdirectory), Drawbridge `359790cad3f23c2df7281e87004e51369fb9fba4` and Conric `ed9286297e92b8c049f70eb15a3b5556ed107ff0`. Each revision's package metadata and root MIT license were anonymously reachable on 2026-11-09. Built metadata must retain these requirements; uv-only overrides do not establish a wheel's dependency sources.

## Public baseline and provenance

The proposed first public tree is a fresh baseline rather than an imported private phase ledger. It contains the implemented shelf library, stacks, sealing, CLI, tests and unchanged validation machinery. It carries no fabricated completed phase events or copied acceptance closures. Its future roadmap contains only unstarted work, including held MCP work. Implemented prerequisites belong to the shipped baseline, without a counterfeit completion record.

Pre-release lifecycle records, raw logs, source snapshots and private qualification runs are not public artifacts. Their eventual disposition and discarded-history removal require the explicit transition approval; current private records stay intact during preparation. The public tree must pass fresh qualification using public prerequisites. Source-clone results and installed-wheel tests are distinct observations.

## Qualification observations

On 2026-11-09, a new Python 3.12 environment installed the actual wheel from declared public sources without private configuration or a reused dependency cache. The unchanged 66 product tests ran against that installed package outside any source checkout. Console and module help, a hermetic shelf workflow and an independent build/install from the source distribution were exercised. The wheel records exact substrate Git revisions; its remaining dependencies resolve from their declared public requirements, whereas the source checkout uses the complete lockfile.

A fresh clone of the proposed single-root public baseline passed all 243 repository tests and the unchanged policy gates using public prerequisites. That observation precedes this release-summary update; the final source payload must receive its own exact-tree qualification before publication. The original failed qualification is retained during preparation: temporary storage for required execution evidence was correctly refused, and the launcher was corrected to use durable private state without changing tests or validators. The manual build retains the known version warning between the pinned admonish plugin and mdBook; its links, assets, anchors, search and print checks passed.

These are macOS observations. Earlier Linux/Amazon Linux development qualification is historical capability evidence, not a new Linux installation of these release artifacts. Python 3.13 is admitted by dependency metadata but has not received this clean-install run. Windows remains unqualified. Manual document inspection and the CLI human usability demo are separate from these automated results.

## Licensing and notices

Original work is Copyright © 2026 Wolf McNally under [MIT](LICENSE), also included in the standalone package. [Package notices](project/NOTICE) distinguish separately installed dependencies and optional tools. Vellric's separate AGPL terms are not replaced by Biblios's MIT declaration.

Repository-only third-party assets retain their notices: ECharts under Apache-2.0; the included Font Awesome SVG icons under CC BY 4.0; mdBook reference/theme material under its included MPL-2.0 license; admonish material under its included MIT license. The [reference catalog](docs/README.md) records provenance and limited quotation bases for other excerpts. These repository governance/documentation assets are excluded from the wheel and package source distribution.

## Boundaries

MCP remains held. Excision's exposed scope remains unresolved. Existing shelves, configuration and services are unchanged. The CLI human usability demo remains open; automated checks do not supply that judgment.

The final packet must identify exact artifacts/source, privacy and license dispositions, real clean-install and unchanged-gate results, controlled old refs/object stores and historical copies, inaccessible-copy limits, installed-user continuity and forward-only reconciliation after partial failure. Replacing a GitHub ref alone does not prove removal of cached or unreferenced objects. This document authorizes no visibility change, cleanup, remote replacement, tag or publication.

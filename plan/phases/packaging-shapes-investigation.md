---
id: packaging-shapes-investigation
title: Packaging investigation across the four deployment shapes
order: '0.5'
parent: null
depends_on: []
informs: []
---

# Packaging investigation across the four deployment shapes

A research phase. Its deliverable is a brief, not code.

## Goal

Write the packaging investigation brief the entry-point brief proposes, so that the open packaging decision is settled on evidence before the access planes are built: for each of the four deployment shapes in operator requirement 8, what is installed, what is opened, where the MCP server runs, and whether one package with subcommands or a library plus a server package serves all four without contortion.

## Decomposition

One coherent outcome; no children.

## Deliverables

- `briefs/packaging-shapes.md`, cataloged in `AGENTS.md`, covering: the very simple single-shelf dynamic deployment; the single-shelf static deployment; the multi-shelf dynamic deployment behind a chat application's connector; and the multi-shelf dynamic deployment where the shelves in the query stack are reached through adapters.
- For each shape: the installed packages and their transitive weight, the files opened at runtime, the process that runs the server if any, and the dependencies a static receiver must not need.
- A recommendation on package layout with its reasons, marked as a proposal for the operator to rule on, and the ruling recorded in the entry-point brief's Rulings section when made.

## Acceptance

- `./bin/check-catalogs` passes with the new brief cataloged.
- The brief names every dependency each shape installs by reading the lockfile and the substrates' own manifests, not from memory, and dates its sources.
- The operator has ruled on the packaging proposal, or the brief records the exact decision still open and what would settle it.

## Brief refs

- [Entry-point brief: operator requirement 8, Proposals, Rulings and Unresolved](../../briefs/BRIEF.md)

## User Demo

N/A: the deliverable is a brief; the operator reads it.


## Public baseline

The shelf, stack, sealing and CLI prerequisites are shipped in the 0.1.0 baseline described in [release provenance](../../RELEASE.md#public-baseline-and-provenance). Their private lifecycle records are not imported as completed public phases. This future phase has not started. MCP planning and implementation remain held by the operator; this baseline selects no successor.

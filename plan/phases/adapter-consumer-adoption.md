---
id: adapter-consumer-adoption
title: Adapter-reached multi-shelf consumer adoption
order: '7'
parent: null
depends_on: []
informs: []
---

# Adapter-reached multi-shelf consumer adoption

Sketch only. The adoption itself is planned and executed in the consumer's own repository; this phase records what Biblios must expose and verifies it from this side.

## Goal

A secured consumer whose nodes each hold shelves inside wrapped volumes can replace its own index and source modules with Biblios library calls, selecting and federating the shelves of its unlocked nodes through its own adapters and running the search with its own providers, without Biblios learning anything about labels, clearances or keys.

## Deliverables

- A library surface for opening a shelf through an adapter-supplied handle rather than a plain directory path, if the stack phase did not already provide it.
- A written adoption note describing which consumer modules map to which library calls, kept to the capability class.
- Tests proving a stack over adapter-opened shelves behaves identically to a stack over directories.

## Acceptance

- `./bin/check all` passes.
- A test proves a stack over adapter-opened shelves returns the same hits as the same shelves opened by path.
- Nothing in Biblios references a label, clearance, key or node.

## Brief refs

- [Entry-point brief: Operator requirements 5, 8 and 9, Consumers, What Biblios is not](../../briefs/BRIEF.md)

## User Demo

N/A from this repository; the consumer's adoption phase carries the demo.


## Public baseline

The shelf, stack, sealing and CLI prerequisites are shipped in the 0.1.0 baseline described in [release provenance](../../RELEASE.md#public-baseline-and-provenance). Their private lifecycle records are not imported as completed public phases. This future phase has not started. MCP planning and implementation remain held by the operator; this baseline selects no successor.

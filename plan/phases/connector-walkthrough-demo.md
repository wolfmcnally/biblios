---
id: connector-walkthrough-demo
title: Chat-application connector walkthrough
order: '6'
parent: null
depends_on:
- mcp-server-plane
informs: []
---

# Chat-application connector walkthrough

Sketch; tighten at entry.

## Goal

A documented, repeatable walkthrough attaches the Biblios MCP server as a custom connector to a chat application that consumes streamable-HTTP MCP servers per workspace, with bearer or OAuth auth, and treats every unannotated tool as a write. The application's agent searches across the user's stack and reads a page. Feeding the application's own documents into a shelf is a later step and not part of this phase.

## Deliverables

- A manual chapter with the exact steps, the connector configuration, and what the operator should see.
- Any server configuration the walkthrough needs, kept generic to the capability class.
- A recorded walkthrough in the phase's User Demo protocol.

## Acceptance

- `./bin/check all` passes.
- The operator completes the walkthrough against a real instance of such an application and the agent returns a hit from a shelf with its page citation.

## Brief refs

- [Entry-point brief: Operator requirement 10, Consumers, Access planes (MCP server)](../../briefs/BRIEF.md)

## User Demo

To be written at entry; this phase is the demo.


## Public baseline

The shelf, stack, sealing and CLI prerequisites are shipped in the 0.1.0 baseline described in [release provenance](../../RELEASE.md#public-baseline-and-provenance). Their private lifecycle records are not imported as completed public phases. This future phase has not started. MCP planning and implementation remain held by the operator; this baseline selects no successor.

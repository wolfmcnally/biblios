---
id: mcp-server-plane
title: MCP server with read-only annotations
order: '5'
parent: null
depends_on: []
informs:
- connector-walkthrough-demo
---

# MCP server with read-only annotations

Sketch; tighten at entry.

## Goal

A stateless streamable-HTTP MCP server exposes the read tools (`search`, `resolve`, `read`, `download`, `ls`, `status`, `shelves`) annotated read-only and non-destructive, and the curation tools (`add`, `update`, `remove`, `rename`, `move`, `mkdir`) that a deployment may leave disabled. The tool list is the forcing function for the library's verbs; the server adds no logic.

## Deliverables

- The server package under `project/biblios/server/`, with its dependency added to `project/pyproject.toml` and `uv.lock` in this phase and not before.
- Tool definitions with annotations that a connector-consuming chat application reads as read-only.
- A configuration that selects which shelves form the served stack and whether curation tools are enabled.
- Tests that exercise the server in-process over a temporary stack and prove annotations and the disabled-curation mode.
- Manual chapter for running the server.

## Acceptance

- `./bin/check all` passes.
- A test proves the read tools carry read-only and non-destructive annotations and the curation tools do not.
- A test proves a deployment with curation disabled refuses every curation tool.
- A test proves a search over the served stack returns the same hits as the library call.

## Brief refs

- [Entry-point brief: Access planes (MCP server), Operator requirement 8 and 10, Consumers, Rulings](../../briefs/BRIEF.md)

## User Demo

To be written at entry: start the server over two shelves and search from an MCP client.


## Public baseline

The shelf, stack, sealing and CLI prerequisites are shipped in the 0.1.0 baseline described in [release provenance](../../RELEASE.md#public-baseline-and-provenance). Their private lifecycle records are not imported as completed public phases. This future phase has not started. MCP planning and implementation remain held by the operator; this baseline selects no successor.

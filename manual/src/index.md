# Biblios

Biblios builds standalone, portable knowledge bases called shelves. A shelf is one directory that a user adds documents to, updates and removes them from, carries to another machine, ships to a deployment as a static artifact, and searches either by itself or together with other shelves in a stack.

Three libraries do the work. Drawbridge turns any file into a page-anchored Markdown mirror with a provenance header. Castle keeps the exact source bytes content-addressed, with an append-only journal and a durable namespace of named files and folders. Palace chunks the mirrors, embeds them under a pinned identity, and searches any number of stores at once with hybrid retrieval and a single rerank over the union. Biblios composes them and invents as little as possible.

Start with [Get Started](getting_started.md) to set up the repository and run its checks. Read [Shelves and Stacks](shelves_and_stacks.md) for the concepts the library is built around.

```admonish note title="Status"
Biblios is in its first phases. The Python library is the contract and the only interface for now; a command-line tool and an MCP server arrive later as thin layers over it. This manual describes what exists and names what is planned.
```

Biblios is developed with the [Agentic Coding Starter](https://github.com/wolfmcnally/agentic-coding-starter) methodology: written briefs, binding policies, a phased plan, independent review and executable checks. That methodology is documented in its own manual; this one is about Biblios.

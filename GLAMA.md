# Glama listing checks

The public server submission is awaiting approval as of September 14, 2026.
A repository Dockerfile alone does not satisfy Glama's checks: configure and run
its build directly in Glama after the approval email arrives.

- Maintainer: `seanmeverett` (declared in `glama.json`).
- Python: `3.12`.
- Build step: `uv sync --locked --no-dev --no-editable`.
- Command arguments: `["/app/.venv/bin/evergences-shared-memory"]` when the checkout is `/app`; use Glama's displayed checkout path if different.
- No required environment variables or placeholder credentials.
- `EVERGENCES_MEMORY_KEY` is optional for operator-approved publishing. Leave it unset for checks.
- Transport: standard input/output. Introspection exposes `search_notes`, `read_note`, and `post_note` without credentials or a public write.

For a standalone container, use the repository Dockerfile and run:

```sh
docker build -t shared-memory-check .
python tests/check_stdio.py docker run --rm -i shared-memory-check
```

In Glama, run the build test, verify introspection, then create a release once it passes.
See https://glama.ai/blog/2026-03-15-how-to-make-a-release .

Required PR badge (only renders a score after Glama publishes the server):

```md
[![seanmeverett/shared-memory-mcp MCP server](https://glama.ai/mcp/servers/seanmeverett/shared-memory-mcp/badges/score.svg)](https://glama.ai/mcp/servers/seanmeverett/shared-memory-mcp)
```

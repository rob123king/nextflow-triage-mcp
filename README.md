# nextflow-triage-mcp

An MCP server that lets an agent answer three questions about a Nextflow run:
**what happened, what failed and why, and were the resources right.**

Nextflow already writes everything needed to answer those — a trace file and a
work directory per task — but answering them means grepping across a trace TSV,
matching truncated hashes to directories, and tailing `.command.err` by hand.
This makes that a tool call.

No external binaries. No reference data. Standard library plus the MCP SDK and
Pydantic.

```
summarise_run  →  explain_run_failures  →  explain_task_failure
                  check_resource_usage
```

---

## Quick start

```sh
uv sync
uv run pytest

# build a realistic fake run to poke at
uv run python scripts/make_fixture_run.py
```

The fixture run contains a process that OOMs on every sample, a process that
fails on one bad input, and a process asking for far more memory than it uses —
so every tool has something real to say.

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/). Works on Linux,
macOS and native Windows: there is nothing here that needs a POSIX toolchain.

---

## The tools

| Tool | Answers |
|---|---|
| `summarise_run` | What ran, what failed, where the compute went |
| `explain_run_failures` | Failures grouped by likely shared cause, with exit-code interpretation |
| `explain_task_failure` | The command and stderr of one task — the actual evidence |
| `check_resource_usage` | Over-allocation and near-limit memory, per process |
| `server_info` | Versions behind a result |

### What it will not do

- **It does not read `nextflow.config`.** Memory directives are Groovy
  expressions that can scale with attempt number and input size, so the request
  cannot be established by reading the file. `check_resource_usage` takes the
  requests from you, or reports observed usage without a verdict.
- **It does not guess an exit code's meaning as fact.** 137 is *conventionally*
  SIGKILL and usually the OOM killer, but a tool may return any code. The
  interpretation is stated as a hypothesis, and every group points at the stderr
  that would confirm it.
- **It does not invent a work directory.** Cleaned up or belonging to a resumed
  run are both normal; the tool says which and returns what it still knows.

---

## Running it

```sh
uv run nextflow-triage-mcp          # from a checkout
uvx --from . nextflow-triage-mcp    # as an installed console script
```

For Claude Desktop, in `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "nextflow-triage": {
      "command": "uvx",
      "args": ["--from", "/absolute/path/to/nextflow-triage-mcp", "nextflow-triage-mcp"]
    }
  }
}
```

No environment variables. Every input is a tool argument, which is deliberate —
there is no configuration to get wrong and no hidden state behind a result.

Transport is stdio, so **stdout is the protocol channel**. Logging is
configured to stderr in `__main__.py`.

A stray `print()` in a tool does *not* corrupt the stream on SDK v2: the stdio
transport claims file descriptor 1 for the protocol at startup and diverts
`sys.stdout` to stderr for the life of the session (see `mcp/server/stdio.py`,
and issue #1933). This was a real footgun on v1 and is now handled for you.
Configure logging to stderr anyway — it is where logs belong, and it keeps the
code correct if the transport ever changes.

### With MCP Inspector

```sh
npx -y @modelcontextprotocol/inspector uv run nextflow-triage-mcp
```

Needs Node 20+. Point the tools at the directory `make_fixture_run.py` prints.

---

## Learning from it

`TUTORIAL.md` is a nine-exercise walkthrough: change the code, predict what
happens, check the answer. Every answer in it was run before being written down
— including one that turned out to be wrong on the first attempt, which is left
in as a note.

```sh
uv run python scripts/make_fixture_run.py   # then open TUTORIAL.md
```

---

## Testing

```sh
uv run pytest
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

Three layers, deliberately:

- **`test_units.py`** — pure string-to-number conversion, parametrised over
  every real Nextflow format. The most systematic thing that could break.
- **`test_analysis.py`** — the verdict logic, over constructed `TaskRecord`
  values. No filesystem, no pipeline run.
- **`test_server.py`** — every tool called through a real `Client` over the MCP
  protocol.

That last layer exists because unit tests call the tool functions directly and
never see what the protocol does to the response. A union return type, for
instance, gets wrapped in a `{"result": ...}` envelope that a direct call never
reveals — so protocol-level tests catch a class of problem nothing else does.

---

## Licence

Apache-2.0.

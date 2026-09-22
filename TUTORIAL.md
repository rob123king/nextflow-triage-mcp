# Learning MCP by changing this server

A worked tutorial. Nine exercises, each one a small change to real code with a
verified answer. Work through them in order — later ones assume earlier ones.

Everything here has been run. Where the tutorial says "you will see X", X is
what actually happened, not what ought to happen.

**Ground rule: don't read an answer until you've tried.** The exercises are
sized so that a wrong first attempt is likely and instructive.

---

## Part 0 — Setup

```sh
cd nextflow-triage-mcp
uv sync
uv run pytest                          # expect 95 passed
uv run python scripts/make_fixture_run.py
```

That last command prints a directory (default `~/nf-fixture-run`). Export it,
because every exercise uses it:

```sh
export RUN=~/nf-fixture-run
```

Keep a second terminal open for `uv run pytest`. You'll use it constantly.

---

## Part 1 — Look before you touch

Four things to do without editing anything.

### 1.1 See what a model sees

```sh
uv run python -c "from nextflow_triage.server import SERVER_INSTRUCTIONS; print(SERVER_INSTRUCTIONS)"
```

That block is sent once at connection and placed in the model's context next to
the tool list. It is the only place to state a rule that spans tools.

Now the per-tool text:

```sh
uv run python - <<'PY'
import anyio
from mcp import Client
from nextflow_triage.server import mcp

async def main():
    async with Client(mcp) as c:
        for t in (await c.list_tools()).tools:
            print(f"\n{'='*70}\n{t.name}\n{'='*70}\n{t.description}")
anyio.run(main)
PY
```

**Read `summarise_run`'s description properly.** Notice it names
`explain_run_failures` explicitly rather than saying "call the next tool". A
model planning its approach before any call has only this text to go on.

### 1.2 See the schema you never wrote

```sh
uv run python - <<'PY'
import anyio, json
from mcp import Client
from nextflow_triage.server import mcp

async def main():
    async with Client(mcp) as c:
        tool = next(t for t in (await c.list_tools()).tools if t.name == "check_resource_usage")
        print(json.dumps(tool.input_schema, indent=2))
anyio.run(main)
PY
```

Nobody wrote that JSON. The SDK derived it from the Python type hints on
`check_resource_usage(run_dir: str, requested_memory: dict[str, str] | None = None)`.
That is what `@mcp.tool()` is for.

### 1.3 Run the whole path

```sh
uv run python - <<'PY'
import anyio, os
from mcp import Client
from nextflow_triage.server import mcp

RUN = os.path.expanduser("~/nf-fixture-run")

async def main():
    async with Client(mcp) as c:
        s = (await c.call_tool("summarise_run", {"run_dir": RUN})).structured_content
        print("SUMMARY:", s["headline"])

        f = (await c.call_tool("explain_run_failures", {"run_dir": RUN})).structured_content
        print("\nFAILURES:", f["headline"])
        worst = f["groups"][0]

        d = (await c.call_tool("explain_task_failure",
             {"run_dir": RUN, "task_id": worst["example_task_id"]})).structured_content
        print("\nSTDERR:", d["stderr_tail"])
anyio.run(main)
PY
```

Each tool hands the next one an identifier. That chain is the design.

### 1.4 See the three failure modes

```sh
uv run python - <<'PY'
import anyio, os
from mcp import Client
from nextflow_triage.server import mcp

RUN = os.path.expanduser("~/nf-fixture-run")

async def main():
    async with Client(mcp) as c:
        for label, name, args in [
            ("works",          "summarise_run",       {"run_dir": RUN}),
            ("tool error",     "summarise_run",       {"run_dir": "/nowhere"}),
            ("bad argument",   "check_resource_usage",
                                {"run_dir": RUN, "requested_memory": {"X": "loads"}}),
        ]:
            r = await c.call_tool(name, args)
            print(f"{label:14s} is_error={r.is_error}")
            if r.is_error:
                print("   ", str(r.content[0].text)[:160].replace("\n", " "))
anyio.run(main)
PY
```

Both errors come back as *results* with `is_error=True`, carrying a readable
message. The call succeeded at protocol level; the model reads the text and can
retry. That is deliberate — see Exercise 7 for the other kind.

---

## Part 2 — Exercises

### Exercise 1 — Add an exit-code interpretation (warm-up)

Exit code **139** is 128+11, conventionally SIGSEGV: a segfault. Add an
interpretation for it.

**Do it test-first.** Add a failing test to `tests/test_analysis.py`:

```python
def test_segfaults_are_interpreted() -> None:
    trace = make_trace(task("1", status="FAILED", exit_code="139"))
    interpretation = group_failures(trace).groups[0].interpretation
    assert "segmentation fault" in interpretation
```

```sh
uv run pytest tests/test_analysis.py -k segfault    # should FAIL
```

Now make it pass.

<details>
<summary>Answer</summary>

In `src/nextflow_triage/analysis.py`, inside `_interpret_exit_code`, add to the
dict:

```python
        "139": (
            "Exit 139 is 128+11, conventionally SIGSEGV -- a segmentation "
            "fault. That is a bug or a corrupt input in the tool itself, not a "
            "resourcing problem: more memory will not fix it."
        ),
```

**Why this order matters.** Writing the test first forced you to decide what the
message should *say* before deciding how to store it. That is the whole habit:
the interpretation text is the product here, and the dict is just where it
lives.
</details>

---

### Exercise 2 — Add a field to a response, and watch the schema move

`RunSummary` reports counts but never says how many *distinct processes* the
run had. Add `distinct_processes: int`.

```sh
# before
uv run python -c "
from nextflow_triage.analysis import RunSummary
print(sorted(RunSummary.model_json_schema()['properties']))"
```

Make the change, then run that again.

<details>
<summary>Answer</summary>

In `analysis.py`, add to the `RunSummary` model:

```python
    distinct_processes: int = Field(
        description="How many distinct processes this run touched."
    )
```

and in `summarise()`, inside the `return RunSummary(...)`:

```python
        distinct_processes=len(summaries),
```

Note `summaries` is the full list, not the truncated `summaries[:MAX_SAMPLE]` —
the count must describe everything, not the sample.

**What it taught.** You changed a Pydantic model and the *output schema the
model receives* changed with it, with no separate schema file to keep in sync.
Confirm:

```sh
uv run python - <<'PY'
import anyio
from mcp import Client
from nextflow_triage.server import mcp
async def main():
    async with Client(mcp) as c:
        t = next(x for x in (await c.list_tools()).tools if x.name == "summarise_run")
        print("distinct_processes" in str(t.output_schema))
anyio.run(main)
PY
```
</details>

---

### Exercise 3 — Break a test on purpose

In `units.py`, the duration regex is:

```python
_DURATION_PART = re.compile(r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>ms|s|m|h|d)", re.IGNORECASE)
```

Swap `ms|s|m` to `s|m|ms` and run the tests.

```sh
uv run pytest tests/test_units.py -q
```

**Predict before you run it: how many tests fail, and which?**

<details>
<summary>Answer</summary>

Two fail: `test_durations_parse[154ms-0.154]` and
`test_milliseconds_are_not_read_as_minutes`.

With `s` first, `154ms` no longer matches as milliseconds. The observed result
is `9240.0` seconds — the regex ends up reading it as **154 minutes** instead of
0.154 seconds, a factor of 60,000 out.

(If you predicted "154 seconds", so did the person who wrote this tutorial
before running it. Verifying beats reasoning.)

**Why this exercise exists.** That bug would be silent. Every duration in every
response would be wrong by a factor of 1000, nothing would crash, and the
numbers would look plausible. The comment in `units.py` says *"the alternation
order in the regex is load-bearing"* — now you know why it earned a comment.

Revert the change.
</details>

---

### Exercise 4 — Make a description fail its own test

Open `server.py` and shorten `explain_task_failure`'s docstring to one line:

```python
    """Read the stderr of a failed task."""
```

```sh
uv run pytest tests/test_server.py -q
```

<details>
<summary>Answer</summary>

`test_every_tool_description_is_written_for_a_model` fails: the description is
under 120 characters.

That test looks trivial and is not. A tool description is the *entire* interface
a model gets — no README, no source, no examples — and it is read on every
single call. Treating it as a tested artifact is the difference between a
server a model uses correctly and one it guesses at.

Revert.
</details>

---

### Exercise 5 — Add a whole new tool

The trace file records an `attempt` number per task. The parser reads it into
`TaskRecord.attempt` — and nothing ever uses it.

Retries matter: a task that failed twice and succeeded on the third attempt is
recorded as `COMPLETED`, so the run looks clean. But a process that needs
retrying is usually brushing a resource limit.

**Build `check_retries(run_dir)`.** It should report how many tasks were
retried, how many eventually succeeded, and a bounded list.

First give the fixture something to find. In `scripts/make_fixture_run.py` and
`tests/factories.py` the trace rows end with `"1"` for attempt — that is the
last field written in the `"\t".join([...])`. Change one row's attempt to `"3"`.

<details>
<summary>Answer</summary>

**1. `analysis.py`** — add the models and the pure function:

```python
class RetriedTask(BaseModel):
    """A task that Nextflow had to run more than once."""

    process: str
    tag: str
    attempts: int
    final_status: str


class RetryReport(BaseModel):
    """Which tasks needed retrying, and whether retrying worked."""

    trace_file: str
    tasks_retried: int
    retries_that_eventually_succeeded: int
    tasks: list[RetriedTask]
    sample_note: str
    headline: str


def find_retries(trace: Trace) -> RetryReport:
    """Report tasks that ran more than once.

    Retries are invisible in a plain summary -- a task that failed twice and
    succeeded on the third attempt counts as COMPLETED -- but they are a strong
    signal of a flaky process or a resource limit being brushed.
    """
    retried = [task for task in trace.tasks if task.attempt > 1]
    recovered = [task for task in retried if not task.failed]
    rows = [
        RetriedTask(
            process=task.process,
            tag=task.tag,
            attempts=task.attempt,
            final_status=task.status,
        )
        for task in sorted(retried, key=lambda t: (-t.attempt, t.process))
    ]
    if not retried:
        headline = "No task in this run was retried."
    else:
        headline = (
            f"{len(retried)} tasks were retried; {len(recovered)} eventually "
            f"succeeded. Repeated retries usually mean a resource limit being "
            f"brushed rather than a random fault."
        )
    return RetryReport(
        trace_file=str(trace.path),
        tasks_retried=len(retried),
        retries_that_eventually_succeeded=len(recovered),
        tasks=rows[:MAX_SAMPLE],
        sample_note=f"Showing {min(MAX_SAMPLE, len(rows))} of {len(rows)} retried tasks.",
        headline=headline,
    )
```

**2. `server.py`** — import `RetryReport` and `find_retries`, add
`"check_retries"` to `implemented_tools`, and add the tool:

```python
@mcp.tool()
def check_retries(run_dir: str) -> RetryReport:
    """Report tasks Nextflow had to run more than once. Call alongside summarise_run.

    run_dir: as for summarise_run.

    Retries are invisible in a plain summary: a task that failed twice and
    succeeded on its third attempt is recorded as COMPLETED, so the run looks
    clean. They matter because a process that needs retrying is usually brushing
    a resource limit rather than hitting a random fault -- especially if your
    config scales memory with task.attempt, in which case a retry that succeeded
    is telling you the first request was too small.

    Read `retries_that_eventually_succeeded` against `tasks_retried`. Retries
    that succeeded point at resourcing; retries that still failed point at the
    input or the tool.
    """
    return find_retries(load_run(_run_dir(run_dir)))
```

**3. Update `tests/test_server.py`** — `EXPECTED_TOOLS` and the `calls` list in
`test_every_tool_returns_a_flat_structured_payload`.

Verify:

```sh
uv run pytest -q && uv run ruff check . && uv run mypy
```

**Three things this exercised.** The tool function is four lines because the
work lives in a pure function that takes a `Trace` — so it is testable without
any filesystem. The description explains *why the answer matters*, not just what
the fields are. And two tests failed the moment you added a tool without
updating them, which is exactly what you want: the tool list is a contract.
</details>

---

### Exercise 6 — Break the response shape, then understand why it matters

Change `summarise_run`'s return annotation to a union:

```python
def summarise_run(run_dir: str) -> RunSummary | str:
```

Run the flat-payload test:

```sh
uv run pytest tests/test_server.py -k flat -q
```

<details>
<summary>Answer</summary>

It fails. The payload is now nested:

```
summarise_run nests its payload under 'result'; every other tool is flat
```

The SDK cannot use a union as an output schema directly, so it wraps that one
tool's payload in `{"result": ...}`. Confirm it in isolation:

```sh
uv run python - <<'PY'
import anyio
from mcp import Client
from mcp.server import MCPServer
from pydantic import BaseModel

class A(BaseModel):
    value: int = 1

class B(BaseModel):
    other: str = "x"

probe = MCPServer("probe")

@probe.tool()
def single() -> A:
    """Return one model type, which is the normal case."""
    return A()

@probe.tool()
def union_return() -> A | B:
    """Return a union, to see what the SDK does to the payload."""
    return A()

async def main():
    async with Client(probe) as c:
        for name in ("single", "union_return"):
            r = await c.call_tool(name, {})
            print(f"{name:14s} {list((r.structured_content or {}).keys())}")
anyio.run(main)
PY
```

Output:

```
single         ['value']
union_return   ['result']
```

**Why this is the most valuable exercise here.** That inconsistency is invisible
to a direct Python call — `summarise_run(...)` returns a `RunSummary` either
way. Only a test that goes through a real client sees it. One tool shaped
differently from the other five is exactly what degrades tool-use reliability,
and it was found on a previous project by precisely this test.

The rule that follows: **one tool, one return type.** If you need two response
shapes, that is two tools.

Revert.
</details>

---

### Exercise 7 — Try to corrupt the protocol, and find out you can't

Under the stdio transport, stdout carries JSON-RPC. So a `print()` inside a tool
should destroy the stream. Try it — add to `summarise_run`:

```python
    print("DEBUG: about to summarise")
```

Then run it as a real subprocess, not in memory:

```sh
uv run python - <<'PY'
import anyio, os, sys
from mcp import Client, StdioServerParameters
async def main():
    p = StdioServerParameters(command="uv", args=["run","nextflow-triage-mcp"], env={**os.environ})
    async with Client(p) as c:
        r = await c.call_tool("summarise_run", {"run_dir": os.path.expanduser("~/nf-fixture-run")})
        print("is_error:", r.is_error)
anyio.run(main)
PY
```

**Predict what happens. Then run it.**

<details>
<summary>Answer</summary>

It works. `is_error: False`, and `DEBUG: about to summarise` appears on your
terminal — via **stderr**, not the protocol stream.

SDK v2 defends against this. Read the source:

```sh
sed -n '80,100p' .venv/lib/python3.13/site-packages/mcp/server/stdio.py
```

At session start the transport claims file descriptor 1 for the protocol and
points `sys.stdout` at fd 2 instead (`_open_stdout_diversion` does
`os.dup(2)`). The comment references issue #1933: on v1 this was a real footgun
and closing the wrapper could destroy `sys.stdout` for the whole process.

**Two lessons.** First, the ecosystem is young and moving; advice that was
correct a version ago may not be now, and reading the SDK source is a five-minute
habit that settles these questions. (The README of this repo said the opposite
until this exercise was written — it has been corrected.)

Second: configure logging to stderr anyway, as `__main__.py` does. Relying on
the transport to rescue you is depending on an implementation detail.

Revert.
</details>

---

### Exercise 8 — Find the unbounded response

Every response in this server is bounded except one path. Find it.

Hint: `MAX_SAMPLE` bounds the lists. What about the *strings*?

```sh
grep -rn "MAX_" src/nextflow_triage/
```

<details>
<summary>Answer</summary>

`affected_tags` is bounded to `MAX_SAMPLE`, and the log tails are bounded by
`MAX_TAIL_LINES` and `MAX_LINE_CHARS`. But `FailureGroup.interpretation` and
every `headline` are unbounded strings built with f-strings — in practice short,
but nothing enforces it.

The more interesting answer is `status_counts` in `RunSummary`. It is a `dict`
with one key per distinct status, and while Nextflow only emits a handful,
nothing in the code caps it.

**The real lesson is about proportion.** Neither is worth fixing: statuses come
from a fixed vocabulary and headlines are written by us, not derived from input.
A cap there would be ceremony. The rule is "no *unbounded list derived from
input data*", and applying it to things that cannot grow is cargo-culting.

Knowing which constraints apply where is the actual skill. If you want to prove
it to yourself, write a test that feeds 10,000 processes into `summarise` and
assert the response stays small:

```python
def test_a_huge_run_still_produces_a_small_response() -> None:
    trace = make_trace(*[task(str(i), process=f"P{i}") for i in range(10_000)])
    summary = summarise(trace)
    assert len(summary.processes) == 5
    assert len(summary.model_dump_json()) < 4_000
```
</details>

---

### Exercise 9 — Change what the model is told, without changing what it does

`explain_run_failures` currently groups by `(process, exit_code)`. A model
reading the output cannot tell whether two groups for the same process are
related.

Don't change the grouping. Change the *description* so a model handles it
correctly, then check you got it right by reading the output as a model would.

<details>
<summary>Answer</summary>

There is no single right answer, which is the point. A good one adds:

```
    A process can appear in more than one group if its tasks failed with
    different exit codes. That usually means two distinct problems, not one --
    report them separately rather than summing the counts.
```

**Why this is the last exercise.** Every other exercise changed behaviour. This
one changes only the prompt, and it is the highest-leverage kind of change in
MCP work: the same code becomes more reliable in an agent's hands because the
instructions got better. Tool descriptions are not documentation. They are the
part of the system you tune.

Verify it took effect:

```sh
uv run python -c "
from nextflow_triage.server import explain_run_failures as f
print(f.__doc__)"
```
</details>

---

## Part 3 — Where to go next

Real runs. Point `summarise_run` at one of your own:

```sh
uv run python -c "
import anyio, sys
from mcp import Client
from nextflow_triage.server import mcp
async def main():
    async with Client(mcp) as c:
        r = await c.call_tool('summarise_run', {'run_dir': sys.argv[1]})
        print(r.content[0].text if r.is_error else r.structured_content['headline'])
anyio.run(main)" /path/to/a/real/run
```

If it fails, the error should tell you what to do. If it doesn't, that's a bug
worth fixing — and fixing it is Exercise 10.

Things worth building next, roughly in order of usefulness:

- **`compare_runs(run_dir_a, run_dir_b)`** — did the pipeline get slower? Which
  process regressed? Needs thought about what "the same task" means across runs.
- **Parse `.nextflow.log`** for the session ID and the resume status, so
  `summarise_run` can say whether `-resume` was used.
- **A cursor** on `explain_run_failures`, for a run with thousands of failures.
  Note what the previous project learned: paging is one category at a time, and
  `next_cursor` must come back null rather than an empty page.

And the general habit worth keeping: when you are unsure what the SDK does,
**test it in ten lines** rather than reasoning about it. Exercises 6 and 7 both
existed because someone guessed, and one of the guesses was wrong.

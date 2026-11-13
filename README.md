# pytest-zygote

Run pytest in parallel without paying for the imports N times.

```bash
pip install pytest-zygote
pytest --workers 4
```

Tested on Python 3.9-3.14 and pytest 7.0-9.1.

## The problem

A pytest-xdist worker is a fresh interpreter. execnet's popen gateway starts each one as
`python -u -B -c ...`, so every worker imports your whole dependency tree from scratch and
keeps its own copy in memory. If importing your app costs 400 MB — a few ML or LLM libraries
will do it — then `-n 4` costs 2 GB before a single assertion runs, and almost none of it is
shareable: it is private anonymous heap, not file-backed pages.

That is what puts a ceiling on parallelism in a container. You want more workers because your
tests wait on the network; you can't have them because each one costs another 400 MB.

## What this does

Collect in the parent, `gc.freeze()`, then `fork()` a worker per slot. Workers inherit the
already-imported heap as copy-on-write pages, so the imports are paid once for the whole run.

Measured on a suite of 1089 tests whose imports cost ~430 MB (litellm, pydantic-ai, pandas,
matplotlib), peak memory of the whole container:

| | wall | peak memory |
|---|---|---|
| one process | 28.3 s | 510 MB |
| `pytest -n 4` (xdist) | 21.3 s | 1945 MB |
| `pytest --workers 4` | 16.1 s | 810 MB |
| `pytest --workers 8` | 14.7 s | 1004 MB |
| `pytest --workers 12` | 13.8 s | 1104 MB |

**A worker costs ~46 MB here; an xdist worker costs ~490 MB.** It is also faster than xdist,
because a forked worker starts warm and skips the import that each xdist worker repeats.

Forking alone keeps the heap shared only until a worker runs a full collection: that writes to
every inherited object's GC header and un-shares the whole heap. An explicit `gc.collect()`
anywhere in a test, fixture or library triggers one, and so does enough growth in long-lived
objects. `gc.freeze()` moves everything imported so far into a permanent generation that
collections never walk, so the heap stays shared either way. On a 1761-test suite whose workers
never run a full collection, the freeze changes nothing (~510 MB peak at `--workers 4` either
way); add one `gc.collect()` per worker and the peak goes to ~910 MB without it, each worker
un-sharing 150-180 MB. The same holds on Python 3.12, 3.13 and 3.14. The freeze costs one
`gc.collect()` in the controller (~0.2 s there). The technique is Instagram's, upstreamed as
`gc.freeze()` in Python 3.7.

## Compared with pytest-xdist

The difference is entirely in how a worker is born.

| | pytest-xdist | pytest-zygote |
|---|---|---|
| how a worker starts | a new interpreter (`python -u -B -c ...` via execnet) | `fork()` of the process that just collected |
| who imports your code | every worker, separately | the controller, once |
| who collects | every worker repeats collection | the controller only |
| memory per worker* | ~490 MB | ~46 MB |
| time before the first test | each worker pays the full import | workers start warm |
| distribution | `load`, `loadscope`, `loadfile`, `loadgroup`, `worksteal`, `each` | a file per worker, idle workers steal half of the longest share |
| workers on other machines | yes, over ssh or a socket | no, this box only |
| Windows | yes | no — `fork()` only, so Linux and macOS |
| `--looponfail` | yes | no |
| a worker that dies | `--max-worker-restart` | replaced, and the test it killed is reported as failed |
| maturity | since 2010, used everywhere | new |

\* On the suite in the table above, whose imports cost ~430 MB. If your imports are cheap,
both numbers collapse and the distinction stops mattering.

**Use xdist** when you need workers on other machines, a `--dist` mode that pins related tests
to one worker, Windows, or simply when it already works for you. It is the mature, general
answer and this plugin does not try to replace it.

**Use zygote** when your imports are expensive and memory is what caps your parallelism —
a container with a hard limit, CI with a small runner, a laptop that swaps at `-n 8`. The
worse your import cost, the bigger the gap. The suite above needs 1945 MB to run four xdist
workers; it runs twelve forked workers in 1104 MB.

They are mutually exclusive in one run: passing `--workers` and `-n` together is an error,
not a silent choice of one.

## Options

| | |
|---|---|
| `--workers N` | run tests in N forked workers, or `auto` for one per core. Default `0`, which leaves pytest alone |
| `--workers-max-restarts N` | cap how many dead workers get replaced. Default: one per worker; `0` disables |
| `--workers-no-freeze` | skip `gc.freeze()`. Diagnostic — it costs the saving once a worker runs a full collection |

The worker count is capped at the number of collected tests. Each worker gets
`PYTEST_ZYGOTE_WORKER=gw<n>` in its environment, like xdist's `PYTEST_XDIST_WORKER`.

## When a worker dies

A worker killed mid-test — in a container, usually the memory cgroup — would otherwise take
its test down silently, and with `-q` you would not even learn which one. The controller sees
the socket close, reports that test as failed with the signal name, puts the worker's queued
work back, and forks a replacement:

```
zygote worker gw0 killed by signal 9 (SIGKILL) while running this test.
In a container SIGKILL here is almost always the memory cgroup.
```

## How reports get back

Workers run `runtestprotocol(item, log=False)` and stream their reports to the controller as
length-prefixed JSON over a `socketpair()`. Only the controller calls
`pytest_runtest_logreport`, so the terminal reporter, `--junit-xml`, coverage and any plugin
of your own that listens to reports all see one ordered stream from one process. Nothing needs
locking and nothing is written twice.

Each worker is also told the test *after* the one it is running. pytest finalizes a fixture
when the next test no longer needs it, so without that lookahead every module- and
class-scoped fixture would tear down after each test.

## Limits

- **Needs `os.fork()`** — Linux and macOS. Not Windows.
- **Mutually exclusive with xdist's `-n`.** Pass one or the other.
- **`--workers` is also pytest-parallel's flag.** That plugin was archived in May 2024; if you
  have both installed, argparse fails at startup and you have to drop one.
- **Scheduling is per test**, so a module-scoped fixture is set up once per worker that draws a
  test from that module — the same trade xdist makes without `--dist loadfile`.
- **`-x` can overshoot** by a test or two, since several are in flight when the first failure
  lands.
- **Your conftest must not open things at import time.** A module-level DB connection, event
  loop or thread pool created during collection is inherited by every fork. Anything created
  inside a fixture is fine, because that runs in the worker.

## macOS

CI runs the suite on macOS, but the numbers above and the day-to-day use are Linux. macOS
treats forking a process that has already initialized certain Objective-C frameworks as
unsafe; if you hit `__NSPlaceholderDictionary initialize` crashes, set
`OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES`, the same workaround xdist users apply.

## License

MIT.

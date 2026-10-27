# Load-testing the two servers

This directory contains a reproducible end-to-end benchmark for the
`single_threaded` and `multi_threaded` branches. It is deliberately harder to
fool than a single `autocannon` invocation:

- Both branches are built from their committed tips in separate git worktrees,
  with `--release`.
- Both branches expose the **same HTTP request loop and the same endpoints**.
  The only difference between them is the concurrency model.
- Every measurement is preceded by a warm-up that is discarded.
- Each point is repeated (default 3 runs) and the **median** is reported.
- CPU affinity is topology-aware: on machines with at least 4 physical cores,
  the load generator gets one full core and the server gets the rest. On
  smaller machines pinning is disabled automatically, because sharing a
  physical core between client and server makes the baseline unfair.
- Workloads cover CPU-bound work, blocking I/O, a small in-memory response, a
  larger in-memory response, and a static file read from disk.
- Every workload has sustained-throughput tests at several concurrency levels.
  The CPU and blocking-I/O workloads also have **fixed-work** tests that force
  every request to complete, so p50/p99 include the time requests spend queued
  behind other connections.
- Throughput, latency percentiles, completed requests, errors, CPU cores used,
  and peak RSS are recorded in both JSON and Markdown.

## Endpoints

The server has no external dependencies; these routes live in `src/main.rs` and
are identical on both branches:

| Route | What it measures |
|---|---|
| `/` | Small HTML file read from disk on every request (file I/O + networking) |
| `/json` | Tiny JSON response built in memory (request/response path only) |
| `/memory?bytes=4096` | In-memory response of the requested size (network/memory path) |
| `/cpu?iters=1000000` | Fixed amount of integer CPU work (genuinely CPU-bound) |
| `/sleep?ms=10` | Handler blocks without using CPU (concurrency-bound) |

`iters`, `ms`, and `bytes` are clamped to safe maxima. The CPU loop has a data
dependency and a `black_box` at the end, so the optimizer cannot delete it.

`SERVER_ADDR` selects the bind address and `SERVER_THREADS` selects the fixed
pool size on the multi-threaded branch. Both default to the original values
(`127.0.0.1:8080` and 4 workers).

## Run it

From either branch after committing the benchmark changes:

```bash
./bench/run.py
```

A shorter smoke test:

```bash
./bench/run.py --runs 1 --duration 2 --warmup 1 --only cpu
```

A more statistically useful run:

```bash
./bench/run.py --runs 5 --duration 10 --warmup 3
```

Useful options:

```text
--threads N          worker count for the multi-threaded variant (default 4)
--control            also test multi_threaded with SERVER_THREADS=1
--no-pin             do not pin server/client CPUs
--server-cpus LIST   taskset CPU list for the server
--client-cpus LIST   taskset CPU list for autocannon
--branches A,B       branches to build (must include both servers)
--only SUBSTRING     run only matching scenario names
--keep-worktrees     leave build worktrees under /tmp/opencode for inspection
```

The last report is written to `bench/results/latest.md` and
`bench/results/latest.json`; timestamped copies are kept as well. Raw runs are
in the JSON file, including min/max RPS and run-to-run spread.

## How to read the results

- **RPS is the primary metric** for "which server handles more requests per
  second". The speedup column is `multi RPS / single RPS`.
- Compare **the same concurrency** and the **same workload** across branches.
  Concurrency 1 is a fairness check: with one connection, both servers do the
  same work in one thread, so they should be close.
- With concurrency greater than 1, a single-threaded blocking server can only
  serve one connection at a time. The fixed pool can serve up to `--threads`
  connections at once, so CPU-bound and sleep workloads should scale until the
  pool or the available cores are saturated.
- `CPU cores` is process CPU time divided by wall time, summed over all
  threads. `1.00` means one core was busy; `3.80` means the process used about
  3.8 cores. A single-threaded server cannot exceed roughly `1.00`.
- **Latency caveat:** in the sustained-throughput tables, autocannon only
  records latency for requests it completes. With keep-alive and concurrency
  above the number of threads that are actually serving, queued connections do
  not contribute latency samples. Therefore, at high concurrency compare
  throughput, not p50/p99.
- **Fixed-work tables** use `autocannon --amount N`, so every request must
  finish. Their p50/p99 therefore include queueing delay and are directly
  comparable between branches. A larger spread between the two p99 values is
  the clearest sign of head-of-line blocking in the single-threaded server.
- At concurrency 1 the sustained-throughput latency numbers are also directly
  comparable.

## Why the single-threaded branch was changed

Before this benchmark, `single_threaded` accepted a connection and immediately
wrote `hello.html` without reading the request at all. That means autocannon was
not measuring HTTP request handling; it was measuring one unread socket. The
branch now uses the same request parser and response loop as the multi-threaded
branch, so the comparison is about concurrency, not about one server being
unable to speak HTTP.

## When the load generator is the bottleneck

On small machines, `/json`, `/memory`, and sometimes `/` can hit the load
generator's ceiling before the server's ceiling. The tell-tale signs are:

- both branches report nearly the same RPS,
- server CPU stays well below one core per worker,
- increasing connections does not increase RPS.

In those cases the RPS comparison says "the client is the bottleneck", not
"the multi-threaded server is slower". The CPU-bound and sleep workloads are
the cleanest tests because the server is doing measurable work per request.

## Recommendations for the cleanest numbers

1. Close other CPU-heavy applications (browsers, IDEs, Docker builds).
2. Run on AC power with the performance governor if available.
3. Use a machine with at least 4 **physical** cores if you want automatic
   server/client isolation. On a 2-core/4-thread laptop the server and load
   generator must share physical cores, so use c=1 as the fairness check and
   trust the medians rather than a single run.
4. Prefer 5 runs and 10-second measurements for final numbers.
5. If the static/JSON workloads look client-bound (both branches hit the same
   RPS ceiling while server CPU is low), give autocannon more cores with
   `--client-cpus` or run the client on another machine.

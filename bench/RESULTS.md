# Measured results

> Snapshot, not a universal guarantee. Rerun `./bench/run.py --runs 3 --duration 5 --warmup 2`
> on the target machine; absolute numbers and client-boundedness will change.

## Key takeaways from this run

- **CPU-bound, 1 connection:** 218.8 req/s single vs 209.4 req/s multi (`0.96x`).
  This is the fairness check: with one connection both servers do the same work
  in one thread, so they should be close.
- **CPU-bound, 4+ connections:** single stays flat at ~190-230 req/s while the
  fixed pool reaches ~625-683 req/s (`2.94x`-`3.25x`). The pool used ~2.7-2.9
  logical cores; this laptop has only 2 physical cores, so the practical ceiling
  is below the pool's 4 workers.
- **CPU-bound fixed work (1,000 requests, c=16):** 5.02s single vs 2.02s multi.
  p99 latency: **1,533 ms** single vs **371 ms** multi.
- **Blocking I/O (`/sleep?ms=10`):** 92 req/s single vs 94 req/s multi at c=1
  (`1.02x`), then 94 req/s single vs ~377 req/s multi at c=4/16 (`~4.0x`,
  matching the 4-worker pool). Fixed 200-request run: 3.02s vs 1.02s; p99
  **1,781 ms** vs **425 ms**.
- **Static file:** 1.77x-1.87x faster at c=4/16. At c=1 the two are within 7%.
- **JSON and 4 KiB memory responses were client-bound on this laptop.** Both
  branches reported similar RPS, server CPU stayed below one core per worker,
  and the multi-threaded build was even slightly slower at some points. That is
  a limit of the load generator/host, not evidence that a thread pool is bad;
  use more cores (`--client-cpus`) or a separate load-generating machine before
  drawing conclusions from those rows.

The important pattern is the same in both the CPU and sleep tests: at c=1 the
branches are at parity, and as soon as concurrency exceeds one connection the
single-threaded server is capped by its one connection slot while the fixed pool
serves up to four requests at once. The fixed-work tests show the same thing in
latency form, including the queueing delay that the sustained-throughput
latency samples cannot show.

---

# Load-test report

Generated: 2026-10-26 15:18:26

## Environment

- Host: `thankgod-HP-ProBook-440-G5`
- CPU: Intel(R) Core(TM) i5-6200U CPU @ 2.30GHz (2 physical cores / 4 logical CPUs)
- Kernel: `7.0.0-34-generic`
- Rust: `rustc 1.97.1 (8bab26f4f 2026-07-14)`
- Load generator: `autocannon v8.0.0`
- Duration: 5.0s measured, 2.0s warm-up, 3 run(s) per point
- Server affinity: `none`; client affinity: `none`
- `single`: `single_threaded` @ `4a15145`
- `multi`: `multi_threaded` @ `ff9d929`

Reported values are the median across runs. RPS is the primary metric;
CPU is total process CPU summed over all threads (`1.00` = one saturated core).

> Sustained-throughput tables use a fixed duration and keep-alive connections.
> Autocannon only records latency for requests that complete, so at concurrency
> above the number of serving threads the queued connections have no latency
> samples. The fixed-work tables exist for that reason: they force every
> request to complete and include queueing time in p50/p99.

## CPU-bound sustained throughput (`/cpu?iters=1000000`)

| Connections | single req/s | multi req/s | speedup | single p50/p99 | multi p50/p99 | CPU cores (single/multi) |
|---|---:|---:|---:|---:|---:|---:|
| 1 | 218.8 | 209.4 | 0.96x | 4.0/7.0 | 4.0/8.0 | 0.91/0.90 |
| 4 | 205.6 | 630.8 | 3.07x | 4.0/7.0 | 5.0/17.0 | 0.90/2.71 |
| 8 | 192.4 | 624.4 | 3.25x | 4.0/13.0 | 5.0/17.0 | 0.83/2.72 |
| 16 | 232.4 | 683.4 | 2.94x | 4.0/6.0 | 5.0/14.0 | 0.94/2.88 |

## In-memory JSON (`/json`)

| Connections | single req/s | multi req/s | speedup | single p50/p99 | multi p50/p99 | CPU cores (single/multi) |
|---|---:|---:|---:|---:|---:|---:|
| 1 | 10,432.8 | 9,227.6 | 0.88x | 0.0/0.0 | 0.0/0.0 | 0.37/0.35 |
| 4 | 10,103.2 | 8,900.0 | 0.88x | 0.0/0.0 | 0.0/2.0 | 0.36/0.45 |
| 16 | 7,718.0 | 11,197.6 | 1.45x | 0.0/0.0 | 0.0/1.0 | 0.33/0.52 |

## Static file (`/`, small HTML read from disk)

| Connections | single req/s | multi req/s | speedup | single p50/p99 | multi p50/p99 | CPU cores (single/multi) |
|---|---:|---:|---:|---:|---:|---:|
| 1 | 7,632.0 | 7,120.4 | 0.93x | 0.0/0.0 | 0.0/0.0 | 0.44/0.44 |
| 4 | 7,223.6 | 12,760.8 | 1.77x | 0.0/0.0 | 0.0/1.0 | 0.42/0.82 |
| 16 | 5,982.8 | 11,183.2 | 1.87x | 0.0/0.0 | 0.0/1.0 | 0.41/0.74 |

## Blocking I/O (`/sleep?ms=10`)

| Connections | single req/s | multi req/s | speedup | single p50/p99 | multi p50/p99 | CPU cores (single/multi) |
|---|---:|---:|---:|---:|---:|---:|
| 1 | 92.2 | 94.0 | 1.02x | 10.0/12.0 | 10.0/12.0 | 0.02/0.01 |
| 4 | 94.2 | 376.6 | 4.00x | 10.0/12.0 | 10.0/12.0 | 0.01/0.04 |
| 16 | 93.8 | 378.4 | 4.03x | 10.0/12.0 | 10.0/12.0 | 0.01/0.04 |

## Larger in-memory response (`/memory?bytes=4096`)

| Connections | single req/s | multi req/s | speedup | single p50/p99 | multi p50/p99 | CPU cores (single/multi) |
|---|---:|---:|---:|---:|---:|---:|
| 4 | 14,927.2 | 13,160.8 | 0.88x | 0.0/0.0 | 0.0/1.0 | 0.41/0.60 |
| 16 | 15,101.6 | 12,954.4 | 0.86x | 0.0/0.0 | 0.0/1.0 | 0.43/0.60 |

## CPU-bound fixed work, c=16 (1,000 requests, all must complete)

| Scenario | Requests | Connections | single elapsed | single req/s | multi elapsed | multi req/s | speedup | single p50/p99 ms | multi p50/p99 ms | CPU cores (single/multi) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| cpu-queue-c16 | 1000 | 16 | 5.02s | 200.0 | 2.02s | 500.0 | 2.50x | 4.0/1533.0 | 5.0/371.0 | 0.87/2.08 |

## Blocking I/O fixed work, c=16 (200 requests, all must complete)

| Scenario | Requests | Connections | single elapsed | single req/s | multi elapsed | multi req/s | speedup | single p50/p99 ms | multi p50/p99 ms | CPU cores (single/multi) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| sleep-queue-c16 | 200 | 16 | 3.02s | 66.7 | 1.02s | 200.0 | 3.00x | 10.0/1781.0 | 10.0/425.0 | 0.01/0.03 |

## Raw run spread

| Scenario | Variant | Mode | RPS min | RPS median | RPS max | Elapsed median | CPU cores |
|---|---|---|---:|---:|---:|---:|---:|
| cpu-c1 | single | duration | 202.2 | 218.8 | 226.6 | 5.01s | 0.91 |
| cpu-c1 | multi | duration | 201.2 | 209.4 | 237.6 | 5.02s | 0.90 |
| cpu-c4 | single | duration | 180.8 | 205.6 | 210.2 | 5.04s | 0.90 |
| cpu-c4 | multi | duration | 525.4 | 630.8 | 691.0 | 5.02s | 2.71 |
| cpu-c8 | single | duration | 175.2 | 192.4 | 219.8 | 5.04s | 0.83 |
| cpu-c8 | multi | duration | 607.0 | 624.4 | 686.4 | 5.02s | 2.72 |
| cpu-c16 | single | duration | 215.2 | 232.4 | 233.8 | 5.02s | 0.94 |
| cpu-c16 | multi | duration | 582.8 | 683.4 | 716.0 | 5.03s | 2.88 |
| json-c1 | single | duration | 9,820.0 | 10,432.8 | 10,562.4 | 5.01s | 0.37 |
| json-c1 | multi | duration | 8,880.8 | 9,227.6 | 9,737.2 | 5.01s | 0.35 |
| json-c4 | single | duration | 8,382.8 | 10,103.2 | 10,525.6 | 5.01s | 0.36 |
| json-c4 | multi | duration | 8,444.8 | 8,900.0 | 13,797.6 | 5.02s | 0.45 |
| json-c16 | single | duration | 7,507.2 | 7,718.0 | 9,218.0 | 5.02s | 0.33 |
| json-c16 | multi | duration | 9,270.0 | 11,197.6 | 12,074.4 | 5.02s | 0.52 |
| static-c1 | single | duration | 6,791.6 | 7,632.0 | 7,725.2 | 5.01s | 0.44 |
| static-c1 | multi | duration | 4,213.0 | 7,120.4 | 8,020.0 | 5.01s | 0.44 |
| static-c4 | single | duration | 6,823.6 | 7,223.6 | 8,134.4 | 5.01s | 0.42 |
| static-c4 | multi | duration | 10,399.2 | 12,760.8 | 12,944.8 | 5.01s | 0.82 |
| static-c16 | single | duration | 5,458.6 | 5,982.8 | 7,432.4 | 5.03s | 0.41 |
| static-c16 | multi | duration | 9,554.0 | 11,183.2 | 11,677.6 | 5.02s | 0.74 |
| sleep-c1 | single | duration | 91.2 | 92.2 | 94.0 | 5.01s | 0.02 |
| sleep-c1 | multi | duration | 92.8 | 94.0 | 94.0 | 5.01s | 0.01 |
| sleep-c4 | single | duration | 93.4 | 94.2 | 94.2 | 5.02s | 0.01 |
| sleep-c4 | multi | duration | 376.2 | 376.6 | 378.8 | 5.02s | 0.04 |
| sleep-c16 | single | duration | 93.8 | 93.8 | 94.4 | 5.02s | 0.01 |
| sleep-c16 | multi | duration | 378.2 | 378.4 | 378.6 | 5.02s | 0.04 |
| memory-c4 | single | duration | 8,400.0 | 14,927.2 | 15,144.8 | 5.01s | 0.41 |
| memory-c4 | multi | duration | 13,016.8 | 13,160.8 | 13,448.8 | 5.01s | 0.60 |
| memory-c16 | single | duration | 8,806.4 | 15,101.6 | 16,360.0 | 5.02s | 0.43 |
| memory-c16 | multi | duration | 12,860.0 | 12,954.4 | 13,496.8 | 5.02s | 0.60 |
| cpu-queue-c16 | single | amount | 200.0 | 200.0 | 200.0 | 5.02s | 0.87 |
| cpu-queue-c16 | multi | amount | 500.0 | 500.0 | 500.0 | 2.02s | 2.08 |
| sleep-queue-c16 | single | amount | 66.7 | 66.7 | 66.7 | 3.02s | 0.01 |
| sleep-queue-c16 | multi | amount | 200.0 | 200.0 | 200.0 | 1.02s | 0.03 |


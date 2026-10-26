# multithreaded_webserver
Multithreaded webserver built in rust to utitlize rust's concurrency features.

## Load testing

Two implementations live on separate branches:

- `single_threaded` — one blocking connection at a time.
- `multi_threaded` — a fixed-size worker pool.

Both branches expose the same request parser and benchmark endpoints, so the
only difference under load is the concurrency model. See
[`bench/README.md`](bench/README.md) for the methodology and run:

```bash
./bench/run.py
```

The harness builds both branches in release mode, warms up, repeats each
measurement, pins the server and load generator to separate CPUs, and reports
throughput, latency, errors, CPU use, and run-to-run spread for CPU-bound,
file/static, in-memory, and blocking-I/O workloads.

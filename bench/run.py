#!/usr/bin/env python3
"""End-to-end load test for the single-threaded and fixed-thread-pool servers.

The script builds both branches in isolated git worktrees, starts each server in
turn, warms it up, runs autocannon, and writes a median-of-N-runs report.

Only committed branch tips are tested. Run it from a clean checkout after
committing the server/benchmark changes.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RESULTS_DIR = REPO / "bench" / "results"

# ---------------------------------------------------------------------------
# Workloads
# ---------------------------------------------------------------------------
# Each scenario is run once per variant and repeated --runs times. "duration"
# scenarios measure sustained throughput; "amount" scenarios force a fixed
# number of requests to complete, so every request (including queued ones) gets
# a latency sample.
SCENARIOS = [
    {"name": "cpu-c1", "path": "/cpu?iters=1000000", "connections": 1, "workload": "cpu", "mode": "duration"},
    {"name": "cpu-c4", "path": "/cpu?iters=1000000", "connections": 4, "workload": "cpu", "mode": "duration"},
    {"name": "cpu-c8", "path": "/cpu?iters=1000000", "connections": 8, "workload": "cpu", "mode": "duration"},
    {"name": "cpu-c16", "path": "/cpu?iters=1000000", "connections": 16, "workload": "cpu", "mode": "duration"},
    {"name": "json-c1", "path": "/json", "connections": 1, "workload": "json", "mode": "duration"},
    {"name": "json-c4", "path": "/json", "connections": 4, "workload": "json", "mode": "duration"},
    {"name": "json-c16", "path": "/json", "connections": 16, "workload": "json", "mode": "duration"},
    {"name": "static-c1", "path": "/", "connections": 1, "workload": "static", "mode": "duration"},
    {"name": "static-c4", "path": "/", "connections": 4, "workload": "static", "mode": "duration"},
    {"name": "static-c16", "path": "/", "connections": 16, "workload": "static", "mode": "duration"},
    {"name": "sleep-c1", "path": "/sleep?ms=10", "connections": 1, "workload": "sleep", "mode": "duration"},
    {"name": "sleep-c4", "path": "/sleep?ms=10", "connections": 4, "workload": "sleep", "mode": "duration"},
    {"name": "sleep-c16", "path": "/sleep?ms=10", "connections": 16, "workload": "sleep", "mode": "duration"},
    {"name": "memory-c4", "path": "/memory?bytes=4096", "connections": 4, "workload": "memory", "mode": "duration"},
    {"name": "memory-c16", "path": "/memory?bytes=4096", "connections": 16, "workload": "memory", "mode": "duration"},
    # Fixed-work queueing tests: all requests must finish, so p50/p99 include
    # time spent waiting behind other connections.
    {"name": "cpu-queue-c16", "path": "/cpu?iters=1000000", "connections": 16,
     "workload": "cpu-queue", "mode": "amount", "amount": 1000},
    {"name": "sleep-queue-c16", "path": "/sleep?ms=10", "connections": 16,
     "workload": "sleep-queue", "mode": "amount", "amount": 200},
]

WORKLOAD_TITLES = {
    "cpu": "CPU-bound sustained throughput (`/cpu?iters=1000000`)",
    "json": "In-memory JSON (`/json`)",
    "static": "Static file (`/`, small HTML read from disk)",
    "sleep": "Blocking I/O (`/sleep?ms=10`)",
    "memory": "Larger in-memory response (`/memory?bytes=4096`)",
}
QUEUE_WORKLOAD_TITLES = {
    "cpu-queue": "CPU-bound fixed work, c=16 (1,000 requests, all must complete)",
    "sleep-queue": "Blocking I/O fixed work, c=16 (200 requests, all must complete)",
}


def run(cmd, **kwargs):
    return subprocess.run(cmd, check=True, text=True, **kwargs)


def git(*args) -> str:
    return run(["git", *args], cwd=REPO, capture_output=True).stdout.strip()


def autocannon_version() -> str:
    exe = shutil.which("autocannon")
    if not exe:
        sys.exit("autocannon is not installed or not on PATH")
    try:
        lines = run([exe, "--version"], capture_output=True).stdout.strip().splitlines()
        return lines[0] if lines else "autocannon"
    except Exception:
        return "autocannon"


def prepare_branch(branch: str, build_root: Path) -> Path:
    """Create a detached worktree at the branch tip and build it in release mode."""
    dest = build_root / branch.replace("/", "_")
    if dest.exists():
        shutil.rmtree(dest)
    run(["git", "worktree", "add", "--detach", str(dest), branch], cwd=REPO)
    run(["cargo", "build", "--release"], cwd=dest)

    binary = dest / "target" / "release" / "multithreaded_server"
    if not binary.exists():
        sys.exit(f"build did not produce {binary}")
    return binary


def cleanup_worktree(path: Path):
    try:
        run(["git", "worktree", "remove", "--force", str(path)], cwd=REPO)
    except subprocess.CalledProcessError:
        shutil.rmtree(path, ignore_errors=True)
        try:
            run(["git", "worktree", "prune"], cwd=REPO)
        except subprocess.CalledProcessError:
            pass


def wait_for_port(port: int, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"server did not listen on 127.0.0.1:{port}")


def http_get(port: int, path: str, timeout: float = 5.0) -> bytes:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"GET {path} returned {response.status}")
        return response.read()


def cpu_seconds(pid: int) -> float:
    """utime + stime for a process, in seconds."""
    stat = Path(f"/proc/{pid}/stat").read_text()
    fields = stat.rsplit(")", 1)[1].split()
    ticks = int(fields[11]) + int(fields[12])  # utime, stime
    return ticks / os.sysconf("SC_CLK_TCK")


def peak_rss_mb(pid: int) -> float:
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("VmHWM:"):
            return int(line.split()[1]) / 1024
    return 0.0


def parse_autocannon(stdout: str) -> dict:
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            return json.loads(line)
    raise RuntimeError(f"could not parse autocannon output:\n{stdout}")


def autocannon_args(scenario: dict, duration: int, port: int) -> list[str]:
    # -j is newline-delimited JSON, -t 30 avoids timeouts for queued keep-alive
    # connections, -p 1 measures one request at a time per connection.
    args = [
        "-j",
        "-c", str(scenario["connections"]),
        "-p", "1",
        "-t", "30",
    ]
    if scenario["mode"] == "amount":
        args += ["-a", str(scenario["amount"])]
    else:
        args += ["-d", str(duration)]
    args.append(f"http://127.0.0.1:{port}{scenario['path']}")
    return args


def run_autocannon(scenario: dict, duration: int, port: int,
                   client_cpus: str | None) -> dict:
    cmd = ["autocannon", *autocannon_args(scenario, duration, port)]
    if client_cpus:
        cmd = ["taskset", "-c", client_cpus, *cmd]

    proc = run(cmd, capture_output=True)
    return parse_autocannon(proc.stdout)


def run_one(binary: Path, env_extra: dict[str, str], scenario: dict,
            run_index: int, args, server_cpus: str | None,
            client_cpus: str | None) -> dict:
    name = scenario["name"]
    path = scenario["path"]
    connections = scenario["connections"]
    port = args.port

    env = os.environ.copy()
    env["SERVER_ADDR"] = f"127.0.0.1:{port}"
    env.update(env_extra)

    cmd = [str(binary)]
    if server_cpus:
        cmd = ["taskset", "-c", server_cpus, *cmd]

    server = subprocess.Popen(
        cmd,
        cwd=binary.parent.parent.parent,  # worktree root: <root>/target/release/bin
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        wait_for_port(port)

        # Warm up the process (CPU frequency, allocator, page cache). Warm-up
        # traffic is never included in the measurements.
        if args.warmup > 0:
            warmup = max(1, int(round(args.warmup)))
            warmup_scenario = dict(scenario, mode="duration")
            warmup_scenario.pop("amount", None)
            run_autocannon(warmup_scenario, warmup, port, client_cpus)
            time.sleep(0.3)

        cpu_before = cpu_seconds(server.pid)
        started = time.monotonic()
        ac = run_autocannon(scenario, max(1, int(round(args.duration))), port, client_cpus)
        wall = time.monotonic() - started

        time.sleep(0.05)
        cpu_after = cpu_seconds(server.pid)
        rss = peak_rss_mb(server.pid)

        sample_seconds = float(ac.get("duration", wall)) or wall
        cpu_pct = (cpu_after - cpu_before) / sample_seconds * 100.0

        result = {
            "scenario": name,
            "workload": scenario["workload"],
            "path": path,
            "mode": scenario["mode"],
            "amount": int(scenario.get("amount", 0)),
            "connections": connections,
            "run": run_index + 1,
            "rps": float(ac["requests"]["average"]),
            "rps_total": int(ac["requests"]["total"]),
            "rps_stddev": float(ac["requests"].get("stddev", 0.0)),
            "latency_p50_ms": float(ac["latency"].get("p50", 0.0)),
            "latency_p99_ms": float(ac["latency"].get("p99", 0.0)),
            "latency_max_ms": float(ac["latency"].get("max", 0.0)),
            "throughput_mb_s": float(ac["throughput"]["average"]) / (1024 * 1024),
            "errors": int(ac.get("errors", 0)),
            "timeouts": int(ac.get("timeouts", 0)),
            "non2xx": int(ac.get("non2xx", 0)),
            "completed_2xx": int(ac.get("2xx", 0)),
            "cpu_pct": cpu_pct,
            "cpu_cores": cpu_pct / 100.0,
            "peak_rss_mb": rss,
            "duration_s": float(ac.get("duration", 0.0)),
            "wall_s": wall,
        }
        return result
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()


def median(values: list[float]) -> float:
    return statistics.median(values) if values else 0.0


def summarize(runs: list[dict]) -> dict:
    def col(key):
        return [float(r[key]) for r in runs]

    return {
        "runs": len(runs),
        "connections": int(runs[0]["connections"]),
        "mode": runs[0]["mode"],
        "amount": int(runs[0]["amount"]),
        "rps": median(col("rps")),
        "rps_min": min(col("rps")),
        "rps_max": max(col("rps")),
        "p50": median(col("latency_p50_ms")),
        "p99": median(col("latency_p99_ms")),
        "duration_s": median(col("duration_s")),
        "throughput_mb_s": median(col("throughput_mb_s")),
        "cpu_cores": median(col("cpu_cores")),
        "peak_rss_mb": median(col("peak_rss_mb")),
        "errors": sum(int(r["errors"]) for r in runs),
        "timeouts": sum(int(r["timeouts"]) for r in runs),
        "non2xx": sum(int(r["non2xx"]) for r in runs),
    }


def fmt(value: float, digits: int = 1) -> str:
    return f"{value:,.{digits}f}"


def speedup(a: float, b: float) -> str:
    if a <= 0:
        return "n/a"
    return f"{b / a:.2f}x"


def cpu_model() -> str:
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "unknown"


def _expand_cpu_list(text: str) -> list[int]:
    cpus = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            first, last = part.split("-", 1)
            cpus.extend(range(int(first), int(last) + 1))
        else:
            cpus.append(int(part))
    return cpus


def cpu_core_groups() -> list[list[int]]:
    """Physical core groups (logical CPUs that share a core) in the affinity set."""
    try:
        available = set(os.sched_getaffinity(0))
    except AttributeError:
        available = set(range(os.cpu_count() or 1))

    groups: dict[tuple[int, ...], list[int]] = {}
    for cpu in sorted(available):
        try:
            text = Path(
                f"/sys/devices/system/cpu/cpu{cpu}/topology/thread_siblings_list"
            ).read_text().strip()
            siblings = tuple(sorted(c for c in _expand_cpu_list(text) if c in available))
        except OSError:
            siblings = (cpu,)
        groups.setdefault(siblings, list(siblings))
    return list(groups.values())


def cpu_topology() -> tuple[int, int]:
    """Return (logical CPUs, physical cores) when /proc/cpuinfo exposes them."""
    logical = os.cpu_count() or 1
    try:
        text = Path("/proc/cpuinfo").read_text()
    except OSError:
        return logical, logical

    physical_cores = set()
    physical_id = None
    core_id = None
    for line in text.splitlines() + [""]:
        if not line.strip():
            if physical_id is not None and core_id is not None:
                physical_cores.add((physical_id, core_id))
            physical_id = core_id = None
        elif line.startswith("physical id"):
            physical_id = line.split(":", 1)[1].strip()
        elif line.startswith("core id"):
            core_id = line.split(":", 1)[1].strip()
    return logical, len(physical_cores) or logical


def make_report(args, variants, summaries, commits, pinning) -> str:
    logical_cpus, physical_cores = cpu_topology()
    lines = []
    lines.append("# Load-test report")
    lines.append("")
    lines.append(f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")
    lines.append("## Environment")
    lines.append("")
    lines.append(f"- Host: `{platform.node()}`")
    lines.append(f"- CPU: {cpu_model()} ({physical_cores} physical cores / {logical_cpus} logical CPUs)")
    lines.append(f"- Kernel: `{platform.release()}`")
    lines.append(f"- Rust: `{run(['rustc', '--version'], capture_output=True).stdout.strip()}`")
    lines.append(f"- Load generator: `{autocannon_version()}`")
    lines.append(f"- Duration: {args.duration}s measured, {args.warmup}s warm-up, {args.runs} run(s) per point")
    lines.append(f"- Server affinity: `{pinning['server'] or 'none'}`; client affinity: `{pinning['client'] or 'none'}`")
    for variant in variants:
        lines.append(f"- `{variant['name']}`: `{variant['branch']}` @ `{commits[variant['branch']]}`"
                     + (" (SERVER_THREADS=1)" if variant['env'].get("SERVER_THREADS") == "1" else ""))
    lines.append("")
    lines.append("Reported values are the median across runs. RPS is the primary metric;")
    lines.append("CPU is total process CPU summed over all threads (`1.00` = one saturated core).")
    lines.append("")
    lines.append("> Sustained-throughput tables use a fixed duration and keep-alive connections.")
    lines.append("> Autocannon only records latency for requests that complete, so at concurrency")
    lines.append("> above the number of serving threads the queued connections have no latency")
    lines.append("> samples. The fixed-work tables exist for that reason: they force every")
    lines.append("> request to complete and include queueing time in p50/p99.")
    lines.append("")

    # Group by workload, preserving scenario ordering.
    order = [s["name"] for s in SCENARIOS]
    scenario_names = [n for n in order if n in summaries]

    def append_error_notes(names: list[str]):
        errors = []
        for name in names:
            block = summaries[name]
            for variant in variants:
                m = block[variant["name"]]
                if m["errors"] or m["timeouts"] or m["non2xx"]:
                    errors.append(f"{name}/{variant['name']}: "
                                  f"{m['errors']} errors, {m['timeouts']} timeouts, {m['non2xx']} non-2xx")
        if errors:
            lines.append("Non-zero error counters:")
            lines.append("")
            for item in errors:
                lines.append(f"- {item}")
            lines.append("")

    # Sustained throughput (duration-based) tables.
    for workload, title in WORKLOAD_TITLES.items():
        names = [n for n in scenario_names if summaries[n][variants[0]["name"]]["workload"] == workload]
        if not names:
            continue
        lines.append(f"## {title}")
        lines.append("")
        header = ["Connections"]
        for variant in variants:
            header += [f"{variant['name']} req/s"]
        if len(variants) >= 2:
            header.append("speedup")
            header += [f"{variants[0]['name']} p50/p99", f"{variants[-1]['name']} p50/p99"]
        header += ["CPU cores (single/multi)"]
        lines.append("| " + " | ".join(header) + " |")
        lines.append("|" + "|".join(["---"] + ["---:" for _ in header[1:]]) + "|")

        for name in names:
            block = summaries[name]
            first = block[variants[0]["name"]]
            row = [str(first["connections"])]
            for variant in variants:
                row.append(fmt(block[variant["name"]]["rps"]))
            if len(variants) >= 2:
                row.append(speedup(first["rps"], block[variants[-1]["name"]]["rps"]))
                for variant in (variants[0], variants[-1]):
                    m = block[variant["name"]]
                    row.append(f"{m['p50']:.1f}/{m['p99']:.1f}")
            row.append("/".join(f"{block[v['name']]['cpu_cores']:.2f}" for v in variants))
            lines.append("| " + " | ".join(row) + " |")
        lines.append("")
        append_error_notes(names)

    # Fixed-work queueing tables: every request completes, so latency includes
    # time spent waiting behind other connections.
    for workload, title in QUEUE_WORKLOAD_TITLES.items():
        names = [n for n in scenario_names if summaries[n][variants[0]["name"]]["workload"] == workload]
        if not names:
            continue
        lines.append(f"## {title}")
        lines.append("")
        header = ["Scenario", "Requests", "Connections"]
        for variant in variants:
            header += [f"{variant['name']} elapsed", f"{variant['name']} req/s"]
        header += ["speedup"]
        for variant in (variants[0], variants[-1]):
            header += [f"{variant['name']} p50/p99 ms"]
        header += ["CPU cores (single/multi)"]
        lines.append("| " + " | ".join(header) + " |")
        lines.append("|" + "|".join(["---", "---:", "---:"] + ["---:" for _ in header[3:]]) + "|")

        for name in names:
            block = summaries[name]
            first = block[variants[0]["name"]]
            row = [name, str(first["amount"]), str(first["connections"])]
            for variant in variants:
                m = block[variant["name"]]
                row += [f"{m['duration_s']:.2f}s", fmt(m["rps"])]
            row.append(speedup(first["rps"], block[variants[-1]["name"]]["rps"]))
            for variant in (variants[0], variants[-1]):
                m = block[variant["name"]]
                row.append(f"{m['p50']:.1f}/{m['p99']:.1f}")
            row.append("/".join(f"{block[v['name']]['cpu_cores']:.2f}" for v in variants))
            lines.append("| " + " | ".join(row) + " |")
        lines.append("")
        append_error_notes(names)

    lines.append("## Raw run spread")
    lines.append("")
    lines.append("| Scenario | Variant | Mode | RPS min | RPS median | RPS max | Elapsed median | CPU cores |")
    lines.append("|---|---|---|---:|---:|---:|---:|---:|")
    for name in scenario_names:
        block = summaries[name]
        for variant in variants:
            m = block[variant["name"]]
            lines.append(f"| {name} | {variant['name']} | {m['mode']} | {fmt(m['rps_min'])} | "
                         f"{fmt(m['rps'])} | {fmt(m['rps_max'])} | {m['duration_s']:.2f}s | "
                         f"{m['cpu_cores']:.2f} |")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runs", type=int, default=3, help="repetitions per point (default: 3)")
    parser.add_argument("--duration", type=float, default=5, help="measured seconds per run (default: 5)")
    parser.add_argument("--warmup", type=float, default=2, help="warm-up seconds per run (default: 2)")
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument("--threads", type=int, default=4, help="worker count for the multi-threaded variant")
    parser.add_argument("--branches", default="single_threaded,multi_threaded")
    parser.add_argument("--only", default="", help="only run scenarios whose name contains this substring")
    parser.add_argument("--control", action="store_true",
                        help="also run the multi-threaded build with SERVER_THREADS=1 as a sanity check")
    parser.add_argument("--server-cpus", default=None, help="taskset CPU list for the server")
    parser.add_argument("--client-cpus", default=None, help="taskset CPU list for autocannon")
    parser.add_argument("--no-pin", action="store_true", help="disable CPU pinning")
    parser.add_argument("--keep-worktrees", action="store_true")
    parser.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
    args = parser.parse_args()

    if not shutil.which("autocannon"):
        sys.exit("autocannon is required (npm i -g autocannon)")
    if args.runs < 1:
        sys.exit("--runs must be >= 1")
    if args.duration < 1:
        sys.exit("--duration must be >= 1 second")
    if args.warmup < 0:
        sys.exit("--warmup must be >= 0 seconds")

    branches = [b.strip() for b in args.branches.split(",") if b.strip()]
    if "single_threaded" not in branches or "multi_threaded" not in branches:
        sys.exit("--branches must include single_threaded and multi_threaded")

    variants = [
        {"name": "single", "branch": "single_threaded", "env": {}},
        {"name": "multi", "branch": "multi_threaded", "env": {"SERVER_THREADS": str(args.threads)}},
    ]
    if args.control:
        variants.append({"name": "multi-1", "branch": "multi_threaded",
                         "env": {"SERVER_THREADS": "1"}})

    # Pin only when the machine has enough physical cores to give the load
    # generator its own core. Pinning on a 2-core/4-thread laptop makes c=1
    # results depend on which hyperthread the OS happens to use.
    pinning = {"server": None, "client": None}
    if not args.no_pin:
        if args.server_cpus or args.client_cpus:
            pinning["server"] = args.server_cpus
            pinning["client"] = args.client_cpus
        else:
            groups = cpu_core_groups()
            if len(groups) >= 4:
                pinning["client"] = ",".join(str(c) for c in groups[-1])
                pinning["server"] = ",".join(str(c) for group in groups[:-1] for c in group)
            else:
                print(f"note: {len(groups)} physical core(s) available; "
                      "automatic CPU pinning disabled", file=sys.stderr)

    if (pinning["server"] or pinning["client"]) and not shutil.which("taskset"):
        sys.exit("CPU pinning was requested but taskset is not available; use --no-pin")

    scenarios = [s for s in SCENARIOS if args.only in s["name"]]
    if not scenarios:
        sys.exit(f"no scenarios matched --only={args.only!r}")

    build_root = Path(tempfile.mkdtemp(prefix="server-bench-", dir="/tmp/opencode"))
    worktrees: dict[str, Path] = {}
    binaries: dict[str, Path] = {}
    results = []
    raw_runs = []
    commits = {}

    try:
        print("Preparing release builds...", flush=True)
        for branch in branches:
            commits[branch] = git("rev-parse", "--short", branch)
            print(f"  {branch} @ {commits[branch]}", flush=True)
            binaries[branch] = prepare_branch(branch, build_root)
            worktrees[branch] = binaries[branch].parent.parent.parent

        # Fail fast if any endpoint is broken before spending minutes on load.
        print("Checking endpoints...", flush=True)
        for variant in variants:
            binary = binaries[variant["branch"]]
            env = os.environ.copy()
            env["SERVER_ADDR"] = f"127.0.0.1:{args.port}"
            env.update(variant["env"])
            cmd = [str(binary)]
            if pinning["server"]:
                cmd = ["taskset", "-c", pinning["server"], *cmd]
            proc = subprocess.Popen(cmd, cwd=binary.parent.parent.parent, env=env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                wait_for_port(args.port)
                http_get(args.port, "/")
                http_get(args.port, "/cpu?iters=1000")
                http_get(args.port, "/sleep?ms=1")
                http_get(args.port, "/memory?bytes=16")
                http_get(args.port, "/json")
            finally:
                proc.terminate()
                proc.wait(timeout=5)

        total = len(scenarios) * len(variants) * args.runs
        done = 0

        for variant in variants:
            print(f"\n=== {variant['name']} ({variant['branch']}) ===", flush=True)
            for scenario in scenarios:
                name = scenario["name"]
                for run_index in range(args.runs):
                    done += 1
                    print(f"[{done}/{total}] {variant['name']} {name} run {run_index + 1}",
                          flush=True)
                    result = run_one(
                        binaries[variant["branch"]],
                        variant["env"],
                        scenario,
                        run_index,
                        args,
                        pinning["server"],
                        pinning["client"],
                    )
                    result["variant"] = variant["name"]
                    raw_runs.append(result)
                    results.append((scenario, variant, result))

        summaries: dict[str, dict[str, dict]] = {}
        for scenario, variant, result in results:
            name = scenario["name"]
            summaries.setdefault(name, {}).setdefault(variant["name"], []).append(result)

        aggregated = {
            name: {
                variant_name: summarize(runs)
                for variant_name, runs in variant_runs.items()
            }
            for name, variant_runs in summaries.items()
        }
        workload_by_name = {s["name"]: s["workload"] for s in SCENARIOS}
        for name, block in aggregated.items():
            for variant_name in block:
                block[variant_name]["workload"] = workload_by_name[name]

        args.results_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        json_path = args.results_dir / f"report-{stamp}.json"
        md_path = args.results_dir / f"report-{stamp}.md"
        payload = {
            "generated": stamp,
            "args": vars(args) | {"results_dir": str(args.results_dir)},
            "commits": commits,
            "pinning": pinning,
            "host": {
                "node": platform.node(),
                "cpu": cpu_model(),
                "physical_cores": cpu_topology()[1],
                "logical_cpus": cpu_topology()[0],
                "kernel": platform.release(),
                "rustc": run(["rustc", "--version"], capture_output=True).stdout.strip(),
                "autocannon": autocannon_version(),
            },
            "raw_runs": raw_runs,
            "summary": aggregated,
        }
        json_path.write_text(json.dumps(payload, indent=2) + "\n")

        report = make_report(args, variants, aggregated, commits, pinning)
        md_path.write_text(report + "\n")
        (args.results_dir / "latest.md").write_text(report + "\n")
        (args.results_dir / "latest.json").write_text(json.dumps(payload, indent=2) + "\n")

        print("\n" + report)
        print(f"\nWrote {md_path}")
        print(f"Wrote {json_path}")
        return 0
    finally:
        if not args.keep_worktrees:
            for path in worktrees.values():
                cleanup_worktree(path)
        else:
            print(f"Worktrees kept under {build_root}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)

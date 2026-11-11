"""What the system did during a load test, from Prometheus (docs/load_test.md).

    python loadtest/prom_snapshot.py --start <unix> --end <unix> [--out file.json]

Summarises the window: runs by status and their server-side latency (submission to final
status, queue wait, processing), queue depth, worker utilisation, the gateway's model-call
latency, API latency, and CPU and memory per container (cAdvisor). Uses only the standard
library; Prometheus at PROMETHEUS_URL (default http://localhost:9090).
"""

import argparse
import json
import os
import urllib.parse
import urllib.request

PROMETHEUS = os.environ.get("PROMETHEUS_URL", "http://localhost:9090")
SERVICE = "container_label_com_docker_compose_service"
PROJECT = 'container_label_com_docker_compose_project=~".*dispute.*"'


def query(expr: str, at: int) -> list[dict]:
    url = f"{PROMETHEUS}/api/v1/query?" + urllib.parse.urlencode({"query": expr, "time": at})
    with urllib.request.urlopen(url, timeout=30) as res:
        body = json.load(res)
    if body["status"] != "success":
        raise RuntimeError(f"{expr}: {body}")
    return body["data"]["result"]


def scalar(expr: str, at: int) -> float | None:
    result = query(expr, at)
    if not result:
        return None
    value = float(result[0]["value"][1])
    return None if value != value else round(value, 3)  # NaN -> None


def by_label(expr: str, at: int, label: str) -> dict[str, float]:
    return {r["metric"].get(label, ""): round(float(r["value"][1]), 3)
            for r in query(expr, at) if r["value"][1] not in ("NaN",)}


def snapshot(start: int, end: int) -> dict:
    w = f"{max(end - start, 60)}s"
    q = lambda p, m, extra="": scalar(  # noqa: E731
        f"histogram_quantile({p}, sum by (le) (increase({m}_bucket{extra}[{w}])))", end)
    workers = scalar('count(up{job="worker"} == 1)', end) or 1
    return {
        "window_seconds": end - start,
        "workers": workers,
        "runs_by_status": by_label(
            f"sum by (status) (increase(runs_finished_total[{w}]))", end, "status"),
        "run_end_to_end_s": {"p50": q(0.5, "run_end_to_end_seconds"),
                             "p95": q(0.95, "run_end_to_end_seconds")},
        "run_queue_wait_s": {"p50": q(0.5, "run_queue_wait_seconds"),
                             "p95": q(0.95, "run_queue_wait_seconds")},
        "run_processing_s": {"p50": q(0.5, "run_processing_seconds"),
                             "p95": q(0.95, "run_processing_seconds")},
        "queue_depth": {
            "max": scalar(f'max_over_time(max(rq_jobs{{state="queued"}})[{w}:5s])', end),
            "at_end": scalar('max(rq_jobs{state="queued"})', end)},
        "worker_utilisation_mean": scalar(
            f"sum(increase(worker_busy_seconds_total[{w}])) / ({workers} * {end - start})",
            end),
        "model_calls_by_step": by_label(
            f"sum by (step) (increase(llm_requests_total[{w}]))", end, "step"),
        "gateway_latency_s": {"p50": q(0.5, "llm_latency_seconds"),
                              "p95": q(0.95, "llm_latency_seconds")},
        "agent_node_p95_s": by_label(
            f"histogram_quantile(0.95, sum by (le, node) "
            f"(increase(agent_node_seconds_bucket[{w}])))", end, "node"),
        "api_latency_p95_s": by_label(
            f"histogram_quantile(0.95, sum by (le, path) "
            f"(increase(http_request_duration_seconds_bucket{{path!=\"/metrics\"}}[{w}])))",
            end, "path"),
        "api_requests_by_status": by_label(
            f"sum by (status) (increase(http_requests_total{{path!=\"/metrics\"}}[{w}]))",
            end, "status"),
        "cpu_cores_mean": by_label(
            f"avg_over_time((sum by ({SERVICE}) (rate(container_cpu_usage_seconds_total"
            f"{{{PROJECT}}}[1m])))[{w}:15s])", end, SERVICE),
        "cpu_cores_max": by_label(
            f"max_over_time((sum by ({SERVICE}) (rate(container_cpu_usage_seconds_total"
            f"{{{PROJECT}}}[1m])))[{w}:15s])", end, SERVICE),
        "memory_mib_max": {k: round(v / 2**20) for k, v in by_label(
            f"max_over_time((sum by ({SERVICE}) (container_memory_working_set_bytes"
            f"{{{PROJECT}}}))[{w}:15s])", end, SERVICE).items()},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--start", type=int, required=True)
    parser.add_argument("--end", type=int, required=True)
    parser.add_argument("--out")
    args = parser.parse_args()
    data = snapshot(args.start, args.end)
    text = json.dumps(data, indent=2)
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")


if __name__ == "__main__":
    main()

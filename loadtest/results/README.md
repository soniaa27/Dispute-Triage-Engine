# Load test results (11 November 2026)

Analysed in [docs/load_test.md](../../docs/load_test.md). Per run: Locust's CSVs (`_stats`,
`_stats_history`, `_failures`, `_exceptions`), the per-step summary with every run's time and
status (`_summary.json`), and Prometheus over the run's window (`_prometheus.json`).

| Run | Workers | Load | Valid |
|-----|--------:|------|-------|
| `a_1worker_0.1rps` | 1 | 0.1/s for 10 min | yes |
| `b_1worker_ramp` | 1 | 0.025, 0.05, 0.075, 0.1/s, 5 min each | yes (the Mac slept during the drain, after the last step) |
| `c_3workers_ramp` | 3 | 0.05 to 0.3/s, 4 min each | steps 1 to 3 only: the Mac slept during step 4 to 5 |
| `c2_3workers_ramp_upper` | 3 | 0.2, 0.25, 0.3/s, 4 min each | yes (re-run of the invalid steps, machine kept awake) |
| `d_3workers_0.1rps` | 3 | 0.1/s for 10 min | yes |

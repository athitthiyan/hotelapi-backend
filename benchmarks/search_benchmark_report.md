# StayEase Search Benchmark Report

| Rooms | p50 (ms) | p95 (ms) | p99 (ms) |
|---|---:|---:|---:|
| 100 | 11.31 | 26.75 | 26.75 |
| 500 | 16.39 | 22.66 | 22.66 |
| 1000 | 12.32 | 16.56 | 16.56 |

Notes:
- Environment: FastAPI TestClient with temporary SQLite database.
- Cache was cleared before every request to measure uncached search latency.
- Dataset was seeded with available Chennai rooms and active inventory for the queried date range.

# Employee Attendance & Analytics API

FastAPI + MongoDB. All application code is in `app/main.py`.

## Run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # MONGO_URI / MONGO_DB (real environment variables take priority)
uvicorn app.main:app --port 8000
```
MongoDB 6.0+ is required (local `docker run -p 27017:27017 mongo:7` or Atlas M0). Indexes are created at start-up; no manual steps.

## Check it
`python smoke_test.py` runs against a live server (`BASE_URL`, default `http://localhost:8000`). It creates its own
employees and logs in the far-future month 2031-03, covering: grace-period boundary, half-up rounding, overnight shift,
mid-month joiner, employees with no logs, a weekend record, ties at the leaderboard cutoff, the 7-day moving average,
concurrent punch-in / punch-out, regularization history, and the explain endpoint (IXSCAN, no COLLSCAN).

## Notes
- Everything is implemented: all endpoints in `openapi.yaml`, the three aggregation pipelines, and `/admin/explain/{endpoint}`.
- Analytics read the stored derived values. Rounding is half-up (Python `Decimal`, or an explicit floor-based expression inside the trend pipeline, because `$round` rounds half to even).
- The department trend builds one row per day with `$range` + `$unwind` inside the pipeline (instead of `$densify`), so
  it also works when the department has no logs at all.
- Concurrent PATCH requests use an optimistic check on the record's current state; the loser gets 409 and nothing is overwritten.
- The review of the starter code is in `REVIEW.md`; design answers are in `DECISIONS.md`.

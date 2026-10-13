# Employee Attendance & Analytics API

FastAPI + PyMongo implementation for the candidate assignment. Requires Python 3.11+ and MongoDB 6.0+.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI='mongodb://localhost:27017'
export MONGO_DB='attendance_db'
python sample_seed.py
uvicorn app.main:app --port 8000
```

The application creates indexes idempotently on startup. `GET /health` pings MongoDB. Environment variables take precedence over `.env`.

## Implementation notes

- Unique indexes enforce employee-code and employee/date uniqueness under concurrency.
- Instants are stored as BSON dates and returned/accepted as epoch milliseconds.
- Attendance calculations use IST shift rules and half-up rounding.
- Manual regularizations append audit entries and use optimistic compare-and-swap; concurrent losing edits return 409.
- Analytics are implemented with MongoDB aggregation pipelines. Department trend uses `$densify` and `$setWindowFields` for gap filling and moving averages.
- `GET /admin/explain/{endpoint}` returns MongoDB execution-statistics explain output for the supported endpoint query shapes.

## Important

Run the application against MongoDB 6.0+ and exercise the awkward cases in the assignment before submission. The explain endpoint reports the actual server plan; index use depends on the specific filter combination and MongoDB's planner. Read `REVIEW.md` and `DECISIONS.md` and be ready to explain the implementation in your own words.

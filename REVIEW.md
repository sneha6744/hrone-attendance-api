# REVIEW.md

Defects found in the starter `app/main.py`, in the order they appear in the file. All are fixed in the submitted code.

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Builds the shift start with `punch_in.replace(hour=..)` on a UTC/naive datetime, so "09:30" means 09:30 UTC (15:00 IST), not IST. | Shift 09:30, punch-in 10:05 IST (04:35Z) is treated as 5 hours *early* and returns 0 instead of 35. | Convert to IST and build the shift start from the attendance date in IST (`at_ist`). |
| 2 | `compute_late_minutes` | Floors to whole minutes *before* comparing with `> 10`, so the grace boundary is wrong. | 09:40:01 on a 09:30 shift gives `int(10.01)=10`, not `> 10`, so 0 instead of 10 (R2). | Compare in seconds (`> 600`), then floor to minutes. |
| 3 | `compute_late_minutes` | Uses the punch-in's own calendar day as the shift start, and does not truncate to whole seconds. | Overnight shift 22:00, punch-in 01:00 next day measures from 22:00 *that* day (negative, so 0) instead of the previous day (180). | Measure from the attendance date (R1), which already handles the overnight rule; instants are truncated to seconds on input. |
| 4 | `compute_work_hours` | Uses `round()` on a float: banker's rounding plus binary float error, not half-up (R4). | 9h 07m 30s = 9.125 h returns 9.12 instead of 9.13. | `Decimal` division with `ROUND_HALF_UP`. |
| 5 | `compute_overtime` | Subtracts a naive datetime (PyMongo returns naive ones) from an IST-aware one. | Punch-out raises `TypeError`, so a 500. | Client created with `tz_aware=True`; everything is converted to aware UTC/IST. |
| 6 | `compute_overtime` | No 30-minute minimum (R3). | 5 minutes after shift end returns 5, not 0. | Return the minutes only if `>= 30`. |
| 7 | `compute_overtime` | The shift end is always on the record's date. An overnight shift ends the next day. | Shift 22:00-06:00, out 06:40 next day: 1480 minutes instead of 40. | Add one day to the end of an overnight shift. |
| 8 | `MongoClient(...)` | Not `tz_aware`, so every datetime read back is naive and the code has to guess its zone. | Mixed naive/aware arithmetic (see 5). | `tz_aware=True, tzinfo=UTC`; `as_utc()` for anything naive. |
| 9 | `/health` | Always returns ok, never talks to MongoDB. | Stop MongoDB: still 200 (must be 503). | Ping MongoDB; return 503 on failure. |
| 10 | start-up | No indexes at all, and no unique index anywhere. Every query is a collection scan and nothing enforces uniqueness. | `explain` shows COLLSCAN; duplicates possible. | Indexes created idempotently at start-up (see DECISIONS.md). |
| 11 | `create_employee` | Check-then-insert race: two simultaneous requests both pass `find_one` and both insert. | Fire 10 parallel POSTs: several 201s, duplicate `emp_code`. | Unique index plus `DuplicateKeyError` -> 409. |
| 12 | `EmployeeIn` | No validation of `emp_code` format, e-mail, `HH:MM`, `joined_on`, or `shift_start == shift_end`. | `shift_start: "25:99"` is accepted, then `punch-in` crashes (500). `joined_on: "2026-02-30"` accepted. | Pydantic patterns, a real-date check and a shift-differs check, all returning 422. |
| 13 | `create_employee` | `created_at = datetime.now()` is local naive time, and is returned as an ISO string. | Response shows a string, not epoch milliseconds; wrong zone on a non-UTC server. | UTC time truncated to ms; returned as epoch ms. |
| 14 | `list_employees` | `skip = page * page_size` skips the first page. | `page=1` returns employees 21-40. | `skip = (page - 1) * page_size`. |
| 15 | `list_employees` | `total` ignores the department filter; no sort, so pages are unstable; `page`/`page_size` unchecked (0, negative, 100000). | `?department=Sales` returns total = all employees. | `count_documents(filter)`, sort by `emp_code`, `page >= 1`, `1 <= page_size <= 100`. |
| 16 | `punch_in` | Unknown employee: `emp` is `None` and `emp["shift_start"]` raises. | POST with a bad `emp_code`: 500 instead of 404. | Explicit 404 check. |
| 17 | `punch_in` | `status` accepts any string and `punched_at` is not validated; `if body.punched_at` treats 0 as missing. | `status: "ABSENT"` creates a record; a seconds value (1783312500) silently becomes January 1970; a huge value raises (500). | `Literal` for status; strict integer epoch-ms with a range; 422 otherwise. |
| 18 | `punch_in` | `datetime.fromtimestamp(ms / 1000)` is naive server-local time, and `.date()` is that local date, not the IST date (R1). No overnight rule. Sub-second part is kept. | 20:00 UTC (= 01:30 IST next day) is filed on the wrong date. | `ms_to_dt` (aware UTC, truncated to seconds) and `attendance_date()` (IST + overnight rule). |
| 19 | `punch_in` | Check-then-insert race again. | Two parallel punch-ins can both return 201. | Unique `(emp_code, date)` index; `DuplicateKeyError` -> 409. |
| 20 | `punch_in` response | Leaks the MongoDB `_id` as `id` and returns raw datetimes. | Response has `id`, ISO strings for `punch_in`. | One serializer (`attendance_to_api`): no `_id`, epoch ms. |
| 21 | `list_attendance` | Loads every matching document into memory, sorts in Python, then slices. | Slow and memory-heavy at 100k records. | `find().sort().skip().limit()` served by an index. |
| 22 | `list_attendance` | Sorts by `date` only (no `emp_code` tie-break); `date_from > date_to`, bad dates, bad `status`, bad paging are not rejected; legacy documents without `half_day`/`history` are returned as-is. | `date_from=2026-08-01&date_to=2026-07-01` returns 200 with nothing. | Sort `date` desc, `emp_code` asc; 422 for invalid input; defaults for missing fields. |

## Looked at and decided it was fine

- `def` (sync) endpoints with PyMongo: FastAPI runs them in a thread pool, so they do not block the event loop.
- `load_dotenv()`: it does not override variables already in the environment, which is what the contract needs.
- `doc.pop("_id")` after `insert_one`: needed, because PyMongo adds `_id` to the dict.
- Projection `{"_id": 0}` in `list_employees` and the 201 status codes: correct.

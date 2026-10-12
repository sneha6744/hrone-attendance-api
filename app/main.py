"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (a local .env is loaded for convenience; real environment variables win)

Layout of this file
  1. configuration, database, indexes
  2. pure helpers (time zone, R1-R5 rules, rounding)   <- no database access, easy to unit test
  3. request models and validation helpers
  4. serialisation (Mongo document -> API JSON)
  5. endpoints: system, employees, attendance
  6. analytics pipelines and endpoints
  7. explain endpoint
"""
import calendar
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date as Date
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

log = logging.getLogger("attendance")

# --------------------------------------------------------------------------- #
# 1. Configuration, database, indexes
# --------------------------------------------------------------------------- #
load_dotenv()  # does NOT override variables that are already set in the environment

MONGO_URI = os.getenv("MONGO_URI") or "mongodb://localhost:27017"
MONGO_DB = os.getenv("MONGO_DB") or "attendance_db"

# tz_aware=True: PyMongo returns timezone-aware (UTC) datetimes instead of naive ones.
client = MongoClient(MONGO_URI, tz_aware=True, tzinfo=timezone.utc, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]

_indexes_ready = False
_index_attempts = 0


def ensure_indexes() -> None:
    """Create every index we rely on. create_index is idempotent, so this is safe on every start."""
    global _indexes_ready, _index_attempts
    _index_attempts += 1
    specs = [
        # employees
        (db.employees, [("emp_code", ASCENDING)], {"unique": True}),
        (db.employees, [("department", ASCENDING), ("emp_code", ASCENDING)], {}),
        (db.employees, [("department", ASCENDING), ("joined_on", ASCENDING)], {}),
        (db.employees, [("joined_on", ASCENDING)], {}),
        # attendance_logs
        (db.attendance_logs, [("emp_code", ASCENDING), ("date", ASCENDING)], {"unique": True}),
        (db.attendance_logs, [("date", DESCENDING), ("emp_code", ASCENDING)], {}),
        (db.attendance_logs, [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], {}),
        (db.attendance_logs, [("emp_code", ASCENDING), ("punch_in", DESCENDING)], {}),
    ]
    ok = True
    for coll, keys, opts in specs:
        try:
            coll.create_index(keys, **opts)
        except PyMongoError as exc:  # keep the app alive; /health will keep retrying
            ok = False
            log.error("could not create index %s on %s: %s", keys, coll.name, exc)
    _indexes_ready = ok


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except Exception as exc:  # never block start-up; the first /health call retries
        log.error("index creation failed at startup: %s", exc)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def mongo_error_handler(_, exc: PyMongoError):
    return JSONResponse(status_code=503, content={"detail": "database error"})


# --------------------------------------------------------------------------- #
# 2. Pure helpers: time zone, rules R1 - R5, rounding
# --------------------------------------------------------------------------- #
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
NO_WORK_STATUSES = ("ABSENT", "LEAVE")
GRACE_SECONDS = 10 * 60          # R2
MIN_OVERTIME_MINUTES = 30        # R3
HALF_DAY_HOURS = 4.5             # R5


def ms_to_dt(ms: int) -> datetime:
    """Epoch milliseconds -> aware UTC datetime, truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=UTC)


def now_seconds() -> datetime:
    """Current time as an aware UTC datetime truncated to whole seconds (R1)."""
    return datetime.now(UTC).replace(microsecond=0)


def now_millis() -> datetime:
    """Current time truncated to milliseconds (BSON dates have millisecond precision)."""
    n = datetime.now(UTC)
    return n.replace(microsecond=(n.microsecond // 1000) * 1000)


def as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Make any datetime aware. Naive values (e.g. from an un-configured client) are taken as UTC."""
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def to_ms(value: Any) -> Optional[int]:
    """Datetime -> integer epoch milliseconds (exact integer arithmetic, no floats)."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    return (as_utc(value) - EPOCH) // timedelta(milliseconds=1)


def minutes_of(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return minutes_of(shift_end) <= minutes_of(shift_start)


def at_ist(day: Date, hhmm: str) -> datetime:
    """The instant `hhmm` IST on calendar day `day`."""
    h, m = divmod(minutes_of(hhmm), 60)
    return datetime(day.year, day.month, day.day, h, m, tzinfo=IST)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> Date:
    """R1: IST calendar date of the punch-in; for an overnight shift, a punch-in earlier than
    shift_end belongs to the shift that started the previous day."""
    local = as_utc(punch_in).astimezone(IST)
    day = local.date()
    if is_overnight(shift_start, shift_end):
        seconds_into_day = local.hour * 3600 + local.minute * 60 + local.second
        if seconds_into_day < minutes_of(shift_end) * 60:
            day -= timedelta(days=1)
    return day


def compute_late_minutes(punch_in: datetime, shift_start: str, day: Date) -> int:
    """R2: late only if strictly more than 10:00 after shift start; then whole minutes since start."""
    seconds = (as_utc(punch_in) - at_ist(day, shift_start)) // timedelta(seconds=1)
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, rounded to 2 decimals half-up."""
    seconds = (as_utc(punch_out) - as_utc(punch_in)) // timedelta(seconds=1)
    hours = (Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(hours)


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, day: Date) -> int:
    """R3: whole minutes after shift end, counted only if at least 30. The end of an overnight shift
    falls on the next calendar day."""
    end = at_ist(day, shift_end)
    if is_overnight(shift_start, shift_end):
        end += timedelta(days=1)
    minutes = (as_utc(punch_out) - end) // timedelta(minutes=1)
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def compute_derived(emp: dict, day: Date, status: str, punch_in: Optional[datetime],
                    punch_out: Optional[datetime]) -> dict:
    """All four derived fields for one record, following the table in DATA_MODEL.md."""
    out = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if status not in PRESENCE_STATUSES or punch_in is None:
        return out
    out["late_minutes"] = compute_late_minutes(punch_in, emp["shift_start"], day)
    if punch_out is not None:
        hours = compute_work_hours(punch_in, punch_out)
        out["work_hours"] = hours
        out["overtime_minutes"] = compute_overtime(punch_out, emp["shift_start"], emp["shift_end"], day)
        out["half_day"] = hours < HALF_DAY_HOURS
    return out


def to_decimal(value: Any) -> Decimal:
    """Float/int/Decimal -> Decimal. Floats are first rounded to 6 places to remove binary noise
    (e.g. a sum that should be 18.25 but came out as 18.249999999999996)."""
    if isinstance(value, Decimal):
        return value
    return Decimal(repr(round(float(value), 6)))


def round_half_up(value: Any, places: int = 2) -> float:
    """R8: half-up rounding (not banker's rounding, not float rounding)."""
    return float(to_decimal(value).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def count_working_days(start: Date, end: Date) -> int:
    """R7: Monday - Friday days in [start, end], no holiday calendar."""
    if end < start:
        return 0
    return sum(1 for i in range((end - start).days + 1) if (start + timedelta(days=i)).weekday() < 5)


# --------------------------------------------------------------------------- #
# 3. Request models and validation helpers
# --------------------------------------------------------------------------- #
# strict=True: floats, strings and booleans are rejected; the range rejects "seconds" values.
EpochMs = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
HHMM = Annotated[str, Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")]
MonthQuery = Annotated[str, Query(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")]
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: HHMM = "09:30"
    shift_end: HHMM = "18:30"
    joined_on: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")

    @field_validator("joined_on")
    @classmethod
    def _real_date(cls, v: str) -> str:
        Date.fromisoformat(v)  # raises ValueError for e.g. 2026-02-30 -> 422
        return v

    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None
    status: Optional[PresenceStatus] = None  # None -> PRESENT


class PunchOutIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


def fail_422(loc: tuple, msg: str, value: Any = None):
    """Raise a 422 with FastAPI's standard validation-error body."""
    raise RequestValidationError([{"type": "value_error", "loc": loc, "msg": msg, "input": value}])


def parse_date_or_422(value: str, loc: tuple) -> Date:
    if not DATE_RE.match(value):
        fail_422(loc, "date must be YYYY-MM-DD", value)
    try:
        return Date.fromisoformat(value)
    except ValueError:
        fail_422(loc, "not a real calendar date", value)


def month_bounds(month: str) -> tuple[Date, Date]:
    """'YYYY-MM' -> (first day, last day) of that month."""
    year, mon = int(month[:4]), int(month[5:7])
    try:
        first = Date(year, mon, 1)
    except ValueError:
        fail_422(("query", "month"), "invalid month", month)
    return first, Date(year, mon, calendar.monthrange(year, mon)[1])


def require(name: str, value: Any, in_: str = "query"):
    if value is None:
        fail_422((in_, name), "field required")


# --------------------------------------------------------------------------- #
# 4. Serialisation: Mongo document -> API JSON
# --------------------------------------------------------------------------- #
def employee_to_api(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_ms(doc.get("created_at")),
    }


def history_to_api(entry: dict) -> dict:
    changes = {}
    for key, change in (entry.get("changes") or {}).items():
        if key in ("punch_in", "punch_out"):  # BSON dates inside the nested object -> epoch ms
            changes[key] = {"from": to_ms(change.get("from")), "to": to_ms(change.get("to"))}
        else:
            changes[key] = {"from": change.get("from"), "to": change.get("to")}
    return {"at": to_ms(entry.get("at")), "by": entry.get("by"), "reason": entry.get("reason"), "changes": changes}


def attendance_to_api(doc: dict) -> dict:
    """Never exposes _id. Missing legacy fields get their documented defaults."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_ms(doc.get("punch_in")),
        "punch_out": to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day") or False),
        "history": [history_to_api(h) for h in (doc.get("history") or [])],
    }


# --------------------------------------------------------------------------- #
# 5. Endpoints: system, employees, attendance
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    if not _indexes_ready and _index_attempts < 5:  # start-up could not reach MongoDB: try again
        ensure_indexes()
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_millis()
    try:  # uniqueness is enforced by the unique index, not by a read-then-write
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_to_api(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {} if department is None else {"department": department}
    cursor = (
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [employee_to_api(d) for d in cursor],
        "total": db.employees.count_documents(q),
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "unknown employee")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_seconds()
    day = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    status = body.status or "PRESENT"
    doc = {
        "emp_code": body.emp_code,
        "date": day.isoformat(),
        "status": status,
        "punch_in": ts,
        "punch_out": None,
        **compute_derived(emp, day, status, ts, None),
        "history": [],
    }
    try:  # the unique (emp_code, date) index makes this race-safe: one insert wins, the rest raise
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return attendance_to_api(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "unknown employee")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_seconds()
    # Most recent record whose punch_in is at or before ts (works across midnight). Records with a
    # null punch_in (ABSENT / LEAVE) never match a date comparison.
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", DESCENDING)]
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    p_in = as_utc(rec["punch_in"])
    if ts <= p_in:
        fail_422(("body", "punched_at"), "punched_at must be after punch_in", to_ms(ts))
    if ts - p_in > timedelta(hours=24):
        fail_422(("body", "punched_at"), "punched_at is more than 24 hours after punch_in", to_ms(ts))
    derived = compute_derived(emp, Date.fromisoformat(rec["date"]), rec["status"], p_in, ts)
    # The filter on punch_out makes the update atomic: only one of several simultaneous requests matches.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, **derived}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return attendance_to_api(updated)


ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


def attendance_filter(emp_code: Optional[str], date_from: Optional[str], date_to: Optional[str],
                      status: Optional[str]) -> dict:
    q: dict = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    d_from = parse_date_or_422(date_from, ("query", "date_from")) if date_from is not None else None
    d_to = parse_date_or_422(date_to, ("query", "date_to")) if date_to is not None else None
    if d_from and d_to and d_from > d_to:
        fail_422(("query", "date_from"), "date_from must not be after date_to", date_from)
    if d_from or d_to:
        q["date"] = {}
        if d_from:
            q["date"]["$gte"] = d_from.isoformat()
        if d_to:
            q["date"]["$lte"] = d_to.isoformat()
    if status is not None:
        q["status"] = status
    return q


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = attendance_filter(emp_code, date_from, date_to, status)
    cursor = (
        db.attendance_logs.find(q).sort(ATTENDANCE_SORT).skip((page - 1) * page_size).limit(page_size)
    )
    return {
        "items": [attendance_to_api(d) for d in cursor],
        "total": db.attendance_logs.count_documents(q),
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    day = parse_date_or_422(date, ("path", "date"))
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "unknown employee")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no record for that date")

    old_in, old_out = rec.get("punch_in"), rec.get("punch_out")
    new_status = body.status or rec["status"]

    if new_status in NO_WORK_STATUSES:
        if body.punch_in is not None or body.punch_out is not None:
            fail_422(("body", "status"), f"{new_status} clears punch times; do not send punch_in/punch_out")
        new_in = new_out = None
    else:
        new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else as_utc(old_in)
        new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else as_utc(old_out)
        if new_in is None:
            fail_422(("body", "punch_in"), "a presence status requires a punch_in")
        if body.punch_in is not None and attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != day:
            fail_422(("body", "punch_in"), "punch_in must stay on the record's attendance date", body.punch_in)
        if new_out is not None and (body.punch_in is not None or body.punch_out is not None):
            if new_out <= new_in or new_out - new_in > timedelta(hours=24):
                fail_422(("body", "punch_out"), "punch_out must be after punch_in and within 24 hours",
                         body.punch_out)

    new_vals = {"status": new_status, "punch_in": new_in, "punch_out": new_out,
                **compute_derived(emp, day, new_status, new_in, new_out)}
    old_vals = {
        "status": rec["status"],
        "punch_in": as_utc(old_in).replace(microsecond=0) if old_in else None,
        "punch_out": as_utc(old_out).replace(microsecond=0) if old_out else None,
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day") or False),
    }
    changes = {k: {"from": old_vals[k], "to": new_vals[k]} for k in new_vals if old_vals[k] != new_vals[k]}
    if not changes:
        fail_422(("body",), "the request changes nothing")

    entry = {"at": now_millis(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # Optimistic concurrency: the write only applies if the record is still exactly as we read it
    # (same status, punch times and history length). Otherwise the loser gets 409 and no entry is lost.
    history = rec.get("history")
    guard = {"_id": rec["_id"], "status": rec["status"], "punch_in": old_in, "punch_out": old_out,
             "history": {"$size": len(history)} if isinstance(history, list) else None}
    if isinstance(history, list):
        update = {"$set": dict(new_vals), "$push": {"history": entry}}
    else:  # legacy record without a usable history array
        update = {"$set": {**new_vals, "history": [entry]}}
    updated = db.attendance_logs.find_one_and_update(guard, update, return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "record was modified concurrently; please retry")
    return attendance_to_api(updated)


# --------------------------------------------------------------------------- #
# 6. Analytics: aggregation pipelines and endpoints
# --------------------------------------------------------------------------- #
# Reusable pipeline expressions. Stored derived values are read as they are (never recomputed).
IS_PRESENT = {"$in": ["$status", list(PRESENCE_STATUSES)]}
# $dayOfWeek: 1 = Sunday ... 7 = Saturday. The date string is parsed as UTC midnight, so the weekday is exact.
IS_WEEKDAY = {"$in": [
    {"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d",
                                        "onError": None, "onNull": None}}},
    [2, 3, 4, 5, 6],
]}
PRESENT_WEIGHT = {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}   # R5: half day = 0.5
LATE = {"$ifNull": ["$late_minutes", 0]}                                  # missing means 0
HAS_HOURS = {"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]}


def month_accumulators() -> dict:
    """$group accumulators shared by the employee summary and the department summary."""
    return {
        "present_days": {"$sum": {"$cond": [{"$and": [IS_PRESENT, IS_WEEKDAY]}, PRESENT_WEIGHT, 0]}},  # R6, R7
        "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
        "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        "late_count": {"$sum": {"$cond": [{"$gt": [LATE, 0]}, 1, 0]}},
        "total_late_minutes": {"$sum": LATE},
        "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        "wh_sum": {"$sum": {"$cond": [HAS_HOURS, "$work_hours", 0]}},
        "wh_cnt": {"$sum": {"$cond": [HAS_HOURS, 1, 0]}},
    }


def half_up_expr(expr: Any, places: int) -> dict:
    """Half-up rounding inside a pipeline ($round would round half to even). The tiny epsilon makes exact
    ties such as 0.03125 -> 0.0313 immune to binary floating point noise."""
    scale = 10 ** places
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [expr, scale]}, 0.500000001]}}, scale]}


# ---- employee monthly ------------------------------------------------------ #
def employee_monthly_pipeline(emp_code: str, first: Date, last: Date) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {"_id": None, **month_accumulators()}},
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: MonthQuery):
    first, last = month_bounds(month)
    emp = db.employees.find_one({"emp_code": emp_code}, {"joined_on": 1})
    if emp is None:
        raise HTTPException(404, "unknown employee")
    agg = next(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, first, last)), {})
    working_days = count_working_days(max(first, Date.fromisoformat(emp["joined_on"])), last)
    present = to_decimal(agg.get("present_days", 0))
    pct = None if working_days == 0 else round_half_up(present * 100 / working_days, 2)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up(present, 2),
        "leave_days": int(agg.get("leave_days", 0)),
        "late_count": int(agg.get("late_count", 0)),
        "total_late_minutes": int(agg.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(agg.get("total_overtime_minutes", 0)),
        "attendance_pct": pct,
    }


# ---- department summary ---------------------------------------------------- #
SUMMARY_SUMS = ("present_days", "wh_sum", "wh_cnt", "late_count", "total_late_minutes",
                "leave_days", "on_duty_count")


def department_summary_pipeline(first: Date, last: Date, department: Optional[str]) -> list:
    emp_match: dict = {"joined_on": {"$lte": last.isoformat()}}  # R9 (only people who have joined)
    if department is not None:
        emp_match["department"] = department
    return [
        {"$match": emp_match},
        # Start from employees so that people with zero logs still exist as documents (headcount).
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
                {"$group": {"_id": None, **month_accumulators()}},
            ],
            "as": "monthly",
        }},
        {"$addFields": {"m": {"$arrayElemAt": ["$monthly", 0]}}},  # missing when the employee has no logs
        {"$group": {"_id": "$department", "headcount": {"$sum": 1},
                    **{k: {"$sum": f"$m.{k}"} for k in SUMMARY_SUMS}}},
        {"$sort": {"_id": ASCENDING}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: MonthQuery, department: Optional[str] = None):
    first, last = month_bounds(month)
    items = []
    for row in db.employees.aggregate(department_summary_pipeline(first, last, department)):
        count = int(row.get("wh_cnt", 0))
        avg = round_half_up(to_decimal(row["wh_sum"]) / count, 2) if count else None
        items.append({
            "department": row["_id"],
            "headcount": int(row["headcount"]),
            "present_days": round_half_up(row.get("present_days", 0), 2),
            "avg_work_hours": avg,
            "late_count": int(row.get("late_count", 0)),
            "total_late_minutes": int(row.get("total_late_minutes", 0)),
            "leave_count": int(row.get("leave_days", 0)),
            "on_duty_count": int(row.get("on_duty_count", 0)),
        })
    return {"month": month, "items": items}


# ---- late leaderboard ------------------------------------------------------ #
def late_leaderboard_pipeline(first: Date, last: Date, limit: int, department: Optional[str]) -> list:
    pipeline: list = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": LATE},
                    "late_count": {"$sum": {"$cond": [{"$gt": [LATE, 0]}, 1, 0]}}}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs of unknown employees are dropped here
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})  # rank inside the department only
    pipeline += [
        # $rank = standard competition ranking (1, 2, 2, 4). The limit is applied AFTER ranking.
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name",
                      "department": "$emp.department", "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: MonthQuery, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    first, last = month_bounds(month)
    rows = db.attendance_logs.aggregate(late_leaderboard_pipeline(first, last, limit, department))
    items = [{"rank": int(r["rank"]), "emp_code": r["emp_code"], "name": r["name"],
              "department": r["department"], "total_late_minutes": int(r["total_late_minutes"]),
              "late_count": int(r["late_count"])} for r in rows]
    return {"month": month, "items": items}


# ---- department trend ------------------------------------------------------ #
MAX_TREND_DAYS = 92


def build_trend_pipeline(department: str, date_from: str, date_to: str) -> list:
    d_from = parse_date_or_422(date_from, ("query", "from"))
    d_to = parse_date_or_422(date_to, ("query", "to"))
    if d_to < d_from:
        fail_422(("query", "to"), "to must not be before from", date_to)
    n_days = (d_to - d_from).days + 1
    if n_days > MAX_TREND_DAYS:
        fail_422(("query", "to"), f"range exceeds {MAX_TREND_DAYS} days", date_to)

    staff = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
    if not staff:
        raise HTTPException(404, "unknown department")
    codes = [e["emp_code"] for e in staff]
    joined = [e["joined_on"] for e in staff]

    start = datetime(d_from.year, d_from.month, d_from.day, tzinfo=UTC)  # day 0 as a BSON date
    day_ms = 24 * 3600 * 1000

    # One output row per calendar day, built from $range, so weekends and empty days are always present.
    row = {"$let": {
        "vars": {"d": {"$add": [start, {"$multiply": ["$$i", day_ms]}]}},
        "in": {"$let": {
            "vars": {"ds": {"$dateToString": {"format": "%Y-%m-%d", "date": "$$d"}}},
            "in": {"$let": {
                "vars": {"rec": {"$arrayElemAt": [
                    {"$filter": {"input": "$daily", "as": "x", "cond": {"$eq": ["$$x._id", "$$ds"]}}}, 0]}},
                "in": {
                    "date": "$$ds",
                    "is_working_day": {"$in": [{"$dayOfWeek": "$$d"}, [2, 3, 4, 5, 6]]},
                    "headcount": {"$size": {"$filter": {"input": {"$literal": joined}, "as": "j",
                                                        "cond": {"$lte": ["$$j", "$$ds"]}}}},  # R9
                    "present_count": {"$ifNull": ["$$rec.present_count", 0]},
                    "late_count": {"$ifNull": ["$$rec.late_count", 0]},
                },
            }},
        }},
    }}

    return [
        {"$match": {"emp_code": {"$in": codes}, "date": {"$gte": d_from.isoformat(), "$lte": d_to.isoformat()}}},
        # $facet always emits exactly one document, even when no log matched, so the series is never empty.
        {"$facet": {"daily": [
            {"$group": {"_id": "$date",
                        "present_count": {"$sum": {"$cond": [IS_PRESENT, PRESENT_WEIGHT, 0]}},
                        "late_count": {"$sum": {"$cond": [{"$gt": [LATE, 0]}, 1, 0]}}}},
        ]}},
        {"$project": {"_id": 0, "rows": {"$map": {"input": {"$range": [0, n_days]}, "as": "i", "in": row}}}},
        {"$unwind": "$rows"},
        {"$replaceRoot": {"newRoot": "$rows"}},
        {"$addFields": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            half_up_expr({"$divide": ["$present_count", "$headcount"]}, 4),
            None,
        ]}}},
        # Mean of the non-null rates over the current row and up to 6 rows before it (inside the range).
        {"$setWindowFields": {"sortBy": {"date": 1},
                              "output": {"moving_avg_7d": {"$avg": "$attendance_rate",
                                                           "window": {"documents": [-6, 0]}}}}},
        {"$addFields": {"moving_avg_7d": {"$cond": [
            {"$lte": ["$moving_avg_7d", None]}, None, half_up_expr("$moving_avg_7d", 4)]}}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1,
                      "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1}},
        {"$sort": {"date": 1}},
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    pipeline = build_trend_pipeline(department, from_, to)
    return {"department": department, "items": list(db.attendance_logs.aggregate(pipeline))}


# --------------------------------------------------------------------------- #
# 7. Explain endpoint
# --------------------------------------------------------------------------- #
def run_explain(collection: str, command: dict) -> dict:
    raw = db.command({"explain": command, "verbosity": "executionStats"})
    # Round-trip through Extended JSON so every BSON type in the output becomes plain JSON.
    return {"collection": collection,
            "explain": json.loads(json_util.dumps(raw, json_options=json_util.RELAXED_JSON_OPTIONS))}


@app.get("/admin/explain/{endpoint}")
def admin_explain(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary",
                      "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    # Every branch builds the query with the same function the real endpoint uses.
    if endpoint == "attendance_list":
        q = attendance_filter(emp_code, date_from, date_to, status)
        cmd = {"find": "attendance_logs", "filter": q, "sort": dict(ATTENDANCE_SORT),
               "skip": (page - 1) * page_size, "limit": page_size}
        out = run_explain("attendance_logs", cmd)
    elif endpoint == "employee_monthly":
        require("emp_code", emp_code)
        require("month", month)
        first, last = month_bounds(month)
        pipeline = employee_monthly_pipeline(emp_code, first, last)
        out = run_explain("attendance_logs", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}})
    elif endpoint == "department_summary":
        require("month", month)
        first, last = month_bounds(month)
        pipeline = department_summary_pipeline(first, last, department)
        out = run_explain("employees", {"aggregate": "employees", "pipeline": pipeline, "cursor": {}})
    elif endpoint == "late_leaderboard":
        require("month", month)
        first, last = month_bounds(month)
        pipeline = late_leaderboard_pipeline(first, last, limit, department)
        out = run_explain("attendance_logs", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}})
    else:  # department_trend
        require("department", department)
        require("from", from_)
        require("to", to)
        pipeline = build_trend_pipeline(department, from_, to)
        out = run_explain("attendance_logs", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}})
    return {"endpoint": endpoint, **out}

"""Smoke test against a running server:  python smoke_test.py   (BASE_URL defaults to http://localhost:8000)
Uses only the standard library. Creates its own employees (random codes) and logs in March 2031."""
import json
import os
import random
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

BASE = os.getenv("BASE_URL", "http://localhost:8000")
IST = timezone(timedelta(hours=5, minutes=30))
failures = []


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw.decode()


def ms(day, hh, mm, ss=0):
    """Epoch milliseconds of an IST wall-clock time on a 2031-03-<day> date (day may exceed 31 -> April)."""
    return int(datetime(2031, 3, 1, hh, mm, ss, tzinfo=IST).timestamp() * 1000) + (day - 1) * 86_400_000


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"   -> {detail}"))
    if not cond:
        failures.append(name)


base = random.randint(100000, 899000)
code = lambda i: f"EMP{base + i}"
DEPT, DEPT_H, DEPT_C = f"SMK{base}", f"SMKH{base}", f"SMKC{base}"


def make(i, dept, joined, start="09:30", end="18:30"):
    s, b = call("POST", "/employees", {"emp_code": code(i), "name": f"Emp {i}", "email": f"e{i}@example.com",
                                       "department": dept, "shift_start": start, "shift_end": end, "joined_on": joined})
    assert s == 201, (s, b)
    return b


def pin(i, day, hh, mm, ss=0, status=None):
    body = {"emp_code": code(i), "punched_at": ms(day, hh, mm, ss)}
    if status:
        body["status"] = status
    return call("POST", "/attendance/punch-in", body)


def pout(i, day, hh, mm, ss=0):
    return call("POST", "/attendance/punch-out", {"emp_code": code(i), "punched_at": ms(day, hh, mm, ss)})


s, b = call("GET", "/health")
check("health 200", s == 200 and b == {"status": "ok"}, (s, b))

# ---- employees ----------------------------------------------------------- #
A, B, C, D, E, F, G = range(7)
emp = make(A, DEPT, "2031-03-01")
check("employee has epoch-ms created_at and no _id", isinstance(emp["created_at"], int) and "_id" not in emp, emp)
make(B, DEPT, "2031-03-17")
make(C, DEPT, "2031-03-01", "22:00", "06:00")
make(D, DEPT, "2031-03-01")
make(E, DEPT, "2031-04-02")
make(F, DEPT, "2031-03-01")
make(G, DEPT, "2031-03-01")
s, _ = call("POST", "/employees", {"emp_code": code(A), "name": "x", "email": "x@x.com", "department": "d", "joined_on": "2031-03-01"})
check("duplicate emp_code -> 409", s == 409, s)
s, _ = call("POST", "/employees", {"emp_code": "BAD1", "name": "x", "email": "x@x.com", "department": "d", "joined_on": "2031-03-01"})
check("bad emp_code -> 422", s == 422, s)
s, _ = call("POST", "/employees", {"emp_code": "EMP999999", "name": "x", "email": "x@x.com", "department": "d",
                                   "joined_on": "2031-03-01", "shift_start": "09:00", "shift_end": "09:00"})
check("shift_start == shift_end -> 422", s == 422, s)
s, b = call("GET", f"/employees?department={DEPT}&page=1&page_size=3")
check("employee list: filtered total, sorted, first page", s == 200 and b["total"] == 7 and
      [e["emp_code"] for e in b["items"]] == [code(0), code(1), code(2)], b)
s, b = call("GET", f"/employees?department={DEPT}&page=3&page_size=3")
check("employee list: last page", [e["emp_code"] for e in b["items"]] == [code(6)], b)
check("page_size 101 -> 422", call("GET", "/employees?page_size=101")[0] == 422)

# ---- punch-in / punch-out rules ------------------------------------------ #
s, b = pin(A, 3, 9, 40, 0)
check("09:40:00 is not late, date is IST day", s == 201 and b["late_minutes"] == 0 and b["date"] == "2031-03-03" and b["history"] == [], b)
s, b = pout(A, 3, 18, 30, 0)
check("work 8.83, overtime 0", s == 200 and b["work_hours"] == 8.83 and b["overtime_minutes"] == 0 and not b["half_day"], b)
s, b = pin(A, 4, 9, 40, 1)
check("09:40:01 -> late 10", s == 201 and b["late_minutes"] == 10, b)
s, b = pout(A, 4, 19, 10)
check("overtime 40, work 9.5", b["overtime_minutes"] == 40 and b["work_hours"] == 9.5, b)
pin(A, 5, 9, 0)
s, b = pout(A, 5, 18, 7, 30)
check("9.125 h rounds half-up to 9.13, 7 min overtime ignored", b["work_hours"] == 9.13 and b["overtime_minutes"] == 0, b)
s, b = pin(A, 6, 10, 0, status="WFH")
check("WFH punch-in, late 30", s == 201 and b["status"] == "WFH" and b["late_minutes"] == 30, b)
s, b = pout(A, 6, 13, 0)
check("3.0 h -> half day", b["work_hours"] == 3.0 and b["half_day"] is True, b)
pin(A, 7, 9, 30)
s, b = pin(A, 8, 10, 0)           # Saturday
pout(A, 8, 15, 0)
check("weekend punch-in accepted", s == 201 and b["date"] == "2031-03-08", b)
s, b = pin(A, 3, 12, 0)
check("second punch-in same day -> 409", s == 409, (s, b))
s, b = pout(A, 3, 20, 0)
check("second punch-out -> 409", s == 409, (s, b))
check("unknown employee punch-in -> 404", call("POST", "/attendance/punch-in", {"emp_code": "EMP000001"})[0] == 404)
check("punched_at in seconds -> 422", call("POST", "/attendance/punch-in", {"emp_code": code(A), "punched_at": 1783312500})[0] == 422)
check("punched_at float -> 422", call("POST", "/attendance/punch-in", {"emp_code": code(A), "punched_at": 1783312500000.5})[0] == 422)
check("punched_at string -> 422", call("POST", "/attendance/punch-in", {"emp_code": code(A), "punched_at": "1783312500000"})[0] == 422)
check("status ABSENT on punch-in -> 422", call("POST", "/attendance/punch-in", {"emp_code": code(A), "status": "ABSENT"})[0] == 422)

# overnight shift
s, b = pin(C, 3, 21, 55)
check("overnight punch-in belongs to its start day", s == 201 and b["date"] == "2031-03-03" and b["late_minutes"] == 0, b)
s, b = pout(C, 4, 6, 40)
check("overnight punch-out across midnight: overtime 40, 8.75 h", s == 200 and b["overtime_minutes"] == 40 and b["work_hours"] == 8.75, b)
s, b = pin(C, 4, 3, 0)  # 03:00 < shift_end -> previous day's shift, already exists
check("early-morning punch-in maps to previous day (409 here)", s == 409, (s, b))
# lateness
pin(F, 3, 10, 40)
pin(G, 3, 10, 0)
s, b = pout(F, 5, 10, 0)
check("punch-out more than 24 h after -> 422", s == 422, (s, b))
s, b = pout(G, 3, 9, 0)
check("no punch-in at or before punched_at -> 404", s == 404, (s, b))

# ---- list attendance ----------------------------------------------------- #
s, b = call("GET", f"/attendance?emp_code={code(A)}&page_size=2")
check("attendance list: sorted by date desc, total filtered", s == 200 and b["total"] == 6 and
      [r["date"] for r in b["items"]] == ["2031-03-08", "2031-03-07"] and "id" not in b["items"][0], b)
check("date_from > date_to -> 422", call("GET", "/attendance?date_from=2031-03-09&date_to=2031-03-01")[0] == 422)
check("bad status filter -> 422", call("GET", "/attendance?status=NOPE")[0] == 422)

# ---- regularization ------------------------------------------------------- #
make(7, DEPT_H, "2031-03-01")
H = 7
pin(H, 3, 10, 5)
pout(H, 3, 18, 35)
patch = lambda day, body: call("PATCH", f"/attendance/{code(H)}/2031-03-{day:02d}", {"reason": "biometric glitch", "regularized_by": "hr", **body})
s, b = patch(3, {"punch_in": ms(3, 9, 28)})
check("PATCH recomputes and appends history", s == 200 and b["late_minutes"] == 0 and b["work_hours"] == 9.12 and len(b["history"]) == 1
      and set(b["history"][0]["changes"]) == {"punch_in", "work_hours", "late_minutes"}
      and b["history"][0]["changes"]["late_minutes"] == {"from": 35, "to": 0}
      and b["history"][0]["changes"]["punch_in"]["from"] == ms(3, 10, 5), b)
check("PATCH that changes nothing -> 422", patch(3, {"punch_in": ms(3, 9, 28)})[0] == 422)
check("PATCH ABSENT with punch_in -> 422", patch(3, {"status": "ABSENT", "punch_in": ms(3, 9, 0)})[0] == 422)
check("PATCH punch_in on another day -> 422", patch(3, {"punch_in": ms(4, 9, 0)})[0] == 422)
check("PATCH punch_out before punch_in -> 422", patch(3, {"punch_out": ms(3, 9, 0)})[0] == 422)
check("PATCH unknown date -> 404", patch(20, {"status": "LEAVE"})[0] == 404)
s, b = patch(3, {"status": "LEAVE"})
check("PATCH to LEAVE clears times, 2 history entries", s == 200 and b["punch_in"] is None and b["punch_out"] is None
      and b["work_hours"] is None and len(b["history"]) == 2, b)
# Friday of employee A -> LEAVE
s, b = call("PATCH", f"/attendance/{code(A)}/2031-03-07", {"status": "LEAVE", "reason": "approved leave", "regularized_by": "hr"})
check("A's Friday set to LEAVE", s == 200 and b["status"] == "LEAVE", b)

# ---- analytics ------------------------------------------------------------ #
s, b = call("GET", f"/analytics/employees/{code(A)}/monthly?month=2031-03")
exp = {"emp_code": code(A), "month": "2031-03", "working_days": 21, "present_days": 3.5, "leave_days": 1, "late_count": 3,
       "total_late_minutes": 70, "total_overtime_minutes": 40, "attendance_pct": 16.67}
check("employee monthly (A)", s == 200 and b == exp, b)
s, b = call("GET", f"/analytics/employees/{code(B)}/monthly?month=2031-03")
check("mid-month joiner: working days counted from joined_on, no logs", b["working_days"] == 11 and b["present_days"] == 0 and b["attendance_pct"] == 0.0, b)
s, b = call("GET", f"/analytics/employees/{code(E)}/monthly?month=2031-03")
check("joined after the month: 0 working days, pct null", b["working_days"] == 0 and b["attendance_pct"] is None, b)
check("unknown employee monthly -> 404", call("GET", "/analytics/employees/EMP000001/monthly?month=2031-03")[0] == 404)
check("bad month -> 422", call("GET", f"/analytics/employees/{code(A)}/monthly?month=2031-13")[0] == 422)

s, b = call("GET", f"/analytics/departments/summary?month=2031-03&department={DEPT}")
exp_item = {"department": DEPT, "headcount": 6, "present_days": 6.5, "avg_work_hours": 7.37, "late_count": 5,
            "total_late_minutes": 170, "leave_count": 1, "on_duty_count": 0}
check("department summary (no-log and future joiners handled)", s == 200 and b["items"] == [exp_item], b)

s, b = call("GET", f"/analytics/leaderboard/late?month=2031-03&department={DEPT}&limit=1")
rows = [(r["rank"], r["emp_code"], r["total_late_minutes"]) for r in b["items"]]
check("leaderboard: tie at the cutoff returns both, rank 1,1", rows == [(1, code(A), 70), (1, code(F), 70)], b)
s, b = call("GET", f"/analytics/leaderboard/late?month=2031-03&department={DEPT}&limit=3")
rows = [(r["rank"], r["emp_code"]) for r in b["items"]]
check("leaderboard: ranks 1,1,3 (next rank skipped)", rows == [(1, code(A)), (1, code(F)), (3, code(G))], b)
check("leaderboard limit 51 -> 422", call("GET", "/analytics/leaderboard/late?month=2031-03&limit=51")[0] == 422)

s, b = call("GET", f"/analytics/departments/{DEPT}/trend?from=2031-03-03&to=2031-03-09")
items = b["items"] if s == 200 else []
check("trend: one row per day", [r["date"] for r in items] == [f"2031-03-{d:02d}" for d in range(3, 10)], b)
if len(items) == 7:
    check("trend: working-day flags", [r["is_working_day"] for r in items] == [True] * 5 + [False] * 2, items)
    check("trend: headcount (mid-month joiner and future joiner excluded)", all(r["headcount"] == 5 for r in items), items)
    check("trend: present_count", [r["present_count"] for r in items] == [4, 1, 1, 0.5, 0, 1, 0], items)
    check("trend: late_count", [r["late_count"] for r in items] == [2, 1, 0, 1, 0, 1, 0], items)
    check("trend: attendance_rate (null on weekend)", [r["attendance_rate"] for r in items] == [0.8, 0.2, 0.2, 0.1, 0.0, None, None], items)
    check("trend: 7-day moving average, short window at the start",
          [r["moving_avg_7d"] for r in items] == [0.8, 0.5, 0.4, 0.325, 0.26, 0.26, 0.26], items)
s, b = call("GET", f"/analytics/departments/{DEPT_C}/trend?from=2031-03-03&to=2031-03-04")
check("trend: unknown department -> 404", s == 404, (s, b))
check("trend: to < from -> 422", call("GET", f"/analytics/departments/{DEPT}/trend?from=2031-03-09&to=2031-03-03")[0] == 422)
check("trend: > 92 days -> 422", call("GET", f"/analytics/departments/{DEPT}/trend?from=2031-01-01&to=2031-04-03")[0] == 422)
s, b = call("GET", f"/analytics/departments/{DEPT}/trend?from=2031-03-24&to=2031-03-26")  # Mon-Wed, no logs at all
check("trend: working days without records -> rate 0.0 (headcount 6 incl. the 17th joiner)",
      s == 200 and len(b["items"]) == 3 and all(r["present_count"] == 0 and r["headcount"] == 6 and r["attendance_rate"] == 0.0
                                                 and r["moving_avg_7d"] == 0.0 for r in b["items"]), b)
s, b = call("GET", f"/analytics/departments/{DEPT}/trend?from=2031-01-01&to=2031-01-03")  # before anyone joined
check("trend: headcount 0 -> rate and moving average null",
      s == 200 and all(r["headcount"] == 0 and r["attendance_rate"] is None and r["moving_avg_7d"] is None for r in b["items"]), b)

# ---- concurrency ---------------------------------------------------------- #
make(8, DEPT_C, "2031-03-01")
with ThreadPoolExecutor(10) as ex:
    res = list(ex.map(lambda _: pin(8, 10, 9, 30), range(10)))
codes = sorted(r[0] for r in res)
check("10 parallel punch-ins -> exactly one 201", codes.count(201) == 1 and codes.count(409) == 9, codes)
with ThreadPoolExecutor(10) as ex:
    res = list(ex.map(lambda _: pout(8, 10, 18, 30), range(10)))
codes = sorted(r[0] for r in res)
check("10 parallel punch-outs -> exactly one 200", codes.count(200) == 1 and codes.count(409) == 9, codes)
with ThreadPoolExecutor(10) as ex:
    res = list(ex.map(lambda i: call("POST", "/employees", {"emp_code": f"EMP{base + 50}", "name": "R", "email": "r@x.com",
                                                             "department": DEPT_C, "joined_on": "2031-03-01"}), range(10)))
codes = sorted(r[0] for r in res)
check("10 parallel employee creates -> exactly one 201", codes.count(201) == 1 and codes.count(409) == 9, codes)
with ThreadPoolExecutor(8) as ex:
    res = list(ex.map(lambda i: call("PATCH", f"/attendance/{code(8)}/2031-03-10",
                                     {"punch_in": ms(10, 9, 0, i), "reason": "concurrent edit", "regularized_by": f"hr{i}"}), range(1, 9)))
ok = sum(1 for r in res if r[0] == 200)
s, b = call("GET", f"/attendance?emp_code={code(8)}&date_from=2031-03-10&date_to=2031-03-10")
check("concurrent PATCHes: history entries == successful PATCHes, no 5xx",
      all(r[0] in (200, 409) for r in res) and len(b["items"][0]["history"]) == ok >= 1, (sorted(r[0] for r in res), b))

# ---- explain -------------------------------------------------------------- #
queries = {
    "attendance_list": f"emp_code={code(A)}&date_from=2031-03-01&date_to=2031-03-31",
    "employee_monthly": f"emp_code={code(A)}&month=2031-03",
    "department_summary": "month=2031-03",
    "late_leaderboard": "month=2031-03&limit=5",
    "department_trend": f"department={DEPT}&from=2031-03-03&to=2031-03-09",
}
for name, qs in queries.items():
    s, b = call("GET", f"/admin/explain/{name}?{qs}")
    txt = json.dumps(b)
    check(f"explain {name}: IXSCAN and no COLLSCAN", s == 200 and "IXSCAN" in txt and "COLLSCAN" not in txt, (s, txt[:300]))
check("explain without required params -> 422", call("GET", "/admin/explain/employee_monthly")[0] == 422)

print()
print("ALL CHECKS PASSED" if not failures else f"{len(failures)} FAILED: {failures}")
raise SystemExit(1 if failures else 0)

# DECISIONS.md

1. **Indexes.** `employees`: unique `emp_code` (identity, duplicate guard), `(department, emp_code)` (filtered, sorted list), `(department, joined_on)` and `(joined_on)` (headcount filter). `attendance_logs`: unique `(emp_code, date)` (punch-in race, monthly, joins), `(date desc, emp_code)` (list sort, month scans), `(status, date desc, emp_code)` (status filter), `(emp_code, punch_in desc)` (find the open record at punch-out). Rejected: an index on `late_minutes`; the leaderboard sums per employee, so it would not help.

2. **Punch-in race.** Both requests build the same record and call `insert_one`. The unique `(emp_code, date)` index lets one insert win; the other raises `DuplicateKeyError`, which I turn into 409. There is no read before the write.

3. **Ties.** I rank with `$rank` first and cut with `rank <= limit` afterwards. A tie at the cutoff shares one rank, so all tied people are returned, and the list can be longer than `limit`. Rows are ordered by minutes, then `emp_code`.

4. **Headcount.** The pipeline starts from `employees` (joined on or before month end) and `$lookup`s the logs. No logs just means an empty array; the employee is still counted by `$sum: 1`.

5. **100x data.** Stop reading raw logs for analytics. Keep a per-employee, per-day rollup, updated at punch and correction time, and aggregate that.

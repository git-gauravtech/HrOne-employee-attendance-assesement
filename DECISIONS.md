# DECISIONS.md

1. **Indexes.**
   - `employees`: Unique index on `emp_code` enforces business identifier uniqueness and fast `$lookup` joins. Compound index on `(department, joined_on)` accelerates department filtering and headcount period queries (R9).
   - `attendance_logs`: Unique compound index on `(emp_code, date)` enforces at most one record per employee per day and supports employee monthly lookups. Compound index on `(date: -1, emp_code: 1)` optimizes sorted pagination and date-range filters. `(date, late_minutes)` accelerates leaderboard filtering.
   - *Considered and rejected*: A large compound index covering `(emp_code, date, status, work_hours, late_minutes)`. Write overhead and memory footprint on high-volume punch operations outweighed query gains, as prefix index scans already satisfy all queries without collection scans.

2. **Punch-in race.**
   Both requests independently resolve the employee and compute the same attendance date, then execute `insert_one()`. MongoDB's unique compound index on `(emp_code, date)` permits exactly one insert to commit; the concurrent insert fails with `DuplicateKeyError`. The code catches this error and returns HTTP 409 Conflict without relying on flawed check-then-insert logic.

3. **Ties.**
   The late leaderboard uses MongoDB's `$setWindowFields` with `$rank` (standard competition ranking: 1, 2, 2, 4). The `limit` parameter is applied after ranking (`rank <= limit`), ensuring all employees tied at or below the cutoff rank are retained in the response.

4. **Headcount.**
   The aggregation pipeline starts on the `employees` collection filtered by `joined_on <= end_of_month`, then performs an outer `$lookup` into `attendance_logs`. Because every matching employee is grouped by department before counting, employees with zero attendance records are fully preserved in `headcount`.

5. **One thing you would change for 100x data.**
   Materialize daily and monthly attendance summaries into dedicated rollup collections using MongoDB Change Streams or midnight cron rollups, allowing analytics endpoints to query pre-aggregated stats in O(1) time instead of aggregating millions of raw punch logs on the fly.

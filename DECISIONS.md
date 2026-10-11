# DECISIONS.md

1. **Indexes.**
I created `employees.emp_code` (unique) to enforce employee identity and power O(1) lookups. On `employees`, `(department, joined_on)` and `joined_on` back department summaries and R9 headcount date filtering. On `attendance_logs`, `(emp_code, date)` (unique) enforces the natural key and race safety. `(date DESC, emp_code ASC)` backs `/attendance` list sorting and pagination. `(emp_code, punch_in DESC)` enables fast punch-out matching, and `(date, late_minutes)` backs the late leaderboard aggregation.
*Rejected:* An index on `status` alone was rejected due to low cardinality (only 5 status values), which creates index write overhead with minimal scan selectivity.

2. **Punch-in race.**
Both concurrent requests compute the attendance date and attempt `insert_one` with `(emp_code, date)`. MongoDB's unique compound index acts as an atomic serialization barrier: the first insert succeeds and returns `201 Created`. The second immediately raises `pymongo.errors.DuplicateKeyError` (code 11000). The endpoint catches this error and returns `409 Conflict`. No check-then-insert race can slip through.

3. **Ties.**
The pipeline uses MongoDB's `$setWindowFields` with the `$rank` operator, implementing standard competition ranking (e.g., ranks 1, 2, 2, 4). The `limit` filter (`rank <= limit`) is applied strictly *after* ranking. Both tied employees share the qualified rank, so both are returned (even if total rows exceed `limit`), ordered by `total_late_minutes` descending, then `emp_code` ascending.

4. **Headcount.**
The department summary pipeline initiates from the `employees` collection (matching `joined_on <= month_end`) instead of `attendance_logs`. It groups by department to establish true headcount first, then performs a `$lookup` to join monthly attendance records. Employees with zero logs produce an empty array, correctly counting toward headcount while contributing zero present days.

5. **One thing you would change for 100x data.**
At 10M+ records, real-time cross-collection grouping and `$densify` on raw logs would strain memory and I/O. I would introduce incremental pre-aggregation using MongoDB Change Streams or a scheduled worker into a dedicated `daily_department_rollups` collection. Analytical endpoints would query pre-aggregated rollups via simple index lookups in sub-millisecond time.

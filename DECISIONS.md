# DECISIONS.md

1. **Indexes.** I use a unique `employees.emp_code` index for employee identity and concurrent duplicate protection, plus `(department, joined_on)` for department headcount lookups. Attendance has a unique `(emp_code, date)` index for the one-record-per-day rule, `(date DESC, emp_code ASC)` for the list endpoint, `(status, date)` for status-filtered searches, and `(emp_code, punch_in DESC)` for finding the latest punch to close. I would add workload-specific indexes only after checking real execution plans; every index adds write cost.

2. **Punch-in race.** Both requests may initially see no record. Each attempts an insert; MongoDB's unique `(emp_code, date)` index permits one insert only. The winner receives 201 and the losing request catches `DuplicateKeyError` and receives 409. An application-level pre-check alone would not be sufficient.

3. **Ties.** `$rank` assigns equal late-minute totals the same rank and skips the next rank. Filtering `rank <= limit` happens after ranking, so all employees tied at the cutoff are returned, even if that makes the list longer than `limit`.

4. **Headcount.** The summary groups employees by department using `joined_on <= month-end`, separately from attendance logs. Thus employees with no logs are still counted, and employees who join later are excluded.

5. **100x data.** I would inspect explain plans and production query distributions, add measured compound indexes, and consider pre-aggregated daily summaries updated idempotently. I would also add integration/load tests and monitor slow queries before introducing caching.

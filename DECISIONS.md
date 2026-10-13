1. **Indexes.** A unique `employees.emp_code` index enforces employee identity. The `department, emp_code` index supports department filtering and ordered employee lists; `joined_on` supports period headcount. On attendance, the unique `emp_code, date` index prevents duplicate daily punches and serves employee history lookups. The date and date/employee indexes support month filters and list sorting; status/date supports status-filtered lists. I considered indexing `late_minutes`, but the leaderboard still has to group all matching month records, so that index would add write cost without replacing the date scan.

2. **Punch-in race.** Both requests can pass the employee lookup and calculate the same attendance day. The unique attendance index accepts one insert; MongoDB rejects the other with a duplicate-key error, which becomes 409.

3. **Ties.** `$rank` ranks only by total late minutes. The limit is applied to rank, so every employee tied at the cutoff is returned. Employee code sorts tied rows consistently.

4. **Headcount.** The department pipeline starts from employees who joined by month-end, then looks up logs and preserves employees with no matches before grouping.

5. **At 100x scale.** I would add precomputed monthly rollups for dashboard queries and measure them against the live aggregations before choosing a refresh strategy.

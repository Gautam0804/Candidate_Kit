# Starter code review

The original starter file is not present in this checkout, so I cannot certify that this list covers every defect from that version. These are defects I confirmed in the implementation available at the start of this review and then fixed.

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `create_employee` | `created_at` was stored with fractional seconds although API instants must be whole seconds. | Inspect the raw BSON timestamp immediately after employee creation; its `microsecond` could be nonzero. | Truncate to seconds before storing. |
| 2 | `punch_in` | A punch-in without `punched_at` used `datetime.now()` with fractional seconds and stored them. | Punch in with only `emp_code`, then inspect the stored BSON value. | Truncate the generated timestamp before calculation and storage. |
| 3 | `list_attendance` | The response exposed MongoDB's internal `_id` as `id`, contrary to the contract. | Call `GET /attendance` and inspect each item for an `id` field. | Remove `_id` from the response; attendance is addressed by employee and date. |
| 4 | `EmployeeIn` and `list_attendance` | Input validation did not enforce the employee-code, email, shift-time, canonical-date, and attendance-status constraints in the API contract. | Submit `emp_code: "BAD"`, shift time `09:30:00`, compact date `20261001`, or `status=INVALID`; the old code accepted some of these. | Add Pydantic field constraints and strict `YYYY-MM-DD` and status validation. |
| 5 | `explain_endpoint` | Several explain branches built shortened pipelines; the trend branch explained only its initial department match. | Compare the `command` sent to `explain` with the full aggregation called by its endpoint. | Extract shared pipeline builders and use them in both the endpoint and its explain branch. |

## Checked and not considered defects

- Punch-in uniqueness is enforced by a compound unique index, so concurrent duplicate requests produce one insert and one conflict.
- Regularization uses a conditional update and `$push` for history, so updates cannot silently overwrite a history entry.
- The leaderboard uses `$rank` before applying `limit`, preserving competition ranks and ties at the cutoff.
- The department summary starts with employees and preserves unmatched lookup rows, so employees with no logs count toward headcount.
- The trend expands dates and calculates its moving average inside MongoDB rather than building the series in a Python loop.

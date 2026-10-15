# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

1. **Indexes.** Which indexes did you create, and which query or guarantee does each one serve? Which one did you consider and reject?
2. **Punch-in race.** Two identical punch-in requests arrive at the same instant. Walk through exactly what happens in your code.
3. **Ties.** Two employees are tied on late minutes at the cutoff of `limit`. What does your leaderboard return, and why?
4. **Headcount.** How does your department summary make sure employees with zero logs are counted?
5. **One thing you would change** if this had to serve 100x the data.

# Design Decisions

## 1. FastAPI for the REST API

**Decision:** Use FastAPI to implement HTTP endpoints.

**Rationale:** FastAPI provides request validation, structured responses, and interactive API documentation through Swagger UI.

## 2. MongoDB for Data Storage

**Decision:** Use MongoDB as the application's database.

**Rationale:** MongoDB supports document-oriented employee and attendance records.

## 3. Epoch Milliseconds for Punch Timestamps

**Decision:** Represent punch-in and punch-out timestamps as integer epoch milliseconds.

**Rationale:** This provides a consistent timestamp representation for attendance records and time calculations.

## 4. Separate Attendance Operations

**Decision:** Provide separate endpoints for punch-in, punch-out, listing attendance, and regularizing attendance.

**Rationale:** Each endpoint has a distinct responsibility and can validate its operation independently.

## 5. Attendance Correction History

**Decision:** Record correction details in attendance history.

**Rationale:** An audit history helps identify the previous values, corrected values, reason, and administrator associated with a change.

## 6. Dedicated Analytics Endpoints

**Decision:** Provide separate endpoints for employee monthly analytics, department summaries, late-arrival rankings, and department trends.

**Rationale:** Dedicated endpoints make attendance reporting easier to consume and maintain.

## 7. Input Validation and Error Responses

**Decision:** Validate request data and return meaningful errors for invalid or conflicting operations.

**Rationale:** Consistent validation protects data integrity and helps API clients understand failures.

## 8. Local Development and API Documentation

**Decision:** Run the API locally with Uvicorn and use Swagger UI for interactive testing.

**Rationale:** This supports rapid development and endpoint verification.

## Notes

These decisions describe the intended design based on the API's observed behavior. Verify each decision against the implementation and assignment requirements before submission.


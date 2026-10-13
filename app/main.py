"""Employee Attendance & Analytics API - corrected foundation.

Run from the repository root:
    uvicorn app.main:app --port 8000

Environment:
    MONGO_URI (required in deployment; defaults to local MongoDB for development)
    MONGO_DB  (defaults to attendance_db)

The API endpoints and MongoDB aggregations are implemented against openapi.yaml.
"""
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

IST = timezone(timedelta(hours=5, minutes=30))
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
db = client[MONGO_DB]

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# --------------------------------------------------------------------------- #
# Startup indexes
# --------------------------------------------------------------------------- #
@app.on_event("startup")
def create_indexes() -> None:
    """Create indexes idempotently before serving requests."""
    db.employees.create_index("emp_code", unique=True, name="uniq_employee_code")
    db.employees.create_index(
        [("department", 1), ("emp_code", 1)], name="department_emp_code"
    )
    db.employees.create_index("joined_on", name="employee_joined_on")
    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1)],
        unique=True,
        name="uniq_employee_attendance_date",
    )
    db.attendance_logs.create_index(
        [("date", -1)], name="attendance_date_desc"
    )
    db.attendance_logs.create_index(
        [("date", -1), ("emp_code", 1)], name="attendance_date_emp_code_sort"
    )
    db.attendance_logs.create_index(
        [("status", 1), ("date", -1)], name="attendance_status_date"
    )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000


def truncate_epoch_ms(timestamp_ms: int) -> int:
    """Validate epoch milliseconds and truncate to whole seconds (R1)."""
    if isinstance(timestamp_ms, bool) or not isinstance(timestamp_ms, int):
        raise ValueError("Timestamp must be an integer in epoch milliseconds")
    if not MIN_EPOCH_MS <= timestamp_ms <= MAX_EPOCH_MS:
        raise ValueError("Timestamp is outside the supported epoch-millisecond range")
    return (timestamp_ms // 1000) * 1000


def epoch_ms_to_datetime(timestamp_ms: int) -> datetime:
    """Convert epoch milliseconds to a whole-second, IST-aware datetime."""
    truncated_ms = truncate_epoch_ms(timestamp_ms)
    return datetime.fromtimestamp(truncated_ms / 1000, tz=timezone.utc).astimezone(IST)


def ensure_ist(value: datetime) -> datetime:
    """Interpret naive MongoDB datetimes as UTC; return an IST-aware datetime."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(IST)


def get_shift_window(
    punch_in: datetime, shift_start: str, shift_end: str
) -> tuple[datetime, datetime]:
    """Build shift boundaries in IST, including overnight shifts."""
    punch_in = ensure_ist(punch_in)
    try:
        start_time = time.fromisoformat(shift_start)
        end_time = time.fromisoformat(shift_end)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="Invalid shift time") from exc

    shift_date = punch_in.date()
    overnight = end_time <= start_time
    if overnight and punch_in.time().replace(tzinfo=None) < end_time:
        shift_date -= timedelta(days=1)

    start_dt = datetime.combine(shift_date, start_time, tzinfo=IST)
    end_date = shift_date + timedelta(days=1) if overnight else shift_date
    end_dt = datetime.combine(end_date, end_time, tzinfo=IST)
    return start_dt, end_dt


def compute_late_minutes(punch_in: datetime, shift_start: datetime) -> int:
    """Apply the 10-minute grace period and return whole late minutes."""
    elapsed_seconds = int(
        (ensure_ist(punch_in) - ensure_ist(shift_start)).total_seconds()
    )
    if elapsed_seconds <= 600:
        return 0
    return elapsed_seconds // 60


def compute_shift_late_minutes(
    punch_in: datetime, shift_start: datetime, shift_end: datetime
) -> int:
    """Calculate lateness without misclassifying an overnight early-morning punch.

    A punch before the end of an overnight shift belongs to the previous
    calendar day's shift and is not late relative to that shift's start.
    Punches on the shift-start date still use the normal 10-minute grace rule.
    """
    punch_in = ensure_ist(punch_in)
    shift_start = ensure_ist(shift_start)
    shift_end = ensure_ist(shift_end)
    is_overnight = shift_end.date() > shift_start.date()
    is_next_day_early_punch = (
        is_overnight
        and punch_in.date() > shift_start.date()
        and punch_in < shift_end
    )
    if is_next_day_early_punch:
        return 0
    return compute_late_minutes(punch_in, shift_start)


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> Decimal:
    """Return elapsed work hours rounded half-up to two decimal places."""
    elapsed_seconds = int((ensure_ist(punch_out) - ensure_ist(punch_in)).total_seconds())
    if elapsed_seconds < 0:
        raise ValueError("punch_out cannot be before punch_in")
    return (Decimal(elapsed_seconds) / Decimal(3600)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


def compute_overtime(punch_out: datetime, shift_end: datetime) -> int:
    """Count whole overtime minutes only when at least 30 minutes elapsed."""
    elapsed_seconds = int((ensure_ist(punch_out) - ensure_ist(shift_end)).total_seconds())
    whole_minutes = elapsed_seconds // 60
    return whole_minutes if whole_minutes >= 30 else 0


def mongo_datetime(value: datetime) -> datetime:
    """Return UTC-aware datetime for PyMongo storage (MongoDB persists UTC)."""
    return ensure_ist(value).astimezone(timezone.utc)


def api_timestamp(value: datetime) -> int:
    """Serialize a stored or aware datetime as whole-second epoch milliseconds."""
    utc_value = ensure_ist(value).astimezone(timezone.utc).replace(microsecond=0)
    return int(utc_value.timestamp()) * 1000


def api_encode(value):
    """Recursively encode API datetimes as epoch milliseconds, never ISO strings."""
    if isinstance(value, datetime):
        return api_timestamp(value)
    if isinstance(value, dict):
        return {key: api_encode(item) for key, item in value.items() if key != "_id"}
    if isinstance(value, (list, tuple)):
        return [api_encode(item) for item in value]
    return jsonable_encoder(value)


def parse_iso_date(value: str) -> date:
    """Parse only the contract's canonical YYYY-MM-DD representation."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Date must use YYYY-MM-DD")
    return date.fromisoformat(value)


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: str = Field(default="PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    emp_code: str
    punched_at: Optional[StrictInt] = None


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Optional[str] = Field(default=None, pattern=r"^(PRESENT|WFH|ON_DUTY|ABSENT|LEAVE)$")
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints provided in starter
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError as exc:
        raise HTTPException(status_code=503, detail="MongoDB unavailable") from exc
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    # Basic format validation; the OpenAPI contract remains authoritative.
    try:
        parse_iso_date(body.joined_on)
        time.fromisoformat(body.shift_start)
        time.fromisoformat(body.shift_end)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid date or shift time") from exc

    doc = body.model_dump()
    doc["created_at"] = mongo_datetime(datetime.now(IST).replace(microsecond=0))
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="emp_code already exists") from exc
    doc.pop("_id", None)
    return api_encode(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {"department": department} if department else {}
    total = db.employees.count_documents(q)
    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", 1)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": api_encode(items), "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.status not in {"PRESENT", "WFH", "ON_DUTY"}:
        raise HTTPException(status_code=422, detail="Invalid attendance status")

    try:
        ts = epoch_ms_to_datetime(body.punched_at) if body.punched_at is not None else datetime.now(IST).replace(microsecond=0)
    except (ValueError, OverflowError, OSError) as exc:
        raise HTTPException(status_code=422, detail="Invalid punched_at timestamp") from exc

    shift_start_dt, shift_end_dt = get_shift_window(
        ts, emp["shift_start"], emp["shift_end"]
    )
    attendance_date = shift_start_dt.date().isoformat()
    late_minutes = compute_shift_late_minutes(
        ts, shift_start_dt, shift_end_dt
    )
    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": mongo_datetime(ts),
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        result = db.attendance_logs.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="Already punched in for this date") from exc

    doc.pop("_id", None)
    return api_encode(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    """Close the latest punch-in not later than punched_at, atomically."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    try:
        punched_out_at = (
            epoch_ms_to_datetime(body.punched_at)
            if body.punched_at is not None
            else datetime.now(IST).replace(microsecond=0)
        )
    except (ValueError, OverflowError, OSError) as exc:
        raise HTTPException(status_code=422, detail="Invalid punched_at timestamp") from exc

    # Find the most recent record first, including records already closed, so a
    # repeated punch-out returns 409 instead of accidentally closing an older day.
    candidate = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": mongo_datetime(punched_out_at)}},
        sort=[("punch_in", -1)],
    )
    if candidate is None:
        raise HTTPException(status_code=404, detail="No punch-in found")
    if candidate.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Already punched out")

    punch_in_at = ensure_ist(candidate["punch_in"]).replace(microsecond=0)
    elapsed_seconds = int((punched_out_at - punch_in_at).total_seconds())
    if elapsed_seconds <= 0 or elapsed_seconds > 24 * 60 * 60:
        raise HTTPException(
            status_code=422,
            detail="punched_at must be after punch_in and within 24 hours",
        )

    shift_start_dt, shift_end_dt = get_shift_window(
        punch_in_at, emp["shift_start"], emp["shift_end"]
    )
    work_hours = compute_work_hours(punch_in_at, punched_out_at)
    overtime_minutes = compute_overtime(punched_out_at, shift_end_dt)
    half_day = work_hours < Decimal("4.50")

    # Compare-and-set: only one concurrent request can change punch_out from null.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": candidate["_id"], "punch_out": None},
        {"$set": {
            "punch_out": mongo_datetime(punched_out_at),
            "work_hours": float(work_hours),
            "overtime_minutes": overtime_minutes,
            "half_day": half_day,
        }},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="Already punched out")

    updated.pop("_id", None)
    return api_encode(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = Query(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        date_filter = {}
        try:
            if date_from:
                parse_iso_date(date_from)
                date_filter["$gte"] = date_from
            if date_to:
                parse_iso_date(date_to)
                date_filter["$lte"] = date_to
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Dates must use YYYY-MM-DD") from exc
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from must be <= date_to")
        q["date"] = date_filter
    if status:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    docs = list(
        db.attendance_logs.find(q, {"_id": 1, "emp_code": 1, "date": 1, "status": 1,
                                     "punch_in": 1, "punch_out": 1, "work_hours": 1,
                                     "late_minutes": 1, "overtime_minutes": 1, "half_day": 1,
                                     "history": 1})
        .sort([("date", -1), ("emp_code", 1)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    for doc in docs:
        doc.pop("_id", None)
    return {"items": api_encode(docs), "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{attendance_date}")
def regularize_attendance(emp_code: str, attendance_date: str, body: RegularizeIn):
    """Correct one attendance record and append a concurrency-safe audit entry."""
    try:
        parse_iso_date(attendance_date)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="date must use YYYY-MM-DD") from exc

    if not 5 <= len(body.reason) <= 200:
        raise HTTPException(status_code=422, detail="reason must be 5-200 characters")
    if not 1 <= len(body.regularized_by) <= 50:
        raise HTTPException(status_code=422, detail="regularized_by must be 1-50 characters")
    if body.status is not None and body.status not in {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}:
        raise HTTPException(status_code=422, detail="Invalid attendance status")

    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    current = db.attendance_logs.find_one({"emp_code": emp_code, "date": attendance_date})
    if current is None:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    old_history = current.get("history") or []
    old_status = current.get("status")
    new_status = body.status if body.status is not None else old_status

    absent_or_leave = new_status in {"ABSENT", "LEAVE"}
    if absent_or_leave and (body.punch_in is not None or body.punch_out is not None):
        raise HTTPException(status_code=422, detail="ABSENT/LEAVE cannot include punch times")

    try:
        if absent_or_leave:
            new_punch_in = None
            new_punch_out = None
        else:
            raw_in = body.punch_in if body.punch_in is not None else current.get("punch_in")
            raw_out = body.punch_out if body.punch_out is not None else current.get("punch_out")
            if raw_in is None:
                raise HTTPException(status_code=422, detail="Presence status requires punch_in")
            new_punch_in = epoch_ms_to_datetime(raw_in) if isinstance(raw_in, int) else ensure_ist(raw_in)
            new_punch_out = epoch_ms_to_datetime(raw_out) if isinstance(raw_out, int) else (ensure_ist(raw_out) if raw_out is not None else None)
    except (ValueError, OverflowError, OSError) as exc:
        raise HTTPException(status_code=422, detail="Invalid punch timestamp") from exc

    if new_punch_in is not None:
        shift_start_dt, shift_end_dt = get_shift_window(
            new_punch_in, emp["shift_start"], emp["shift_end"]
        )
        # Attendance date is the date the shift started, including overnight shifts.
        if shift_start_dt.date().isoformat() != attendance_date:
            raise HTTPException(
                status_code=422,
                detail="punch_in must belong to the record attendance date",
            )
        new_late = compute_shift_late_minutes(
            new_punch_in, shift_start_dt, shift_end_dt
        )
    else:
        shift_end_dt = None
        new_late = 0

    if new_punch_out is not None:
        if new_punch_in is None:
            raise HTTPException(status_code=422, detail="punch_out requires punch_in")
        duration = (new_punch_out - new_punch_in).total_seconds()
        if duration <= 0 or duration > 24 * 60 * 60:
            raise HTTPException(status_code=422, detail="punch_out must be after punch_in and within 24 hours")
        work_hours = compute_work_hours(new_punch_in, new_punch_out)
        new_overtime = compute_overtime(new_punch_out, shift_end_dt)
        new_half_day = work_hours < Decimal("4.50")
    else:
        work_hours = None
        new_overtime = 0
        new_half_day = False

    old_punch_in = ensure_ist(current["punch_in"]) if current.get("punch_in") is not None else None
    old_punch_out = ensure_ist(current["punch_out"]) if current.get("punch_out") is not None else None
    old_values = {
        "status": old_status,
        "punch_in": old_punch_in,
        "punch_out": old_punch_out,
        "work_hours": current.get("work_hours"),
        "late_minutes": current.get("late_minutes", 0),
        "overtime_minutes": current.get("overtime_minutes", 0),
        "half_day": current.get("half_day", False),
    }
    new_values = {
        "status": new_status,
        "punch_in": new_punch_in,
        "punch_out": new_punch_out,
        "work_hours": float(work_hours) if work_hours is not None else None,
        "late_minutes": new_late,
        "overtime_minutes": new_overtime,
        "half_day": new_half_day,
    }

    def comparable(field, value):
        if field in {"punch_in", "punch_out"}:
            return int(ensure_ist(value).timestamp() * 1000) if value is not None else None
        if field == "work_hours" and value is not None:
            return float(value)
        if field == "late_minutes":
            return int(value or 0)
        if field == "overtime_minutes":
            return int(value or 0)
        if field == "half_day":
            return bool(value)
        return value

    changes = {}
    for field in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"):
        old_value = old_values[field]
        new_value = new_values[field]
        if comparable(field, old_value) != comparable(field, new_value):
            if field in {"punch_in", "punch_out"}:
                from_value = mongo_datetime(old_value) if old_value is not None else None
                to_value = mongo_datetime(new_value) if new_value is not None else None
            else:
                from_value = old_value if old_value is not None else (0 if field in {"late_minutes", "overtime_minutes"} else False if field == "half_day" else None)
                to_value = new_value
            changes[field] = {"from": from_value, "to": to_value}

    if not changes:
        raise HTTPException(status_code=422, detail="Request does not change the record")

    history_entry = {
        "at": mongo_datetime(datetime.now(IST).replace(microsecond=0)),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    set_values = {
        "status": new_status,
        "punch_in": mongo_datetime(new_punch_in) if new_punch_in is not None else None,
        "punch_out": mongo_datetime(new_punch_out) if new_punch_out is not None else None,
        "work_hours": new_values["work_hours"],
        "late_minutes": new_late,
        "overtime_minutes": new_overtime,
        "half_day": new_half_day,
    }
    # Optimistic concurrency: only update if history still equals the version read above.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": current["_id"], "history": old_history},
        {"$set": set_values, "$push": {"history": history_entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="Record changed concurrently; retry the correction")

    updated.pop("_id", None)
    return api_encode(updated)


def build_employee_monthly_pipeline(emp_code, month_start, next_month, month_end_date):
    pipeline = [
        {"$match": {"emp_code": emp_code}},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"employee_code": "$emp_code"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$emp_code", "$$employee_code"]},
                    {"$gte": ["$date", month_start]},
                    {"$lt": ["$date", next_month]},
                ]}}},
                {"$project": {
                    "_id": 0, "date": 1, "status": 1, "half_day": 1,
                    "late_minutes": 1, "overtime_minutes": 1,
                }},
            ],
            "as": "logs",
        }},
        {"$set": {
            "_joined_date": {"$dateFromString": {"dateString": "$joined_on"}},
            "_month_start": {"$dateFromString": {"dateString": month_start}},
            "_month_end": {"$dateFromString": {"dateString": month_end_date.isoformat()}},
        }},
        {"$set": {
            "_working_start": {"$cond": [
                {"$gt": ["$_joined_date", "$_month_start"]},
                "$_joined_date", "$_month_start",
            ]},
        }},
        {"$set": {
            "working_days": {"$cond": [
                {"$gt": ["$_working_start", "$_month_end"]},
                0,
                {"$size": {"$filter": {
                    "input": {"$map": {
                        "input": {"$range": [
                            0,
                            {"$add": [{"$dateDiff": {
                                "startDate": "$_working_start",
                                "endDate": "$_month_end",
                                "unit": "day",
                            }}, 1]},
                        ]},
                        "as": "offset",
                        "in": {"$dateAdd": {
                            "startDate": "$_working_start",
                            "unit": "day",
                            "amount": "$$offset",
                        }},
                    }},
                    "as": "workday",
                    "cond": {"$and": [
                        {"$gte": [{"$dayOfWeek": "$$workday"}, 2]},
                        {"$lte": [{"$dayOfWeek": "$$workday"}, 6]},
                    ]},
                }}}
            ]},
            "present_days": {"$sum": {"$map": {
                "input": {"$filter": {
                    "input": "$logs", "as": "log",
                    "cond": {"$and": [
                        {"$in": ["$$log.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$$log.date"}}}, 2]},
                        {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$$log.date"}}}, 6]},
                    ]},
                }},
                "as": "log",
                "in": {"$cond": [{"$eq": ["$$log.half_day", True]}, 0.5, 1]},
            }}},
            "leave_days": {"$size": {"$filter": {
                "input": "$logs", "as": "log", "cond": {"$eq": ["$$log.status", "LEAVE"]},
            }}},
            "late_count": {"$size": {"$filter": {
                "input": "$logs", "as": "log", "cond": {"$gt": [{"$ifNull": ["$$log.late_minutes", 0]}, 0]},
            }}},
            "total_late_minutes": {"$sum": {"$map": {
                "input": "$logs", "as": "log", "in": {"$ifNull": ["$$log.late_minutes", 0]},
            }}},
            "total_overtime_minutes": {"$sum": {"$map": {
                "input": "$logs", "as": "log", "in": {"$ifNull": ["$$log.overtime_minutes", 0]},
            }}},
        }},
        {"$project": {
            "_id": 0, "emp_code": 1, "working_days": 1, "present_days": 1,
            "leave_days": 1, "late_count": 1, "total_late_minutes": 1,
            "total_overtime_minutes": 1,
        }},
    ]
    return pipeline


def build_department_summary_pipeline(employee_match, month_start, next_month):
    pipeline = [
        {"$match": employee_match},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"employee_code": "$emp_code"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$emp_code", "$$employee_code"]},
                    {"$gte": ["$date", month_start]},
                    {"$lt": ["$date", next_month]},
                ]}}},
                {"$project": {
                    "_id": 0, "date": 1, "status": 1, "work_hours": 1,
                    "half_day": 1, "late_minutes": 1, "overtime_minutes": 1,
                }},
            ],
            "as": "logs",
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$set": {
            "_is_present": {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
            "_is_weekday": {"$and": [
                {"$ne": [{"$ifNull": ["$logs.date", None]}, None]},
                {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": {"$ifNull": ["$logs.date", month_start]}}}}, 2]},
                {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": {"$ifNull": ["$logs.date", month_start]}}}}, 6]},
            ]},
        }},
        {"$group": {
            "_id": "$department",
            "headcount": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": {"$cond": [
                {"$and": ["$_is_present", "$_is_weekday"]},
                {"$cond": [{"$eq": [{"$ifNull": ["$logs.half_day", False]}, True]}, 0.5, 1]},
                0,
            ]}},
            "work_hour_records": {"$push": {"$cond": [
                {"$and": ["$_is_present", {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]}]},
                "$logs.work_hours", None,
            ]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
        }},
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": {"$size": "$headcount"},
            "present_days": 1,
            "avg_work_hours": {"$avg": "$work_hour_records"},
            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1,
        }},
        {"$sort": {"department": 1}},
    ]
    return pipeline


def build_late_leaderboard_pipeline(month_start, next_month, department, limit):
    pipeline = [
        {"$match": {"date": {"$gte": month_start, "$lt": next_month}}},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "late_count": {"$sum": {
                "$cond": [
                    {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                    1,
                    0,
                ]
            }},
        }},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "employee",
        }},
        {"$unwind": "$employee"},
    ]

    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})

    # Rank only by total late minutes so equal totals receive the same rank.
    # Apply the secondary emp_code ordering after rank is calculated.
    pipeline.extend([
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "employee.emp_code": 1}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$employee.emp_code",
            "name": "$employee.name",
            "department": "$employee.department",
            "total_late_minutes": 1,
            "late_count": 1,
        }},
    ])

    return pipeline


def build_department_trend_pipeline(department, from_date, to_date):
    pipeline = [
        {"$match": {"department": department}},
        {"$group": {
            "_id": None,
            "employees": {"$push": {"emp_code": "$emp_code", "joined_on": "$joined_on"}},
            "emp_codes": {"$addToSet": "$emp_code"},
        }},
        {"$set": {
            "_start": {"$dateFromString": {"dateString": from_date}},
            "_end": {"$dateFromString": {"dateString": to_date}},
        }},
        {"$set": {"_dates": {"$map": {
            "input": {"$range": [0, {"$add": [{"$dateDiff": {
                "startDate": "$_start", "endDate": "$_end", "unit": "day",
            }}, 1]}]},
            "as": "offset",
            "in": {"$dateAdd": {
                "startDate": "$_start", "unit": "day", "amount": "$$offset",
            }},
        }}}},
        {"$unwind": "$_dates"},
        {"$set": {
            "date": {"$dateToString": {"date": "$_dates", "format": "%Y-%m-%d"}},
            "is_working_day": {"$and": [
                {"$gte": [{"$dayOfWeek": "$_dates"}, 2]},
                {"$lte": [{"$dayOfWeek": "$_dates"}, 6]},
            ]},
            "headcount": {"$size": {"$filter": {
                "input": "$employees", "as": "employee",
                "cond": {"$lte": ["$$employee.joined_on", {
                    "$dateToString": {"date": "$_dates", "format": "%Y-%m-%d"}
                }]},
            }}},
        }},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"day": "$date", "codes": "$emp_codes"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$date", "$$day"]},
                    {"$in": ["$emp_code", "$$codes"]},
                ]}}},
                {"$project": {
                    "_id": 0, "status": 1, "half_day": 1, "late_minutes": 1,
                }},
            ],
            "as": "_logs",
        }},
        {"$set": {
            "present_count": {"$sum": {"$map": {
                "input": {"$filter": {
                    "input": "$_logs", "as": "log",
                    "cond": {"$in": ["$$log.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                }},
                "as": "log",
                "in": {"$cond": [{"$eq": ["$$log.half_day", True]}, 0.5, 1]},
            }}},
            "late_count": {"$size": {"$filter": {
                "input": "$_logs", "as": "log",
                "cond": {"$gt": [{"$ifNull": ["$$log.late_minutes", 0]}, 0]},
            }}},
        }},
        {"$set": {
            "attendance_rate": {"$cond": [
                {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                # Explicit positive half-up rounding to 4 decimal places.
                {"$divide": [{"$floor": {"$add": [
                    {"$multiply": [{"$divide": ["$present_count", "$headcount"]}, 10000]},
                    0.5,
                ]}}, 10000]},
                None,
            ]},
        }},
        {"$setWindowFields": {
            "sortBy": {"_dates": 1},
            "output": {"_moving_avg_raw": {
                "$avg": "$attendance_rate",
                "window": {"documents": [-6, 0]},
            }},
        }},
        {"$set": {"moving_avg_7d": {"$cond": [
            {"$eq": ["$_moving_avg_raw", None]},
            None,
            {"$divide": [{"$floor": {"$add": [
                {"$multiply": ["$_moving_avg_raw", 10000]}, 0.5,
            ]}}, 10000]},
        ]}}},
        {"$sort": {"_dates": 1}},
        {"$project": {
            "_id": 0, "date": 1, "is_working_day": 1, "headcount": 1,
            "present_count": 1, "late_count": 1, "attendance_rate": 1,
            "moving_avg_7d": 1,
        }},
    ]
    return pipeline


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    """Monthly employee summary computed in MongoDB aggregation."""
    # Validate YYYY-MM strictly and derive an exclusive next-month boundary.
    import re
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise HTTPException(status_code=422, detail="month must use YYYY-MM")
    try:
        month_start_date = date.fromisoformat(f"{month}-01")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid month") from exc

    if month_start_date.month == 12:
        next_month_date = date(month_start_date.year + 1, 1, 1)
    else:
        next_month_date = date(month_start_date.year, month_start_date.month + 1, 1)

    month_start = month_start_date.isoformat()
    next_month = next_month_date.isoformat()
    month_end_date = next_month_date - timedelta(days=1)

    pipeline = build_employee_monthly_pipeline(emp_code, month_start, next_month, month_end_date)

    result = list(db.employees.aggregate(pipeline))
    if not result:
        raise HTTPException(status_code=404, detail="Employee not found")

    summary = result[0]
    working_days = int(summary.get("working_days", 0))
    present_days = float(summary.get("present_days", 0))
    attendance_pct = None
    if working_days:
        attendance_pct = float(
            (Decimal(str(present_days)) * Decimal(100) / Decimal(working_days)).quantize(
                Decimal("0.0001"), rounding=ROUND_HALF_UP
            )
        )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": int(summary.get("leave_days", 0)),
        "late_count": int(summary.get("late_count", 0)),
        "total_late_minutes": int(summary.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(summary.get("total_overtime_minutes", 0)),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    """Summarize monthly attendance by department using a cross-collection pipeline."""
    import re

    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise HTTPException(status_code=422, detail="month must use YYYY-MM")
    try:
        month_start_date = date.fromisoformat(f"{month}-01")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid month") from exc

    if month_start_date.month == 12:
        next_month_date = date(month_start_date.year + 1, 1, 1)
    else:
        next_month_date = date(month_start_date.year, month_start_date.month + 1, 1)
    month_start = month_start_date.isoformat()
    next_month = next_month_date.isoformat()
    month_end = (next_month_date - timedelta(days=1)).isoformat()

    employee_match = {"joined_on": {"$lte": month_end}}
    if department is not None:
        employee_match["department"] = department

    pipeline = build_department_summary_pipeline(employee_match, month_start, next_month)

    results = list(db.employees.aggregate(pipeline, allowDiskUse=True))
    items = []
    for item in results:
        avg_hours = item.get("avg_work_hours")
        if avg_hours is not None:
            avg_hours = float(Decimal(str(avg_hours)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            ))
        items.append({
            "department": item["department"],
            "headcount": int(item["headcount"]),
            "present_days": float(item.get("present_days", 0)),
            "avg_work_hours": avg_hours,
            "late_count": int(item.get("late_count", 0)),
            "total_late_minutes": int(item.get("total_late_minutes", 0)),
            "leave_count": int(item.get("leave_count", 0)),
            "on_duty_count": int(item.get("on_duty_count", 0)),
        })

    return {"month": month, "items": items}



@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str,
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    """Rank employees by total late minutes using MongoDB competition ranking."""
    import re

    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise HTTPException(status_code=422, detail="month must use YYYY-MM")

    month_start = f"{month}-01"
    year, month_number = map(int, month.split("-"))
    if month_number == 12:
        next_month = f"{year + 1:04d}-01-01"
    else:
        next_month = f"{year:04d}-{month_number + 1:02d}-01"

    pipeline = build_late_leaderboard_pipeline(month_start, next_month, department, limit)
    rows = list(db.attendance_logs.aggregate(pipeline, allowDiskUse=True))
    return {
        "month": month,
        "items": [
            {
                "rank": int(row["rank"]),
                "emp_code": row["emp_code"],
                "name": row["name"],
                "department": row["department"],
                "total_late_minutes": int(row["total_late_minutes"]),
                "late_count": int(row["late_count"]),
            }
            for row in rows
        ],
    }


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    """Return a gap-filled daily attendance trend for one department."""
    import re

    date_pattern = re.compile(r"\d{4}-\d{2}-\d{2}")
    if not date_pattern.fullmatch(from_date) or not date_pattern.fullmatch(to_date):
        raise HTTPException(status_code=422, detail="from and to must use YYYY-MM-DD")
    try:
        start_day = date.fromisoformat(from_date)
        end_day = date.fromisoformat(to_date)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid calendar date") from exc

    day_count = (end_day - start_day).days + 1
    if day_count <= 0:
        raise HTTPException(status_code=422, detail="to must be on or after from")
    if day_count > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(status_code=404, detail="Department not found")

    # One department-level seed document generates every requested calendar day.
    # All date expansion, attendance aggregation, and rolling-window calculation
    # remain in MongoDB; Python only validates inputs and shapes the response.
    pipeline = build_department_trend_pipeline(department, from_date, to_date)

    rows = list(db.employees.aggregate(pipeline, allowDiskUse=True))
    # Numeric normalization ensures the JSON contract is stable across MongoDB
    # integer/double representations, while preserving null values.
    result = []
    for row in rows:
        result.append({
            "date": row["date"],
            "is_working_day": bool(row["is_working_day"]),
            "headcount": int(row["headcount"]),
            "present_count": float(row.get("present_count", 0)),
            "late_count": int(row.get("late_count", 0)),
            "attendance_rate": None if row.get("attendance_rate") is None else float(row["attendance_rate"]),
            "moving_avg_7d": None if row.get("moving_avg_7d") is None else float(row["moving_avg_7d"]),
        })
    return {"department": department, "from": from_date, "to": to_date, "items": result}


# --------------------------------------------------------------------------- #
# Admin query explain
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = Query(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Return MongoDB executionStats for a named endpoint's principal query."""
    import re

    allowed = {
        "attendance_list", "employee_monthly", "department_summary",
        "late_leaderboard", "department_trend",
    }
    if endpoint not in allowed:
        raise HTTPException(status_code=422, detail="Unsupported endpoint")

    if endpoint == "attendance_list":
        query = {}
        if emp_code:
            query["emp_code"] = emp_code
        if date_from or date_to:
            date_query = {}
            try:
                if date_from:
                    parse_iso_date(date_from)
                    date_query["$gte"] = date_from
                if date_to:
                    parse_iso_date(date_to)
                    date_query["$lte"] = date_to
            except ValueError as exc:
                raise HTTPException(status_code=422, detail="Dates must use YYYY-MM-DD") from exc
            if date_from and date_to and date_from > date_to:
                raise HTTPException(status_code=422, detail="date_from must be <= date_to")
            query["date"] = date_query
        if status:
            query["status"] = status
        command = {
            "find": "attendance_logs",
            "filter": query,
            "sort": {"date": -1, "emp_code": 1},
            "skip": (page - 1) * page_size,
            "limit": page_size,
            "projection": {
                "_id": 1, "emp_code": 1, "date": 1, "status": 1,
                "punch_in": 1, "punch_out": 1, "work_hours": 1,
                "late_minutes": 1, "overtime_minutes": 1, "half_day": 1,
                "history": 1,
            },
        }
        collection = "attendance_logs"

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required")
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
            raise HTTPException(status_code=422, detail="month must use YYYY-MM")
        month_start_date = date.fromisoformat(month + "-01")
        next_month_date = (date(month_start_date.year + 1, 1, 1)
                           if month_start_date.month == 12
                           else date(month_start_date.year, month_start_date.month + 1, 1))
        month_end_date = next_month_date - timedelta(days=1)
        pipeline = build_employee_monthly_pipeline(
            emp_code, month_start_date.isoformat(), next_month_date.isoformat(), month_end_date
        )
        command = {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}
        collection = "employees"

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
            raise HTTPException(status_code=422, detail="month must use YYYY-MM")
        month_start_date = date.fromisoformat(month + "-01")
        next_month_date = (date(month_start_date.year + 1, 1, 1)
                           if month_start_date.month == 12
                           else date(month_start_date.year, month_start_date.month + 1, 1))
        month_end = (next_month_date - timedelta(days=1)).isoformat()
        employee_filter = {"joined_on": {"$lte": month_end}}
        if department is not None:
            employee_filter["department"] = department
        pipeline = build_department_summary_pipeline(
            employee_filter, month_start_date.isoformat(), next_month_date.isoformat()
        )
        command = {"aggregate": "employees", "pipeline": pipeline, "cursor": {}, "allowDiskUse": True}
        collection = "employees"

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
            raise HTTPException(status_code=422, detail="month must use YYYY-MM")
        month_start_date = date.fromisoformat(month + "-01")
        next_month_date = (date(month_start_date.year + 1, 1, 1)
                           if month_start_date.month == 12
                           else date(month_start_date.year, month_start_date.month + 1, 1))
        pipeline = build_late_leaderboard_pipeline(
            month_start_date.isoformat(), next_month_date.isoformat(), department, limit
        )
        command = {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}, "allowDiskUse": True}
        collection = "attendance_logs"

    else:  # department_trend
        if not department or not from_date or not to_date:
            raise HTTPException(status_code=422, detail="department, from and to are required")
        try:
            start = parse_iso_date(from_date)
            end = parse_iso_date(to_date)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="from and to must use YYYY-MM-DD") from exc
        days = (end - start).days + 1
        if days <= 0 or days > 92:
            raise HTTPException(status_code=422, detail="Require to >= from and a maximum range of 92 days")
        if db.employees.find_one({"department": department}, {"_id": 1}) is None:
            raise HTTPException(status_code=404, detail="Department not found")
        pipeline = build_department_trend_pipeline(department, from_date, to_date)
        command = {"aggregate": "employees", "pipeline": pipeline, "cursor": {}, "allowDiskUse": True}
        collection = "employees"

    try:
        explanation = db.command("explain", command, verbosity="executionStats")
    except PyMongoError as exc:
        raise HTTPException(status_code=503, detail="Unable to explain MongoDB query") from exc
    return api_encode({"endpoint": endpoint, "collection": collection, "explain": explanation})

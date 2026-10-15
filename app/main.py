"""
Employee Attendance & Analytics API
Run: python -m uvicorn app.main:app --port 8000
"""
import os
import re
import calendar
from datetime import datetime, date, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path as ApiPath
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()
MONGO_URI = os.getenv("MONGO_URI", "mongodb://127.0.0.1:27017")
MONGO_DB = os.getenv("MONGO_DB", "hrone_candidate")
client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000, tz_aware=False)
db = client[MONGO_DB]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
STATUSES = {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}
MIN_EPOCH_MS, MAX_EPOCH_MS = 100_000_000_000, 4_102_444_800_000
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# ------------------------------- utilities ---------------------------------
def now_utc():
    return datetime.now(UTC).replace(microsecond=0).replace(tzinfo=None)

def trunc_dt(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(microsecond=0, tzinfo=None)

def from_ms(value: int) -> datetime:
    if type(value) is not int or not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
        raise HTTPException(422, "punched_at must be an integer epoch-millisecond timestamp in range")
    return datetime.fromtimestamp(value / 1000, tz=UTC).replace(microsecond=0, tzinfo=None)

def to_ms(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp() * 1000)

def public_employee(doc):
    return {k: (to_ms(v) if k == "created_at" else v) for k, v in doc.items() if k != "_id"}

def public_attendance(doc):
    result = {k: v for k, v in doc.items() if k not in {"_id", "id"}}
    for key in ("punch_in", "punch_out"):
        result[key] = to_ms(result.get(key))
    history = []
    for entry in result.get("history", []):
        item = dict(entry)
        item["at"] = to_ms(item.get("at"))
        changes = {}
        for field, change in item.get("changes", {}).items():
            ch = dict(change)
            if field in {"punch_in", "punch_out"}:
                ch["from"] = to_ms(ch.get("from"))
                ch["to"] = to_ms(ch.get("to"))
            changes[field] = ch
        item["changes"] = changes
        history.append(item)
    result["history"] = history
    return result

def parse_date(value: str, field="date") -> date:
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
        return parsed
    except Exception:
        raise HTTPException(422, f"{field} must be YYYY-MM-DD")

def parse_month(value: str) -> tuple[date, date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value or ""):
        raise HTTPException(422, "month must be YYYY-MM")
    year, month = map(int, value.split("-"))
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])

def parse_clock(value: str):
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value or ""):
        raise HTTPException(422, "shift times must use HH:MM")
    h, m = map(int, value.split(":"))
    return time(h, m)

def round_half_up(value, places=2):
    if value is None:
        return None
    quant = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quant, rounding=ROUND_HALF_UP))

def working_days(start: date, end: date) -> int:
    if end < start:
        return 0
    count = 0
    current = start
    while current <= end:
        if current.weekday() < 5:
            count += 1
        current += timedelta(days=1)
    return count

def ist_dt(dt):
    return dt.replace(tzinfo=UTC).astimezone(IST)

def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    local = ist_dt(punch_in)
    start, end = parse_clock(shift_start), parse_clock(shift_end)
    day = local.date()
    if end <= start and local.time().replace(tzinfo=None) < end:
        day -= timedelta(days=1)
    return day.isoformat()

def shift_boundary(attendance_day: str, clock_value: str, overnight_end=False, shift_start=None):
    day = parse_date(attendance_day)
    clock = parse_clock(clock_value)
    if overnight_end and shift_start is not None and parse_clock(clock_value) <= parse_clock(shift_start):
        day += timedelta(days=1)
    return datetime.combine(day, clock, tzinfo=IST).astimezone(UTC).replace(tzinfo=None)

def compute_late_minutes(punch_in: datetime, shift_start: str, date_str: str) -> int:
    punch = ist_dt(punch_in)
    boundary = datetime.combine(parse_date(date_str), parse_clock(shift_start), tzinfo=IST)
    delta = (punch - boundary).total_seconds()
    return int(delta // 60) if delta > 600 else 0

def compute_work_hours(punch_in, punch_out):
    seconds = (punch_out - punch_in).total_seconds()
    return round_half_up(seconds / 3600, 2)

def compute_overtime(punch_out, shift_start, shift_end, date_str):
    overnight = parse_clock(shift_end) <= parse_clock(shift_start)
    end = shift_boundary(date_str, shift_end, overnight_end=overnight, shift_start=shift_start)
    minutes = int((punch_out - end).total_seconds() // 60)
    return minutes if minutes >= 30 else 0

def derive_fields(punch_in, punch_out, shift_start, shift_end, day):
    late = compute_late_minutes(punch_in, shift_start, day) if punch_in else 0
    hours = compute_work_hours(punch_in, punch_out) if punch_in and punch_out else None
    overtime = compute_overtime(punch_out, shift_start, shift_end, day) if punch_in and punch_out else 0
    return {"late_minutes": late, "work_hours": hours, "overtime_minutes": overtime,
            "half_day": bool(hours is not None and hours < 4.50)}

def clean_doc(doc):
    return {k: v for k, v in doc.items() if k != "_id"}

def validate_page(page, page_size):
    if page < 1:
        raise HTTPException(422, "page must be at least 1")
    if not 1 <= page_size <= 100:
        raise HTTPException(422, "page_size must be between 1 and 100")


def ensure_indexes():
    indexes = {
        "employees": [
            ([("emp_code", ASCENDING)], True, "uq_employee_code"),
            ([("department", ASCENDING), ("emp_code", ASCENDING)], False, "ix_employee_department_code"),
            ([("joined_on", ASCENDING), ("department", ASCENDING)], False, "ix_employee_joined_department"),
        ],
        "attendance_logs": [
            ([("emp_code", ASCENDING), ("date", ASCENDING)], True, "uq_attendance_employee_date"),
            ([("date", DESCENDING), ("emp_code", ASCENDING)], False, "ix_attendance_date_code"),
            ([("emp_code", ASCENDING), ("date", DESCENDING)], False, "ix_attendance_code_date"),
            ([("status", ASCENDING), ("date", ASCENDING), ("emp_code", ASCENDING)], False, "ix_attendance_status_date_code"),
            ([("date", ASCENDING), ("late_minutes", DESCENDING), ("emp_code", ASCENDING)], False, "ix_attendance_date_late"),
            ([("date", ASCENDING), ("department", ASCENDING)], False, "ix_attendance_date_department"),
        ],
    }

    for collection_name, definitions in indexes.items():
        collection = db[collection_name]
        existing = collection.index_information()

        for keys, unique, name in definitions:
            matching = next(
                (
                    info for info in existing.values()
                    if info.get("key") == keys
                ),
                None,
            )

            if matching:
                if unique and not matching.get("unique", False):
                    raise RuntimeError(
                        f"Index {matching['name']} exists without required uniqueness"
                    )
                continue

            collection.create_index(
                keys,
                unique=unique,
                name=name,
            )


@app.on_event("startup")
def startup():
    ensure_indexes()


# --------------------------------- models -----------------------------------
class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("shift_start", "shift_end")
    @classmethod
    def valid_clock(cls, v):
        parse_clock(v)
        return v

    @field_validator("joined_on")
    @classmethod
    def valid_joined_on(cls, v):
        parse_date(v, "joined_on")
        return v

class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[int] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def valid_ms(cls, v):
        if v is not None and (type(v) is not int or not MIN_EPOCH_MS <= v <= MAX_EPOCH_MS):
            raise ValueError("punched_at must be an integer epoch-millisecond timestamp in range")
        return v

class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def valid_ms(cls, v):
        if v is not None and (type(v) is not int or not MIN_EPOCH_MS <= v <= MAX_EPOCH_MS):
            raise ValueError("punched_at must be an integer epoch-millisecond timestamp in range")
        return v

class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")
    @classmethod
    def valid_ms(cls, v):
        if v is not None and (type(v) is not int or not MIN_EPOCH_MS <= v <= MAX_EPOCH_MS):
            raise ValueError("timestamps must be integer epoch milliseconds in range")
        return v


# --------------------------------- routes -----------------------------------
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(503, "MongoDB unavailable")

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if body.shift_start == body.shift_end:
        raise HTTPException(422, "shift_start must differ from shift_end")
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return public_employee(doc)

@app.get("/employees")
def list_employees(department: Optional[str] = None,
                   page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    query = {"department": department} if department is not None else {}
    total = db.employees.count_documents(query)
    items = list(db.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING)
                 .skip((page - 1) * page_size).limit(page_size))
    return {"items": [public_employee(x) for x in items], "total": total, "page": page, "page_size": page_size}

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    instant = trunc_dt(from_ms(body.punched_at) if body.punched_at is not None else now_utc().replace(tzinfo=UTC))
    day = attendance_date(instant, emp["shift_start"], emp["shift_end"])
    doc = {"emp_code": body.emp_code, "date": day, "status": body.status, "punch_in": instant,
           "punch_out": None, "work_hours": None,
           "late_minutes": compute_late_minutes(instant, emp["shift_start"], day),
           "overtime_minutes": 0, "half_day": False, "history": []}
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "attendance record already exists for this date")
    return public_attendance(doc)

@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    instant = trunc_dt(from_ms(body.punched_at) if body.punched_at is not None else now_utc().replace(tzinfo=UTC))
    record = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": instant}},
                                         sort=[("punch_in", DESCENDING)])
    if not record:
        raise HTTPException(404, "no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "record already punched out")
    if instant <= record["punch_in"] or instant > record["punch_in"] + timedelta(hours=24):
        raise HTTPException(422, "punch-out must be after punch-in and within 24 hours")
    derived = derive_fields(record["punch_in"], instant, emp["shift_start"], emp["shift_end"], record["date"])
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {"punch_out": instant, **derived}}, return_document=ReturnDocument.AFTER)
    if not updated:
        raise HTTPException(409, "record already punched out")
    return public_attendance(updated)

@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None,
                    date_to: Optional[str] = None, status: Optional[str] = None,
                    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    query = {}
    if emp_code is not None: query["emp_code"] = emp_code
    if date_from is not None:
        parse_date(date_from, "date_from")
        query.setdefault("date", {})["$gte"] = date_from
    if date_to is not None:
        parse_date(date_to, "date_to")
        query.setdefault("date", {})["$lte"] = date_to
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be on or before date_to")
    if status is not None:
        if status not in STATUSES: raise HTTPException(422, "invalid status")
        query["status"] = status
    total = db.attendance_logs.count_documents(query)
    docs = list(db.attendance_logs.find(query, {"_id": 0}).sort([("date", DESCENDING), ("emp_code", ASCENDING)])
                .skip((page - 1) * page_size).limit(page_size))
    return {"items": [public_attendance(x) for x in docs], "total": total, "page": page, "page_size": page_size}

@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    parse_date(date)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    old = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not old:
        raise HTTPException(404, "attendance record not found")
    proposed = {k: old.get(k) for k in ("status", "punch_in", "punch_out")}
    request = body.model_dump(exclude_unset=True)
    reason, by = request.pop("reason"), request.pop("regularized_by")
    for field, value in request.items():
        proposed[field] = from_ms(value) if field in {"punch_in", "punch_out"} and value is not None else value
    status = proposed["status"]
    if status in {"ABSENT", "LEAVE"}:
        if ("punch_in" in request and request["punch_in"] is not None) or ("punch_out" in request and request["punch_out"] is not None):
            raise HTTPException(422, "ABSENT/LEAVE cannot have punch times")
        proposed["punch_in"] = proposed["punch_out"] = None
    elif proposed["punch_in"] is None:
        raise HTTPException(422, "presence status requires punch_in")
    if proposed["punch_in"] is not None:
        new_day = attendance_date(proposed["punch_in"], emp["shift_start"], emp["shift_end"])
        if new_day != date:
            raise HTTPException(422, "punch_in must belong to the record attendance date")
    if proposed["punch_in"] and proposed["punch_out"]:
        if proposed["punch_out"] <= proposed["punch_in"] or proposed["punch_out"] > proposed["punch_in"] + timedelta(hours=24):
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    if proposed["punch_in"] is None and proposed["punch_out"] is not None:
        raise HTTPException(422, "punch_out requires punch_in")
    derived = derive_fields(proposed["punch_in"], proposed["punch_out"], emp["shift_start"], emp["shift_end"], date)
    changes = {}
    for field in ("status", "punch_in", "punch_out", "late_minutes", "work_hours", "overtime_minutes", "half_day"):
        before = old.get(field)
        after = proposed.get(field) if field in proposed else derived[field]
        if field in derived:
            after = derived[field]
        if before != after:
            changes[field] = {"from": before, "to": after}
    if not changes:
        raise HTTPException(422, "request does not change the record")
    history_entry = {"at": now_utc(), "by": by, "reason": reason, "changes": changes}
    set_values = {**proposed, **derived}
    updated = db.attendance_logs.find_one_and_update(
        {"_id": old["_id"], "history": old.get("history", [])},
        {"$set": set_values, "$push": {"history": history_entry}},
        return_document=ReturnDocument.AFTER)
    if not updated:
        raise HTTPException(409, "record changed concurrently; retry")
    return public_attendance(updated)


# --------------------------- analytics pipelines ----------------------------
def monthly_match(start, end, emp_code=None):
    q = {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}
    if emp_code: q["emp_code"] = emp_code
    return q

def present_fraction_expr():
    return {"$cond": [{"$in": ["$status", list(PRESENCE)]},
                      {"$cond": ["$half_day", 0.5, 1.0]}, 0.0]}

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    start, end = parse_month(month)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp: raise HTTPException(404, "employee not found")
    effective_start = max(start, parse_date(emp["joined_on"], "joined_on"))
    wd = working_days(effective_start, end)
    pipeline = [
        {"$match": monthly_match(start, end, emp_code)},
        {"$group": {"_id": None,
                    "present_days": {"$sum": {"$cond": [{"$and": [
                        {"$in": ["$status", list(PRESENCE)]},
                        {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]},
                        {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 2]}]},
                        {"$cond": ["$half_day", 0.5, 1.0]}, 0]}},
                    "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "total_overtime_minutes": {"$sum": "$overtime_minutes"}}}
    ]
    result = list(db.attendance_logs.aggregate(pipeline))
    row = result[0] if result else {}
    present = round_half_up(row.get("present_days", 0), 2)
    return {"emp_code": emp_code, "month": month, "working_days": wd, "present_days": present,
            "leave_days": row.get("leave_days", 0), "late_count": row.get("late_count", 0),
            "total_late_minutes": row.get("total_late_minutes", 0),
            "total_overtime_minutes": row.get("total_overtime_minutes", 0),
            "attendance_pct": round_half_up(present / wd * 100, 2) if wd else None}

@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    start, end = parse_month(month)
    ematch = {"joined_on": {"$lte": end.isoformat()}}
    if department is not None: ematch["department"] = department
    pipeline = [
        {"$match": ematch},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1}, "codes": {"$push": "$emp_code"}}},
        {"$lookup": {"from": "attendance_logs", "let": {"codes": "$codes"},
                     "pipeline": [{"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
                                  {"$match": {"$expr": {"$in": ["$emp_code", "$$codes"]}}},
                                  {"$group": {"_id": None,
                                    "present_days": {"$sum": {"$cond": [{"$and": [
                                        {"$in": ["$status", list(PRESENCE)]},
                                        {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 2]},
                                        {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]}]},
                                        {"$cond": ["$half_day", 0.5, 1]}, 0]}},
                                    "avg_work_hours": {"$avg": {"$cond": [{"$and": [
                                        {"$in": ["$status", list(PRESENCE)]}, {"$ne": ["$work_hours", None]}]},
                                        "$work_hours", None]}},
                                    "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                                    "total_late_minutes": {"$sum": "$late_minutes"},
                                    "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                                    "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}}}}],
                     "as": "stats"}},
        {"$unwind": {"path": "$stats", "preserveNullAndEmptyArrays": True}},
        {"$project": {"_id": 0, "department": "$_id", "headcount": 1,
                      "present_days": {"$ifNull": ["$stats.present_days", 0]},
                      "avg_work_hours": {"$ifNull": ["$stats.avg_work_hours", None]},
                      "late_count": {"$ifNull": ["$stats.late_count", 0]},
                      "total_late_minutes": {"$ifNull": ["$stats.total_late_minutes", 0]},
                      "leave_count": {"$ifNull": ["$stats.leave_count", 0]},
                      "on_duty_count": {"$ifNull": ["$stats.on_duty_count", 0]}}},
        {"$sort": {"department": 1}}
    ]
    rows = list(db.employees.aggregate(pipeline))
    for row in rows:
        row["present_days"] = round_half_up(row["present_days"], 2)
        row["avg_work_hours"] = round_half_up(row["avg_work_hours"], 2)
    return {"month": month, "items": rows}

@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    start, end = parse_month(month)
    pipeline = [
        {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1, "_id": 1},
                              "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name",
                      "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}}
    ]
    return {"month": month, "items": list(db.attendance_logs.aggregate(pipeline))}

@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(..., alias="from"), to_date: str = Query(..., alias="to")):
    start, end = parse_date(from_date, "from"), parse_date(to_date, "to")
    if end < start: raise HTTPException(422, "to must be on or after from")
    if (end - start).days + 1 > 92: raise HTTPException(422, "range cannot exceed 92 days")
    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, "department not found")
    # MongoDB fills every date, including weekends and days with no logs.
    pipeline = [
        {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
        {"$match": {"employee.department": department}},
        {"$group": {"_id": "$date",
                    "present_count": {"$sum": {"$cond": [{"$in": ["$status", list(PRESENCE)]},
                                                          {"$cond": ["$half_day", 0.5, 1]}, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}}}},
        {"$project": {"date": {"$dateFromString": {"dateString": "$_id"}}, "present_count": 1, "late_count": 1}},
        {"$densify": {"field": "date", "range": {"step": 1, "unit": "day",
                      "bounds": [datetime.combine(start, time.min), datetime.combine(end, time.min)]}}},
        {"$set": {"date_string": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date"}},
                  "present_count": {"$ifNull": ["$present_count", 0]},
                  "late_count": {"$ifNull": ["$late_count", 0]}}},
        {"$lookup": {"from": "employees", "let": {"day": "$date_string"},
                     "pipeline": [{"$match": {"department": department}},
                                  {"$match": {"$expr": {"$lte": ["$joined_on", "$$day"]}}},
                                  {"$count": "n"}], "as": "hc"}},
        {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]},
                  "is_working_day": {"$and": [{"$gte": [{"$dayOfWeek": "$date"}, 2]},
                                               {"$lte": [{"$dayOfWeek": "$date"}, 6]}]}}},
        {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                                                {"$round": [{"$divide": ["$present_count", "$headcount"]}, 4]}, None]}}},
        {"$setWindowFields": {"sortBy": {"date": 1},
                              "output": {"moving_avg_7d": {"$avg": "$attendance_rate",
                                      "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": "$date_string", "is_working_day": 1, "headcount": 1,
                      "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1}},
        {"$sort": {"date": 1}}
    ]
    rows = list(db.attendance_logs.aggregate(pipeline))
    for row in rows:
        row["present_count"] = round_half_up(row.get("present_count", 0), 2)
        row["attendance_rate"] = round_half_up(row.get("attendance_rate"), 4)
        row["moving_avg_7d"] = round_half_up(row.get("moving_avg_7d"), 4)
    return {"department": department, "items": rows}

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: str, emp_code: Optional[str] = None, month: Optional[str] = None,
                     department: Optional[str] = None, limit: int = Query(10, ge=1, le=50),
                     date_from: Optional[str] = None, date_to: Optional[str] = None,
                     status: Optional[str] = None, page: int = Query(1, ge=1),
                     page_size: int = Query(20, ge=1, le=100),
                     from_date: Optional[str] = Query(None, alias="from"),
                     to_date: Optional[str] = Query(None, alias="to")):
    if endpoint == "attendance_list":
        query = {}
        if emp_code is not None: query["emp_code"] = emp_code
        if date_from is not None:
            parse_date(date_from, "date_from"); query.setdefault("date", {})["$gte"] = date_from
        if date_to is not None:
            parse_date(date_to, "date_to"); query.setdefault("date", {})["$lte"] = date_to
        if date_from and date_to and date_from > date_to: raise HTTPException(422, "date_from must be on or before date_to")
        if status:
            if status not in STATUSES: raise HTTPException(422, "invalid status")
            query["status"] = status
        explain = db.command("explain", {"find": "attendance_logs", "filter": query,
            "sort": {"date": -1, "emp_code": 1}, "skip": (page-1)*page_size, "limit": page_size},
            verbosity="executionStats")
        collection = "attendance_logs"
    elif endpoint == "employee_monthly":
        if not emp_code or not month: raise HTTPException(422, "emp_code and month are required")
        start, end = parse_month(month)
        pipeline = [{"$match": monthly_match(start, end, emp_code)}, {"$sort": {"date": 1}}]
        explain = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
                             verbosity="executionStats")
        collection = "attendance_logs"
    elif endpoint == "department_summary":
        if not month: raise HTTPException(422, "month is required")
        start, end = parse_month(month)
        query = {"joined_on": {"$lte": end.isoformat()}}
        if department is not None: query["department"] = department
        explain = db.command("explain", {"find": "employees", "filter": query,
                                         "sort": {"department": 1}}, verbosity="executionStats")
        collection = "employees"
    elif endpoint == "late_leaderboard":
        if not month: raise HTTPException(422, "month is required")
        start, end = parse_month(month)
        pipeline = [{"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}, "late_minutes": {"$gt": 0}}},
                    {"$sort": {"late_minutes": -1, "emp_code": 1}}]
        explain = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
                             verbosity="executionStats")
        collection = "attendance_logs"
    elif endpoint == "department_trend":
        if not department or not from_date or not to_date: raise HTTPException(422, "department, from and to are required")
        parse_date(from_date, "from"); parse_date(to_date, "to")
        if to_date < from_date or (parse_date(to_date)-parse_date(from_date)).days+1 > 92:
            raise HTTPException(422, "invalid trend date range")
        pipeline = [{"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
                    {"$sort": {"date": 1, "emp_code": 1}}]
        explain = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
                             verbosity="executionStats")
        collection = "attendance_logs"
    else:
        raise HTTPException(422, "unknown endpoint")
    return {"endpoint": endpoint, "collection": collection, "explain": explain}

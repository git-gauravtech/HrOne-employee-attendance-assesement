"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import calendar
import json
import os
import re
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, List, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

# Load .env but do NOT override real environment variables
load_dotenv(override=False)

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# --------------------------------------------------------------------------- #
# Startup: create indexes idempotently
# --------------------------------------------------------------------------- #
@app.on_event("startup")
def startup():
    """Create all required indexes idempotently at startup."""

    def _ensure_indexes(collection, index_specs):
        try:
            for keys, kwargs in index_specs:
                collection.create_index(keys, **kwargs)
        except Exception as exc:
            error_code = getattr(exc, "code", None)
            if error_code in (85, 86):
                collection.drop_indexes()
                for keys, kwargs in index_specs:
                    collection.create_index(keys, **kwargs)
            else:
                raise

    # --- employees collection ---
    _ensure_indexes(db.employees, [
        ("emp_code", {"unique": True}),
        ("department", {}),
        ("joined_on", {}),
        ([("department", ASCENDING), ("joined_on", ASCENDING)], {}),
    ])

    # --- attendance_logs collection ---
    _ensure_indexes(db.attendance_logs, [
        ([("emp_code", ASCENDING), ("date", ASCENDING)], {"unique": True}),
        ([("date", DESCENDING), ("emp_code", ASCENDING)], {}),
        ([("date", ASCENDING), ("late_minutes", ASCENDING)], {}),
        ([("emp_code", ASCENDING), ("punch_in", DESCENDING)], {}),
    ])


# --------------------------------------------------------------------------- #
# Helpers & Validation
# --------------------------------------------------------------------------- #
MIN_EPOCH_MS = 100_000_000_000      # anything below looks like seconds
MAX_EPOCH_MS = 4_102_444_800_000    # 2100-01-01 00:00:00 UTC

HH_MM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
DATE_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
ALLOWED_STATUSES = {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}
PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}


def validate_epoch_ms(value, field_name: str = "punched_at") -> int:
    """Validate that *value* is a proper epoch-millisecond integer."""
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise HTTPException(422, detail=f"{field_name} must be an integer (epoch milliseconds)")
    if value < MIN_EPOCH_MS:
        raise HTTPException(422, detail=f"{field_name} looks like seconds — must be epoch milliseconds (>= {MIN_EPOCH_MS})")
    if value > MAX_EPOCH_MS:
        raise HTTPException(422, detail=f"{field_name} is out of range (> {MAX_EPOCH_MS})")
    return value


def validate_calendar_date(date_str: str, field_name: str = "date") -> datetime:
    """Validate that date_str is a real calendar date YYYY-MM-DD."""
    if not isinstance(date_str, str) or not DATE_RE.match(date_str):
        raise HTTPException(422, detail=f"{field_name} must be YYYY-MM-DD")
    try:
        return datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(422, detail=f"{field_name} is not a valid calendar date")


def get_month_date_range(month_str: str, field_name: str = "month"):
    """Validate YYYY-MM and return start date string, end date string, and last day integer."""
    if not isinstance(month_str, str) or not MONTH_RE.match(month_str):
        raise HTTPException(422, detail=f"{field_name} must be YYYY-MM")
    try:
        year, mo = map(int, month_str.split("-"))
        num_days = calendar.monthrange(year, mo)[1]
        start_str = f"{month_str}-01"
        end_str = f"{month_str}-{num_days:02d}"
        return start_str, end_str, num_days
    except ValueError:
        raise HTTPException(422, detail=f"{field_name} is not a valid month")


def validate_pagination(page: int, page_size: int):
    """Validate pagination: page >= 1, 1 <= page_size <= 100."""
    if page < 1:
        raise HTTPException(422, detail="page must be >= 1")
    if page_size < 1 or page_size > 100:
        raise HTTPException(422, detail="page_size must be between 1 and 100")


def epoch_ms_to_datetime(ms: int) -> datetime:
    """Convert epoch milliseconds to a UTC-aware datetime, truncated to whole seconds (R1)."""
    ms_truncated = (ms // 1000) * 1000
    return datetime.fromtimestamp(ms_truncated / 1000, tz=timezone.utc)


def datetime_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert a datetime (or None) to epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def round_half_up_2(val: Optional[float]) -> Optional[float]:
    """Round to 2 decimal places half-up (R8)."""
    if val is None:
        return None
    return float(Decimal(str(val)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def round_half_up_4(val: Optional[float]) -> Optional[float]:
    """Round to 4 decimal places half-up (R8)."""
    if val is None:
        return None
    return float(Decimal(str(val)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def compute_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    Determine the IST attendance date for a punch-in (R1).
    Overnight shifts: if IST punch time is earlier than shift_end, belongs to previous day.
    """
    ist_time = punch_in_utc.astimezone(IST)
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))

    is_overnight = (eh, em) <= (sh, sm)
    if is_overnight:
        punch_ist_mins = ist_time.hour * 60 + ist_time.minute
        shift_end_mins = eh * 60 + em
        if punch_ist_mins < shift_end_mins:
            ist_time = ist_time - timedelta(days=1)

    return ist_time.strftime("%Y-%m-%d")


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, attendance_date: str) -> int:
    """
    R2: Grace period 10 minutes. Late only if strictly more than 10:00 after shift_start.
    When late, floor whole minutes elapsed since shift_start.
    """
    h, m = map(int, shift_start.split(":"))
    year, month, day = map(int, attendance_date.split("-"))
    shift_start_ist = datetime(year, month, day, h, m, 0, 0, tzinfo=IST)

    punch_ist = punch_in_utc.astimezone(IST)
    diff_seconds = (punch_ist - shift_start_ist).total_seconds()

    if diff_seconds <= 600:
        return 0
    return int(diff_seconds // 60)


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up."""
    seconds = (punch_out_utc - punch_in_utc).total_seconds()
    raw = Decimal(str(seconds)) / Decimal("3600")
    return float(raw.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out_utc: datetime, shift_end: str, attendance_date: str,
                     shift_start: str) -> int:
    """
    R3: Overtime = floor minutes between shift_end and punch_out, counted only when >= 30; else 0.
    """
    eh, em = map(int, shift_end.split(":"))
    sh, sm = map(int, shift_start.split(":"))
    year, month, day = map(int, attendance_date.split("-"))

    shift_end_ist = datetime(year, month, day, eh, em, 0, 0, tzinfo=IST)
    is_overnight = (eh, em) <= (sh, sm)
    if is_overnight:
        shift_end_ist += timedelta(days=1)

    punch_out_ist = punch_out_utc.astimezone(IST)
    diff_seconds = (punch_out_ist - shift_end_ist).total_seconds()

    if diff_seconds < 0:
        return 0

    minutes = int(diff_seconds // 60)
    return minutes if minutes >= 30 else 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: half_day = true when rounded work_hours < 4.50."""
    if work_hours is None:
        return False
    return work_hours < 4.50


def serialize_employee(doc: dict) -> dict:
    """Convert employee document to API response shape."""
    result = {}
    for key in ("emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on"):
        result[key] = doc.get(key)
    result["created_at"] = datetime_to_epoch_ms(doc.get("created_at"))
    return result


def serialize_history(history: list) -> list:
    """Convert BSON datetimes in history entries to epoch milliseconds."""
    result = []
    for entry in history:
        serialized = {
            "at": datetime_to_epoch_ms(entry.get("at")),
            "by": entry.get("by"),
            "reason": entry.get("reason"),
            "changes": {},
        }
        for field_name, change in entry.get("changes", {}).items():
            if field_name in ("punch_in", "punch_out"):
                serialized["changes"][field_name] = {
                    "from": datetime_to_epoch_ms(change.get("from")),
                    "to": datetime_to_epoch_ms(change.get("to")),
                }
            else:
                serialized["changes"][field_name] = change
        result.append(serialized)
    return result


def serialize_attendance(doc: dict) -> dict:
    """Convert attendance_logs document to API response shape (AttendanceRecord)."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": datetime_to_epoch_ms(doc.get("punch_in")),
        "punch_out": datetime_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": serialize_history(doc.get("history") or []),
    }


def clean_explain_doc(doc: dict) -> dict:
    """Convert raw BSON types (Timestamp, ObjectId) in explain results to JSON-serializable types."""
    return json.loads(json_util.dumps(doc))


# --------------------------------------------------------------------------- #
# Aggregation Pipeline Builders (Shared between endpoints and explain)
# --------------------------------------------------------------------------- #
def build_employee_monthly_pipeline(emp_code: str, start_date_str: str, end_date_str: str) -> list:
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_date_str, "$lte": end_date_str},
            }
        },
        {
            "$group": {
                "_id": None,
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {
                                        "$and": [
                                            {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 2]},
                                            {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]},
                                        ]
                                    },
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
            }
        },
    ]


def build_department_summary_pipeline(start_date_str: str, end_date_str: str, department: Optional[str] = None) -> list:
    match_emp = {"joined_on": {"$lte": end_date_str}}
    if department:
        match_emp["department"] = department

    return [
        {"$match": match_emp},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", start_date_str]},
                                    {"$lte": ["$date", end_date_str]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": {
                    "department": "$department",
                    "emp_code": "$emp_code",
                },
                "emp_present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, 2]},
                                    {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, 6]},
                                ]
                            },
                            {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "emp_work_hours_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            "$logs.work_hours",
                            0.0,
                        ]
                    }
                },
                "emp_work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "emp_late_count": {
                    "$sum": {"$cond": [{"$gt": ["$logs.late_minutes", 0]}, 1, 0]}
                },
                "emp_total_late_minutes": {
                    "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                },
                "emp_leave_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}
                },
                "emp_on_duty_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}
                },
            }
        },
        {
            "$group": {
                "_id": "$_id.department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$emp_present_days"},
                "total_work_hours": {"$sum": "$emp_work_hours_sum"},
                "total_work_hours_records": {"$sum": "$emp_work_hours_count"},
                "late_count": {"$sum": "$emp_late_count"},
                "total_late_minutes": {"$sum": "$emp_total_late_minutes"},
                "leave_count": {"$sum": "$emp_leave_count"},
                "on_duty_count": {"$sum": "$emp_on_duty_count"},
            }
        },
        {"$sort": {"_id": 1}},
    ]


def build_late_leaderboard_pipeline(start_date_str: str, end_date_str: str, limit: int = 10, department: Optional[str] = None) -> list:
    pipeline = [
        {
            "$match": {
                "date": {"$gte": start_date_str, "$lte": end_date_str},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                },
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ])
    return pipeline


def build_department_trend_pipeline(department: str, from_date: str, to_date: str, d_from: datetime, d_to_bound: datetime) -> list:
    return [
        {
            "$match": {
                "date": {"$gte": from_date, "$lte": to_date}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
            }
        },
        {
            "$project": {
                "date": "$_id",
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$unionWith": {
                "coll": "employees",
                "pipeline": [
                    {"$sort": {"emp_code": 1}},
                    {"$limit": 1},
                    {
                        "$project": {
                            "_id": 0,
                            "date": {"$literal": from_date},
                            "present_count": {"$literal": 0.0},
                            "late_count": {"$literal": 0},
                        }
                    },
                ],
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {"$sum": "$present_count"},
                "late_count": {"$sum": "$late_count"},
            }
        },
        {
            "$project": {
                "asDate": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$densify": {
                "field": "asDate",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [d_from, d_to_bound],
                },
            }
        },
        {
            "$project": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$asDate"}},
                "asDate": 1,
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "pipeline": [{"$match": {"department": department}}],
                "as": "dept_emps",
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": {
                    "$and": [
                        {"$gte": [{"$dayOfWeek": "$asDate"}, 2]},
                        {"$lte": [{"$dayOfWeek": "$asDate"}, 6]},
                    ]
                },
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": "$dept_emps",
                            "as": "e",
                            "cond": {"$lte": ["$$e.joined_on", "$date"]},
                        }
                    }
                },
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$eq": ["$is_working_day", True]},
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                },
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"]
                        },
                    }
                },
            }
        },
        {"$sort": {"date": 1}},
    ]


# --------------------------------------------------------------------------- #
# Request Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[object] = None
    status: str = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[object] = None


class RegularizeIn(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[object] = None
    punch_out: Optional[object] = None
    reason: str
    regularized_by: str


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    """Readiness probe: returns 200 only if MongoDB answers a ping."""
    try:
        db.command("ping")
    except Exception:
        raise HTTPException(503, detail="MongoDB is not available")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if not HH_MM_RE.match(body.shift_start):
        raise HTTPException(422, detail="shift_start must be HH:MM (24-hour)")
    if not HH_MM_RE.match(body.shift_end):
        raise HTTPException(422, detail="shift_end must be HH:MM (24-hour)")
    if body.shift_start == body.shift_end:
        raise HTTPException(422, detail="shift_start and shift_end must be different")

    validate_calendar_date(body.joined_on, "joined_on")

    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, detail="emp_code already exists")

    doc.pop("_id", None)
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = 1, page_size: int = 20):
    validate_pagination(page, page_size)

    q = {}
    if department:
        q["department"] = department

    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip(skip)
        .limit(page_size)
    )

    return {
        "items": [serialize_employee(item) for item in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, detail=f"Employee {body.emp_code} not found")

    if body.status not in PRESENCE_STATUSES:
        raise HTTPException(422, detail=f"status must be one of {', '.join(sorted(PRESENCE_STATUSES))}")

    if body.punched_at is not None:
        validated_ms = validate_epoch_ms(body.punched_at, "punched_at")
        ts = epoch_ms_to_datetime(validated_ms)
    else:
        now = datetime.now(timezone.utc)
        ts = now.replace(microsecond=0)

    attendance_date = compute_attendance_date(ts, emp["shift_start"], emp["shift_end"])
    late = compute_late_minutes(ts, emp["shift_start"], attendance_date)

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, detail="already punched in for this date")

    doc.pop("_id", None)
    return serialize_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, detail=f"Employee {body.emp_code} not found")

    if body.punched_at is not None:
        validated_ms = validate_epoch_ms(body.punched_at, "punched_at")
        punch_out_dt = epoch_ms_to_datetime(validated_ms)
    else:
        now = datetime.now(timezone.utc)
        punch_out_dt = now.replace(microsecond=0)

    # Contract: Closes the employee's most recent record whose punch_in is at or before punched_at
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": punch_out_dt}},
        sort=[("punch_in", DESCENDING)],
    )

    if not rec:
        # Check if an attendance record exists for this employee whose punch_in is later than punched_at
        future_rec = db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$gt": punch_out_dt}},
            sort=[("punch_in", ASCENDING)],
        )
        if future_rec:
            raise HTTPException(422, detail="punched_at must be strictly after punch_in")
        raise HTTPException(404, detail="No punch-in record found for punch-out")

    if rec.get("punch_out") is not None:
        raise HTTPException(409, detail="Record is already punched out")

    punch_in_dt = rec["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)

    if punch_out_dt <= punch_in_dt:
        raise HTTPException(422, detail="punched_at must be strictly after punch_in")

    if (punch_out_dt - punch_in_dt).total_seconds() > 24 * 3600:
        raise HTTPException(422, detail="punched_at cannot be more than 24 hours after punch_in")

    work_hours = compute_work_hours(punch_in_dt, punch_out_dt)
    overtime = compute_overtime(punch_out_dt, emp["shift_end"], rec["date"], emp["shift_start"])
    half_day = compute_half_day(work_hours)

    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hours,
                "overtime_minutes": overtime,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(409, detail="Record was already punched out concurrently")

    return serialize_attendance(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    validate_pagination(page, page_size)

    if status is not None and status not in ALLOWED_STATUSES:
        raise HTTPException(422, detail=f"status must be one of {', '.join(sorted(ALLOWED_STATUSES))}")

    if date_from:
        validate_calendar_date(date_from, "date_from")
    if date_to:
        validate_calendar_date(date_to, "date_to")

    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, detail="date_from must be <= date_to")

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )

    items = [serialize_attendance(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    validate_calendar_date(date, "date")
    if not body.reason or len(body.reason) < 5 or len(body.reason) > 200:
        raise HTTPException(422, detail="reason must be between 5 and 200 characters")
    if not body.regularized_by or len(body.regularized_by) < 1 or len(body.regularized_by) > 50:
        raise HTTPException(422, detail="regularized_by must be between 1 and 50 characters")

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(404, detail="Attendance record not found")

    old_status = rec["status"]
    old_pin = rec.get("punch_in")
    if old_pin and old_pin.tzinfo is None:
        old_pin = old_pin.replace(tzinfo=timezone.utc)
    old_pout = rec.get("punch_out")
    if old_pout and old_pout.tzinfo is None:
        old_pout = old_pout.replace(tzinfo=timezone.utc)

    old_work_hours = rec.get("work_hours")
    old_late = rec.get("late_minutes", 0)
    old_overtime = rec.get("overtime_minutes", 0)
    old_half_day = rec.get("half_day", False)

    new_status = body.status if body.status is not None else old_status
    if new_status not in ALLOWED_STATUSES:
        raise HTTPException(422, detail=f"status must be one of {', '.join(sorted(ALLOWED_STATUSES))}")

    # Determine punch_in
    if body.punch_in is not None:
        pin_ms = validate_epoch_ms(body.punch_in, "punch_in")
        new_pin = epoch_ms_to_datetime(pin_ms)
    else:
        new_pin = old_pin

    # Determine punch_out
    if body.punch_out is not None:
        pout_ms = validate_epoch_ms(body.punch_out, "punch_out")
        new_pout = epoch_ms_to_datetime(pout_ms)
    else:
        new_pout = old_pout

    # Status consistency rules
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, detail="punch_in and punch_out cannot be supplied when setting status to ABSENT or LEAVE")
        new_pin = None
        new_pout = None
        new_work_hours = None
        new_late = 0
        new_overtime = 0
        new_half_day = False
    else:
        if new_pin is None:
            raise HTTPException(422, detail="A presence status requires a punch_in")

        computed_date = compute_attendance_date(new_pin, emp["shift_start"], emp["shift_end"])
        if computed_date != date:
            raise HTTPException(422, detail=f"punch_in belongs to date {computed_date}, not {date}")

        if new_pout is not None:
            if new_pout <= new_pin:
                raise HTTPException(422, detail="punch_out must be after punch_in")
            if (new_pout - new_pin).total_seconds() > 24 * 3600:
                raise HTTPException(422, detail="punch_out must be within 24 hours of punch_in")
            new_work_hours = compute_work_hours(new_pin, new_pout)
            new_overtime = compute_overtime(new_pout, emp["shift_end"], date, emp["shift_start"])
            new_half_day = compute_half_day(new_work_hours)
        else:
            new_work_hours = None
            new_overtime = 0
            new_half_day = False

        new_late = compute_late_minutes(new_pin, emp["shift_start"], date)

    # Calculate actual changes
    changes = {}
    if old_status != new_status:
        changes["status"] = {"from": old_status, "to": new_status}

    if datetime_to_epoch_ms(old_pin) != datetime_to_epoch_ms(new_pin):
        changes["punch_in"] = {"from": old_pin, "to": new_pin}

    if datetime_to_epoch_ms(old_pout) != datetime_to_epoch_ms(new_pout):
        changes["punch_out"] = {"from": old_pout, "to": new_pout}

    if old_work_hours != new_work_hours:
        changes["work_hours"] = {"from": old_work_hours, "to": new_work_hours}

    if old_late != new_late:
        changes["late_minutes"] = {"from": old_late, "to": new_late}

    if old_overtime != new_overtime:
        changes["overtime_minutes"] = {"from": old_overtime, "to": new_overtime}

    if old_half_day != new_half_day:
        changes["half_day"] = {"from": old_half_day, "to": new_half_day}

    if not changes:
        raise HTTPException(422, detail="Request changed nothing")

    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    audit_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    history_len = len(rec.get("history") or [])
    if history_len == 0:
        filter_query = {
            "emp_code": emp_code,
            "date": date,
            "$or": [
                {"history": {"$exists": False}},
                {"history": None},
                {"history": {"$size": 0}},
            ],
        }
    else:
        filter_query = {
            "emp_code": emp_code,
            "date": date,
            "history": {"$size": history_len},
        }

    updated = db.attendance_logs.find_one_and_update(
        filter_query,
        {
            "$set": {
                "status": new_status,
                "punch_in": new_pin,
                "punch_out": new_pout,
                "work_hours": new_work_hours,
                "late_minutes": new_late,
                "overtime_minutes": new_overtime,
                "half_day": new_half_day,
            },
            "$push": {"history": audit_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(409, detail="Concurrent regularization conflict")

    return serialize_attendance(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    start_date_str, end_date_str, _ = get_month_date_range(month)

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, detail=f"Employee {emp_code} not found")

    # Working days per R7
    joined_on = emp["joined_on"]
    eff_start = max(start_date_str, joined_on)
    working_days = 0
    if eff_start <= end_date_str:
        cur = datetime.strptime(eff_start, "%Y-%m-%d")
        end_dt = datetime.strptime(end_date_str, "%Y-%m-%d")
        while cur <= end_dt:
            if cur.weekday() < 5:
                working_days += 1
            cur += timedelta(days=1)

    pipeline = build_employee_monthly_pipeline(emp_code, start_date_str, end_date_str)
    agg = list(db.attendance_logs.aggregate(pipeline))
    if agg:
        data = agg[0]
        present_days = float(data.get("present_days", 0.0))
        leave_days = int(data.get("leave_days", 0))
        late_count = int(data.get("late_count", 0))
        total_late_minutes = int(data.get("total_late_minutes", 0))
        total_overtime_minutes = int(data.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    attendance_pct = None
    if working_days > 0:
        raw_pct = Decimal(str(present_days)) / Decimal(str(working_days)) * Decimal("100")
        attendance_pct = float(raw_pct.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up_2(present_days),
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    start_date_str, end_date_str, _ = get_month_date_range(month)

    pipeline = build_department_summary_pipeline(start_date_str, end_date_str, department)
    rows = list(db.employees.aggregate(pipeline))
    items = []
    for r in rows:
        total_wh_records = r.get("total_work_hours_records", 0)
        avg_wh = None
        if total_wh_records > 0:
            avg_wh = round_half_up_2(r.get("total_work_hours", 0.0) / total_wh_records)

        items.append({
            "department": r["_id"],
            "headcount": int(r["headcount"]),
            "present_days": round_half_up_2(r.get("present_days", 0.0)),
            "avg_work_hours": avg_wh,
            "late_count": int(r.get("late_count", 0)),
            "total_late_minutes": int(r.get("total_late_minutes", 0)),
            "leave_count": int(r.get("leave_count", 0)),
            "on_duty_count": int(r.get("on_duty_count", 0)),
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = 10, department: Optional[str] = None):
    start_date_str, end_date_str, _ = get_month_date_range(month)
    if limit < 1 or limit > 50:
        raise HTTPException(422, detail="limit must be between 1 and 50")

    pipeline = build_late_leaderboard_pipeline(start_date_str, end_date_str, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    d_from = validate_calendar_date(from_date, "from")
    d_to = validate_calendar_date(to_date, "to")

    dept_count = db.employees.count_documents({"department": department})
    if dept_count == 0:
        raise HTTPException(404, detail="Department not found")

    if d_to < d_from:
        raise HTTPException(422, detail="from must be <= to")
    if (d_to - d_from).days > 92:
        raise HTTPException(422, detail="Date range cannot exceed 92 days")

    d_to_bound = d_to + timedelta(days=1)
    pipeline = build_department_trend_pipeline(department, from_date, to_date, d_from, d_to_bound)
    rows = list(db.attendance_logs.aggregate(pipeline))
    items = []
    for r in rows:
        rate = r.get("attendance_rate")
        m_avg = r.get("moving_avg_7d")
        items.append({
            "date": r["date"],
            "is_working_day": bool(r["is_working_day"]),
            "headcount": int(r["headcount"]),
            "present_count": round_half_up_2(r.get("present_count", 0.0)),
            "late_count": int(r.get("late_count", 0)),
            "attendance_rate": round_half_up_4(rate),
            "moving_avg_7d": round_half_up_4(m_avg),
        })

    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = 1,
    page_size: int = 20,
):
    valid_endpoints = {
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    }
    if endpoint not in valid_endpoints:
        raise HTTPException(422, detail=f"endpoint must be one of {sorted(valid_endpoints)}")

    if endpoint == "attendance_list":
        validate_pagination(page, page_size)
        if status is not None and status not in ALLOWED_STATUSES:
            raise HTTPException(422, detail=f"status must be one of {', '.join(sorted(ALLOWED_STATUSES))}")
        if date_from:
            validate_calendar_date(date_from, "date_from")
        if date_to:
            validate_calendar_date(date_to, "date_to")
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, detail="date_from must be <= date_to")

        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status

        skip = (page - 1) * page_size
        explain_res = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": DESCENDING, "emp_code": ASCENDING},
                "skip": skip,
                "limit": page_size,
            },
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": clean_explain_doc(explain_res),
        }

    elif endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(422, detail="emp_code is required for employee_monthly")
        if not month:
            raise HTTPException(422, detail="month is required for employee_monthly")
        start_date_str, end_date_str, _ = get_month_date_range(month)

        pipe = build_employee_monthly_pipeline(emp_code, start_date_str, end_date_str)
        explain_res = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipe, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": clean_explain_doc(explain_res),
        }

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, detail="month is required for department_summary")
        start_date_str, end_date_str, _ = get_month_date_range(month)

        pipe = build_department_summary_pipeline(start_date_str, end_date_str, department)
        explain_res = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipe, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": clean_explain_doc(explain_res),
        }

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, detail="month is required for late_leaderboard")
        if limit < 1 or limit > 50:
            raise HTTPException(422, detail="limit must be between 1 and 50")
        start_date_str, end_date_str, _ = get_month_date_range(month)

        pipe = build_late_leaderboard_pipeline(start_date_str, end_date_str, limit, department)
        explain_res = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipe, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": clean_explain_doc(explain_res),
        }

    elif endpoint == "department_trend":
        if not department:
            raise HTTPException(422, detail="department is required for department_trend")
        if not from_date or not to_date:
            raise HTTPException(422, detail="from and to dates are required for department_trend")
        d_from = validate_calendar_date(from_date, "from")
        d_to = validate_calendar_date(to_date, "to")
        if d_to < d_from:
            raise HTTPException(422, detail="from must be <= to")
        if (d_to - d_from).days > 92:
            raise HTTPException(422, detail="Date range cannot exceed 92 days")

        d_to_bound = d_to + timedelta(days=1)
        pipe = build_department_trend_pipeline(department, from_date, to_date, d_from, d_to_bound)
        explain_res = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipe, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": clean_explain_doc(explain_res),
        }

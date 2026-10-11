"""
Employee Attendance & Analytics API

FastAPI + MongoDB backend implementing attendance punch-in, punch-out,
regularization with audit trail, and MongoDB aggregation analytics.
"""
from contextlib import asynccontextmanager
from datetime import date as dt_date, datetime, time as dt_time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from http import HTTPStatus
import os
import re
from typing import Any, Dict, List, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
import pymongo
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

# Load environment variables (.env is fallback, real env takes precedence)
load_dotenv()

IST = timezone(timedelta(hours=5, minutes=30))
EPOCH_MIN = 100000000000
EPOCH_MAX = 4102444800000

# --------------------------------------------------------------------------- #
# Database Connection & Lifecycle
# --------------------------------------------------------------------------- #
client: Optional[MongoClient] = None
db: Any = None


def get_db():
    global client, db
    if db is None:
        mongo_uri = os.getenv("MONGO_URI") or os.getenv("MONGODB_URI") or "mongodb://localhost:27017"
        mongo_db = os.getenv("MONGO_DB") or os.getenv("DATABASE_NAME") or "attendance_db"
        client = MongoClient(mongo_uri, tz_aware=True, serverSelectionTimeoutMS=5000)
        db = client[mongo_db]
    return db


def create_indexes():
    database = get_db()
    # employees indexes
    database.employees.create_index([("emp_code", 1)], unique=True)
    database.employees.create_index([("department", 1), ("joined_on", 1)])
    database.employees.create_index([("joined_on", 1)])

    # attendance_logs indexes
    database.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    database.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    database.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    database.attendance_logs.create_index([("date", 1), ("emp_code", 1)])
    database.attendance_logs.create_index([("date", 1), ("late_minutes", 1)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: ensure indexes are created idempotently
    try:
        create_indexes()
    except Exception:
        # If DB is temporarily unavailable at startup, don't crash app import
        pass
    yield
    # Shutdown
    global client
    if client:
        client.close()


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan
)


# --------------------------------------------------------------------------- #
# Precision Rounding & Date/Time Helpers
# --------------------------------------------------------------------------- #
def round_half_up(val: Optional[float], decimals: int = 2) -> Optional[float]:
    if val is None:
        return None
    d = Decimal(str(val))
    target = Decimal("10") ** -decimals
    return float(d.quantize(target, rounding=ROUND_HALF_UP))


def to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    # Truncate to whole seconds
    sec = int(dt.timestamp())
    return sec * 1000


def from_epoch_ms(ms: int) -> datetime:
    sec = ms // 1000
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def truncate_datetime(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    sec = int(dt.timestamp())
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def parse_hh_mm(s: str) -> dt_time:
    h, m = map(int, s.split(":"))
    return dt_time(hour=h, minute=m)


def get_attendance_date(punch_in_dt: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Shift times and attendance dates are IST (UTC+05:30).
    A record's date is the IST calendar date of its punch_in.
    Overnight shifts (shift_end <= shift_start): punch-in earlier than shift_end
    belongs to the shift that started the previous day.
    """
    punch_in_ist = punch_in_dt.astimezone(IST)
    ist_calendar_date = punch_in_ist.date()
    end_time = parse_hh_mm(shift_end)
    start_time = parse_hh_mm(shift_start)

    is_overnight = end_time <= start_time
    if is_overnight and punch_in_ist.time() < end_time:
        att_date = ist_calendar_date - timedelta(days=1)
    else:
        att_date = ist_calendar_date
    return att_date.isoformat()


def compute_late_minutes(punch_in_dt: datetime, shift_start: str, attendance_date_str: str) -> int:
    """
    R2: Grace period 10 minutes.
    Late only if punch-in is strictly more than 10:00 (600s) after shift_start.
    Then late_minutes = whole minutes (floor) since shift_start, not since grace period ended.
    """
    y, m, d = map(int, attendance_date_str.split("-"))
    sh_h, sh_m = map(int, shift_start.split(":"))
    shift_start_dt = datetime(y, m, d, sh_h, sh_m, 0, tzinfo=IST)
    diff_sec = (punch_in_dt - shift_start_dt).total_seconds()
    if diff_sec > 600:
        return int(diff_sec // 60)
    return 0


def compute_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    diff_sec = (punch_out_dt - punch_in_dt).total_seconds()
    d_sec = Decimal(int(diff_sec))
    hours = d_sec / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out_dt: datetime, shift_start: str, shift_end: str, attendance_date_str: str) -> int:
    """
    R3: Whole minutes (floor) from shift_end to punch_out, counted only if >= 30; otherwise 0.
    For overnight shifts, shift_end is on attendance_date + 1 day in IST.
    """
    y, m, d = map(int, attendance_date_str.split("-"))
    end_h, end_m = map(int, shift_end.split(":"))
    start_time = parse_hh_mm(shift_start)
    end_time = parse_hh_mm(shift_end)

    base_date = dt_date(y, m, d)
    if end_time <= start_time:
        base_date = base_date + timedelta(days=1)

    shift_end_dt = datetime(base_date.year, base_date.month, base_date.day, end_h, end_m, 0, tzinfo=IST)
    diff_sec = (punch_out_dt - shift_end_dt).total_seconds()
    if diff_sec >= 1800:
        return int(diff_sec // 60)
    return 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """
    R5: After punch-out, half_day is true when rounded work_hours < 4.50.
    """
    if work_hours is not None and work_hours < 4.50:
        return True
    return False


def get_month_working_days(joined_on_str: str, month_str: str) -> int:
    """
    R7: Monday - Friday, no holiday calendar; counted from joined_on for mid-month joiners.
    """
    year, month = map(int, month_str.split("-"))
    joined_date = dt_date.fromisoformat(joined_on_str)
    # Month start and end
    month_start = dt_date(year, month, 1)
    if month == 12:
        month_end = dt_date(year + 1, 1, 1) - timedelta(days=1)
    else:
        month_end = dt_date(year, month + 1, 1) - timedelta(days=1)

    effective_start = max(month_start, joined_date)
    if effective_start > month_end:
        return 0

    count = 0
    cur = effective_start
    while cur <= month_end:
        if cur.weekday() < 5:  # Mon-Fri
            count += 1
        cur += timedelta(days=1)
    return count


def serialize_doc_record(doc: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(doc)
    out.pop("_id", None)
    out.pop("id", None)

    # Convert punch_in and punch_out to epoch ms
    if "punch_in" in out and isinstance(out["punch_in"], datetime):
        out["punch_in"] = to_epoch_ms(out["punch_in"])
    if "punch_out" in out and isinstance(out["punch_out"], datetime):
        out["punch_out"] = to_epoch_ms(out["punch_out"])

    # Ensure defaults
    if "history" not in out or out["history"] is None:
        out["history"] = []
    else:
        hist_out = []
        for h in out["history"]:
            h_entry = dict(h)
            if "at" in h_entry and isinstance(h_entry["at"], datetime):
                h_entry["at"] = to_epoch_ms(h_entry["at"])
            # In history.changes, punch_in / punch_out from/to are epoch ms
            if "changes" in h_entry and isinstance(h_entry["changes"], dict):
                c_out = {}
                for k, v in h_entry["changes"].items():
                    c_item = dict(v)
                    if k in ("punch_in", "punch_out"):
                        if isinstance(c_item.get("from"), datetime):
                            c_item["from"] = to_epoch_ms(c_item["from"])
                        if isinstance(c_item.get("to"), datetime):
                            c_item["to"] = to_epoch_ms(c_item["to"])
                    c_out[k] = c_item
                h_entry["changes"] = c_out
            hist_out.append(h_entry)
        out["history"] = hist_out

    if "half_day" not in out or out["half_day"] is None:
        out["half_day"] = False
    if "late_minutes" not in out or out["late_minutes"] is None:
        out["late_minutes"] = 0
    if "overtime_minutes" not in out or out["overtime_minutes"] is None:
        out["overtime_minutes"] = 0

    return out


def serialize_employee(doc: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(doc)
    out.pop("_id", None)
    if "created_at" in out and isinstance(out["created_at"], datetime):
        out["created_at"] = to_epoch_ms(out["created_at"])
    return out


# --------------------------------------------------------------------------- #
# Pydantic Schemas
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v: str) -> str:
        try:
            dt_date.fromisoformat(v)
        except ValueError:
            raise ValueError("joined_on must be YYYY-MM-DD")
        return v

    @model_validator(mode="after")
    def validate_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=EPOCH_MIN, le=EPOCH_MAX)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=EPOCH_MIN, le=EPOCH_MAX)


class RegularizeRequest(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[int] = Field(None, ge=EPOCH_MIN, le=EPOCH_MAX)
    punch_out: Optional[int] = Field(None, ge=EPOCH_MIN, le=EPOCH_MAX)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

@app.get("/health", tags=["system"])
def health():
    """
    Returns 200 only if MongoDB answers a ping, otherwise 503.
    """
    database = get_db()
    try:
        database.client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Database unavailable"
        )


@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeCreate):
    """
    Create an employee. Enforces uniqueness via MongoDB unique index on emp_code.
    """
    database = get_db()
    now_utc = truncate_datetime(datetime.now(timezone.utc))
    doc = body.model_dump()
    doc["created_at"] = now_utc

    try:
        database.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")

    return serialize_employee(doc)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    """
    List employees sorted by emp_code ascending. Total reflects applied filters.
    """
    database = get_db()
    q = {}
    if department:
        q["department"] = department

    total = database.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = database.employees.find(q).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [serialize_employee(d) for d in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size
    }


@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchInRequest):
    """
    Records punch-in. Date follows R1, late_minutes follows R2.
    Race-safe: exactly one 201 and rest 409 for simultaneous punch-ins on same date.
    """
    database = get_db()
    emp = database.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_in_dt = from_epoch_ms(body.punched_at)
    else:
        punch_in_dt = truncate_datetime(datetime.now(timezone.utc))

    att_date = get_attendance_date(punch_in_dt, emp["shift_start"], emp["shift_end"])
    late_mins = compute_late_minutes(punch_in_dt, emp["shift_start"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_mins,
        "overtime_minutes": 0,
        "half_day": False,
        "history": []
    }

    try:
        database.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="already punched in for this date"
        )

    return serialize_doc_record(doc)


@app.post("/attendance/punch-out", status_code=200, tags=["attendance"])
def punch_out(body: PunchOutRequest):
    """
    Closes employee's most recent record whose punch_in <= punched_at.
    Computes work_hours (R4), overtime_minutes (R3) and half_day (R5).
    Two simultaneous punch-outs yield exactly one 200 and the other 409.
    """
    database = get_db()
    emp = database.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_out_dt = from_epoch_ms(body.punched_at)
    else:
        punch_out_dt = truncate_datetime(datetime.now(timezone.utc))

    # Find the target record to punch out:
    # First priority: open record for this employee
    candidate = database.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_out": None, "punch_in": {"$ne": None}},
        sort=[("punch_in", -1)]
    )

    if not candidate:
        # Check most recent record in general
        candidate = database.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$ne": None}},
            sort=[("punch_in", -1)]
        )

    if not candidate:
        raise HTTPException(
            status_code=404,
            detail="No punch-in found"
        )

    punch_in_dt = candidate["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)

    if punch_out_dt <= punch_in_dt:
        raise HTTPException(
            status_code=422,
            detail="punch_out must be after punch_in"
        )

    if punch_out_dt > punch_in_dt + timedelta(hours=24):
        raise HTTPException(
            status_code=422,
            detail="punch_out cannot be more than 24 hours after punch_in"
        )

    if candidate.get("punch_out") is not None:
        raise HTTPException(
            status_code=409,
            detail="already punched out"
        )

    work_hrs = compute_work_hours(punch_in_dt, punch_out_dt)
    ot_mins = compute_overtime(punch_out_dt, emp["shift_start"], emp["shift_end"], candidate["date"])
    half_d = compute_half_day(work_hrs)

    # Race-safe update: update only if punch_out is still None
    updated = database.attendance_logs.find_one_and_update(
        {
            "_id": candidate["_id"],
            "punch_out": None
        },
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hrs,
                "overtime_minutes": ot_mins,
                "half_day": half_d
            }
        },
        return_document=pymongo.ReturnDocument.AFTER
    )

    if not updated:
        raise HTTPException(
            status_code=409,
            detail="already punched out"
        )

    return serialize_doc_record(updated)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    """
    List attendance records sorted by date descending, then emp_code ascending.
    """
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=422,
            detail="date_from must be <= date_to"
        )

    database = get_db()
    q: Dict[str, Any] = {}
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

    total = database.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = database.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)]).skip(skip).limit(page_size)
    items = [serialize_doc_record(d) for d in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size
    }


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeRequest = ...
):
    """
    Regularize (manually correct) one day's record.
    Recompute derived fields and append one entry to history.
    """
    database = get_db()
    emp = database.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    existing = database.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not existing:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    target_status = body.status if body.status is not None else existing.get("status")

    # Rule: setting status ABSENT or LEAVE clears both punch times, supplying punch_in or punch_out with it is 422
    if target_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=422,
                detail="punch times cannot be supplied for ABSENT or LEAVE"
            )
        new_punch_in = None
        new_punch_out = None
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        # Presence status
        if body.punch_in is not None:
            new_punch_in = from_epoch_ms(body.punch_in)
            # punch_in must stay on the record's attendance date (R1)
            derived_date = get_attendance_date(new_punch_in, emp["shift_start"], emp["shift_end"])
            if derived_date != date:
                raise HTTPException(
                    status_code=422,
                    detail="punch_in does not match attendance date"
                )
        else:
            new_punch_in = existing.get("punch_in")

        if new_punch_in is None:
            raise HTTPException(
                status_code=422,
                detail="presence status requires punch_in"
            )

        if new_punch_in.tzinfo is None:
            new_punch_in = new_punch_in.replace(tzinfo=timezone.utc)

        new_late_minutes = compute_late_minutes(new_punch_in, emp["shift_start"], date)

        if body.punch_out is not None:
            new_punch_out = from_epoch_ms(body.punch_out)
        else:
            new_punch_out = existing.get("punch_out")

        if new_punch_out is not None:
            if new_punch_out.tzinfo is None:
                new_punch_out = new_punch_out.replace(tzinfo=timezone.utc)

            if new_punch_out <= new_punch_in:
                raise HTTPException(
                    status_code=422,
                    detail="punch_out must be after punch_in"
                )
            if new_punch_out > new_punch_in + timedelta(hours=24):
                raise HTTPException(
                    status_code=422,
                    detail="punch_out cannot be more than 24 hours after punch_in"
                )
            new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
            new_overtime_minutes = compute_overtime(new_punch_out, emp["shift_start"], emp["shift_end"], date)
            new_half_day = compute_half_day(new_work_hours)
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    # Determine what actually changed
    old_punch_in = existing.get("punch_in")
    old_punch_out = existing.get("punch_out")
    if old_punch_in is not None and old_punch_in.tzinfo is None:
        old_punch_in = old_punch_in.replace(tzinfo=timezone.utc)
    if old_punch_out is not None and old_punch_out.tzinfo is None:
        old_punch_out = old_punch_out.replace(tzinfo=timezone.utc)

    changes_db: Dict[str, Dict[str, Any]] = {}

    if existing.get("status") != target_status:
        changes_db["status"] = {"from": existing.get("status"), "to": target_status}
    if old_punch_in != new_punch_in:
        changes_db["punch_in"] = {"from": old_punch_in, "to": new_punch_in}
    if old_punch_out != new_punch_out:
        changes_db["punch_out"] = {"from": old_punch_out, "to": new_punch_out}
    if existing.get("work_hours") != new_work_hours:
        changes_db["work_hours"] = {"from": existing.get("work_hours"), "to": new_work_hours}
    if existing.get("late_minutes", 0) != new_late_minutes:
        changes_db["late_minutes"] = {"from": existing.get("late_minutes", 0), "to": new_late_minutes}
    if existing.get("overtime_minutes", 0) != new_overtime_minutes:
        changes_db["overtime_minutes"] = {"from": existing.get("overtime_minutes", 0), "to": new_overtime_minutes}
    if existing.get("half_day", False) != new_half_day:
        changes_db["half_day"] = {"from": existing.get("half_day", False), "to": new_half_day}

    if not changes_db:
        raise HTTPException(
            status_code=422,
            detail="request changes nothing"
        )

    hist_entry = {
        "at": truncate_datetime(datetime.now(timezone.utc)),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes_db
    }

    # Race-safe update to avoid losing history
    current_hist = existing.get("history", [])
    updated = database.attendance_logs.find_one_and_update(
        {
            "_id": existing["_id"],
            "history": current_hist
        },
        {
            "$set": {
                "status": target_status,
                "punch_in": new_punch_in,
                "punch_out": new_punch_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day
            },
            "$push": {
                "history": hist_entry
            }
        },
        return_document=pymongo.ReturnDocument.AFTER
    )

    if not updated:
        raise HTTPException(
            status_code=409,
            detail="Concurrent modification detected"
        )

    return serialize_doc_record(updated)


# --------------------------------------------------------------------------- #
# Analytics Endpoints (MongoDB Aggregations)
# --------------------------------------------------------------------------- #

@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
):
    """
    Monthly attendance summary for one employee backed by a MongoDB aggregation.
    """
    database = get_db()
    emp = database.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    working_days = get_month_working_days(emp["joined_on"], month)
    m_start = f"{month}-01"
    m_end = f"{month}-31"

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": m_start, "$lte": m_end}
            }
        },
        {
            "$project": {
                "status": 1,
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "half_day": {"$ifNull": ["$half_day", False]},
                "dow": {
                    "$dayOfWeek": {
                        "$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}
                    }
                }
            }
        },
        {
            "$project": {
                "is_weekday": {"$in": ["$dow", [2, 3, 4, 5, 6]]},
                "is_present_status": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                "is_leave": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]},
                "late_minutes": 1,
                "overtime_minutes": 1,
                "half_day": 1
            }
        },
        {
            "$project": {
                "present_weight": {
                    "$cond": [
                        {"$and": ["$is_weekday", "$is_present_status"]},
                        {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                        0.0
                    ]
                },
                "is_leave": 1,
                "is_late": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]},
                "late_minutes": 1,
                "overtime_minutes": 1
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {"$sum": "$present_weight"},
                "leave_days": {"$sum": "$is_leave"},
                "late_count": {"$sum": "$is_late"},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"}
            }
        }
    ]

    res = list(database.attendance_logs.aggregate(pipeline))
    if res:
        r = res[0]
        present_days = float(r["present_days"])
        leave_days = int(r["leave_days"])
        late_count = int(r["late_count"])
        total_late_minutes = int(r["total_late_minutes"])
        total_overtime_minutes = int(r["total_overtime_minutes"])
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    if working_days > 0:
        attendance_pct = round_half_up((present_days / working_days) * 100, 2)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct
    }


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None
):
    """
    Department summary for a month via a cross-collection MongoDB aggregation.
    Headcount per R9 (includes people with 0 logs, excludes people joined later).
    """
    database = get_db()
    m_start = f"{month}-01"
    m_end = f"{month}-31"

    match_stage: Dict[str, Any] = {"joined_on": {"$lte": m_end}}
    if department:
        match_stage["department"] = department

    pipeline = [
        {"$match": match_stage},
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
                                    {"$gte": ["$date", m_start]},
                                    {"$lte": ["$date", m_end]}
                                ]
                            }
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "logs": {"$push": "$logs"}
            }
        },
        {
            "$project": {
                "department": "$_id",
                "headcount": 1,
                "flat_logs": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": [],
                        "in": {"$concatArrays": ["$$value", "$$this"]}
                    }
                }
            }
        },
        {
            "$unwind": {
                "path": "$flat_logs",
                "preserveNullAndEmptyArrays": True
            }
        },
        {
            "$project": {
                "department": 1,
                "headcount": 1,
                "log": "$flat_logs",
                "is_weekday": {
                    "$cond": [
                        {"$ifNull": ["$flat_logs", False]},
                        {
                            "$in": [
                                {
                                    "$dayOfWeek": {
                                        "$dateFromString": {
                                            "dateString": "$flat_logs.date",
                                            "format": "%Y-%m-%d"
                                        }
                                    }
                                },
                                [2, 3, 4, 5, 6]
                            ]
                        },
                        False
                    ]
                },
                "is_present_status": {
                    "$in": ["$flat_logs.status", ["PRESENT", "WFH", "ON_DUTY"]]
                }
            }
        },
        {
            "$project": {
                "department": 1,
                "headcount": 1,
                "log": 1,
                "present_weight": {
                    "$cond": [
                        {"$and": ["$is_weekday", "$is_present_status"]},
                        {"$cond": [{"$eq": ["$log.half_day", True]}, 0.5, 1.0]},
                        0.0
                    ]
                },
                "is_present_with_hours": {
                    "$and": [
                        "$is_present_status",
                        {"$ne": ["$log.work_hours", None]}
                    ]
                },
                "is_late": {"$cond": [{"$gt": ["$log.late_minutes", 0]}, 1, 0]},
                "is_leave": {"$cond": [{"$eq": ["$log.status", "LEAVE"]}, 1, 0]},
                "is_on_duty": {"$cond": [{"$eq": ["$log.status", "ON_DUTY"]}, 1, 0]}
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$first": "$headcount"},
                "present_days": {"$sum": "$present_weight"},
                "work_hours_list": {
                    "$push": {
                        "$cond": ["$is_present_with_hours", "$log.work_hours", "$$REMOVE"]
                    }
                },
                "late_count": {"$sum": "$is_late"},
                "total_late_minutes": {"$sum": {"$ifNull": ["$log.late_minutes", 0]}},
                "leave_count": {"$sum": "$is_leave"},
                "on_duty_count": {"$sum": "$is_on_duty"}
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": {"$round": ["$present_days", 2]},
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": [{"$size": "$work_hours_list"}, 0]},
                        {"$round": [{"$avg": "$work_hours_list"}, 2]},
                        None
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1
            }
        },
        {"$sort": {"department": 1}}
    ]

    items = list(database.employees.aggregate(pipeline))
    return {
        "month": month,
        "items": items
    }


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None
):
    """
    Ranks employees by total late_minutes in the month (only total > 0).
    Standard competition ranking (1, 2, 2, 4) using $setWindowFields and $rank.
    Limit applies after ranking (returns rows with rank <= limit).
    """
    database = get_db()
    m_start = f"{month}-01"
    m_end = f"{month}-31"

    pipeline: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": m_start, "$lte": m_end},
                "late_minutes": {"$gt": 0}
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee"
            }
        },
        {"$unwind": "$employee"}
    ]

    if department:
        pipeline.append({"$match": {"employee.department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                }
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": 1,
                "late_count": 1
            }
        }
    ])

    items = list(database.attendance_logs.aggregate(pipeline))
    return {
        "month": month,
        "items": items
    }


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$")
):
    """
    Daily attendance trend for a department with gap filling via $densify
    and 7-day moving average. Exactly one row per calendar day.
    """
    database = get_db()
    dept_employees = list(database.employees.find(
        {"department": department},
        {"emp_code": 1, "joined_on": 1}
    ))
    if not dept_employees:
        raise HTTPException(
            status_code=404,
            detail="Department not found"
        )

    try:
        d_from = dt_date.fromisoformat(from_date)
        d_to = dt_date.fromisoformat(to_date)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="Invalid date format"
        )

    if d_to < d_from:
        raise HTTPException(
            status_code=422,
            detail="to date must be >= from date"
        )

    if (d_to - d_from).days > 91:
        raise HTTPException(
            status_code=422,
            detail="Range cannot exceed 92 days"
        )

    emp_codes = [e["emp_code"] for e in dept_employees]
    employees_joined = [e["joined_on"] for e in dept_employees]

    from_dt_utc = datetime(d_from.year, d_from.month, d_from.day, tzinfo=timezone.utc)
    to_dt_utc = datetime(d_to.year, d_to.month, d_to.day, tzinfo=timezone.utc)
    to_dt_next = to_dt_utc + timedelta(days=1)

    pipeline = [
        {
            "$match": {
                "emp_code": {"$in": emp_codes},
                "date": {"$gte": from_date, "$lte": to_date}
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                }
            }
        },
        {
            "$project": {
                "date_dt": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}},
                "present_count": 1,
                "late_count": 1
            }
        },
        {
            "$unionWith": {
                "pipeline": [
                    {
                        "$documents": [
                            {"date_dt": from_dt_utc, "dummy": True},
                            {"date_dt": to_dt_utc, "dummy": True}
                        ]
                    }
                ]
            }
        },
        {
            "$group": {
                "_id": "$date_dt",
                "present_count": {"$max": "$present_count"},
                "late_count": {"$max": "$late_count"}
            }
        },
        {
            "$densify": {
                "field": "_id",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt_utc, to_dt_next]
                }
            }
        },
        {
            "$project": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$_id"}},
                "dow": {"$dayOfWeek": "$_id"},
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]}
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": {"$in": ["$dow", [2, 3, 4, 5, 6]]},
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": employees_joined,
                            "as": "j",
                            "cond": {"$lte": ["$$j", "$date"]}
                        }
                    }
                },
                "present_count": 1,
                "late_count": 1
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
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$round": [{"$divide": ["$present_count", "$headcount"]}, 4]},
                        None
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, 0]
                        }
                    }
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {
                    "$cond": [
                        {"$ne": ["$moving_avg_7d", None]},
                        {"$round": ["$moving_avg_7d", 4]},
                        None
                    ]
                }
            }
        },
        {"$sort": {"date": 1}}
    ]

    items = list(database.attendance_logs.aggregate(pipeline))
    return {
        "department": department,
        "items": items
    }


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint
# --------------------------------------------------------------------------- #

@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend"
    ] = Path(...),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    """
    Runs explain with executionStats verbosity for the main query or aggregation
    of the named endpoint.
    """
    database = get_db()

    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(
                status_code=422,
                detail="date_from must be <= date_to"
            )
        q: Dict[str, Any] = {}
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
        explain_res = database.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip,
                "limit": page_size
            },
            verbosity="executionStats"
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_res
        }

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(
                status_code=422,
                detail="emp_code and month are required"
            )
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": f"{month}-01", "$lte": f"{month}-31"}}},
            {"$project": {"status": 1, "late_minutes": 1, "overtime_minutes": 1, "half_day": 1, "date": 1}}
        ]
        explain_res = database.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {}
            },
            verbosity="executionStats"
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_res
        }

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(
                status_code=422,
                detail="month is required"
            )
        match_stage: Dict[str, Any] = {"joined_on": {"$lte": f"{month}-31"}}
        if department:
            match_stage["department"] = department
        pipeline = [
            {"$match": match_stage},
            {"$group": {"_id": "$department", "count": {"$sum": 1}}}
        ]
        explain_res = database.command(
            "explain",
            {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {}
            },
            verbosity="executionStats"
        )
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": explain_res
        }

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(
                status_code=422,
                detail="month is required"
            )
        pipeline = [
            {
                "$match": {
                    "date": {"$gte": f"{month}-01", "$lte": f"{month}-31"},
                    "late_minutes": {"$gt": 0}
                }
            },
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"}
                }
            }
        ]
        explain_res = database.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {}
            },
            verbosity="executionStats"
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_res
        }

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(
                status_code=422,
                detail="department, from, and to are required"
            )
        pipeline = [
            {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
            {"$group": {"_id": "$date", "count": {"$sum": 1}}}
        ]
        explain_res = database.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {}
            },
            verbosity="executionStats"
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_res
        }

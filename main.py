"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (real environment variables win over a local .env)

Status of this file (see REVIEW.md for the defects found in the starter):
  Part A  done : /health, /employees (POST, GET), /attendance/punch-in, /attendance (GET)
                 and the shift helpers (R1 - R5).
  Part B  done : POST /attendance/punch-out, PATCH /attendance/{emp_code}/{date},
                 GET /analytics/employees/{emp_code}/monthly, GET /analytics/departments/summary,
                 GET /analytics/leaderboard/late, GET /analytics/departments/{department}/trend.
  Part C  done : GET /admin/explain/{endpoint}.
  Every endpoint of openapi.yaml is implemented.
"""
import calendar
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from pydantic import AfterValidator, BaseModel, Field, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, OperationFailure, PyMongoError
from starlette.concurrency import run_in_threadpool

# override=False (the default, made explicit): a variable that is already set in the real
# environment is never replaced by the value in a local .env file.
load_dotenv(override=False)

log = logging.getLogger("attendance")

MONGO_URI = os.environ.get("MONGO_URI") or "mongodb://localhost:27017"  # local-dev fallback, no credentials
MONGO_DB = os.environ.get("MONGO_DB") or "attendance_db"

# tz_aware=True: PyMongo hands back UTC-aware datetimes instead of naive ones, so a stored
# instant can never be mistaken for server-local time. The short selection timeout keeps
# /health from hanging for 30 s when MongoDB is down.
client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
GRACE_SECONDS = 10 * 60  # R2
MIN_OVERTIME_MINUTES = 30  # R3
HALF_DAY_BELOW_HUNDREDTHS = 450  # R5: rounded work_hours < 4.50


# --------------------------------------------------------------------------- #
# Indexes (created at startup, idempotent: same name + same keys + same options)
# --------------------------------------------------------------------------- #
INDEXES: dict[str, list[tuple[list[tuple[str, int]], dict[str, Any]]]] = {
    "employees": [
        # Uniqueness of emp_code is enforced here, not by a check-then-insert.
        ([("emp_code", ASCENDING)], {"name": "uq_emp_code", "unique": True}),
        # GET /employees?department=X sorted by emp_code.
        ([("department", ASCENDING), ("emp_code", ASCENDING)], {"name": "ix_department_emp_code"}),
        # GET /analytics/departments/summary without a department: employees who joined on or before the end of the
        # month (R9). Measured with explain: without this index that first $match is a COLLSCAN. (A (department,
        # joined_on) index was tried and removed: dropping it changed no plan; ix_department_emp_code serves the
        # department-filtered summary and the trend as well.)
        ([("joined_on", ASCENDING)], {"name": "ix_joined_on"}),
    ],
    "attendance_logs": [
        # One record per (emp_code, date): the punch-in race guard. Also serves emp_code
        # (+ date range) filters on GET /attendance, scanned in reverse for date DESC.
        ([("emp_code", ASCENDING), ("date", ASCENDING)], {"name": "uq_emp_code_date", "unique": True}),
        # GET /attendance with no emp_code: date range + the default sort (date DESC, emp_code ASC).
        ([("date", DESCENDING), ("emp_code", ASCENDING)], {"name": "ix_date_desc_emp_code"}),
        # POST /attendance/punch-out: "most recent record of this employee with punch_in <= punched_at".
        ([("emp_code", ASCENDING), ("punch_in", DESCENDING)], {"name": "ix_emp_code_punch_in_desc"}),
        # GET /attendance?status=...: equality on status, then range + sort without an in-memory sort.
        ([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], {"name": "ix_status_date_emp_code"}),
    ],
}
_indexes_ready = False


def ensure_indexes() -> None:
    global _indexes_ready
    for coll, specs in INDEXES.items():
        for keys, opts in specs:
            db[coll].create_index(keys, **opts)
    _indexes_ready = True


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        await run_in_threadpool(ensure_indexes)
    except PyMongoError:
        # MongoDB not reachable (yet): /health answers 503 and retries index creation.
        log.exception("could not create indexes at startup")
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: int) -> datetime:
    """Epoch milliseconds -> UTC-aware datetime (integer arithmetic, no float rounding)."""
    return EPOCH + timedelta(milliseconds=ms)


def dt_to_ms(dt: datetime) -> int:
    """Datetime -> epoch milliseconds. A naive datetime is taken to be UTC (how MongoDB stores it)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def truncate_seconds(dt: datetime) -> datetime:
    """R1: instants are truncated to whole seconds before any calculation."""
    return dt.replace(microsecond=0)


def now_utc_ms_precision() -> datetime:
    """Server clock, cut to the millisecond precision MongoDB stores, so response == stored value."""
    n = datetime.now(UTC)
    return n.replace(microsecond=(n.microsecond // 1000) * 1000)


def _whole_seconds(delta: timedelta) -> int:
    return delta.days * 86400 + delta.seconds


_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def parse_date(value: str) -> date:
    """Strict YYYY-MM-DD for a real calendar date (date.fromisoformat alone also accepts '20260706')."""
    if not _DATE_RE.fullmatch(value):
        raise ValueError("must be a date in YYYY-MM-DD format")
    return date(int(value[0:4]), int(value[5:7]), int(value[8:10]))  # ValueError for 2026-02-30


def _check_date(value: str) -> str:
    parse_date(value)
    return value


def _hhmm(value: str) -> tuple[int, int]:
    h, m = value.split(":")
    return int(h), int(m)


# --------------------------------------------------------------------------- #
# Shift rules R1 - R5
# --------------------------------------------------------------------------- #
def is_overnight(shift_start: str, shift_end: str) -> bool:
    return _hhmm(shift_end) <= _hhmm(shift_start)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: IST calendar date of the punch-in; for an overnight shift a punch-in earlier than
    shift_end (IST) belongs to the shift that started the previous day."""
    local = truncate_seconds(punch_in).astimezone(IST)
    d = local.date()
    if is_overnight(shift_start, shift_end) and local.time() < time(*_hhmm(shift_end)):
        d -= timedelta(days=1)
    return d.isoformat()


def shift_start_at(date_str: str, shift_start: str) -> datetime:
    h, m = _hhmm(shift_start)
    return datetime.combine(date.fromisoformat(date_str), time(h, m), tzinfo=IST)


def shift_end_at(date_str: str, shift_start: str, shift_end: str) -> datetime:
    """The shift of attendance day D ends on D, or on D+1 for an overnight shift."""
    h, m = _hhmm(shift_end)
    d = date.fromisoformat(date_str)
    if is_overnight(shift_start, shift_end):
        d += timedelta(days=1)
    return datetime.combine(d, time(h, m), tzinfo=IST)


def compute_late_minutes(punch_in: datetime, shift_start: str, date_str: str) -> int:
    """R2: late only if strictly more than 10:00 after shift_start; then whole minutes (floor)
    since shift_start, not since the end of the grace period."""
    secs = _whole_seconds(truncate_seconds(punch_in) - shift_start_at(date_str, shift_start))
    return secs // 60 if secs > GRACE_SECONDS else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600 rounded to 2 decimals HALF-UP. Pure integer arithmetic:
    floor(secs * 100 / 3600 + 1/2) == (secs + 18) // 36, so no float tie can go the wrong way."""
    secs = _whole_seconds(truncate_seconds(punch_out) - truncate_seconds(punch_in))
    return ((secs + 18) // 36) / 100


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: judged on the ROUNDED work_hours (< 4.50)."""
    return work_hours is not None and round(work_hours * 100) < HALF_DAY_BELOW_HUNDREDTHS


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, date_str: str) -> int:
    """R3: whole minutes (floor) from shift_end to punch_out, counted only if >= 30, else 0."""
    secs = _whole_seconds(truncate_seconds(punch_out) - shift_end_at(date_str, shift_start, shift_end))
    minutes = secs // 60
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


# --------------------------------------------------------------------------- #
# Validation types and errors
# --------------------------------------------------------------------------- #
# strict=True: JSON floats (even 1.5e12), strings and booleans are rejected, not coerced.
EpochMillis = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]
DateStr = Annotated[str, AfterValidator(_check_date)]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]

# [0-9], not \d: the regex engine behind pydantic treats \d as any Unicode digit.
_HHMM = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"


def unprocessable(loc: tuple, msg: str, value: Any = None):
    """Raise a 422 with FastAPI's standard body ({"detail": [ {...} ]})."""
    raise RequestValidationError([{"type": "value_error", "loc": loc, "msg": msg, "input": value}])


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=_HHMM)
    shift_end: str = Field("18:30", pattern=_HHMM)
    joined_on: DateStr

    @model_validator(mode="after")
    def _shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None


class RegularizeIn(BaseModel):
    """Exactly the fields of RegularizeRequest. A derived field sent by a client (work_hours, ...)
    is not declared here, so pydantic ignores it, as the Conventions section of the contract says.
    An explicit null is treated like an omitted field."""

    status: Optional[Status] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Serialisation (MongoDB document -> API JSON). _id never leaves this function.
# --------------------------------------------------------------------------- #
def _to_api_value(value: Any) -> Any:
    return dt_to_ms(value) if isinstance(value, datetime) else value


def _ms_or_none(value: Optional[datetime]) -> Optional[int]:
    return None if value is None else dt_to_ms(value)


def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": _ms_or_none(doc.get("created_at")),
    }


def serialize_history_entry(entry: dict) -> dict:
    # the from/to values of punch_in / punch_out are BSON dates in MongoDB, epoch ms in the API
    changes = {
        field: {"from": _to_api_value(c.get("from")), "to": _to_api_value(c.get("to"))}
        for field, c in (entry.get("changes") or {}).items()
    }
    return {"at": _ms_or_none(entry.get("at")), "by": entry.get("by"), "reason": entry.get("reason"), "changes": changes}


def serialize_record(doc: dict) -> dict:
    """Seeded records may omit half_day / history / late_minutes / overtime_minutes (DATA_MODEL.md):
    missing means false / [] / 0."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": _ms_or_none(doc.get("punch_in")),
        "punch_out": _ms_or_none(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": [serialize_history_entry(e) for e in (doc.get("history") or [])],
    }


# --------------------------------------------------------------------------- #
# Query builders (kept separate so the future explain endpoint can reuse them)
# --------------------------------------------------------------------------- #
ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]
# With an emp_code filter every row has the same emp_code, so the tie-break by emp_code changes nothing: the order is
# identical, but sorting by date alone lets the (emp_code, date) index return the rows already sorted. Measured with
# explain on 100k records: an in-memory SORT over the employee's whole history disappears and 20 keys are read, not 80.
ATTENDANCE_SORT_ONE_EMPLOYEE = [("date", DESCENDING)]


def build_attendance_filter(
    emp_code: Optional[str], date_from: Optional[str], date_to: Optional[str], status: Optional[str]
) -> dict:
    q: dict[str, Any] = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        q["date"] = {}
        if date_from is not None:
            q["date"]["$gte"] = date_from
        if date_to is not None:
            q["date"]["$lte"] = date_to
    if status is not None:
        q["status"] = status
    return q


def build_attendance_find(
    emp_code: Optional[str], date_from: Optional[str], date_to: Optional[str], status: Optional[str], skip: int, limit: int
) -> dict:
    """The page query of GET /attendance, as the keyword arguments of PyMongo's find(). The explain endpoint explains
    exactly this (filter, projection, sort, skip and limit), so the two cannot drift apart."""
    return {
        "filter": build_attendance_filter(emp_code, date_from, date_to, status),
        "projection": {"_id": 0},
        "sort": ATTENDANCE_SORT if emp_code is None else ATTENDANCE_SORT_ONE_EMPLOYEE,
        "skip": skip,
        "limit": limit,
    }


def validate_attendance_dates(date_from: Optional[str], date_to: Optional[str]) -> None:
    if date_from is not None and date_to is not None and date_from > date_to:  # ISO dates sort as text
        unprocessable(("query", "date_from"), "date_from must not be after date_to", date_from)


def page_window(page: int, page_size: int, total: int) -> Optional[int]:
    """skip for the page, or None when the page lies beyond the data (avoids a huge $skip)."""
    skip = (page - 1) * page_size
    return skip if skip < total else None


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "MongoDB is not reachable")
    if not _indexes_ready:  # startup could not reach MongoDB: try again now that it answers
        try:
            ensure_indexes()
        except PyMongoError:
            log.exception("could not create indexes")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc_ms_precision()
    try:
        db.employees.insert_one(doc)  # the unique index on emp_code decides, atomically
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: Page = 1, page_size: PageSize = 20):
    q = {} if department is None else {"department": department}  # exact, case-sensitive
    total = db.employees.count_documents(q)  # counts the FILTERED set
    skip = page_window(page, page_size, total)
    items = []
    if skip is not None:
        cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
        items = [serialize_employee(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, "employee not found")

    punched_at = ms_to_dt(body.punched_at) if body.punched_at is not None else datetime.now(UTC)
    punched_at = truncate_seconds(punched_at)  # R1: the truncated value is what is stored and returned
    d = attendance_date(punched_at, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(punched_at, emp["shift_start"], d),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date): exactly one of N racers wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize_record(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Status] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    validate_attendance_dates(date_from, date_to)
    total = db.attendance_logs.count_documents(build_attendance_filter(emp_code, date_from, date_to, status))
    skip = page_window(page, page_size, total)
    items = []
    if skip is not None:
        # sort / skip / limit run inside MongoDB and are backed by the indexes above
        cursor = db.attendance_logs.find(**build_attendance_find(emp_code, date_from, date_to, status, skip, page_size))
        items = [serialize_record(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Punch-out and manual correction (PATCH)
# --------------------------------------------------------------------------- #
DAY_SECONDS = 24 * 3600
NO_PUNCH_STATUSES = ("ABSENT", "LEAVE")
STATE_FIELDS = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")


class RuleViolation(Exception):
    """A business-rule violation that must become a 422 (field = the request field it is about)."""

    def __init__(self, field: str, message: str):
        super().__init__(message)
        self.field, self.message = field, message


def duration_error(punch_in: datetime, punch_out: datetime) -> Optional[str]:
    """punch_out must be after punch_in and at most 24 h later (both cut to whole seconds)."""
    secs = _whole_seconds(truncate_seconds(punch_out) - truncate_seconds(punch_in))
    if secs <= 0:
        return "punch_out must be after punch_in"
    if secs > DAY_SECONDS:
        return "punch_out must be within 24 hours of punch_in"
    return None


def derive_fields(emp: dict, date_str: str, punch_in: datetime, punch_out: Optional[datetime]) -> dict:
    """R2-R5 from the final punch times. Without a punch_out there is no work, overtime or half day."""
    out = {
        "late_minutes": compute_late_minutes(punch_in, emp["shift_start"], date_str),
        "work_hours": None,
        "overtime_minutes": 0,
        "half_day": False,
    }
    if punch_out is not None:
        work_hours = compute_work_hours(punch_in, punch_out)
        out["work_hours"] = work_hours
        out["overtime_minutes"] = compute_overtime(punch_out, emp["shift_start"], emp["shift_end"], date_str)
        out["half_day"] = compute_half_day(work_hours)
    return out


def stored_state(rec: dict) -> dict:
    """The seven mutable fields as stored, with the legacy defaults of DATA_MODEL.md applied."""
    return {
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day", False)),
    }


def plan_regularization(
    emp: dict, rec: dict, status: Optional[str], punch_in: Optional[datetime], punch_out: Optional[datetime]
) -> tuple[dict, dict]:
    """Pure function: apply a correction to the stored record.
    Returns (new values of all seven fields, {field: {"from", "to"}} for the ones that really changed)
    or raises RuleViolation. punch_in / punch_out must already be cut to whole seconds."""
    old = stored_state(rec)
    new_status = status if status is not None else old["status"]

    if new_status in NO_PUNCH_STATUSES:
        # ABSENT / LEAVE clear both punch times; supplying one together with it is contradictory
        for field, value in (("punch_in", punch_in), ("punch_out", punch_out)):
            if value is not None:
                raise RuleViolation(field, f"{field} cannot be set when the status is {new_status}")
        new = {"status": new_status, "punch_in": None, "punch_out": None, "work_hours": None,
               "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    else:
        final_in = punch_in if punch_in is not None else old["punch_in"]
        final_out = punch_out if punch_out is not None else old["punch_out"]
        if final_in is None:
            raise RuleViolation("punch_in", f"status {new_status} requires a punch_in")
        if punch_in is not None and attendance_date(final_in, emp["shift_start"], emp["shift_end"]) != rec["date"]:
            raise RuleViolation("punch_in", f"punch_in must fall on the attendance date {rec['date']} (IST, R1)")
        if final_out is not None:
            problem = duration_error(final_in, final_out)
            if problem:
                raise RuleViolation("punch_out", problem)
        new = {"status": new_status, "punch_in": final_in, "punch_out": final_out,
               **derive_fields(emp, rec["date"], final_in, final_out)}

    requested_something_new = (
        (status is not None and status != old["status"])
        or (punch_in is not None and punch_in != old["punch_in"])
        or (punch_out is not None and punch_out != old["punch_out"])
    )
    if not requested_something_new:
        raise RuleViolation("body", "the request does not change anything")

    changes = {f: {"from": old[f], "to": new[f]} for f in STATE_FIELDS if new[f] != old[f]}
    return new, changes


def state_guard(rec: dict) -> dict:
    """Optimistic-concurrency filter: matches only while the record is exactly as it was read.
    `history` is append-only, so its length identifies its state. A value of None also matches a
    missing field, which is how legacy documents without the field are handled."""
    guard: dict[str, Any] = {"_id": rec["_id"]}
    for field in STATE_FIELDS:
        guard[field] = rec.get(field)
    if "history" not in rec:
        guard["history"] = {"$exists": False}
    elif isinstance(rec["history"], list):
        guard["history"] = {"$size": len(rec["history"])}
    else:
        guard["history"] = None
    return guard


def write_correction(rec: dict, new: dict, entry: dict) -> Optional[dict]:
    """ONE atomic update: set the new values and append the history entry, but only if nobody changed
    the record since it was read. Returns the updated document, or None if the guard no longer matches."""
    update: dict[str, Any] = {"$set": dict(new)}
    if isinstance(rec.get("history"), list):
        update["$push"] = {"history": entry}  # append: existing entries are never rewritten
    else:
        update["$set"]["history"] = [entry]  # legacy document: history missing (or null), nothing to preserve
    return db.attendance_logs.find_one_and_update(state_guard(rec), update, return_document=ReturnDocument.AFTER)


def close_record(rec: dict, values: dict) -> Optional[dict]:
    """ONE atomic update that closes an open record. The filter requires it to still be open and its
    punch_in unchanged, so of N simultaneous punch-outs exactly one finds a match."""
    return db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None, "punch_in": rec["punch_in"]},
        {"$set": values},
        return_document=ReturnDocument.AFTER,
    )


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, "employee not found")

    punched_at = truncate_seconds(ms_to_dt(body.punched_at) if body.punched_at is not None else datetime.now(UTC))

    # "most recent record whose punch_in is at or before punched_at": found by punch_in, not by the IST date
    # of punched_at, so an overnight shift closes correctly the next morning. Records without a punch_in
    # (ABSENT / LEAVE) never match a $lte comparison against a date.
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punched_at}}, sort=[("punch_in", DESCENDING)]
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found for this employee")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    problem = duration_error(rec["punch_in"], punched_at)
    if problem:
        unprocessable(("body", "punched_at"), problem, body.punched_at)

    # R3-R5 on the record's own attendance date (not the date of punched_at); late_minutes stays as stored.
    derived = derive_fields(emp, rec["date"], rec["punch_in"], punched_at)
    values = {"punch_out": punched_at, "work_hours": derived["work_hours"],
              "overtime_minutes": derived["overtime_minutes"], "half_day": derived["half_day"]}
    updated = close_record(rec, values)
    if updated is None:
        raise HTTPException(409, "already punched out")  # lost the race, or the record was changed meanwhile
    return serialize_record(updated)


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: DateStr, body: RegularizeIn):  # `date` = the path parameter
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, "employee not found")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")

    def instant(ms: Optional[int]) -> Optional[datetime]:
        return None if ms is None else truncate_seconds(ms_to_dt(ms))  # R1: whole seconds, stored as given

    try:
        new, changes = plan_regularization(emp, rec, body.status, instant(body.punch_in), instant(body.punch_out))
    except RuleViolation as violation:
        loc = ("body",) if violation.field == "body" else ("body", violation.field)
        unprocessable(loc, violation.message)

    entry = {"at": now_utc_ms_precision(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    updated = write_correction(rec, new, entry)
    if updated is None:
        raise HTTPException(409, "the record was modified concurrently; re-read it and try again")
    return serialize_record(updated)


# --------------------------------------------------------------------------- #
# GET /analytics/employees/{emp_code}/monthly
# --------------------------------------------------------------------------- #
PRESENT_STATUSES = ("PRESENT", "WFH", "ON_DUTY")  # R6
# [0-9], not \d (see _HHMM): the month pattern of openapi.yaml, ASCII digits only.
MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
Month = Annotated[str, Query(pattern=MONTH_PATTERN, description="Calendar month, YYYY-MM.")]


def month_days(month: str) -> list[date]:
    """Every calendar day of a YYYY-MM month. Year 0000 satisfies the contract's pattern but is not a year
    that Python dates can express; it has no days here, and no employee can have joined that early anyway."""
    year, mon = int(month[:4]), int(month[5:7])
    if year < 1:
        return []
    return [date(year, mon, d) for d in range(1, calendar.monthrange(year, mon)[1] + 1)]


def weekday_dates(month: str) -> list[str]:
    """The Monday-Friday dates of the month as YYYY-MM-DD strings (R7: no holiday calendar)."""
    return [d.isoformat() for d in month_days(month) if d.weekday() < 5]


def working_days_in_month(month: str, joined_on: str) -> int:
    """R7: Mon-Fri days of the month, counted from joined_on (inclusive) for a mid-month joiner.
    ISO date strings compare chronologically, so a plain string comparison is exact."""
    return sum(1 for d in weekday_dates(month) if d >= joined_on)


def attendance_pct(present_units: int, working_days: int) -> Optional[float]:
    """present_days / working_days * 100 to 2 decimals HALF-UP, or None without working days (R8).
    present_units counts half days: a full day is 2 units, a half day 1, so present_days == present_units / 2.
    The percentage in hundredths is floor(units * 5000 / wd + 1/2) == (units * 10000 + wd) // (2 * wd);
    exact integer arithmetic, so a tie such as 0.5 / 16 = 3.125 % rounds up to 3.13 (round() gives 3.12)."""
    if working_days == 0:
        return None
    return ((present_units * 10_000 + working_days) // (2 * working_days)) / 100


def build_employee_monthly_pipeline(emp_code: str, month: str) -> list[dict]:
    """One employee, one month, one pass over at most ~31 documents.
    $match is a pure index range on (emp_code, date) - the unique index - so the collection is never scanned.
    $group then folds the stored values (analytics never recompute them). A field that is missing or null counts
    as 0 / false (DATA_MODEL.md), hence $ifNull and the $eq against true."""
    present_on_a_weekday = {
        "$and": [
            {"$in": ["$status", list(PRESENT_STATUSES)]},  # R6
            {"$in": ["$date", weekday_dates(month)]},  # R7: present_days counts Mon-Fri records only
        ]
    }
    return [
        # "-01" .. "-31" bound every date of the month as text, whatever its length (no month arithmetic)
        {"$match": {"emp_code": emp_code, "date": {"$gte": f"{month}-01", "$lte": f"{month}-31"}}},
        {
            "$group": {
                "_id": None,
                # 2 units for a present weekday, 1 for a half day (R5), 0 otherwise
                "present_units": {"$sum": {"$cond": [present_on_a_weekday, {"$cond": [{"$eq": ["$half_day", True]}, 1, 2]}, 0]}},
                # the three below cover EVERY record of the month, weekends included
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


def _whole(value: Any) -> int:
    return 0 if value is None else int(value)  # a sum of float-valued minutes (35.0) must still be an integer


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: Month):
    # An unknown employee is a 404; an employee without records is a normal (all-zero) report.
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0, "joined_on": 1})
    if emp is None:
        raise HTTPException(404, "employee not found")

    rows = list(db.attendance_logs.aggregate(build_employee_monthly_pipeline(emp_code, month)))
    agg = rows[0] if rows else {}  # real MongoDB returns no row at all for an empty input
    working_days = working_days_in_month(month, emp.get("joined_on") or "")
    present_units = _whole(agg.get("present_units"))
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_units / 2,
        "leave_days": _whole(agg.get("leave_days")),
        "late_count": _whole(agg.get("late_count")),
        "total_late_minutes": _whole(agg.get("total_late_minutes")),
        "total_overtime_minutes": _whole(agg.get("total_overtime_minutes")),
        "attendance_pct": attendance_pct(present_units, working_days),
    }


# --------------------------------------------------------------------------- #
# GET /analytics/departments/summary
# --------------------------------------------------------------------------- #
def average_hours(hundredths_sum: int, records: int) -> Optional[float]:
    """Mean work_hours over `records` records, 2 decimals HALF-UP, from the sum of the records' work_hours in
    hundredths of an hour (an exact integer). floor(sum / n + 1/2) == (2 * sum + n) // (2 * n): 9.12 and 9.13
    average to 9.125 and give 9.13 (round() gives 9.12), with no float error. None when there is no record."""
    if records == 0:
        return None
    return ((2 * hundredths_sum + records) // (2 * records)) / 100


def build_department_summary_pipeline(month: str, department: Optional[str] = None) -> list[dict]:
    """Runs on `employees`: the only side that knows who is on the payroll, so people without a single log still
    count (R9). Each employee's month of logs is folded INSIDE the $lookup, an index probe on (emp_code, date),
    so the outer pipeline handles one small row per employee and never one row per attendance record.
    The join key is emp_code, not _id."""
    present = {"$in": ["$status", list(PRESENT_STATUSES)]}  # R6
    present_with_hours = {"$and": [present, {"$isNumber": "$work_hours"}]}  # a missing / null work_hours is not a value
    employees_in_scope: dict[str, Any] = {"joined_on": {"$lte": f"{month}-31"}}  # R9: joined on or before month end
    if department is not None:
        employees_in_scope["department"] = department  # exact match
    return [
        {"$match": employees_in_scope},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": f"{month}-01", "$lte": f"{month}-31"}}},
                    {
                        "$group": {
                            "_id": None,
                            # present_days exactly as in the employee report: present AND Mon-Fri, half day = 0.5
                            "present_units": {
                                "$sum": {"$cond": [
                                    {"$and": [present, {"$in": ["$date", weekday_dates(month)]}]},
                                    {"$cond": [{"$eq": ["$half_day", True]}, 1, 2]},
                                    0,
                                ]}
                            },
                            # avg_work_hours: over present-status records that HAVE a work_hours value, weekends included;
                            # kept as (record count, sum in hundredths) so that departments can be combined exactly
                            "hours_records": {"$sum": {"$cond": [present_with_hours, 1, 0]}},
                            # each record in whole hundredths of an hour: floor(x * 100 + 0.5) is exact half-up and immune to
                            # float noise (9.12 * 100 is 911.9999999999999 in a double; truncating would give 911)
                            "hours_hundredths": {"$sum": {"$cond": [present_with_hours, {"$floor": {"$add": [{"$multiply": ["$work_hours", 100]}, 0.5]}}, 0]}},
                            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                            "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                        }
                    },
                ],
                "as": "month_stats",
            }
        },
        # no logs in the month -> empty array -> the field is simply missing, and $sum below ignores it
        {"$set": {"month_stats": {"$arrayElemAt": ["$month_stats", 0]}}},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},  # one per employee, whatever their number of records
                "present_units": {"$sum": "$month_stats.present_units"},
                "hours_records": {"$sum": "$month_stats.hours_records"},
                "hours_hundredths": {"$sum": "$month_stats.hours_hundredths"},
                "late_count": {"$sum": "$month_stats.late_count"},
                "total_late_minutes": {"$sum": "$month_stats.total_late_minutes"},
                "leave_count": {"$sum": "$month_stats.leave_count"},
                "on_duty_count": {"$sum": "$month_stats.on_duty_count"},
            }
        },
        {"$sort": {"_id": 1}},  # department ascending
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: Month, department: Optional[str] = None):
    rows = db.employees.aggregate(build_department_summary_pipeline(month, department))
    # one aggregated row per DEPARTMENT (a handful), only formatted here
    items = [
        {
            "department": r["_id"],
            "headcount": _whole(r.get("headcount")),
            "present_days": _whole(r.get("present_units")) / 2,
            "avg_work_hours": average_hours(_whole(r.get("hours_hundredths")), _whole(r.get("hours_records"))),
            "late_count": _whole(r.get("late_count")),
            "total_late_minutes": _whole(r.get("total_late_minutes")),
            "leave_count": _whole(r.get("leave_count")),
            "on_duty_count": _whole(r.get("on_duty_count")),
        }
        for r in rows
    ]
    return {"month": month, "items": items}


# --------------------------------------------------------------------------- #
# GET /analytics/leaderboard/late
# --------------------------------------------------------------------------- #
Limit = Annotated[int, Query(ge=1, le=50, description="Return every employee whose rank is <= limit.")]


def build_late_leaderboard_pipeline(month: str, limit: int, department: Optional[str] = None) -> list[dict]:
    """Runs on `attendance_logs`. $match is an index range on date (only records with lateness), so employees
    without lateness are never read; the employee is joined by emp_code through the unique index."""
    pipeline: list[dict[str, Any]] = [
        {"$match": {"date": {"$gte": f"{month}-01", "$lte": f"{month}-31"}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        # no preserveNullAndEmptyArrays: logs whose employee does not exist are dropped here
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        # BEFORE the ranking: "ranking happens inside that department only"
        pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        # Standard competition rank (1, 2, 2, 4). sortBy holds ONLY the metric: adding emp_code would make
        # every row unique and destroy the ties. The rank is computed by the database, never from a position.
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit AFTER ranking: ties at the boundary all stay
        {"$sort": {"total_late_minutes": -1, "_id": 1}},  # then emp_code ascending
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: Month, limit: Limit = 10, department: Optional[str] = None):
    rows = db.attendance_logs.aggregate(build_late_leaderboard_pipeline(month, limit, department))
    items = [
        {
            "rank": _whole(r["rank"]),
            "emp_code": r["emp_code"],
            "name": r["name"],
            "department": r["department"],
            "total_late_minutes": _whole(r["total_late_minutes"]),
            "late_count": _whole(r["late_count"]),
        }
        for r in rows
    ]
    return {"month": month, "items": items}


# --------------------------------------------------------------------------- #
# GET /analytics/departments/{department}/trend
# --------------------------------------------------------------------------- #
MAX_TREND_DAYS = 92
TREND_WINDOW_ROWS = 7  # the current row and up to 6 preceding ones


def build_department_trend_pipeline(department: str, first: date, last: date) -> list[dict]:
    """One row per calendar day of [first, last], built entirely inside MongoDB (no per-day work in Python).

    Starting from the department's employees:
      1  $match      the department (index (department, ...)).
      2  $lookup     attendance_logs on emp_code with a sub-pipeline restricted to [first, last]: one index range on
                     the unique (emp_code, date) index per employee. Each record becomes a tiny event
                     {date, units, late}: units = 2 for a present-status record, 1 for a half day, else 0 (so the
                     day's present_count is units / 2); late = 1 when late_minutes > 0.
      3  $set        add ONE more event per employee: {date: the day the employee joined, joined: 1} (built with $map over
                     a one-element array so that its date expression is evaluated). An employee who
                     joined before `first` joins on `first`; one who joins after `last` gets a neutral event on `first`
                     (and his/her logs are dropped), which also guarantees that every employee yields at least one
                     event, so the stream is never empty and $densify always has something to anchor on.
      4  $unwind     the events; 5 $group by date: joined / units / late per day. Only days with an event exist so far.
      6  $project    day = the date as a real BSON date ($densify cannot work on a 'YYYY-MM-DD' string).
      7  $densify    GAP FILLING: inserts a document for every missing day. bounds = [first, last + 1 day) as MongoDB
                     defines them (lower inclusive, upper exclusive); the upper bound is written as `last` at
                     23:59:59.999, which includes `last`, excludes the next day and cannot overflow at 9999-12-31.
      8  $set        the fields of an inserted document are missing: default them to 0.
      9  $setWindowFields   headcount = running sum of `joined` from the first row to the current one (R9, per day).
     10  $set        date (string) and is_working_day (ISO weekday 1-5); then the rate in ten-thousandths (rate_bp):
                     floor((units * 10000 + headcount) / (2 * headcount)) = present / headcount to 4 decimals HALF-UP,
                     in exact integer arithmetic. Only on working days with headcount > 0, otherwise null.
     11  $setWindowFields   moving_bp = $avg of rate_bp over documents [-6, 0]: the current row and up to 6 earlier
                     rows. There is exactly one row per day, so a row is a calendar day; the first rows have a shorter
                     window; $avg ignores the null rates of weekends and returns null when nothing is left.
     12  $project    the contract's fields; the moving average rounds half-up with floor(x + 0.5) (x is a mean of
                     integers with at most 7 terms, so no near-tie can be misjudged); 13 $sort by date."""
    lo, hi = first.isoformat(), last.isoformat()
    first_instant = datetime(first.year, first.month, first.day, tzinfo=UTC)
    last_instant_exclusive = datetime(last.year, last.month, last.day, 23, 59, 59, 999000, tzinfo=UTC)
    present = {"$in": ["$status", list(PRESENT_STATUSES)]}  # R6
    log_event = {
        "$project": {
            "_id": 0,
            "date": 1,
            "units": {"$cond": [present, {"$cond": [{"$eq": ["$half_day", True]}, 1, 2]}, 0]},  # any weekday or weekend
            "late": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]},
        }
    }
    joined_event = {"date": {"$cond": [{"$lt": ["$joined_on", lo]}, lo, "$joined_on"]}, "joined": 1}
    return [
        {"$match": {"department": department}},
        {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
                     "pipeline": [{"$match": {"date": {"$gte": lo, "$lte": hi}}}, log_event], "as": "events"}},
        {"$set": {"events": {"$cond": [{"$lte": ["$joined_on", hi]},
                                       {"$concatArrays": ["$events", {"$map": {"input": [1], "as": "one", "in": joined_event}}]},
                                       [{"date": lo, "joined": 0}]]}}},
        {"$unwind": "$events"},
        {"$group": {"_id": "$events.date", "joined": {"$sum": "$events.joined"}, "units": {"$sum": "$events.units"}, "late": {"$sum": "$events.late"}}},
        {"$project": {"_id": 0, "day": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}, "joined": 1, "units": 1, "late": 1}},
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [first_instant, last_instant_exclusive]}}},
        {"$set": {"joined": {"$ifNull": ["$joined", 0]}, "units": {"$ifNull": ["$units", 0]}, "late": {"$ifNull": ["$late", 0]}}},
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {"headcount": {"$sum": "$joined", "window": {"documents": ["unbounded", "current"]}}}}},
        {"$set": {"date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d"}}, "is_working_day": {"$lte": [{"$isoDayOfWeek": "$day"}, 5]}}},
        {"$set": {"rate_bp": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                                        {"$floor": {"$divide": [{"$add": [{"$multiply": ["$units", 10000]}, "$headcount"]}, {"$multiply": [2, "$headcount"]}]}},
                                        None]}}},
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {"moving_bp": {"$avg": "$rate_bp", "window": {"documents": [-(TREND_WINDOW_ROWS - 1), 0]}}}}},
        {"$project": {
            "_id": 0,
            "date": 1,
            "is_working_day": 1,
            "headcount": 1,
            "present_count": {"$divide": ["$units", 2]},
            "late_count": "$late",
            "attendance_rate": {"$cond": [{"$isNumber": "$rate_bp"}, {"$divide": ["$rate_bp", 10000]}, None]},
            "moving_avg_7d": {"$cond": [{"$isNumber": "$moving_bp"}, {"$divide": [{"$floor": {"$add": ["$moving_bp", 0.5]}}, 10000]}, None]},
        }},
        {"$sort": {"date": 1}},
    ]


def validate_trend_range(from_: str, to: str) -> tuple[date, date]:
    first, last = parse_date(from_), parse_date(to)  # both already validated as real calendar dates
    if last < first:
        unprocessable(("query", "to"), "to must not be before from", to)
    if (last - first).days + 1 > MAX_TREND_DAYS:  # the range covers (last - first + 1) calendar days
        unprocessable(("query", "to"), f"the range must not exceed {MAX_TREND_DAYS} days", to)
    return first, last


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: Annotated[DateStr, Query(alias="from")], to: DateStr):
    first, last = validate_trend_range(from_, to)
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "department not found")  # no employee at all, whatever the join dates
    rows = db.employees.aggregate(build_department_trend_pipeline(department, first, last))
    return {
        "department": department,
        "items": [  # shaping the aggregation's rows for JSON only; every value was computed by MongoDB
            {
                "date": r["date"],
                "is_working_day": bool(r["is_working_day"]),
                "headcount": _whole(r["headcount"]),
                "present_count": float(r["present_count"]),
                "late_count": _whole(r["late_count"]),
                "attendance_rate": r["attendance_rate"],
                "moving_avg_7d": r["moving_avg_7d"],
            }
            for r in rows
        ],
    }


# --------------------------------------------------------------------------- #
# GET /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
Explainable = Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]
MAX_EXPLAIN_SKIP = 2**62  # a page so far out that its skip would overflow MongoDB's 64-bit integers is explained with this one


def find_command(collection: str, spec: dict) -> dict:
    """The `find` command document for the kwargs that build_attendance_find() gives PyMongo's find()."""
    return {"find": collection, "filter": spec["filter"], "projection": spec["projection"], "sort": dict(spec["sort"]),
            "skip": spec["skip"], "limit": spec["limit"]}


def aggregate_command(collection: str, pipeline: list[dict]) -> dict:
    return {"aggregate": collection, "pipeline": pipeline, "cursor": {}}


def require(parameters: dict[str, Any]) -> None:
    """422 listing EVERY required parameter that is missing (the explain endpoint's parameters are all optional in the
    schema because each endpoint needs different ones)."""
    missing = [name for name, value in parameters.items() if value is None]
    if missing:
        raise RequestValidationError([{"type": "missing", "loc": ("query", name), "msg": "Field required", "input": None} for name in missing])


def explain_command(endpoint: str, p: dict[str, Any]) -> tuple[str, dict]:
    """(collection, command) of the main query of `endpoint`, produced by the SAME functions the endpoint itself calls."""
    if endpoint == "attendance_list":
        validate_attendance_dates(p["date_from"], p["date_to"])
        skip = min((p["page"] - 1) * p["page_size"], MAX_EXPLAIN_SKIP)
        spec = build_attendance_find(p["emp_code"], p["date_from"], p["date_to"], p["status"], skip, p["page_size"])
        return "attendance_logs", find_command("attendance_logs", spec)
    if endpoint == "employee_monthly":
        require({"emp_code": p["emp_code"], "month": p["month"]})
        return "attendance_logs", aggregate_command("attendance_logs", build_employee_monthly_pipeline(p["emp_code"], p["month"]))
    if endpoint == "department_summary":
        require({"month": p["month"]})
        return "employees", aggregate_command("employees", build_department_summary_pipeline(p["month"], p["department"]))
    if endpoint == "late_leaderboard":
        require({"month": p["month"]})
        limit = 10 if p["limit"] is None else p["limit"]  # the endpoint's own default
        return "attendance_logs", aggregate_command("attendance_logs", build_late_leaderboard_pipeline(p["month"], limit, p["department"]))
    require({"department": p["department"], "from": p["from"], "to": p["to"]})  # department_trend
    first, last = validate_trend_range(p["from"], p["to"])
    return "employees", aggregate_command("employees", build_department_trend_pipeline(p["department"], first, last))


def run_explain(command: dict) -> dict:
    """MongoDB's own explain, executionStats verbosity. The explained operation is EXECUTED; the server's answer is
    returned untouched. (PyMongo has no explain option on aggregate(); the `explain` command is the supported route.)"""
    return db.command({"explain": command, "verbosity": "executionStats"})


def to_json_document(document: Any) -> Any:
    """BSON -> plain JSON without losing anything: MongoDB's relaxed Extended JSON (dates become {"$date": ...}, 64-bit
    integers plain numbers, NaN / Infinity {"$numberDouble": ...}), so every explain document is valid JSON."""
    return json.loads(json_util.dumps(document, json_options=json_util.RELAXED_JSON_OPTIONS))


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Explainable,
    emp_code: Optional[str] = None,
    month: Annotated[Optional[str], Query(pattern=MONTH_PATTERN)] = None,
    department: Optional[str] = None,
    limit: Annotated[Optional[int], Query(ge=1, le=50)] = None,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Status] = None,
    from_: Annotated[Optional[DateStr], Query(alias="from")] = None,
    to: Optional[DateStr] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    collection, command = explain_command(endpoint, {
        "emp_code": emp_code, "month": month, "department": department, "limit": limit, "date_from": date_from,
        "date_to": date_to, "status": status, "from": from_, "to": to, "page": page, "page_size": page_size,
    })
    try:
        raw = run_explain(command)
    except OperationFailure as exc:
        # The server refused (for instance a pipeline stage it does not know, as MongoDB older than 6.0 does). Say so with
        # the server's own words; a plan is never substituted, because any other plan would describe a different query.
        reason = (exc.details or {}).get("errmsg") or str(exc)
        raise HTTPException(500, f"MongoDB could not explain the {endpoint} query: {reason}")
    return {"endpoint": endpoint, "collection": collection, "explain": to_json_document(raw)}

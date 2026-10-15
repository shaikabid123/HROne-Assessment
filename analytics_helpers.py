"""Shared by the department-summary and leaderboard tests."""
import calendar
from datetime import date, datetime, timezone

from app import main

JUL = "2026-07"   # the 1st is a Wednesday: Sat/Sun are the 4/5, 11/12, 18/19, 25/26; 23 working days
PRESENT = {"PRESENT", "WFH", "ON_DUTY"}


def add_emp(code, dept="Engineering", joined_on="2026-01-05", name=None, **extra):
    main.db.employees.insert_one({"emp_code": code, "name": name or f"Name {code}", "email": f"{code}@example.com",
                                  "department": dept, "shift_start": "09:30", "shift_end": "18:30", "joined_on": joined_on,
                                  "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc), **extra})


def add_rec(code, day, status="PRESENT", late=None, ot=None, half=None, hours=None, **extra):
    """Only the fields that are given are stored; an omitted one is MISSING from the document, like seeded data."""
    doc = {"emp_code": code, "date": day, "status": status}
    for name, value in (("late_minutes", late), ("overtime_minutes", ot), ("half_day", half), ("work_hours", hours)):
        if value is not None:
            doc[name] = value
    doc.update(extra)
    main.db.attendance_logs.insert_one(doc)


def days_of(month):
    y, m = map(int, month.split("-"))
    return [date(y, m, d) for d in range(1, calendar.monthrange(y, m)[1] + 1)]


def weekdays_of(month):
    return [d for d in days_of(month) if d.weekday() < 5]


class SpyCollection:
    def __init__(self, coll, name, calls, overrides):
        self._coll, self._name, self._calls, self._overrides = coll, name, calls, overrides

    def __getattr__(self, attr):
        target = getattr(self._coll, attr)
        if not callable(target):
            return target
        override = self._overrides.get((self._name, attr))

        def recorded(*args, **kwargs):
            self._calls.append((self._name, attr, args, kwargs))
            return override(*args, **kwargs) if override else target(*args, **kwargs)

        return recorded


class SpyDB:
    """Wraps the database (not a collection: a real PyMongo Database hands out a NEW Collection object on every access, so
    patching one is useless). Records every call the application makes as (collection, method, args, kwargs) and can
    replace a method: overrides={("attendance_logs", "aggregate"): function}."""

    def __init__(self, db, overrides=None):
        self._db, self._overrides, self.calls = db, overrides or {}, []

    def __getattr__(self, name):
        return SpyCollection(getattr(self._db, name), name, self.calls, self._overrides)

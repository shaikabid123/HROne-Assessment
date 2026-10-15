"""GET /analytics/employees/{emp_code}/monthly.

July 2026: the 1st is a Wednesday, so Sat/Sun are the 4/5, 11/12, 18/19, 25/26 and there are 23 working days.
The oracle below is plain date loops + exact Decimal maths; it shares no code with the pipeline or with the
integer rounding formula in app/main.py."""
import calendar
import json
import random
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, localcontext

import pytest

from app import main

JUL = "2026-07"
PRESENT = {"PRESENT", "WFH", "ON_DUTY"}
KEYS = ["emp_code", "month", "working_days", "present_days", "leave_days", "late_count",
        "total_late_minutes", "total_overtime_minutes", "attendance_pct"]


# --------------------------------------------------------------------------- helpers
def add_emp(code="EMP0001", joined_on="2026-01-05", dept="Engineering"):
    main.db.employees.insert_one({"emp_code": code, "name": "N", "email": "n@example.com", "department": dept,
                                  "shift_start": "09:30", "shift_end": "18:30", "joined_on": joined_on,
                                  "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc)})


def add_rec(code, day, status="PRESENT", late=None, ot=None, half=None, **extra):
    """Only the fields that are given are stored: an omitted one is MISSING from the document, like seeded data."""
    doc = {"emp_code": code, "date": day, "status": status}
    for name, value in (("late_minutes", late), ("overtime_minutes", ot), ("half_day", half)):
        if value is not None:
            doc[name] = value
    doc.update(extra)
    main.db.attendance_logs.insert_one(doc)


def get(api, code="EMP0001", month=JUL):
    return api.get(f"/analytics/employees/{code}/monthly", params={"month": month})


def days_of(month):
    y, m = map(int, month.split("-"))
    return [date(y, m, d) for d in range(1, calendar.monthrange(y, m)[1] + 1)]


def weekdays_of(month, joined="0000-00-00"):
    return [d for d in days_of(month) if d.weekday() < 5 and d.isoformat() >= joined]


def oracle(emp, records, month):
    """Expected report, straight from R5-R8 and the endpoint description."""
    first, last = days_of(month)[0].isoformat(), days_of(month)[-1].isoformat()
    working = len(weekdays_of(month, emp["joined_on"]))
    present, leave, late_count, late, ot = Decimal(0), 0, 0, 0, 0
    for r in records:
        if r["emp_code"] != emp["emp_code"] or not first <= r["date"] <= last:
            continue
        if r["status"] in PRESENT and date.fromisoformat(r["date"]).weekday() < 5:
            present += Decimal("0.5") if r.get("half_day") else Decimal(1)
        leave += r["status"] == "LEAVE"
        minutes = r.get("late_minutes") or 0
        late += minutes
        late_count += minutes > 0
        ot += r.get("overtime_minutes") or 0
    pct = None
    if working:
        with localcontext() as ctx:
            ctx.prec = 60
            pct = float((present / working * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    return {"emp_code": emp["emp_code"], "month": month, "working_days": working, "present_days": float(present),
            "leave_days": leave, "late_count": late_count, "total_late_minutes": int(late),
            "total_overtime_minutes": int(ot), "attendance_pct": pct}


# =========================================================================== shape, 404 vs empty, validation
class TestContract:
    def test_exact_fields_and_types(self, analytics_api):
        add_emp()
        add_rec("EMP0001", "2026-07-06", late=35, ot=40)
        r = get(analytics_api)
        assert r.status_code == 200
        b = r.json()
        assert list(b) == KEYS
        assert b == {"emp_code": "EMP0001", "month": JUL, "working_days": 23, "present_days": 1.0, "leave_days": 0,
                     "late_count": 1, "total_late_minutes": 35, "total_overtime_minutes": 40, "attendance_pct": 4.35}
        for k in ("working_days", "leave_days", "late_count", "total_late_minutes", "total_overtime_minutes"):
            assert type(b[k]) is int, k
        assert type(b["present_days"]) is float and type(b["attendance_pct"]) is float

    def test_unknown_employee_is_404_with_a_string_detail(self, analytics_api):
        add_emp()
        r = get(analytics_api, "EMP9999")
        assert r.status_code == 404 and isinstance(r.json()["detail"], str)

    @pytest.mark.parametrize("code", ["emp0001", "EMP0001 ", "bad code", "EMP", "0001", "EMP0001%20"])
    def test_emp_code_is_any_string_so_an_unknown_one_is_404_never_422_or_500(self, analytics_api, code):
        add_emp()
        assert get(analytics_api, code).status_code == 404

    def test_employee_without_any_record_is_200_not_404(self, analytics_api):
        add_emp()
        r = get(analytics_api)
        assert r.status_code == 200
        assert r.json() == {"emp_code": "EMP0001", "month": JUL, "working_days": 23, "present_days": 0.0, "leave_days": 0,
                            "late_count": 0, "total_late_minutes": 0, "total_overtime_minutes": 0, "attendance_pct": 0.0}

    def test_real_mongodb_returns_no_row_at_all_for_an_empty_input(self, analytics_api, monkeypatch):
        """mongomock answers an empty $group with a zero row, real MongoDB with nothing. The report must be the
        same all-zero 200 either way (this simulates the real server's answer)."""
        add_emp()
        from tests.analytics_helpers import SpyDB

        monkeypatch.setattr(main, "db", SpyDB(main.db, overrides={("attendance_logs", "aggregate"): lambda *a, **k: iter([])}))
        r = get(analytics_api)
        assert r.status_code == 200
        assert r.json() == {"emp_code": "EMP0001", "month": JUL, "working_days": 23, "present_days": 0.0, "leave_days": 0,
                            "late_count": 0, "total_late_minutes": 0, "total_overtime_minutes": 0, "attendance_pct": 0.0}

    def test_records_of_other_employees_do_not_make_an_unknown_employee_known(self, analytics_api):
        add_emp("EMP0001")
        add_rec("EMP0002", "2026-07-06")                  # a log without an employee document
        assert get(analytics_api, "EMP0002").status_code == 404

    @pytest.mark.parametrize("month", [
        "2026-13", "2026-00", "2026-7", "26-07", "202607", "2026-07-01", "2026/07", "", " 2026-07", "2026-07 ",
        "abcd-ef", "2026-1a", "2026-007", "20266-07", "２０２６-07", "2026-٠٧", "2026-07\n", "July 2026", "2026-W27",
    ])
    def test_invalid_month_is_422(self, analytics_api, month):
        add_emp()
        r = get(analytics_api, month=month)
        assert r.status_code == 422 and isinstance(r.json()["detail"], list)

    def test_missing_month_is_422(self, analytics_api):
        add_emp()
        assert analytics_api.get("/analytics/employees/EMP0001/monthly").status_code == 422

    def test_validation_comes_before_the_employee_lookup(self, analytics_api):
        assert get(analytics_api, "EMP9999", "2026-13").status_code == 422

    @pytest.mark.parametrize("month", ["0001-01", "2026-01", "2026-12", "9999-12", "1970-01"])
    def test_every_pattern_valid_month_is_200(self, analytics_api, month):
        add_emp()
        assert get(analytics_api, month=month).status_code == 200

    def test_year_0000_matches_the_pattern_and_does_not_crash(self, analytics_api):
        add_emp()
        b = get(analytics_api, month="0000-07").json()
        assert (b["working_days"], b["present_days"], b["attendance_pct"]) == (0, 0.0, None)


# =========================================================================== working days / joiners
class TestWorkingDays:
    @pytest.mark.parametrize("joined,expected", [
        ("2026-07-01", 23),   # Wednesday, first day of the month
        ("2026-06-30", 23), ("2025-01-01", 23), ("2000-02-29", 23),
        ("2026-07-10", 16),   # Friday: the 10th, then 13th-31st
        ("2026-07-15", 13),   # Wednesday
        ("2026-07-18", 10),   # Saturday: only the weekdays after it count
        ("2026-07-19", 10),   # Sunday
        ("2026-07-20", 10),   # Monday
        ("2026-07-22", 8),
        ("2026-07-31", 1),    # Friday, last day
        ("2026-08-01", 0),    # Saturday after the month
        ("2026-08-03", 0), ("2030-01-01", 0),
    ])
    def test_counted_from_joined_on_inclusive(self, analytics_api, joined, expected):
        add_emp(joined_on=joined)
        b = get(analytics_api).json()
        assert b["working_days"] == expected
        assert b["attendance_pct"] == (0.0 if expected else None)      # null only when there is no working day
        assert weekdays_of(JUL, joined).__len__() == expected           # the test's own arithmetic agrees

    @pytest.mark.parametrize("joined", ["2026-07-10", "2026-07-15", "2026-07-18", "2026-07-31"])
    def test_joiner_who_attends_every_working_day_has_100_percent(self, analytics_api, joined):
        add_emp(joined_on=joined)
        for d in weekdays_of(JUL, joined):
            add_rec("EMP0001", d.isoformat())
        b = get(analytics_api).json()
        assert b["present_days"] == float(b["working_days"]) == float(len(weekdays_of(JUL, joined)))
        assert b["attendance_pct"] == 100.0

    def test_joiner_is_not_charged_for_the_days_before_joining(self, analytics_api):
        add_emp(joined_on="2026-07-15")
        for d in weekdays_of(JUL, "2026-07-15")[:6]:
            add_rec("EMP0001", d.isoformat())
        b = get(analytics_api).json()
        assert (b["working_days"], b["present_days"], b["attendance_pct"]) == (13, 6.0, 46.15)    # 6/13 = 46.1538

    def test_joined_after_the_month_but_report_still_works_with_no_records(self, analytics_api):
        add_emp(joined_on="2026-09-01")
        b = get(analytics_api).json()
        assert (b["working_days"], b["present_days"], b["attendance_pct"]) == (0, 0.0, None)

    @pytest.mark.parametrize("month,expected", [
        ("2026-07", 23), ("2026-02", 20), ("2024-02", 21), ("2026-03", 22), ("2026-08", 21), ("2026-11", 21),
        ("2026-12", 23), ("2027-01", 21), ("2028-02", 21), ("2100-02", 20),
    ])
    def test_month_lengths_and_leap_years(self, analytics_api, month, expected):
        add_emp(joined_on="2000-01-01")
        assert get(analytics_api, month=month).json()["working_days"] == expected == len(weekdays_of(month))

    def test_the_joined_month_itself_is_prorated_but_later_months_are_full(self, analytics_api):
        add_emp(joined_on="2026-07-15")
        assert get(analytics_api, month="2026-06").json()["working_days"] == 0
        assert get(analytics_api, month="2026-07").json()["working_days"] == 13
        assert get(analytics_api, month="2026-08").json()["working_days"] == 21


# =========================================================================== statuses, half days, weekends
class TestPresenceRules:
    @pytest.mark.parametrize("status,present,leave", [
        ("PRESENT", 1.0, 0), ("WFH", 1.0, 0), ("ON_DUTY", 1.0, 0), ("ABSENT", 0.0, 0), ("LEAVE", 0.0, 1),
    ])
    def test_each_status_on_a_weekday(self, analytics_api, status, present, leave):
        add_emp()
        add_rec("EMP0001", "2026-07-08", status)
        b = get(analytics_api).json()
        assert (b["present_days"], b["leave_days"]) == (present, leave)

    def test_all_five_statuses_together(self, analytics_api):
        add_emp()
        for day, status in zip(("06", "07", "08", "09", "10"), ("PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE")):
            add_rec("EMP0001", f"2026-07-{day}", status)
        b = get(analytics_api).json()
        assert (b["present_days"], b["leave_days"], b["working_days"], b["attendance_pct"]) == (3.0, 1, 23, 13.04)  # 3/23

    @pytest.mark.parametrize("status", ["PRESENT", "WFH", "ON_DUTY"])
    def test_half_day_counts_half_for_every_present_status(self, analytics_api, status):
        add_emp()
        add_rec("EMP0001", "2026-07-06", status, half=True)
        add_rec("EMP0001", "2026-07-07", status, half=False)
        assert get(analytics_api).json()["present_days"] == 1.5

    @pytest.mark.parametrize("status", ["ABSENT", "LEAVE"])
    def test_half_day_flag_on_a_non_present_status_is_ignored(self, analytics_api, status):
        add_emp()
        add_rec("EMP0001", "2026-07-06", status, half=True)
        assert get(analytics_api).json()["present_days"] == 0.0

    def test_present_days_only_counts_weekdays_but_late_and_overtime_count_weekends(self, analytics_api):
        add_emp()
        add_rec("EMP0001", "2026-07-04", "PRESENT", late=20, ot=45)          # Saturday
        add_rec("EMP0001", "2026-07-05", "WFH", late=0, ot=120, half=True)    # Sunday
        add_rec("EMP0001", "2026-07-06", "PRESENT", late=10, ot=30)          # Monday
        b = get(analytics_api).json()
        assert b["present_days"] == 1.0                                       # only Monday
        assert (b["late_count"], b["total_late_minutes"], b["total_overtime_minutes"]) == (2, 30, 195)

    @pytest.mark.parametrize("day", ["2026-07-04", "2026-07-05", "2026-07-11", "2026-07-12", "2026-07-25", "2026-07-26"])
    def test_every_weekend_date_is_excluded_from_present_days(self, analytics_api, day):
        add_emp()
        add_rec("EMP0001", day, "PRESENT")
        assert get(analytics_api).json()["present_days"] == 0.0

    @pytest.mark.parametrize("day", ["2026-07-03", "2026-07-06", "2026-07-10", "2026-07-31", "2026-07-01"])
    def test_weekday_edges_are_included(self, analytics_api, day):
        add_emp()
        add_rec("EMP0001", day, "PRESENT")
        assert get(analytics_api).json()["present_days"] == 1.0

    def test_weekend_working_does_not_raise_the_percentage_above_the_weekday_base(self, analytics_api):
        add_emp()
        for d in weekdays_of(JUL):
            add_rec("EMP0001", d.isoformat())
        for d in days_of(JUL):
            if d.weekday() >= 5:
                add_rec("EMP0001", d.isoformat())
        b = get(analytics_api).json()
        assert (b["present_days"], b["attendance_pct"]) == (23.0, 100.0)

    def test_leave_days_counts_the_leave_records_including_on_a_weekend(self, analytics_api):
        add_emp()
        add_rec("EMP0001", "2026-07-06", "LEAVE")
        add_rec("EMP0001", "2026-07-11", "LEAVE")                              # Saturday
        assert get(analytics_api).json()["leave_days"] == 2

    def test_late_count_counts_records_not_minutes_and_ignores_zero(self, analytics_api):
        add_emp()
        add_rec("EMP0001", "2026-07-06", late=35)
        add_rec("EMP0001", "2026-07-07", late=1)
        add_rec("EMP0001", "2026-07-08", late=0)
        add_rec("EMP0001", "2026-07-09")                                      # missing
        add_rec("EMP0001", "2026-07-10", late=None, **{"late_minutes": None})   # explicit null
        b = get(analytics_api).json()
        assert (b["late_count"], b["total_late_minutes"]) == (2, 36)

    def test_overtime_and_late_of_non_present_records_are_still_summed_as_stored(self, analytics_api):
        add_emp()
        add_rec("EMP0001", "2026-07-06", "ABSENT", late=0, ot=0)
        add_rec("EMP0001", "2026-07-07", "PRESENT", late=5, ot=60)
        b = get(analytics_api).json()
        assert (b["total_late_minutes"], b["total_overtime_minutes"]) == (5, 60)


# =========================================================================== missing optional fields and data hygiene
class TestSeededData:
    def test_present_record_with_only_the_mandatory_fields(self, analytics_api):
        add_emp()
        add_rec("EMP0001", "2026-07-06")
        b = get(analytics_api).json()
        assert (b["present_days"], b["late_count"], b["total_late_minutes"], b["total_overtime_minutes"]) == (1.0, 0, 0, 0)

    def test_null_optional_fields_count_as_zero_and_false(self, analytics_api):
        add_emp()
        add_rec("EMP0001", "2026-07-06", late_minutes=None, overtime_minutes=None, half_day=None, work_hours=None, history=None)
        b = get(analytics_api).json()
        assert (b["present_days"], b["late_count"], b["total_late_minutes"], b["total_overtime_minutes"]) == (1.0, 0, 0, 0)

    def test_absent_and_leave_records_without_punch_times(self, analytics_api):
        add_emp()
        add_rec("EMP0001", "2026-07-06", "ABSENT", punch_in=None, punch_out=None, work_hours=None)
        add_rec("EMP0001", "2026-07-07", "LEAVE")
        b = get(analytics_api).json()
        assert (b["present_days"], b["leave_days"], b["late_count"]) == (0.0, 1, 0)

    def test_float_valued_minutes_still_produce_integers(self, analytics_api):
        add_emp()
        add_rec("EMP0001", "2026-07-06", late=35.0, ot=40.0)
        add_rec("EMP0001", "2026-07-07", late=5.0)
        b = get(analytics_api).json()
        assert (b["total_late_minutes"], b["total_overtime_minutes"], b["late_count"]) == (40, 40, 2)
        assert type(b["total_late_minutes"]) is int and type(b["total_overtime_minutes"]) is int

    def test_stored_values_are_used_not_recomputed(self, analytics_api):
        add_emp()
        # punch times say 4 hours of work, but the stored derived values are what analytics read
        add_rec("EMP0001", "2026-07-06", late=99, ot=77, half=False, work_hours=4.0,
                punch_in=datetime(2026, 7, 6, 3, 30, tzinfo=timezone.utc), punch_out=datetime(2026, 7, 6, 7, 30, tzinfo=timezone.utc))
        b = get(analytics_api).json()
        assert (b["present_days"], b["total_late_minutes"], b["total_overtime_minutes"]) == (1.0, 99, 77)

    def test_the_sample_data_of_the_kit(self, analytics_api):
        from tests.conftest import load_sample
        main.db.employees.insert_many(load_sample("employees"))
        main.db.attendance_logs.insert_many(load_sample("attendance_logs"))
        b = get(analytics_api, "EMP0004").json()               # 07-07 WFH half day (Tue), 07-08 ABSENT (Wed)
        assert (b["present_days"], b["leave_days"], b["late_count"]) == (0.5, 0, 0)
        b = get(analytics_api, "EMP0002").json()               # legacy record on 07-14, no optional fields
        assert b["working_days"] > 0 and b["late_count"] == 0

    def test_only_the_requested_month_is_counted(self, analytics_api):
        add_emp()
        for day in ("2026-06-30", "2026-08-01", "2025-07-06", "2027-07-06"):
            add_rec("EMP0001", day, late=50, ot=50)
        add_rec("EMP0001", "2026-07-01", late=1, ot=1)               # first and last day of the month are included
        add_rec("EMP0001", "2026-07-31", late=2, ot=2)
        b = get(analytics_api).json()
        assert (b["present_days"], b["late_count"], b["total_late_minutes"], b["total_overtime_minutes"]) == (2.0, 2, 3, 3)
        assert get(analytics_api, month="2026-06").json()["total_late_minutes"] == 50
        assert get(analytics_api, month="2026-08").json()["total_late_minutes"] == 50

    def test_only_the_requested_employee_is_counted_even_with_similar_codes(self, analytics_api):
        for code in ("EMP0001", "EMP00010", "EMP0001A", "EMP0002"):
            add_emp(code)
            add_rec(code, "2026-07-06", late=11, ot=22)
        b = get(analytics_api, "EMP0001").json()
        assert (b["present_days"], b["total_late_minutes"], b["total_overtime_minutes"]) == (1.0, 11, 22)

    def test_a_month_with_every_day_recorded(self, analytics_api):
        add_emp()
        for d in days_of(JUL):
            add_rec("EMP0001", d.isoformat(), late=1, ot=2)
        b = get(analytics_api).json()
        assert (b["late_count"], b["total_late_minutes"], b["total_overtime_minutes"], b["present_days"]) == (31, 31, 62, 23.0)


# =========================================================================== rounding
class TestRounding:
    @staticmethod
    def setup_16(api_, units):
        """Joined Friday 2026-07-10 -> 16 working days; `units` half-days of presence (1 unit = 0.5 day)."""
        add_emp(joined_on="2026-07-10")
        days = [d.isoformat() for d in weekdays_of(JUL, "2026-07-10")]
        full, half = divmod(units, 2)
        for d in days[:full]:
            add_rec("EMP0001", d)
        if half:
            add_rec("EMP0001", days[full], half=True)

    @pytest.mark.parametrize("units,expected", [
        (1, 3.13),    # 0.5/16  = 3.125  exactly -> half-up 3.13   (round() and banker's give 3.12)
        (2, 6.25),
        (3, 9.38),    # 1.5/16  = 9.375  exactly
        (5, 15.63),   # 2.5/16  = 15.625 exactly (banker's: 15.62)
        (7, 21.88),   # 3.5/16  = 21.875 exactly
        (9, 28.13),   # 4.5/16  = 28.125 exactly (banker's: 28.12)
        (15, 46.88),  # 7.5/16  = 46.875 exactly
        (16, 50.0),
        (32, 100.0),
        (0, 0.0),
    ])
    def test_exact_ties_round_half_up_with_16_working_days(self, analytics_api, units, expected):
        self.setup_16(analytics_api, units)
        b = get(analytics_api).json()
        assert b["working_days"] == 16 and b["present_days"] == units / 2
        assert b["attendance_pct"] == expected

    def test_python_round_would_have_been_wrong(self):
        assert round(0.5 / 16 * 100, 2) == 3.12 and round(2.5 / 16 * 100, 2) == 15.62      # why banker's rounding is rejected

    @pytest.mark.parametrize("present,expected", [
        (1, 4.35),      # 4.3478
        (0.5, 2.17),    # 2.1739
        (7, 30.43),     # 30.4348
        (12.5, 54.35),  # 54.3478
        (13, 56.52),    # 56.5217
        (22, 95.65),    # 95.6522
        (22.5, 97.83),  # 97.8261
        (23, 100.0),
    ])
    def test_non_tie_values_with_23_working_days(self, analytics_api, present, expected):
        add_emp()
        days = [d.isoformat() for d in weekdays_of(JUL)]
        full, half = divmod(int(present * 2), 2)
        for d in days[:full]:
            add_rec("EMP0001", d)
        if half:
            add_rec("EMP0001", days[full], half=True)
        b = get(analytics_api).json()
        assert b["present_days"] == present and b["attendance_pct"] == expected

    @pytest.mark.parametrize("units,expected", [(0, 0.0), (1, 50.0), (2, 100.0)])
    def test_single_working_day(self, analytics_api, units, expected):
        add_emp(joined_on="2026-07-31")
        if units:
            add_rec("EMP0001", "2026-07-31", half=(units == 1) or None)
        assert get(analytics_api).json()["attendance_pct"] == expected

    def test_result_is_the_exact_double_of_the_two_decimal_value(self, analytics_api):
        self.setup_16(analytics_api, 1)
        text = get(analytics_api).text
        assert '"attendance_pct":3.13' in text.replace(" ", "")

    @pytest.mark.parametrize("units,wd", [(u, w) for w in (1, 7, 8, 13, 16, 20, 21, 22, 23) for u in (0, 1, 2, 3, w, 2 * w - 1, 2 * w)])
    def test_helper_matches_exact_decimal_arithmetic(self, units, wd):
        with localcontext() as ctx:
            ctx.prec = 60
            want = float((Decimal(units) / 2 / wd * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        assert main.attendance_pct(units, wd) == want

    def test_helper_returns_none_without_working_days(self):
        assert main.attendance_pct(0, 0) is None and main.attendance_pct(6, 0) is None


# =========================================================================== pipeline shape
class TestPipeline:
    def test_stage_shape_is_index_friendly_and_single_pass(self):
        pipe = main.build_employee_monthly_pipeline("EMP0001", JUL)
        assert [next(iter(st)) for st in pipe] == ["$match", "$group"]
        match = pipe[0]["$match"]
        assert set(match) == {"emp_code", "date"}                                # exactly the unique index (emp_code, date)
        assert match["emp_code"] == "EMP0001" and match["date"] == {"$gte": "2026-07-01", "$lte": "2026-07-31"}
        assert "$lookup" not in json.dumps(pipe) and "$unwind" not in json.dumps(pipe)
        assert pipe[1]["$group"]["_id"] is None

    def test_weekday_list_given_to_the_database(self):
        assert main.weekday_dates(JUL) == [d.isoformat() for d in weekdays_of(JUL)] and len(main.weekday_dates(JUL)) == 23
        assert main.weekday_dates("2026-02")[0] == "2026-02-02" and main.weekday_dates("2026-02")[-1] == "2026-02-27"

    def test_the_unique_index_that_backs_the_match_exists(self, analytics_api):
        keys = [v["key"] for v in main.db.attendance_logs.index_information().values()]
        assert [("emp_code", 1), ("date", 1)] in keys


# =========================================================================== independent oracle on random data
class TestAgainstTheOracle:
    MONTHS = ["2024-02", "2026-02", "2026-07", "2026-12", "2027-01", "2026-05"]
    STATUSES = ["PRESENT", "PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]

    def test_randomised_months_joiners_and_records(self, analytics_api):
        rng = random.Random(20260710)
        for n in range(60):
            main.db.attendance_logs.delete_many({})
            main.db.employees.delete_many({})
            month = rng.choice(self.MONTHS)
            first, last = days_of(month)[0], days_of(month)[-1]
            joined = (first + timedelta(days=rng.randint(-40, (last - first).days + 6))).isoformat()
            emp = {"emp_code": "EMP0001", "joined_on": joined}
            add_emp("EMP0001", joined)
            records = []
            for k in range(-4, (last - first).days + 5):
                d = first + timedelta(days=k)
                if d.isoformat() < joined or rng.random() > 0.7:
                    continue
                fields = {}
                if rng.random() < 0.6:
                    fields["late"] = rng.choice([0, 1, 10, 11, 35, 150, 35.0])
                if rng.random() < 0.5:
                    fields["ot"] = rng.choice([0, 30, 45, 120])
                if rng.random() < 0.5:
                    fields["half"] = rng.random() < 0.4
                status = rng.choice(self.STATUSES)
                add_rec("EMP0001", d.isoformat(), status, **fields)
                records.append({"emp_code": "EMP0001", "date": d.isoformat(), "status": status, "late_minutes": fields.get("late"),
                                "overtime_minutes": fields.get("ot"), "half_day": fields.get("half")})
            # noise that must never leak in: another employee in the same month
            add_emp("EMP0002", "2020-01-01")
            for d in weekdays_of(month)[:5]:
                add_rec("EMP0002", d.isoformat(), late=99, ot=99)
            assert get(analytics_api, month=month).json() == oracle(emp, records, month), (n, month, joined)

    def test_a_busy_collection_still_gives_the_same_answer(self, analytics_api):
        rng = random.Random(7)
        docs, mine = [], []
        for e in range(120):
            code = f"EMP{e:04d}"
            add_emp(code, "2026-01-05")
            for d in days_of(JUL):
                if rng.random() < 0.8:
                    doc = {"emp_code": code, "date": d.isoformat(), "status": rng.choice(self.STATUSES),
                           "late_minutes": rng.choice([0, 12, 40]), "overtime_minutes": rng.choice([0, 35]), "half_day": rng.random() < 0.2}
                    docs.append(doc)
                    if e == 57:
                        mine.append(doc)
        main.db.attendance_logs.insert_many([dict(d) for d in docs])
        b = get(analytics_api, "EMP0057").json()
        assert b == oracle({"emp_code": "EMP0057", "joined_on": "2026-01-05"}, mine, JUL)


# =========================================================================== real MongoDB only (planner and volume)
def _max_of(node, key):
    found = []

    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                if k == key and isinstance(v, (int, float)):
                    found.append(v)
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(node)
    return max(found) if found else None


def explain_aggregate(pipeline):
    return main.db.command({"explain": {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
                            "verbosity": "executionStats"})


class TestOnRealMongo:
    def test_helper_finds_the_counter_in_nested_explain_output(self):
        assert _max_of({"stages": [{"$cursor": {"executionStats": {"totalDocsExamined": 31}}}], "x": [{"totalDocsExamined": 5}]}, "totalDocsExamined") == 31

    def test_explain_uses_the_index_and_never_scans_the_collection(self, api, real_mongo):
        main.db.employees.insert_one({"emp_code": "EMP0001"})
        docs = [{"emp_code": f"EMP{e:04d}", "date": d.isoformat(), "status": "PRESENT", "late_minutes": 0}
                for e in range(300) for d in days_of("2026-06") + days_of(JUL) + days_of("2026-08")]
        main.db.attendance_logs.insert_many(docs)                                   # ~28,000 documents
        plan = json.dumps(explain_aggregate(main.build_employee_monthly_pipeline("EMP0001", JUL)), default=str)
        assert "IXSCAN" in plan and "COLLSCAN" not in plan
        examined = _max_of(json.loads(plan), "totalDocsExamined")
        assert examined is not None and examined <= 31                              # one employee-month, not the collection

    def test_large_collection_gives_the_oracle_answer_through_the_endpoint(self, api, real_mongo):
        rng = random.Random(3)
        docs = []
        for e in range(400):
            add_emp(f"EMP{e:04d}", "2026-01-05")
        for e in range(400):
            for d in days_of(JUL):
                if rng.random() < 0.9:
                    docs.append({"emp_code": f"EMP{e:04d}", "date": d.isoformat(), "status": rng.choice(self.STATUSES),
                                 "late_minutes": rng.choice([0, 12]), "overtime_minutes": rng.choice([0, 40]), "half_day": rng.random() < 0.1})
        main.db.attendance_logs.insert_many([dict(d) for d in docs])
        mine = [d for d in docs if d["emp_code"] == "EMP0123"]
        assert get(api, "EMP0123").json() == oracle({"emp_code": "EMP0123", "joined_on": "2026-01-05"}, mine, JUL)

    STATUSES = TestAgainstTheOracle.STATUSES

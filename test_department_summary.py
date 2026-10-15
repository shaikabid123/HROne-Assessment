"""GET /analytics/departments/summary. Worked examples are July 2026 (see analytics_helpers)."""
import json
import random
from decimal import ROUND_HALF_UP, Decimal, localcontext

import pytest

from app import main
from tests.analytics_helpers import JUL, PRESENT, add_emp, add_rec, days_of, weekdays_of

ITEM_KEYS = ["department", "headcount", "present_days", "avg_work_hours", "late_count", "total_late_minutes", "leave_count", "on_duty_count"]


def get(api, month=JUL, **params):
    return api.get("/analytics/departments/summary", params={"month": month, **params})


def items(api, month=JUL, **params):
    r = get(api, month, **params)
    assert r.status_code == 200, r.text
    return r.json()["items"]


def one(api, month=JUL, **params):
    (item,) = items(api, month, **params)
    return item


# --------------------------------------------------------------------------- independent oracle
def oracle(employees, logs, month, department=None):
    first, last = days_of(month)[0].isoformat(), days_of(month)[-1].isoformat()
    scope = {e["emp_code"]: e["department"] for e in employees
             if e["joined_on"] <= last and (department is None or e["department"] == department)}
    out = {}
    for dept in sorted(set(scope.values())):
        out[dept] = dict(headcount=sum(1 for d in scope.values() if d == dept), present=Decimal(0), hours=[], late_count=0,
                         total_late=0, leave=0, on_duty=0)
    for r in logs:
        if r["emp_code"] not in scope or not first <= r["date"] <= last:
            continue
        o = out[scope[r["emp_code"]]]
        weekday = days_of(month)[int(r["date"][8:10]) - 1].weekday() < 5
        if r["status"] in PRESENT and weekday:
            o["present"] += Decimal("0.5") if r.get("half_day") else Decimal(1)
        if r["status"] in PRESENT and r.get("work_hours") is not None:
            o["hours"].append(Decimal(str(r["work_hours"])))
        minutes = r.get("late_minutes") or 0
        o["total_late"] += minutes
        o["late_count"] += minutes > 0
        o["leave"] += r["status"] == "LEAVE"
        o["on_duty"] += r["status"] == "ON_DUTY"
    result = []
    for dept, o in out.items():
        avg = None
        if o["hours"]:
            with localcontext() as ctx:
                ctx.prec = 60
                avg = float((sum(o["hours"]) / len(o["hours"])).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        result.append({"department": dept, "headcount": o["headcount"], "present_days": float(o["present"]), "avg_work_hours": avg,
                       "late_count": o["late_count"], "total_late_minutes": int(o["total_late"]), "leave_count": o["leave"],
                       "on_duty_count": o["on_duty"]})
    return result


# =========================================================================== a scenario worked out by hand
@pytest.fixture()
def scenario(analytics_api):
    for code in ("E1", "E2", "E3"):
        add_emp(code, "Engineering")
    add_emp("E4", "Engineering", joined_on="2026-08-01")          # joins after the month: not counted
    add_emp("E5", "Engineering", joined_on="2026-07-31")          # joins on the last day, no logs: counted
    add_emp("S1", "Sales")
    add_emp("S2", "Sales", joined_on="2026-09-01")
    add_emp("O1", "Ops", joined_on="2026-08-15")                  # a department of future joiners only
    add_rec("E1", "2026-07-06", "PRESENT", late=10, hours=9.0)
    add_rec("E1", "2026-07-07", "WFH", half=True, hours=8.0)
    add_rec("E1", "2026-07-08", "LEAVE")
    add_rec("E1", "2026-07-11", "PRESENT", late=20, ot=30, hours=4.0)             # Saturday
    add_rec("E2", "2026-07-06", "ON_DUTY", hours=10.0)
    add_rec("E2", "2026-07-07", "ABSENT", hours=5.0)                              # not present: its hours must not count
    add_rec("E2", "2026-07-09", "PRESENT", late=0)                                # no work_hours at all
    add_rec("S1", "2026-07-06", "PRESENT", hours=8.0)
    return analytics_api


class TestWorkedScenario:
    def test_exact_response(self, scenario):
        r = get(scenario)
        assert r.status_code == 200
        body = r.json()
        assert list(body) == ["month", "items"] and body["month"] == JUL
        assert body["items"] == [
            # present: E1 Mon 1 + Tue half 0.5 (the Saturday is not counted) + E2 Mon 1 + Thu 1 = 3.5
            # hours:   E1 9 + 8 + 4 (weekend records count) + E2 10 = 31 over 4 records = 7.75
            # late:    E1 Mon + E1 Sat = 2 records, 30 minutes; leave: E1 Wed; on duty: E2 Mon
            {"department": "Engineering", "headcount": 4, "present_days": 3.5, "avg_work_hours": 7.75, "late_count": 2,
             "total_late_minutes": 30, "leave_count": 1, "on_duty_count": 1},
            {"department": "Sales", "headcount": 1, "present_days": 1.0, "avg_work_hours": 8.0, "late_count": 0,
             "total_late_minutes": 0, "leave_count": 0, "on_duty_count": 0},
        ]
        assert [list(i) for i in body["items"]] == [ITEM_KEYS, ITEM_KEYS]

    def test_types(self, scenario):
        for i in items(scenario):
            for k in ("headcount", "late_count", "total_late_minutes", "leave_count", "on_duty_count"):
                assert type(i[k]) is int, k
            assert type(i["present_days"]) is float and type(i["avg_work_hours"]) is float

    def test_department_of_future_joiners_only_is_omitted(self, scenario):
        assert "Ops" not in [i["department"] for i in items(scenario)]

    def test_the_same_data_one_month_earlier(self, scenario):
        # June 2026: everybody in the scenario joined later than June except nobody -> E1.. joined 2026-01-05 are in
        got = {i["department"]: i["headcount"] for i in items(scenario, "2026-06")}
        assert got == {"Engineering": 3, "Sales": 1}                         # E5 (07-31), E4, S2, O1 have not joined yet


# =========================================================================== headcount
class TestHeadcount:
    @pytest.mark.parametrize("joined,counted", [
        ("2026-07-31", True), ("2026-07-01", True), ("2026-06-30", True), ("2020-01-01", True),
        ("2026-08-01", False), ("2026-08-03", False), ("2030-12-31", False),
    ])
    def test_joined_on_or_before_the_end_of_the_month(self, analytics_api, joined, counted):
        add_emp("E1", "Engineering", joined_on=joined)
        assert [i["headcount"] for i in items(analytics_api)] == ([1] if counted else [])

    def test_employees_without_any_log_are_counted(self, analytics_api):
        for n in range(5):
            add_emp(f"E{n}")
        i = one(analytics_api)
        assert (i["headcount"], i["present_days"], i["avg_work_hours"], i["late_count"], i["total_late_minutes"],
                i["leave_count"], i["on_duty_count"]) == (5, 0.0, None, 0, 0, 0, 0)

    def test_headcount_does_not_depend_on_the_number_of_records(self, analytics_api):
        add_emp("E1")
        add_emp("E2")
        add_emp("E3")
        for d in weekdays_of(JUL):
            add_rec("E1", d.isoformat(), hours=8.0)                        # 23 records for one employee
        for d in days_of(JUL)[:5]:
            add_rec("E2", d.isoformat(), "LEAVE")                           # 5 records for another, E3 has none
        assert one(analytics_api)["headcount"] == 3

    def test_each_department_counts_its_own_employees(self, analytics_api):
        for code, dept in (("A1", "Alpha"), ("A2", "Alpha"), ("B1", "Beta"), ("C1", "Gamma"), ("C2", "Gamma"), ("C3", "Gamma")):
            add_emp(code, dept)
        assert {i["department"]: i["headcount"] for i in items(analytics_api)} == {"Alpha": 2, "Beta": 1, "Gamma": 3}

    def test_logs_of_an_employee_who_joins_later_are_not_counted_either(self, analytics_api):
        add_emp("E1")
        add_emp("E2", joined_on="2026-08-01")                               # inconsistent data: a July log before joining
        add_rec("E1", "2026-07-06", late=5, hours=8.0)
        add_rec("E2", "2026-07-06", late=99, hours=1.0)
        i = one(analytics_api)
        assert (i["headcount"], i["present_days"], i["total_late_minutes"], i["avg_work_hours"]) == (1, 1.0, 5, 8.0)

    def test_logs_of_employees_that_do_not_exist_are_ignored(self, analytics_api):
        add_emp("E1")
        add_rec("E1", "2026-07-06", late=5)
        add_rec("GHOST", "2026-07-06", late=500, hours=20.0)
        i = one(analytics_api)
        assert (i["headcount"], i["present_days"], i["total_late_minutes"], i["avg_work_hours"]) == (1, 1.0, 5, None)

    def test_the_join_uses_emp_code_and_not_the_document_id(self, analytics_api):
        # an _id join would attach each log to the wrong employee because the ids are shared on purpose
        main.db.employees.insert_many([
            {"_id": "id-1", "emp_code": "E1", "name": "n", "email": "e@x.com", "department": "Alpha", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-05"},
            {"_id": "id-2", "emp_code": "E2", "name": "n", "email": "e@x.com", "department": "Beta", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-05"}])
        main.db.attendance_logs.insert_many([
            {"_id": "id-1", "emp_code": "E2", "date": "2026-07-06", "status": "PRESENT", "late_minutes": 40},
            {"_id": "id-2", "emp_code": "E1", "date": "2026-07-06", "status": "LEAVE"}])
        got = {i["department"]: i for i in items(analytics_api)}
        assert got["Beta"]["total_late_minutes"] == 40 and got["Beta"]["present_days"] == 1.0 and got["Beta"]["leave_count"] == 0
        assert got["Alpha"]["total_late_minutes"] == 0 and got["Alpha"]["leave_count"] == 1 and got["Alpha"]["present_days"] == 0.0


# =========================================================================== present_days
class TestPresentDays:
    @pytest.mark.parametrize("status,expected", [("PRESENT", 1.0), ("WFH", 1.0), ("ON_DUTY", 1.0), ("ABSENT", 0.0), ("LEAVE", 0.0)])
    def test_each_status(self, analytics_api, status, expected):
        add_emp("E1")
        add_rec("E1", "2026-07-08", status)
        assert one(analytics_api)["present_days"] == expected

    @pytest.mark.parametrize("status", ["PRESENT", "WFH", "ON_DUTY"])
    def test_half_day_counts_half(self, analytics_api, status):
        add_emp("E1")
        add_rec("E1", "2026-07-06", status, half=True)
        add_rec("E1", "2026-07-07", status, half=False)
        assert one(analytics_api)["present_days"] == 1.5

    @pytest.mark.parametrize("status", ["ABSENT", "LEAVE"])
    def test_half_day_flag_on_a_non_present_record_is_ignored(self, analytics_api, status):
        add_emp("E1")
        add_rec("E1", "2026-07-06", status, half=True)
        assert one(analytics_api)["present_days"] == 0.0

    @pytest.mark.parametrize("day", ["2026-07-04", "2026-07-05", "2026-07-11", "2026-07-12", "2026-07-25", "2026-07-26"])
    def test_weekend_records_are_not_present_days(self, analytics_api, day):
        add_emp("E1")
        add_rec("E1", day, "PRESENT", late=10, ot=20, hours=8.0)
        i = one(analytics_api)
        assert i["present_days"] == 0.0
        assert (i["late_count"], i["total_late_minutes"], i["avg_work_hours"]) == (1, 10, 8.0)   # but they count elsewhere

    def test_summed_over_every_employee_of_the_department(self, analytics_api):
        for n, days in enumerate((2, 3, 0, 5)):
            add_emp(f"E{n}")
            for d in weekdays_of(JUL)[:days]:
                add_rec(f"E{n}", d.isoformat())
        add_rec("E2", "2026-07-06", half=True)
        assert one(analytics_api)["present_days"] == 10.5


# =========================================================================== avg_work_hours
class TestAverageWorkHours:
    def test_mean_over_records_not_over_employees(self, analytics_api):
        add_emp("A")
        add_emp("B")
        add_rec("A", "2026-07-06", hours=10.0)                             # A: one record of 10
        for d in ("2026-07-07", "2026-07-08", "2026-07-09"):
            add_rec("B", d, hours=6.0)                                     # B: three records of 6
        # records: (10 + 6 + 6 + 6) / 4 = 7.0 ; the mean of the two employee means would be (10 + 6) / 2 = 8.0
        assert one(analytics_api)["avg_work_hours"] == 7.0

    @pytest.mark.parametrize("status,qualifies", [("PRESENT", True), ("WFH", True), ("ON_DUTY", True), ("ABSENT", False), ("LEAVE", False)])
    def test_only_present_status_records_qualify(self, analytics_api, status, qualifies):
        add_emp("E1")
        add_rec("E1", "2026-07-06", "PRESENT", hours=8.0)
        add_rec("E1", "2026-07-07", status, hours=4.0)
        assert one(analytics_api)["avg_work_hours"] == (6.0 if qualifies else 8.0)

    def test_records_without_a_work_hours_value_are_skipped(self, analytics_api):
        add_emp("E1")
        add_rec("E1", "2026-07-06", hours=9.0)
        add_rec("E1", "2026-07-07")                                        # field missing (an open punch-in)
        add_rec("E1", "2026-07-08", work_hours=None)                       # explicit null
        assert one(analytics_api)["avg_work_hours"] == 9.0

    def test_zero_hours_is_a_value(self, analytics_api):
        add_emp("E1")
        add_rec("E1", "2026-07-06", hours=0.0)
        add_rec("E1", "2026-07-07", hours=8.0)
        assert one(analytics_api)["avg_work_hours"] == 4.0

    def test_weekend_records_are_included(self, analytics_api):
        add_emp("E1")
        add_rec("E1", "2026-07-06", hours=8.0)
        add_rec("E1", "2026-07-11", hours=4.0)                             # Saturday
        assert one(analytics_api)["avg_work_hours"] == 6.0

    def test_null_when_nothing_qualifies(self, analytics_api):
        add_emp("E1")
        add_emp("E2")
        add_rec("E1", "2026-07-06", "ABSENT", hours=8.0)
        add_rec("E2", "2026-07-06", "PRESENT")
        assert one(analytics_api)["avg_work_hours"] is None

    @pytest.mark.parametrize("values,expected", [
        ([9.12, 9.13], 9.13),                  # 9.125 exactly: half-up (banker's and float noise give 9.12)
        ([1.0, 1.0, 1.0, 1.02], 1.01),         # 1.005 exactly
        ([0.10, 0.15], 0.13),                  # 0.125
        ([0.07, 0.08], 0.08), ([0.05, 0.06], 0.06), ([7.00, 7.01], 7.01),
        ([8.01, 8.02, 8.02], 8.02),            # 8.01666
        ([7.00, 7.00, 7.01], 7.0),             # 7.00333
        ([8.5], 8.5), ([8.5, 8.5, 8.5], 8.5),
        ([4.49, 4.50], 4.5),                   # 4.495 -> 4.50
        ([3.33, 3.33, 3.34], 3.33),            # 3.33333
        ([10.005 * 0 + 1.11, 2.22], 1.67),     # 1.665 -> 1.67
    ])
    def test_rounding_half_up_at_two_decimals(self, analytics_api, values, expected):
        add_emp("E1")
        for d, v in zip(days_of(JUL), values):
            add_rec("E1", d.isoformat(), hours=v)
        assert one(analytics_api)["avg_work_hours"] == expected

    def test_the_two_decimal_mean_is_computed_exactly_over_many_records(self, analytics_api):
        add_emp("E1")
        add_emp("E2")
        values = [round(7 + (n * 37 % 400) / 100, 2) for n in range(60)]            # 60 different 2-decimal values
        for n, v in enumerate(values):
            add_rec("E1" if n % 2 else "E2", f"2026-07-{n // 2 + 1:02d}", hours=v)
        with localcontext() as ctx:
            ctx.prec = 60
            want = float((sum(Decimal(str(v)) for v in values) / 60).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        assert one(analytics_api)["avg_work_hours"] == want

    def test_python_round_would_have_been_wrong(self):
        assert round((9.12 + 9.13) / 2, 2) == 9.12 and round(1.005, 2) == 1.0          # why banker's / float rounding is rejected

    @pytest.mark.parametrize("total,n,expected", [(0, 0, None), (1825, 2, 9.13), (402, 4, 1.01), (12, 1, 0.12), (0, 3, 0.0), (1, 2, 0.01)])
    def test_helper(self, total, n, expected):
        assert main.average_hours(total, n) == expected


# =========================================================================== late / leave / on-duty counts
class TestCounts:
    def test_late_count_and_minutes_cover_every_record_including_weekends_and_non_present_ones(self, analytics_api):
        add_emp("E1")
        add_emp("E2")
        add_rec("E1", "2026-07-06", late=35)
        add_rec("E1", "2026-07-07", late=1)
        add_rec("E1", "2026-07-08", late=0)
        add_rec("E1", "2026-07-09")                                         # missing
        add_rec("E1", "2026-07-10", late_minutes=None)                      # null
        add_rec("E2", "2026-07-04", late=20)                                # Saturday
        add_rec("E2", "2026-07-06", "ABSENT", late=7)                       # stored as is
        i = one(analytics_api)
        assert (i["late_count"], i["total_late_minutes"]) == (4, 63)

    def test_leave_and_on_duty_cover_weekends_too(self, analytics_api):
        add_emp("E1")
        add_rec("E1", "2026-07-06", "LEAVE")
        add_rec("E1", "2026-07-11", "LEAVE")                                # Saturday
        add_rec("E1", "2026-07-07", "ON_DUTY")
        add_rec("E1", "2026-07-12", "ON_DUTY", half=True)                   # Sunday
        add_rec("E1", "2026-07-08", "WFH")
        i = one(analytics_api)
        assert (i["leave_count"], i["on_duty_count"]) == (2, 2)
        assert i["present_days"] == 2.0                                     # ON_DUTY Tue + WFH Wed; the Sunday half day is a weekend

    def test_float_valued_minutes_still_give_integers(self, analytics_api):
        add_emp("E1")
        add_rec("E1", "2026-07-06", late=35.0)
        add_rec("E1", "2026-07-07", late=5.0)
        i = one(analytics_api)
        assert (i["total_late_minutes"], i["late_count"]) == (40, 2) and type(i["total_late_minutes"]) is int

    def test_missing_optional_fields_everywhere(self, analytics_api):
        add_emp("E1")
        add_rec("E1", "2026-07-06")                                         # status only
        add_rec("E1", "2026-07-07", late_minutes=None, overtime_minutes=None, half_day=None, work_hours=None, history=None)
        i = one(analytics_api)
        assert (i["present_days"], i["avg_work_hours"], i["late_count"], i["total_late_minutes"]) == (2.0, None, 0, 0)


# =========================================================================== month, filter, ordering, validation
class TestMonthDepartmentAndOrdering:
    def test_only_the_requested_month_is_counted_and_its_first_and_last_day_are_included(self, analytics_api):
        add_emp("E1")
        for day in ("2026-06-30", "2026-08-01", "2025-07-06", "2027-07-06"):
            add_rec("E1", day, late=50, hours=1.0)
        add_rec("E1", "2026-07-01", late=1, hours=8.0)
        add_rec("E1", "2026-07-31", late=2, hours=10.0)
        i = one(analytics_api)
        assert (i["present_days"], i["late_count"], i["total_late_minutes"], i["avg_work_hours"]) == (2.0, 2, 3, 9.0)

    def test_departments_are_sorted_ascending(self, analytics_api):
        for n, dept in enumerate(("Sales", "Engineering", "HR", "Ops-1", "Zeta", "alpha")):
            add_emp(f"E{n}", dept)
        assert [i["department"] for i in items(analytics_api)] == ["Engineering", "HR", "Ops-1", "Sales", "Zeta", "alpha"]   # by code point

    def test_department_filter_is_exact_and_case_sensitive(self, scenario):
        assert [i["department"] for i in items(scenario, department="Sales")] == ["Sales"]
        assert items(scenario, department="sales") == [] and items(scenario, department="Sal") == []
        assert items(scenario, department="Nope") == []
        assert items(scenario, department="Ops") == []                      # exists, but nobody has joined yet
        assert items(scenario, department="") == []

    def test_filtering_does_not_change_a_departments_numbers(self, scenario):
        everything = {i["department"]: i for i in items(scenario)}
        for dept in ("Engineering", "Sales"):
            assert items(scenario, department=dept) == [everything[dept]]

    def test_no_employees_gives_an_empty_list(self, analytics_api):
        r = get(analytics_api)
        assert r.status_code == 200 and r.json() == {"month": JUL, "items": []}

    def test_everyone_joined_later_gives_an_empty_list(self, analytics_api):
        add_emp("E1", joined_on="2026-09-01")
        add_rec("E1", "2026-07-06")
        assert items(analytics_api) == []

    @pytest.mark.parametrize("month", ["2026-13", "2026-00", "2026-7", "26-07", "202607", "2026-07-01", "", " 2026-07", "２０２６-07", "2026-07\n"])
    def test_invalid_month_is_422(self, analytics_api, month):
        r = get(analytics_api, month)
        assert r.status_code == 422 and isinstance(r.json()["detail"], list)

    def test_missing_month_is_422(self, analytics_api):
        assert analytics_api.get("/analytics/departments/summary").status_code == 422

    @pytest.mark.parametrize("month", ["0001-01", "0000-07", "2026-12", "9999-12"])
    def test_every_pattern_valid_month_is_200(self, analytics_api, month):
        add_emp("E1")
        assert get(analytics_api, month).status_code == 200

    def test_the_sample_data_of_the_kit(self, analytics_api):
        from tests.conftest import load_sample

        emps, logs = load_sample("employees"), load_sample("attendance_logs")
        main.db.employees.insert_many([dict(e) for e in emps])
        main.db.attendance_logs.insert_many([dict(l) for l in logs])
        assert items(analytics_api) == oracle(emps, logs, JUL)


# =========================================================================== pipeline shape
class TestPipeline:
    def test_stages(self):
        pipe = main.build_department_summary_pipeline(JUL)
        assert [next(iter(s)) for s in pipe] == ["$match", "$lookup", "$set", "$group", "$sort"]
        assert pipe[0]["$match"] == {"joined_on": {"$lte": "2026-07-31"}}
        lookup = pipe[1]["$lookup"]
        assert (lookup["from"], lookup["localField"], lookup["foreignField"]) == ("attendance_logs", "emp_code", "emp_code")
        assert "_id" not in (lookup["localField"], lookup["foreignField"])
        assert next(iter(lookup["pipeline"][0])) == "$match" and lookup["pipeline"][0]["$match"] == {"date": {"$gte": "2026-07-01", "$lte": "2026-07-31"}}
        assert pipe[3]["$group"]["_id"] == "$department" and pipe[3]["$group"]["headcount"] == {"$sum": 1}
        assert pipe[4] == {"$sort": {"_id": 1}}

    def test_department_filter_goes_into_the_first_stage(self):
        assert main.build_department_summary_pipeline(JUL, "Sales")[0]["$match"] == {"joined_on": {"$lte": "2026-07-31"}, "department": "Sales"}

    def test_the_endpoint_makes_exactly_one_aggregation_and_fetches_no_records(self, analytics_api, monkeypatch):
        """A spy between the app and the database sees only the APPLICATION's calls (the engine's own internal calls
        go to the real collections), so this proves that the work is not done by looping over fetched records."""
        add_emp("E1")
        add_rec("E1", "2026-07-06", hours=8.0)
        calls = []

        class SpyCollection:
            def __init__(self, coll, name):
                self._coll, self._name = coll, name

            def __getattr__(self, attr):
                target = getattr(self._coll, attr)
                if not callable(target):
                    return target

                def recorded(*a, **k):
                    calls.append((self._name, attr))
                    return target(*a, **k)

                return recorded

        class SpyDB:
            def __init__(self, db):
                self._db = db

            def __getattr__(self, name):
                return SpyCollection(getattr(self._db, name), name)

        monkeypatch.setattr(main, "db", SpyDB(main.db))
        assert get(analytics_api).status_code == 200
        assert calls == [("employees", "aggregate")]

    def test_indexes_behind_the_pipeline_exist(self, analytics_api):
        emp = [v["key"] for v in main.db.employees.index_information().values()]
        att = [v["key"] for v in main.db.attendance_logs.index_information().values()]
        assert [("joined_on", 1)] in emp and [("department", 1), ("emp_code", 1)] in emp
        assert [("emp_code", 1), ("date", 1)] in att


# =========================================================================== independent oracle
class TestAgainstTheOracle:
    DEPTS = ["Engineering", "Sales", "HR", "Ops"]
    STATUSES = ["PRESENT", "PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]

    def populate(self, rng, month, n_emp):
        first, last = days_of(month)[0], days_of(month)[-1]
        employees, logs = [], []
        for n in range(n_emp):
            code = f"EMP{n:04d}"
            joined = days_of("2026-05")[0] if rng.random() < 0.5 else first.replace(day=1)
            span = (last - first).days + 12
            joined = (first.fromordinal(first.toordinal() + rng.randint(-30, span))).isoformat()
            emp = {"emp_code": code, "department": rng.choice(self.DEPTS), "joined_on": joined}
            employees.append(emp)
            add_emp(code, emp["department"], joined)
            for k in range(-3, (last - first).days + 4):
                day = first.fromordinal(first.toordinal() + k).isoformat()
                if rng.random() > 0.55:
                    continue
                rec = {"emp_code": code, "date": day, "status": rng.choice(self.STATUSES)}
                if rng.random() < 0.6:
                    rec["late_minutes"] = rng.choice([0, 1, 10, 35, 150, 35.0])
                if rng.random() < 0.5:
                    rec["half_day"] = rng.random() < 0.4
                if rng.random() < 0.7:
                    rec["work_hours"] = rng.choice([0.0, 4.49, 4.5, 8.5, 9.12, 9.13, 7.99, 10.05, round(rng.uniform(1, 12), 2)])
                logs.append(rec)
                main.db.attendance_logs.insert_one(dict(rec))
        for g in range(4):                                                  # logs of employees that do not exist
            doc = {"emp_code": f"GHOST{g}", "date": first.isoformat(), "status": "PRESENT", "late_minutes": 77, "work_hours": 9.9}
            main.db.attendance_logs.insert_one(doc)
        return employees, logs

    def test_randomised_departments_joiners_and_records(self, analytics_api):
        rng = random.Random(20260711)
        for n in range(40):
            main.db.employees.delete_many({})
            main.db.attendance_logs.delete_many({})
            month = rng.choice(["2026-07", "2026-02", "2024-02", "2026-12"])
            employees, logs = self.populate(rng, month, rng.randint(1, 14))
            assert items(analytics_api, month) == oracle(employees, logs, month), (n, month)
            dept = rng.choice(self.DEPTS)
            assert items(analytics_api, month, department=dept) == oracle(employees, logs, month, dept), (n, month, dept)


# =========================================================================== real MongoDB only
def _values_of(node, key):
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
    return found


class TestOnRealMongo:
    def test_helper_finds_counters_in_nested_explain_output(self):
        assert _values_of({"stages": [{"$cursor": {"totalDocsExamined": 5}}, {"$lookup": {"totalDocsExamined": 31, "collectionScans": 0}}]}, "totalDocsExamined") == [5, 31]

    def test_explain_uses_indexes_and_the_lookup_only_reads_the_month(self, api, real_mongo):
        for e in range(200):
            add_emp(f"EMP{e:04d}", ["A", "B", "C", "D"][e % 4])
        main.db.attendance_logs.insert_many([{"emp_code": f"EMP{e:04d}", "date": d.isoformat(), "status": "PRESENT", "work_hours": 8.0}
                                             for e in range(200) for m in ("2026-06", JUL, "2026-08") for d in days_of(m)])
        plan = main.db.command({"explain": {"aggregate": "employees", "pipeline": main.build_department_summary_pipeline(JUL), "cursor": {}}, "verbosity": "executionStats"})
        text = json.dumps(plan, default=str)
        assert "COLLSCAN" not in text and "IXSCAN" in text
        assert all(v == 0 for v in _values_of(plan, "collectionScans"))
        assert max(_values_of(plan, "totalDocsExamined")) <= 200 * 31 + 200      # one month of logs, not all three

    def test_large_collection_matches_the_oracle(self, api, real_mongo):
        rng = random.Random(5)
        employees, logs = TestAgainstTheOracle().populate(rng, JUL, 300)
        assert items(api) == oracle(employees, logs, JUL)

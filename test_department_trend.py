"""GET /analytics/departments/{department}/trend.

July 2026: Mon 6, Tue 7, Wed 8, Thu 9, Fri 10, Sat 11, Sun 12, Mon 13, Tue 14. Every expected number below was worked
out by hand (rate = present / headcount, 4 decimals half-up; moving average = mean of the non-null rates among the
current row and up to 6 earlier rows of the range, 4 decimals half-up)."""
import json
import random
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, localcontext

import pytest

from app import main
from tests.analytics_helpers import PRESENT, add_emp, add_rec

ROW_KEYS = ["date", "is_working_day", "headcount", "present_count", "late_count", "attendance_rate", "moving_avg_7d"]


def get(api, dept="Engineering", frm="2026-07-06", to="2026-07-14"):
    return api.get(f"/analytics/departments/{dept}/trend", params={"from": frm, "to": to})


def rows(api, dept="Engineering", frm="2026-07-06", to="2026-07-14"):
    r = get(api, dept, frm, to)
    assert r.status_code == 200, r.text
    return r.json()["items"]


def table(items):
    """[(date, headcount, present, late, rate, moving)] without the date prefix, for compact comparisons."""
    return [(r["date"][8:], r["headcount"], r["present_count"], r["late_count"], r["attendance_rate"], r["moving_avg_7d"]) for r in items]


# --------------------------------------------------------------------------- independent oracle
def oracle(employees, logs, department, first, last):
    in_scope = {e["emp_code"]: e for e in employees if e["department"] == department and e["joined_on"] <= last.isoformat()}
    out, rates = [], []
    with localcontext() as ctx:
        ctx.prec = 60
        for k in range((last - first).days + 1):
            day = first + timedelta(days=k)
            iso = day.isoformat()
            headcount = sum(1 for e in in_scope.values() if e["joined_on"] <= iso)
            present, late = Decimal(0), 0
            for r in logs:
                if r["emp_code"] in in_scope and r["date"] == iso:
                    if r["status"] in PRESENT:
                        present += Decimal("0.5") if r.get("half_day") else Decimal(1)
                    late += (r.get("late_minutes") or 0) > 0
            working = day.weekday() < 5
            rate = (present / headcount).quantize(Decimal("0.0001"), ROUND_HALF_UP) if working and headcount > 0 else None
            window = [x for x in (rates[-6:] + [rate]) if x is not None]
            moving = (sum(window) / len(window)).quantize(Decimal("0.0001"), ROUND_HALF_UP) if window else None
            rates.append(rate)
            out.append({"date": iso, "is_working_day": working, "headcount": headcount, "present_count": float(present), "late_count": late,
                        "attendance_rate": None if rate is None else float(rate), "moving_avg_7d": None if moving is None else float(moving)})
    return out


# =========================================================================== the worked scenario
@pytest.fixture()
def scenario(analytics_api):
    for code, joined in (("E1", "2026-01-05"), ("E2", "2026-01-05"), ("E3", "2026-07-08"), ("E4", "2026-08-01")):
        add_emp(code, "Engineering", joined)
    add_emp("S1", "Sales")
    add_rec("E1", "2026-07-06", "PRESENT")
    add_rec("E2", "2026-07-06", "PRESENT", late=10)
    add_rec("E1", "2026-07-07", "WFH")
    add_rec("E2", "2026-07-07", "ABSENT")
    add_rec("S1", "2026-07-07", "PRESENT", late=99)                      # another department
    add_rec("E4", "2026-07-07", "PRESENT", late=77)                      # joins after the range: ignored
    # Wednesday the 8th: no record at all (a missing weekday)
    add_rec("E1", "2026-07-09", "PRESENT", half=True)
    add_rec("E2", "2026-07-09", "ON_DUTY")
    add_rec("E3", "2026-07-09", "PRESENT", late=5)
    add_rec("E1", "2026-07-10", "LEAVE")
    add_rec("E2", "2026-07-10", "PRESENT")
    add_rec("E3", "2026-07-10", "PRESENT")
    add_rec("E1", "2026-07-11", "PRESENT", late=20)                      # Saturday
    add_rec("E2", "2026-07-11", "WFH")
    # Sunday the 12th: nothing
    for code in ("E1", "E2", "E3"):
        add_rec(code, "2026-07-13", "PRESENT")
    add_rec("E1", "2026-07-14", "PRESENT")
    add_rec("E2", "2026-07-14", "PRESENT", half=True)
    add_rec("E3", "2026-07-14", "ABSENT")
    add_rec("E1", "2026-07-05", "PRESENT", late=50)                      # outside the range
    add_rec("E1", "2026-07-15", "PRESENT", late=50)
    add_rec("GHOST", "2026-07-06", "PRESENT", late=500)                  # no such employee
    return analytics_api


class TestWorkedScenario:
    def test_every_row_against_the_hand_calculation(self, scenario):
        # date  hc present late  rate    moving   (rates in 1/10000: 10000 5000 0 8333 6667 - - 10000 5000)
        assert table(rows(scenario)) == [
            ("06", 2, 2.0, 1, 1.0, 1.0),        # 2/2
            ("07", 2, 1.0, 0, 0.5, 0.75),       # 1/2 ; (1 + .5) / 2
            ("08", 3, 0.0, 0, 0.0, 0.5),        # E3 joined: 0/3, a weekday without any record ; (1 + .5 + 0) / 3
            ("09", 3, 2.5, 1, 0.8333, 0.5833),  # (.5 + 1 + 1) / 3 ; 23333 / 4 = 5833.25
            ("10", 3, 2.0, 0, 0.6667, 0.6),     # 2/3 ; 30000 / 5
            ("11", 3, 2.0, 1, None, 0.6),       # Saturday: counted, but no rate ; the null is ignored
            ("12", 3, 0.0, 0, None, 0.6),       # Sunday with nothing at all
            ("13", 3, 3.0, 0, 1.0, 0.6),        # window = rows 07..13 (row 06 dropped): 30000 / 5
            ("14", 3, 1.5, 0, 0.5, 0.6),        # window = rows 08..14: 30000 / 5
        ]

    def test_full_rows_and_types(self, scenario):
        body = get(scenario).json()
        assert list(body) == ["department", "items"] and body["department"] == "Engineering"
        first = body["items"][0]
        assert first == {"date": "2026-07-06", "is_working_day": True, "headcount": 2, "present_count": 2.0, "late_count": 1,
                         "attendance_rate": 1.0, "moving_avg_7d": 1.0}
        assert all(list(r) == ROW_KEYS for r in body["items"])
        for r in body["items"]:
            assert type(r["is_working_day"]) is bool and type(r["headcount"]) is int and type(r["late_count"]) is int
            assert type(r["present_count"]) is float and isinstance(r["date"], str)

    def test_exactly_one_row_per_calendar_day_in_chronological_order(self, scenario):
        dates = [r["date"] for r in rows(scenario)]
        assert dates == [f"2026-07-{d:02d}" for d in range(6, 15)] == sorted(set(dates))

    def test_is_working_day_is_monday_to_friday(self, scenario):
        assert [r["is_working_day"] for r in rows(scenario)] == [True, True, True, True, True, False, False, True, True]

    def test_the_window_is_seven_rows_not_six_or_eight(self, scenario):
        # on the 13th: 7 rows give 0.6; 8 rows (row 06 too) would give 40000/6 = 0.6667; 6 rows would give 25000/4 = 0.625
        r13 = rows(scenario)[7]
        assert r13["moving_avg_7d"] == 0.6 and r13["moving_avg_7d"] not in (0.6667, 0.625)
        r14 = rows(scenario)[8]                                              # 8 rows: 35000/6 = 0.5833 ; 6 rows: 30000/4 = 0.75
        assert r14["moving_avg_7d"] == 0.6 and r14["moving_avg_7d"] not in (0.5833, 0.75)

    def test_headcount_grows_when_an_employee_joins_inside_the_range_and_future_joiners_never_count(self, scenario):
        assert [r["headcount"] for r in rows(scenario)] == [2, 2, 3, 3, 3, 3, 3, 3, 3]
        assert [r["headcount"] for r in rows(scenario, frm="2026-07-30", to="2026-08-03")] == [3, 3, 4, 4, 4]   # E4 joins on 08-01

    def test_other_departments_future_joiners_ghosts_and_out_of_range_logs_do_not_leak(self, scenario):
        got = rows(scenario)
        assert got[1]["late_count"] == 0                                     # Sales' late 99 and E4's late 77 on the 7th
        assert got[0]["late_count"] == 1                                     # the ghost's late 500 is not counted
        assert sum(r["late_count"] for r in got) == 3                        # 06 + 09 + 11; the 07-05 and 07-15 logs are outside


# =========================================================================== the six situations asked for
class TestRangeShapes:
    def test_a_missing_weekday(self, scenario):
        wed = rows(scenario)[2]
        assert wed == {"date": "2026-07-08", "is_working_day": True, "headcount": 3, "present_count": 0.0, "late_count": 0,
                       "attendance_rate": 0.0, "moving_avg_7d": 0.5}         # a working day without records has rate 0.0, not null

    def test_weekends(self, scenario):
        sat, sun = rows(scenario)[5], rows(scenario)[6]
        assert (sat["is_working_day"], sat["present_count"], sat["late_count"], sat["attendance_rate"]) == (False, 2.0, 1, None)
        assert (sun["is_working_day"], sun["present_count"], sun["late_count"], sun["attendance_rate"]) == (False, 0.0, 0, None)

    def test_an_entirely_empty_range(self, analytics_api):
        add_emp("Q1", "Quiet")
        add_emp("Q2", "Quiet")
        assert table(rows(analytics_api, "Quiet", "2026-07-06", "2026-07-12")) == [
            ("06", 2, 0.0, 0, 0.0, 0.0), ("07", 2, 0.0, 0, 0.0, 0.0), ("08", 2, 0.0, 0, 0.0, 0.0), ("09", 2, 0.0, 0, 0.0, 0.0),
            ("10", 2, 0.0, 0, 0.0, 0.0), ("11", 2, 0.0, 0, None, 0.0), ("12", 2, 0.0, 0, None, 0.0)]

    def test_an_empty_range_that_is_only_a_weekend_has_no_rates_at_all(self, analytics_api):
        add_emp("Q1", "Quiet")
        assert table(rows(analytics_api, "Quiet", "2026-07-11", "2026-07-12")) == [("11", 1, 0.0, 0, None, None), ("12", 1, 0.0, 0, None, None)]

    def test_a_one_day_range(self, scenario):
        assert table(rows(scenario, frm="2026-07-06", to="2026-07-06")) == [("06", 2, 2.0, 1, 1.0, 1.0)]          # window of one row
        assert table(rows(scenario, frm="2026-07-08", to="2026-07-08")) == [("08", 3, 0.0, 0, 0.0, 0.0)]          # empty weekday
        assert table(rows(scenario, frm="2026-07-11", to="2026-07-11")) == [("11", 3, 2.0, 1, None, None)]         # Saturday

    def test_a_range_shorter_than_seven_days_uses_a_shorter_window(self, scenario):
        assert table(rows(scenario, frm="2026-07-06", to="2026-07-09")) == [
            ("06", 2, 2.0, 1, 1.0, 1.0), ("07", 2, 1.0, 0, 0.5, 0.75), ("08", 3, 0.0, 0, 0.0, 0.5), ("09", 3, 2.5, 1, 0.8333, 0.5833)]

    def test_rows_before_from_are_not_in_the_window(self, scenario):
        # starting on the 9th: the 6th-8th exist in the data but must not be part of any window
        assert [(r["date"][8:], r["moving_avg_7d"]) for r in rows(scenario, frm="2026-07-09", to="2026-07-10")] == [("09", 0.8333), ("10", 0.75)]

    def test_a_full_seven_day_window(self, scenario):
        got = rows(scenario, frm="2026-07-06", to="2026-07-12")
        assert [r["moving_avg_7d"] for r in got] == [1.0, 0.75, 0.5, 0.5833, 0.6, 0.6, 0.6]
        # the seventh row is the first full window: all seven rows, the two weekend nulls ignored -> 30000 / 5

    def test_a_range_across_a_month_a_leap_day_and_a_year_boundary(self, analytics_api):
        add_emp("E1", "Engineering", "2000-01-01")
        assert [r["date"] for r in rows(analytics_api, frm="2026-06-29", to="2026-07-03")] == ["2026-06-29", "2026-06-30", "2026-07-01", "2026-07-02", "2026-07-03"]
        assert [r["date"] for r in rows(analytics_api, frm="2024-02-28", to="2024-03-01")] == ["2024-02-28", "2024-02-29", "2024-03-01"]
        assert [r["date"] for r in rows(analytics_api, frm="2025-12-30", to="2026-01-02")] == ["2025-12-30", "2025-12-31", "2026-01-01", "2026-01-02"]

    def test_the_longest_allowed_range_has_one_row_per_day(self, analytics_api):
        add_emp("E1", "Engineering", "2000-01-01")
        got = rows(analytics_api, frm="2026-01-01", to="2026-04-02")             # 31 + 28 + 31 + 2 = 92 days
        assert len(got) == 92 and got[0]["date"] == "2026-01-01" and got[-1]["date"] == "2026-04-02"
        assert [r["date"] for r in got] == [(date(2026, 1, 1) + timedelta(days=k)).isoformat() for k in range(92)]


# =========================================================================== headcount and joiners
class TestHeadcount:
    @pytest.mark.parametrize("joined,expected", [
        ("2026-07-06", [1, 1, 1]),            # joins on `from`: counted from the first row
        ("2026-07-07", [0, 1, 1]),
        ("2026-07-08", [0, 0, 1]),            # joins on `to`: counted on the last row only
        ("2026-07-09", [0, 0, 0]),            # the day after `to`: never
        ("2026-01-05", [1, 1, 1]), ("2030-01-01", [0, 0, 0]),
    ])
    def test_join_date_against_the_range(self, analytics_api, joined, expected):
        add_emp("E1", "Engineering", joined)
        assert [r["headcount"] for r in rows(analytics_api, frm="2026-07-06", to="2026-07-08")] == expected

    def test_everybody_joins_later_gives_a_full_set_of_rows_with_null_rates(self, analytics_api):
        add_emp("E1", "Engineering", "2026-09-01")
        add_rec("E1", "2026-07-07", "PRESENT", late=5)                          # inconsistent: a log before joining, ignored
        got = rows(analytics_api, frm="2026-07-06", to="2026-07-12")
        assert len(got) == 7
        assert all(r["headcount"] == 0 and r["present_count"] == 0.0 and r["late_count"] == 0 for r in got)
        assert all(r["attendance_rate"] is None and r["moving_avg_7d"] is None for r in got)

    def test_a_department_that_fills_up_inside_the_range(self, analytics_api):
        add_emp("N1", "New", "2026-07-08")
        add_emp("N2", "New", "2026-07-09")
        add_rec("N1", "2026-07-08", "PRESENT")
        add_rec("N1", "2026-07-09", "PRESENT")
        add_rec("N2", "2026-07-09", "PRESENT")
        add_rec("N1", "2026-07-10", "PRESENT", half=True)
        add_rec("N2", "2026-07-10", "PRESENT")
        # hc 0 0 1 2 2 -> rates -, -, 1.0, 1.0, 0.75 ; moving: no non-null yet, then 1, 1, (10000 + 10000 + 7500) / 3 = 9166.67
        assert table(rows(analytics_api, "New", "2026-07-06", "2026-07-10")) == [
            ("06", 0, 0.0, 0, None, None), ("07", 0, 0.0, 0, None, None), ("08", 1, 1.0, 0, 1.0, 1.0),
            ("09", 2, 2.0, 0, 1.0, 1.0), ("10", 2, 1.5, 0, 0.75, 0.9167)]

    def test_each_department_counts_only_its_own_people(self, analytics_api):
        for code, dept in (("A1", "Alpha"), ("A2", "Alpha"), ("B1", "Beta")):
            add_emp(code, dept)
        assert [r["headcount"] for r in rows(analytics_api, "Alpha", "2026-07-06", "2026-07-07")] == [2, 2]
        assert [r["headcount"] for r in rows(analytics_api, "Beta", "2026-07-06", "2026-07-07")] == [1, 1]


# =========================================================================== presence rules
class TestPresenceAndLate:
    @pytest.mark.parametrize("status,present", [("PRESENT", 1.0), ("WFH", 1.0), ("ON_DUTY", 1.0), ("ABSENT", 0.0), ("LEAVE", 0.0)])
    def test_each_status(self, analytics_api, status, present):
        add_emp("E1")
        add_rec("E1", "2026-07-08", status)
        (row,) = rows(analytics_api, frm="2026-07-08", to="2026-07-08")
        assert (row["present_count"], row["attendance_rate"]) == (present, present)

    @pytest.mark.parametrize("status", ["PRESENT", "WFH", "ON_DUTY"])
    def test_half_day_counts_half(self, analytics_api, status):
        add_emp("E1")
        add_rec("E1", "2026-07-08", status, half=True)
        (row,) = rows(analytics_api, frm="2026-07-08", to="2026-07-08")
        assert (row["present_count"], row["attendance_rate"]) == (0.5, 0.5)

    @pytest.mark.parametrize("status", ["ABSENT", "LEAVE"])
    def test_half_day_flag_on_a_non_present_status_is_ignored(self, analytics_api, status):
        add_emp("E1")
        add_rec("E1", "2026-07-08", status, half=True)
        assert rows(analytics_api, frm="2026-07-08", to="2026-07-08")[0]["present_count"] == 0.0

    def test_weekend_presence_counts_in_present_count_but_never_gives_a_rate(self, analytics_api):
        add_emp("E1")
        add_emp("E2")
        add_rec("E1", "2026-07-11", "PRESENT", late=5)
        add_rec("E2", "2026-07-11", "ON_DUTY", half=True)
        (row,) = rows(analytics_api, frm="2026-07-11", to="2026-07-11")
        assert (row["present_count"], row["late_count"], row["attendance_rate"], row["moving_avg_7d"]) == (1.5, 1, None, None)

    def test_late_count_counts_records_with_minutes_above_zero_whatever_the_status(self, analytics_api):
        for code in ("E1", "E2", "E3", "E4", "E5"):
            add_emp(code)
        add_rec("E1", "2026-07-08", late=35)
        add_rec("E2", "2026-07-08", late=1)
        add_rec("E3", "2026-07-08", late=0)
        add_rec("E4", "2026-07-08")                                            # no late_minutes
        add_rec("E5", "2026-07-08", "ABSENT", late=9)                          # stored as is
        assert rows(analytics_api, frm="2026-07-08", to="2026-07-08")[0]["late_count"] == 3

    def test_float_valued_late_minutes(self, analytics_api):
        add_emp("E1")
        add_rec("E1", "2026-07-08", late=35.0)
        row = rows(analytics_api, frm="2026-07-08", to="2026-07-08")[0]
        assert row["late_count"] == 1 and type(row["late_count"]) is int

    def test_records_missing_every_optional_field(self, analytics_api):
        add_emp("E1")
        add_rec("E1", "2026-07-08")
        add_rec("E1", "2026-07-09", late_minutes=None, half_day=None, work_hours=None, history=None)
        got = rows(analytics_api, frm="2026-07-08", to="2026-07-09")
        assert [(r["present_count"], r["late_count"]) for r in got] == [(1.0, 0), (1.0, 0)]

    def test_a_missing_weekday_and_an_absent_record_look_alike_in_the_numbers(self, analytics_api):
        add_emp("E1")
        add_rec("E1", "2026-07-07", "ABSENT")
        got = rows(analytics_api, frm="2026-07-06", to="2026-07-07")           # the 6th has no record, the 7th is ABSENT
        assert [(r["present_count"], r["attendance_rate"]) for r in got] == [(0.0, 0.0), (0.0, 0.0)]


# =========================================================================== rounding
class TestRounding:
    @staticmethod
    def sixteen(api, present_by_day):
        for n in range(16):
            add_emp(f"E{n:02d}")
        for day, units in present_by_day.items():                           # units = half days of presence
            full, half = divmod(units, 2)
            for n in range(full):
                add_rec(f"E{n:02d}", day)
            if half:
                add_rec(f"E{full:02d}", day, half=True)

    @pytest.mark.parametrize("units,expected", [
        (1, 0.0313),    # 0.5/16 = 0.03125  exactly: half-up (banker's: 0.0312)
        (3, 0.0938),    # 1.5/16 = 0.09375
        (5, 0.1563),    # 2.5/16 = 0.15625  (banker's: 0.1562)
        (7, 0.2188),    # 3.5/16 = 0.21875
        (9, 0.2813),    # 4.5/16 = 0.28125  (banker's: 0.2812)
        (2, 0.0625), (16, 0.5), (32, 1.0), (0, 0.0),
    ])
    def test_attendance_rate_ties_round_half_up(self, analytics_api, units, expected):
        self.sixteen(analytics_api, {"2026-07-08": units})
        (row,) = rows(analytics_api, frm="2026-07-08", to="2026-07-08")
        assert row["headcount"] == 16 and row["attendance_rate"] == expected and row["moving_avg_7d"] == expected

    def test_python_round_would_have_been_wrong(self):
        assert round(0.5 / 16, 4) == 0.0312 and round(2.5 / 16, 4) == 0.1562 and round(4.5 / 16, 4) == 0.2812

    @pytest.mark.parametrize("present,hc,expected", [(1, 3, 0.3333), (2, 3, 0.6667), (1, 7, 0.1429), (2, 7, 0.2857), (3, 7, 0.4286), (5, 6, 0.8333), (6, 7, 0.8571)])
    def test_non_tie_rates(self, analytics_api, present, hc, expected):
        for n in range(hc):
            add_emp(f"E{n}")
        for n in range(present):
            add_rec(f"E{n}", "2026-07-08")
        assert rows(analytics_api, frm="2026-07-08", to="2026-07-08")[0]["attendance_rate"] == expected

    @pytest.mark.parametrize("day1,day2,expected", [
        (1, 1.5, 0.4167),   # rates .3333 and .5: (3333 + 5000) / 2 = 4166.5 -> half-up 4167 (banker's 4166)
        (2.5, 1.5, 0.6667), # rates .8333 and .5: (8333 + 5000) / 2 = 6666.5 -> half-up 6667 (banker's 6666)
        (0, 3, 0.5),        # 0 and 1.0
        (1, 1, 0.3333),     # equal rates
    ])
    def test_moving_average_ties_round_half_up(self, analytics_api, day1, day2, expected):
        for n in range(3):
            add_emp(f"E{n}")
        for day, present in (("2026-07-06", day1), ("2026-07-07", day2)):
            whole, half = int(present), present % 1 != 0
            for n in range(whole):
                add_rec(f"E{n}", day)
            if half:
                add_rec(f"E{whole}", day, half=True)
        got = rows(analytics_api, frm="2026-07-06", to="2026-07-07")
        assert got[1]["moving_avg_7d"] == expected

    def test_the_moving_average_is_the_mean_of_the_displayed_rates(self, analytics_api):
        # rates .3333 and .6667 are displayed; their mean is .5 exactly (the unrounded 1/3 and 2/3 would also give .5);
        # rates .8333 (5/6) and .1667 (1/6) -> 0.5 as well, but .3333 and .5 prove it: mean(.3333, .5) = .41665 -> .4167
        for n in range(3):
            add_emp(f"E{n}")
        add_rec("E0", "2026-07-06")
        add_rec("E0", "2026-07-07")
        add_rec("E1", "2026-07-07", half=True)
        got = rows(analytics_api, frm="2026-07-06", to="2026-07-07")
        assert [r["attendance_rate"] for r in got] == [0.3333, 0.5] and got[1]["moving_avg_7d"] == 0.4167


# =========================================================================== parameters and errors
class TestValidation:
    def test_unknown_department_is_404(self, analytics_api):
        add_emp("E1", "Engineering")
        r = get(analytics_api, "Nope")
        assert r.status_code == 404 and isinstance(r.json()["detail"], str)

    def test_department_match_is_exact_and_case_sensitive(self, analytics_api):
        add_emp("E1", "Engineering")
        assert get(analytics_api, "engineering").status_code == 404 and get(analytics_api, "Engineerin").status_code == 404

    def test_a_department_whose_employees_all_join_later_exists(self, analytics_api):
        add_emp("E1", "Engineering", "2030-01-01")
        assert get(analytics_api).status_code == 200

    @pytest.mark.parametrize("dept", ["Customer Support", "R&D", "Ünïcode", "a+b", "100%"])
    def test_department_names_that_need_url_encoding(self, analytics_api, dept):
        add_emp("E1", dept)
        r = analytics_api.get(f"/analytics/departments/{__import__('urllib.parse').parse.quote(dept, safe='')}/trend", params={"from": "2026-07-06", "to": "2026-07-06"})
        assert r.status_code == 200 and r.json()["department"] == dept

    @pytest.mark.parametrize("frm,to", [("2026-07-09", "2026-07-08"), ("2026-07-06", "2026-07-05"), ("2026-07-01", "2026-06-30")])
    def test_to_before_from_is_422(self, analytics_api, frm, to):
        add_emp("E1")
        r = get(analytics_api, frm=frm, to=to)
        assert r.status_code == 422 and isinstance(r.json()["detail"], list)

    def test_the_range_limit_is_92_days_inclusive(self, analytics_api):
        add_emp("E1")
        assert get(analytics_api, frm="2026-01-01", to="2026-04-02").status_code == 200       # 92 days
        r = get(analytics_api, frm="2026-01-01", to="2026-04-03")                             # 93 days
        assert r.status_code == 422 and isinstance(r.json()["detail"], list)
        assert get(analytics_api, frm="2026-01-01", to="2026-12-31").status_code == 422
        assert get(analytics_api, frm="2026-07-01", to="2026-09-30").status_code == 200       # a full quarter, 92 days

    @pytest.mark.parametrize("params", [{}, {"from": "2026-07-06"}, {"to": "2026-07-06"}])
    def test_both_dates_are_required(self, analytics_api, params):
        add_emp("E1")
        assert analytics_api.get("/analytics/departments/Engineering/trend", params=params).status_code == 422

    @pytest.mark.parametrize("bad", ["2026-02-30", "2026-13-01", "20260706", "2026-7-6", "", "yesterday", "2026-07-06T00:00:00", " 2026-07-06", "２０２６-07-06"])
    def test_malformed_dates_are_422(self, analytics_api, bad):
        add_emp("E1")
        assert get(analytics_api, frm=bad, to="2026-07-08").status_code == 422
        assert get(analytics_api, frm="2026-07-06", to=bad).status_code == 422

    def test_validation_comes_before_the_department_lookup(self, analytics_api):
        assert get(analytics_api, "Nope", frm="2026-07-09", to="2026-07-08").status_code == 422
        assert get(analytics_api, "Nope", frm="bad", to="2026-07-08").status_code == 422

    @pytest.mark.parametrize("frm,to,n", [("9999-12-31", "9999-12-31", 1), ("0001-01-01", "0001-01-03", 3), ("2026-07-06", "2026-07-06", 1)])
    def test_extreme_dates_do_not_crash(self, analytics_api, frm, to, n):
        add_emp("E1", "Engineering", "0001-01-01")
        assert len(rows(analytics_api, frm=frm, to=to)) == n


# =========================================================================== pipeline shape
class TestPipeline:
    FIRST, LAST = date(2026, 7, 6), date(2026, 7, 14)

    def stages(self, first=None, last=None):
        return main.build_department_trend_pipeline("Engineering", first or self.FIRST, last or self.LAST)

    def test_stage_names_in_order(self):
        names = [next(iter(s)) for s in self.stages()]
        assert names == ["$match", "$lookup", "$set", "$unwind", "$group", "$project", "$densify", "$set", "$setWindowFields", "$set",
                         "$set", "$setWindowFields", "$project", "$sort"]

    def test_the_gap_filling_stage(self):
        densify = next(s for s in self.stages() if "$densify" in s)["$densify"]
        assert densify["field"] == "day" and densify["range"]["step"] == 1 and densify["range"]["unit"] == "day"
        lower, upper = densify["range"]["bounds"]
        assert (lower.year, lower.month, lower.day, lower.hour) == (2026, 7, 6, 0)
        assert (upper.year, upper.month, upper.day, upper.hour, upper.minute, upper.second) == (2026, 7, 14, 23, 59, 59)   # last day included

    def test_the_two_windows(self):
        windows = [s["$setWindowFields"] for s in self.stages() if "$setWindowFields" in s]
        assert windows[0]["sortBy"] == {"day": 1} and windows[0]["output"]["headcount"]["window"] == {"documents": ["unbounded", "current"]}
        assert windows[1]["sortBy"] == {"day": 1}
        moving = windows[1]["output"]["moving_bp"]
        assert moving["$avg"] == "$rate_bp" and moving["window"] == {"documents": [-6, 0]}      # current row + 6 preceding rows

    def test_the_main_query_starts_with_an_indexed_department_match_and_bounds_the_lookup(self):
        pipe = self.stages()
        assert pipe[0] == {"$match": {"department": "Engineering"}}
        lookup = pipe[1]["$lookup"]
        assert (lookup["from"], lookup["localField"], lookup["foreignField"]) == ("attendance_logs", "emp_code", "emp_code")
        assert lookup["pipeline"][0] == {"$match": {"date": {"$gte": "2026-07-06", "$lte": "2026-07-14"}}}

    def test_no_per_day_content_is_generated_in_python(self):
        short = json.dumps(self.stages(date(2026, 7, 6), date(2026, 7, 6)), default=str)
        long = json.dumps(self.stages(date(2026, 4, 1), date(2026, 6, 30)), default=str)       # 91 days
        assert len(short) == len(long)                                                          # same text, only the date constants differ
        assert "$range" not in long and "$limit" not in long

    def test_the_endpoint_makes_one_aggregation_and_never_fetches_logs(self, scenario, monkeypatch):
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
        assert get(scenario).status_code == 200
        assert calls == [("employees", "find_one"), ("employees", "aggregate")]

    def test_indexes_behind_the_pipeline_exist(self, analytics_api):
        emp = [v["key"] for v in main.db.employees.index_information().values()]
        att = [v["key"] for v in main.db.attendance_logs.index_information().values()]
        assert [("department", 1), ("emp_code", 1)] in emp and [("emp_code", 1), ("date", 1)] in att


# =========================================================================== independent oracle
class TestAgainstTheOracle:
    STATUSES = ["PRESENT", "PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]

    def test_randomised_departments_ranges_joiners_and_records(self, analytics_api):
        rng = random.Random(20260713)
        for n in range(50):
            main.db.employees.delete_many({})
            main.db.attendance_logs.delete_many({})
            first = date(2026, 6, 20) + timedelta(days=rng.randint(0, 60))
            last = first + timedelta(days=rng.choice([0, 1, 3, 6, 7, 8, 13, 30, 60]))
            employees, logs = [], []
            for e in range(rng.randint(1, 9)):
                code = f"EMP{e:04d}"
                joined = (first + timedelta(days=rng.randint(-40, (last - first).days + 6))).isoformat()
                emp = {"emp_code": code, "department": rng.choice(["Engineering", "Engineering", "Sales"]), "joined_on": joined}
                employees.append(emp)
                add_emp(code, emp["department"], joined)
                for k in range(-2, (last - first).days + 3):
                    day = (first + timedelta(days=k)).isoformat()
                    if day < joined or rng.random() > 0.6:                       # logs only from the joining day on
                        continue
                    rec = {"emp_code": code, "date": day, "status": rng.choice(self.STATUSES)}
                    if rng.random() < 0.5:
                        rec["late_minutes"] = rng.choice([0, 5, 35, 35.0])
                    if rng.random() < 0.4:
                        rec["half_day"] = rng.random() < 0.5
                    logs.append(rec)
                    main.db.attendance_logs.insert_one(dict(rec))
            add_rec("GHOST", first.isoformat(), late=99)
            if not any(e["department"] == "Engineering" for e in employees):
                continue
            assert rows(analytics_api, "Engineering", first.isoformat(), last.isoformat()) == oracle(employees, logs, "Engineering", first, last), (n, first, last)


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
    """On a real server every test above already runs against the real $densify / $setWindowFields / $lookup;
    these add the planner and the volume."""

    def test_explain_never_scans_a_collection_and_the_lookup_reads_only_the_range(self, api, real_mongo):
        for e in range(150):
            add_emp(f"EMP{e:04d}", "Engineering" if e % 3 else "Sales")
        days = [date(2026, 6, 1) + timedelta(days=k) for k in range(122)]                       # June - September
        main.db.attendance_logs.insert_many([{"emp_code": f"EMP{e:04d}", "date": d.isoformat(), "status": "PRESENT", "late_minutes": e % 5}
                                             for e in range(150) for d in days])
        pipeline = main.build_department_trend_pipeline("Engineering", date(2026, 7, 6), date(2026, 7, 19))
        plan = main.db.command({"explain": {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}, "verbosity": "executionStats"})
        text = json.dumps(plan, default=str)
        assert "COLLSCAN" not in text and "IXSCAN" in text
        assert all(v == 0 for v in _values_of(plan, "collectionScans"))
        engineers = sum(1 for e in range(150) if e % 3)
        assert max(_values_of(plan, "totalDocsExamined")) <= engineers * 14 + 150               # 14 days of logs, not 122

    def test_large_collection_matches_the_oracle(self, api, real_mongo):
        rng = random.Random(11)
        first, last = date(2026, 7, 1), date(2026, 9, 30)                                       # 92 days
        employees, logs = [], []
        for e in range(120):
            code = f"EMP{e:04d}"
            joined = (first + timedelta(days=rng.randint(-60, 80))).isoformat()
            employees.append({"emp_code": code, "department": "Engineering", "joined_on": joined})
            add_emp(code, "Engineering", joined)
            for k in range(92):
                day = (first + timedelta(days=k)).isoformat()
                if day >= joined and rng.random() < 0.7:
                    rec = {"emp_code": code, "date": day, "status": rng.choice(TestAgainstTheOracle.STATUSES), "late_minutes": rng.choice([0, 10, 35]), "half_day": rng.random() < 0.1}
                    logs.append(rec)
        main.db.attendance_logs.insert_many([dict(r) for r in logs])
        got = rows(api, "Engineering", first.isoformat(), last.isoformat())
        assert len(got) == 92 and got == oracle(employees, logs, "Engineering", first, last)

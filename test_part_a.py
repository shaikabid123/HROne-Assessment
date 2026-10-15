"""Focused tests for the Part A fixes. Section letters match the rows in REVIEW.md."""
import os
import pathlib
import subprocess
import sys
import textwrap
import threading
from datetime import datetime, timedelta, timezone

import pytest

from app import main
from tests.conftest import employee_body

IST = main.IST
UTC = timezone.utc


def ist(y, mo, d, h, mi=0, s=0, us=0):
    return datetime(y, mo, d, h, mi, s, us, tzinfo=IST)


def ms(dt):
    return main.dt_to_ms(dt)


# --------------------------------------------------------------------------- #
# Pure functions: R1 - R5, rounding, epoch conversion (no database needed)
# --------------------------------------------------------------------------- #
class TestLate:  # R2
    D = "2026-07-06"

    @pytest.mark.parametrize("h,m,s,expected", [
        (9, 40, 0, 0),      # exactly 10:00 after 09:30 -> inside grace
        (9, 40, 1, 10),     # the contract example
        (10, 15, 59, 45),   # the contract example (floor, counted from shift_start)
        (9, 40, 30, 10),    # old code floored to 10 minutes first, then `> 10` -> wrongly 0
        (9, 41, 0, 11),
        (9, 0, 0, 0),       # early
    ])
    def test_cases(self, h, m, s, expected):
        assert main.compute_late_minutes(ist(2026, 7, 6, h, m, s), "09:30", self.D) == expected

    def test_uses_ist_not_utc(self):
        # 04:10:01 UTC is 09:40:01 IST. The starter used the UTC clock time.
        utc_punch = datetime(2026, 7, 6, 4, 10, 1, tzinfo=UTC)
        assert main.compute_late_minutes(utc_punch, "09:30", self.D) == 10

    def test_subsecond_is_truncated(self):
        assert main.compute_late_minutes(ist(2026, 7, 6, 9, 40, 0, 900_000), "09:30", self.D) == 0

    def test_overnight_after_midnight_counts_from_previous_day_start(self):
        # shift 22:00-06:00, punch-in 00:30 IST on the 7th -> shift of the 6th, 150 min after 22:00
        p = ist(2026, 7, 7, 0, 30)
        d = main.attendance_date(p, "22:00", "06:00")
        assert d == "2026-07-06"
        assert main.compute_late_minutes(p, "22:00", d) == 150


class TestAttendanceDate:  # R1
    def test_utc_evening_is_next_ist_day(self):
        # 20:00 UTC on the 6th = 01:30 IST on the 7th
        p = datetime(2026, 7, 6, 20, 0, tzinfo=UTC)
        assert main.attendance_date(p, "09:30", "18:30") == "2026-07-07"

    def test_day_shift_early_morning_is_same_ist_date(self):
        assert main.attendance_date(ist(2026, 7, 6, 0, 5), "09:30", "18:30") == "2026-07-06"

    @pytest.mark.parametrize("h,m,s,expected", [
        (21, 55, 0, "2026-07-06"),
        (5, 59, 59, "2026-07-05"),   # earlier than shift_end -> previous day's shift
        (6, 0, 0, "2026-07-06"),     # exactly shift_end is NOT earlier than it
        (12, 0, 0, "2026-07-06"),
    ])
    def test_overnight(self, h, m, s, expected):
        assert main.attendance_date(ist(2026, 7, 6, h, m, s), "22:00", "06:00") == expected

    def test_overnight_when_shift_end_equals_shift_start(self):
        assert main.is_overnight("09:30", "09:30") is True


class TestWorkHours:  # R4, R5
    def test_half_up_not_bankers(self):
        a = ist(2026, 7, 6, 9, 0)
        out = ist(2026, 7, 6, 9, 7, 30)         # 450 s = 0.125 h exactly
        assert main.compute_work_hours(a, out) == 0.13   # round(0.125, 2) == 0.12 in Python
        assert round(0.125, 2) == 0.12                   # documents why the starter was wrong

    def test_tie_at_the_half_day_boundary(self):
        a = ist(2026, 7, 6, 9, 0)
        # 4.495 h = 16182 s -> half-up gives 4.50 -> NOT a half day
        out = ist(2026, 7, 6, 13, 29, 42)
        assert main.compute_work_hours(a, out) == 4.5
        assert main.compute_half_day(4.5) is False
        assert main.compute_half_day(4.49) is True

    def test_contract_samples(self):
        assert main.compute_work_hours(ist(2026, 7, 6, 9, 28), ist(2026, 7, 6, 18, 35)) == 9.12
        assert main.compute_work_hours(ist(2026, 7, 6, 21, 55), ist(2026, 7, 7, 6, 40)) == 8.75

    def test_null_work_hours_is_not_half_day(self):
        assert main.compute_half_day(None) is False


class TestOvertime:  # R3
    @pytest.mark.parametrize("minutes,seconds,expected", [
        (0, 0, 0),
        (29, 59, 0),    # 29 whole minutes: below the 30-minute minimum
        (30, 0, 30),    # exactly 30 -> counted
        (30, 59, 30),   # floor, not round
        (40, 0, 40),
    ])
    def test_day_shift(self, minutes, seconds, expected):
        out = ist(2026, 7, 6, 18, 30) + timedelta(minutes=minutes, seconds=seconds)   # shift_end is 18:30
        assert main.compute_overtime(out, "09:30", "18:30", "2026-07-06") == expected

    def test_overnight_shift_ends_next_day(self):
        # attendance day the 6th, shift 22:00-06:00, out 06:40 on the 7th -> 40
        assert main.compute_overtime(ist(2026, 7, 7, 6, 40), "22:00", "06:00", "2026-07-06") == 40

    def test_left_before_shift_end_is_zero(self):
        assert main.compute_overtime(ist(2026, 7, 6, 17, 0), "09:30", "18:30", "2026-07-06") == 0


class TestEpoch:
    def test_round_trip_is_exact(self):
        v = 1_783_312_500_123
        assert main.dt_to_ms(main.ms_to_dt(v)) == v

    def test_naive_datetime_is_taken_as_utc(self):
        assert main.dt_to_ms(datetime(2026, 7, 6, 4, 0)) == main.dt_to_ms(datetime(2026, 7, 6, 4, 0, tzinfo=UTC))


# --------------------------------------------------------------------------- #
# POST /employees
# --------------------------------------------------------------------------- #
class TestCreateEmployee:
    def test_created_shape(self, api):
        r = api.post("/employees", json=employee_body())
        assert r.status_code == 201
        b = r.json()
        assert set(b) == {"emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on", "created_at"}
        assert (b["shift_start"], b["shift_end"]) == ("09:30", "18:30")        # defaults
        assert isinstance(b["created_at"], int) and b["created_at"] > 1_700_000_000_000  # epoch ms, not a datetime

    def test_created_at_matches_what_is_stored(self, api):
        b = api.post("/employees", json=employee_body()).json()
        stored = main.db.employees.find_one({"emp_code": "EMP0001"})
        assert stored["created_at"].tzinfo is not None
        assert main.dt_to_ms(stored["created_at"]) == b["created_at"]
        assert "_id" not in b

    def test_client_supplied_created_at_is_ignored(self, api):
        b = api.post("/employees", json=employee_body(created_at=5)).json()
        assert b["created_at"] != 5

    def test_duplicate_is_409(self, api):
        assert api.post("/employees", json=employee_body()).status_code == 201
        r = api.post("/employees", json=employee_body(name="Other"))
        assert r.status_code == 409
        assert isinstance(r.json()["detail"], str)

    def test_concurrent_duplicates_yield_exactly_one_201(self, api):
        results, barrier = [], threading.Barrier(12)

        def go():
            barrier.wait()
            results.append(api.post("/employees", json=employee_body("EMP7777")).status_code)

        ts = [threading.Thread(target=go) for _ in range(12)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert sorted(results) == [201] + [409] * 11
        assert main.db.employees.count_documents({"emp_code": "EMP7777"}) == 1

    @pytest.mark.parametrize("over", [
        {"emp_code": "E1"}, {"emp_code": "EMP123"[:6]}, {"emp_code": "EMP1234567"}, {"emp_code": "emp0001"},
        {"emp_code": "EMP١٢٣٤"},                      # Arabic-Indic digits must not satisfy \d{4,6}
        {"name": ""}, {"name": "x" * 101},
        {"email": "nope"}, {"email": "a@b"}, {"email": "a b@c.com"},
        {"department": ""}, {"department": "d" * 51},
        {"shift_start": "9:30"}, {"shift_start": "24:00"}, {"shift_end": "18:60"},
        {"shift_start": "10:00", "shift_end": "10:00"},     # must differ
        {"joined_on": "2026-02-30"}, {"joined_on": "20260105"}, {"joined_on": "05-01-2026"}, {"joined_on": ""},
        {"name": 123},
    ])
    def test_invalid_is_422(self, api, over):
        r = api.post("/employees", json=employee_body(**over))
        assert r.status_code == 422, over
        assert isinstance(r.json()["detail"], list)

    @pytest.mark.parametrize("missing", ["emp_code", "name", "email", "department", "joined_on"])
    def test_required_fields(self, api, missing):
        body = employee_body()
        del body[missing]
        assert api.post("/employees", json=body).status_code == 422

    def test_overnight_shift_is_allowed(self, api):
        assert api.post("/employees", json=employee_body(shift_start="22:00", shift_end="06:00")).status_code == 201


# --------------------------------------------------------------------------- #
# GET /employees
# --------------------------------------------------------------------------- #
class TestListEmployees:
    @pytest.fixture()
    def eleven(self, api):
        # inserted out of order on purpose; 7 Engineering, 4 Sales
        for i in (5, 3, 11, 1, 9, 7, 2, 10, 4, 8, 6):
            dept = "Sales" if i % 3 == 0 else "Engineering"
            api.post("/employees", json=employee_body(f"EMP{i:04d}", department=dept))
        return api

    def test_sorted_by_emp_code_and_first_page_starts_at_first_row(self, eleven):
        b = eleven.get("/employees", params={"page_size": 5}).json()
        assert [e["emp_code"] for e in b["items"]] == [f"EMP{i:04d}" for i in (1, 2, 3, 4, 5)]
        assert (b["total"], b["page"], b["page_size"]) == (11, 1, 5)

    def test_page_two_and_last_partial_page(self, eleven):
        p2 = eleven.get("/employees", params={"page": 2, "page_size": 5}).json()
        assert [e["emp_code"] for e in p2["items"]] == [f"EMP{i:04d}" for i in (6, 7, 8, 9, 10)]
        p3 = eleven.get("/employees", params={"page": 3, "page_size": 5}).json()
        assert [e["emp_code"] for e in p3["items"]] == ["EMP0011"]

    def test_page_beyond_the_end_is_empty_not_an_error(self, eleven):
        b = eleven.get("/employees", params={"page": 99, "page_size": 5}).json()
        assert b["items"] == [] and b["total"] == 11
        assert eleven.get("/employees", params={"page": 10**30}).json()["items"] == []

    def test_total_reflects_department_filter(self, eleven):
        b = eleven.get("/employees", params={"department": "Sales"}).json()
        assert b["total"] == 3 and [e["emp_code"] for e in b["items"]] == ["EMP0003", "EMP0006", "EMP0009"]
        b = eleven.get("/employees", params={"department": "Engineering", "page_size": 2, "page": 2}).json()
        assert b["total"] == 8 and [e["emp_code"] for e in b["items"]] == ["EMP0004", "EMP0005"]

    def test_department_match_is_exact_and_case_sensitive(self, eleven):
        assert eleven.get("/employees", params={"department": "sales"}).json()["total"] == 0
        assert eleven.get("/employees", params={"department": "Nope"}).json() == {"items": [], "total": 0, "page": 1, "page_size": 20}

    @pytest.mark.parametrize("params", [{"page": 0}, {"page": -1}, {"page_size": 0}, {"page_size": 101}, {"page": "x"}])
    def test_bad_pagination_is_422(self, api, params):
        assert api.get("/employees", params=params).status_code == 422

    def test_page_size_100_is_the_maximum_allowed(self, api):
        assert api.get("/employees", params={"page_size": 100}).status_code == 200

    def test_items_use_epoch_ms_and_no_id(self, seeded):
        for e in seeded.get("/employees").json()["items"]:
            assert isinstance(e["created_at"], int) and "_id" not in e and "id" not in e

    def test_filtered_list_uses_the_index_sort_in_the_database(self, eleven, commands):
        eleven.get("/employees", params={"department": "Sales", "page": 2, "page_size": 2})
        find = [c for n, c in commands if n == "find" and c["find"] == "employees"][0]
        assert find["sort"] == {"emp_code": 1} and find["skip"] == 2 and find["limit"] == 2


# --------------------------------------------------------------------------- #
# POST /attendance/punch-in
# --------------------------------------------------------------------------- #
@pytest.fixture()
def emps(api):
    api.post("/employees", json=employee_body("EMP0001"))                                    # 09:30-18:30
    api.post("/employees", json=employee_body("EMP0005", shift_start="22:00", shift_end="06:00"))
    return api


class TestPunchIn:
    def test_unknown_employee_is_404_not_500(self, api):
        r = api.post("/attendance/punch-in", json={"emp_code": "EMP9999", "punched_at": ms(ist(2026, 7, 6, 9, 0))})
        assert r.status_code == 404 and isinstance(r.json()["detail"], str)

    def test_created_record_shape_and_values(self, emps):
        r = emps.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(ist(2026, 7, 6, 10, 5))})
        assert r.status_code == 201
        assert r.json() == {
            "emp_code": "EMP0001", "date": "2026-07-06", "status": "PRESENT",
            "punch_in": ms(ist(2026, 7, 6, 10, 5)), "punch_out": None, "work_hours": None,
            "late_minutes": 35, "overtime_minutes": 0, "half_day": False, "history": [],
        }   # no "id", no "_id"

    def test_stored_values_are_utc_datetimes(self, emps):
        emps.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(ist(2026, 7, 6, 10, 5))})
        doc = main.db.attendance_logs.find_one({"emp_code": "EMP0001"})
        assert doc["punch_in"] == datetime(2026, 7, 6, 4, 35, tzinfo=UTC)
        assert doc["date"] == "2026-07-06" and doc["history"] == []

    def test_milliseconds_are_truncated_stored_and_returned(self, emps):
        base = ms(ist(2026, 7, 6, 9, 40, 0))
        r = emps.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": base + 900})
        assert r.json()["punch_in"] == base and r.json()["late_minutes"] == 0     # 09:40:00.900 counts as 09:40:00
        assert main.dt_to_ms(main.db.attendance_logs.find_one({})["punch_in"]) == base

    def test_grace_boundary_over_http(self, emps):
        out = []
        for day, (h, m, s) in zip((6, 7, 8), ((9, 40, 0), (9, 40, 1), (10, 15, 59))):
            r = emps.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(ist(2026, 7, day, h, m, s))})
            out.append(r.json()["late_minutes"])
        assert out == [0, 10, 45]

    def test_date_is_the_ist_date_not_utc_and_not_server_local(self, emps):
        # 20:00 UTC on the 6th is 01:30 IST on the 7th
        r = emps.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(datetime(2026, 7, 6, 20, 0, tzinfo=UTC))})
        assert r.json()["date"] == "2026-07-07"

    def test_overnight_shift_punch_after_midnight_belongs_to_previous_day(self, emps):
        r = emps.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ms(ist(2026, 7, 7, 3, 0))})
        assert r.status_code == 201 and r.json()["date"] == "2026-07-06"
        r = emps.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ms(ist(2026, 7, 6, 21, 55))})
        assert r.status_code == 409          # same attendance day 2026-07-06 -> already exists

    @pytest.mark.parametrize("status", ["WFH", "ON_DUTY", "PRESENT"])
    def test_presence_statuses_accepted(self, emps, status):
        r = emps.post("/attendance/punch-in", json={"emp_code": "EMP0001", "status": status, "punched_at": ms(ist(2026, 7, 6, 9, 0))})
        assert r.status_code == 201 and r.json()["status"] == status

    @pytest.mark.parametrize("status", ["ABSENT", "LEAVE", "HOLIDAY", "present", "", None, 1])
    def test_other_statuses_are_422(self, emps, status):
        r = emps.post("/attendance/punch-in", json={"emp_code": "EMP0001", "status": status, "punched_at": ms(ist(2026, 7, 6, 9, 0))})
        assert r.status_code == 422

    @pytest.mark.parametrize("bad", [
        1783312500,            # seconds, not milliseconds
        99_999_999_999,        # just below the minimum
        4_102_444_800_001,     # just above the maximum
        -5, 0,
        1783312500000.0,       # float, even though integral
        1783312500000.5,
        "1783312500000",       # string
        True,                  # bool is an int subclass in Python
        "2026-07-06T09:00:00Z",
    ])
    def test_invalid_punched_at_is_422(self, emps, bad):
        r = emps.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": bad})
        assert r.status_code == 422, bad

    def test_missing_emp_code_is_422(self, api):
        assert api.post("/attendance/punch-in", json={}).status_code == 422

    def test_punched_at_defaults_to_now(self, emps):
        before = main.dt_to_ms(datetime.now(UTC)) - 1000
        r = emps.post("/attendance/punch-in", json={"emp_code": "EMP0001"})
        assert r.status_code == 201
        assert before <= r.json()["punch_in"] <= main.dt_to_ms(datetime.now(UTC)) + 1000
        assert r.json()["punch_in"] % 1000 == 0          # truncated to whole seconds

    def test_second_punch_in_same_date_is_409(self, emps):
        body = {"emp_code": "EMP0001", "punched_at": ms(ist(2026, 7, 6, 9, 0))}
        assert emps.post("/attendance/punch-in", json=body).status_code == 201
        r = emps.post("/attendance/punch-in", json={**body, "punched_at": ms(ist(2026, 7, 6, 11, 0))})
        assert r.status_code == 409 and isinstance(r.json()["detail"], str)

    def test_concurrent_identical_punch_ins_one_201_rest_409(self, emps):
        body = {"emp_code": "EMP0001", "punched_at": ms(ist(2026, 7, 6, 9, 0))}
        results, barrier = [], threading.Barrier(16)

        def go():
            barrier.wait()
            results.append(emps.post("/attendance/punch-in", json=body).status_code)

        ts = [threading.Thread(target=go) for _ in range(16)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert sorted(results) == [201] + [409] * 15
        assert main.db.attendance_logs.count_documents({"emp_code": "EMP0001", "date": "2026-07-06"}) == 1


# --------------------------------------------------------------------------- #
# GET /attendance
# --------------------------------------------------------------------------- #
class TestListAttendance:
    def test_default_sort_is_date_desc_then_emp_code_asc(self, seeded):
        items = seeded.get("/attendance").json()["items"]
        keys = [(r["date"], r["emp_code"]) for r in items]
        assert keys == [
            ("2026-07-14", "EMP0002"),
            ("2026-07-08", "EMP0003"), ("2026-07-08", "EMP0004"),
            ("2026-07-07", "EMP0001"), ("2026-07-07", "EMP0004"), ("2026-07-07", "EMP0006"),
            ("2026-07-06", "EMP0001"), ("2026-07-06", "EMP0003"), ("2026-07-06", "EMP0005"),
        ]

    def test_filters_and_total(self, seeded):
        b = seeded.get("/attendance", params={"emp_code": "EMP0004"}).json()
        assert b["total"] == 2 and [r["date"] for r in b["items"]] == ["2026-07-08", "2026-07-07"]
        b = seeded.get("/attendance", params={"status": "ABSENT"}).json()
        assert b["total"] == 1 and b["items"][0]["emp_code"] == "EMP0004"
        b = seeded.get("/attendance", params={"date_from": "2026-07-07", "date_to": "2026-07-07"}).json()
        assert b["total"] == 3                                   # both bounds are inclusive
        b = seeded.get("/attendance", params={"date_from": "2026-07-08"}).json()
        assert b["total"] == 3
        b = seeded.get("/attendance", params={"date_to": "2026-07-06"}).json()
        assert b["total"] == 3
        b = seeded.get("/attendance", params={"emp_code": "EMP0001", "status": "PRESENT", "date_from": "2026-07-07"}).json()
        assert b["total"] == 1 and b["items"][0]["date"] == "2026-07-07"

    def test_total_ignores_pagination_but_follows_filters(self, seeded):
        b = seeded.get("/attendance", params={"page": 2, "page_size": 2, "date_from": "2026-07-07"}).json()
        assert b["total"] == 6 and len(b["items"]) == 2 and (b["page"], b["page_size"]) == (2, 2)

    def test_pagination_walks_every_row_once(self, seeded):
        seen = []
        for page in (1, 2, 3, 4):
            seen += [(r["date"], r["emp_code"]) for r in seeded.get("/attendance", params={"page": page, "page_size": 3}).json()["items"]]
        assert len(seen) == 9 and len(set(seen)) == 9          # every row exactly once, none skipped
        by_date_desc_then_code = sorted(sorted(seen, key=lambda k: k[1]), key=lambda k: k[0], reverse=True)
        assert seen == by_date_desc_then_code

    def test_page_beyond_the_end(self, seeded):
        b = seeded.get("/attendance", params={"page": 50}).json()
        assert b["items"] == [] and b["total"] == 9

    @pytest.mark.parametrize("params,code", [
        ({"date_from": "2026-07-09", "date_to": "2026-07-08"}, 422),
        ({"date_from": "2026-13-01"}, 422), ({"date_to": "2026-02-30"}, 422), ({"date_from": "20260706"}, 422),
        ({"date_from": "yesterday"}, 422),
        ({"status": "BOGUS"}, 422), ({"status": "present"}, 422),
        ({"page": 0}, 422), ({"page_size": 101}, 422), ({"page_size": 0}, 422),
        ({"date_from": "2026-07-08", "date_to": "2026-07-08"}, 200),     # equal bounds are fine
    ])
    def test_validation(self, seeded, params, code):
        r = seeded.get("/attendance", params=params)
        assert r.status_code == code
        if code == 422:
            assert isinstance(r.json()["detail"], list)

    def test_record_shape_has_no_id_and_instants_are_ms(self, seeded):
        for r in seeded.get("/attendance", params={"page_size": 100}).json()["items"]:
            assert set(r) == {"emp_code", "date", "status", "punch_in", "punch_out", "work_hours",
                              "late_minutes", "overtime_minutes", "half_day", "history"}
            for k in ("punch_in", "punch_out"):
                assert r[k] is None or (isinstance(r[k], int) and not isinstance(r[k], bool))

    def test_legacy_record_without_history_and_half_day(self, seeded):
        r = seeded.get("/attendance", params={"emp_code": "EMP0002"}).json()["items"][0]
        assert "history" in r and r["history"] == [] and r["half_day"] is False
        assert main.db.attendance_logs.find_one({"emp_code": "EMP0002"}).get("history") is None  # really absent in storage

    def test_missing_derived_fields_default_to_zero_false_empty(self):
        rec = main.serialize_record({"emp_code": "EMP0002", "date": "2026-07-14", "status": "ABSENT"})
        assert rec == {"emp_code": "EMP0002", "date": "2026-07-14", "status": "ABSENT", "punch_in": None,
                       "punch_out": None, "work_hours": None, "late_minutes": 0, "overtime_minutes": 0,
                       "half_day": False, "history": []}

    def test_open_record_and_half_day_and_absent(self, seeded):
        by = {(r["emp_code"], r["date"]): r for r in seeded.get("/attendance", params={"page_size": 100}).json()["items"]}
        open_rec = by[("EMP0006", "2026-07-07")]
        assert open_rec["punch_out"] is None and open_rec["work_hours"] is None
        assert by[("EMP0004", "2026-07-07")]["half_day"] is True and by[("EMP0004", "2026-07-07")]["status"] == "WFH"
        assert by[("EMP0004", "2026-07-08")]["punch_in"] is None

    def test_history_times_are_epoch_ms_even_inside_changes(self, seeded):
        rec = seeded.get("/attendance", params={"emp_code": "EMP0001", "date_from": "2026-07-07"}).json()["items"][0]
        h = rec["history"][0]
        assert h["by"] == "hr.admin" and h["reason"] == "biometric glitch"
        assert h["at"] == ms(datetime(2026, 7, 8, 5, 0, tzinfo=UTC))
        assert h["changes"]["punch_in"] == {"from": ms(datetime(2026, 7, 7, 4, 35, tzinfo=UTC)),
                                            "to": ms(datetime(2026, 7, 7, 3, 58, tzinfo=UTC))}
        assert h["changes"]["late_minutes"] == {"from": 35, "to": 0}
        assert h["changes"]["work_hours"] == {"from": 8.42, "to": 9.03}

    def test_overnight_record_instants(self, seeded):
        r = seeded.get("/attendance", params={"emp_code": "EMP0005"}).json()["items"][0]
        assert r["punch_in"] == ms(datetime(2026, 7, 6, 16, 25, tzinfo=UTC))
        assert r["punch_out"] == ms(datetime(2026, 7, 7, 1, 10, tzinfo=UTC)) and r["overtime_minutes"] == 40

    def test_database_does_the_sorting_and_paging(self, seeded, commands):
        seeded.get("/attendance", params={"status": "PRESENT", "page": 2, "page_size": 3})
        finds = [c for n, c in commands if n == "find" and c["find"] == "attendance_logs"]
        assert len(finds) == 1                                          # no unbounded read of the collection
        f = finds[0]
        assert f["limit"] == 3 and f["skip"] == 3
        assert list(f["sort"].items()) == [("date", -1), ("emp_code", 1)]
        assert f["filter"] == {"status": "PRESENT"}
        assert f["projection"] == {"_id": 0}


# --------------------------------------------------------------------------- #
# Indexes, health, configuration
# --------------------------------------------------------------------------- #
class TestIndexes:
    def test_created_idempotently(self, api):
        main.ensure_indexes()
        main.ensure_indexes()                                  # second and third call must not raise
        emp = main.db.employees.index_information()
        att = main.db.attendance_logs.index_information()
        assert emp["uq_emp_code"]["unique"] and emp["uq_emp_code"]["key"] == [("emp_code", 1)]
        assert att["uq_emp_code_date"]["unique"] and att["uq_emp_code_date"]["key"] == [("emp_code", 1), ("date", 1)]
        assert att["ix_date_desc_emp_code"]["key"] == [("date", -1), ("emp_code", 1)]
        assert "ix_department_emp_code" in emp and "ix_status_date_emp_code" in att

    def test_created_by_application_startup(self):
        main.db.employees.drop()
        main.db.attendance_logs.drop()
        from fastapi.testclient import TestClient
        with TestClient(main.app):                              # entering the context runs the lifespan
            pass
        assert "uq_emp_code" in main.db.employees.index_information()
        assert "uq_emp_code_date" in main.db.attendance_logs.index_information()

    def test_database_rejects_duplicates_even_if_the_code_were_bypassed(self, api):
        main.db.attendance_logs.insert_one({"emp_code": "EMP0001", "date": "2026-07-06", "status": "PRESENT"})
        with pytest.raises(main.DuplicateKeyError):
            main.db.attendance_logs.insert_one({"emp_code": "EMP0001", "date": "2026-07-06", "status": "WFH"})


class TestHealth:
    def test_ok(self, api):
        r = api.get("/health")
        assert r.status_code == 200 and r.json() == {"status": "ok"}

    def test_503_when_mongodb_is_down(self, api, monkeypatch):
        dead = main.MongoClient("mongodb://127.0.0.1:1", serverSelectionTimeoutMS=300)
        monkeypatch.setattr(main, "client", dead)
        r = api.get("/health")
        assert r.status_code == 503 and isinstance(r.json()["detail"], str)


class TestConfiguration:
    def _run(self, tmp_path, env_extra):
        """Copy app/main.py next to a .env file and import it in a clean interpreter."""
        pkg = tmp_path / "app"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "main.py").write_text(pathlib.Path(main.__file__).read_text())
        (tmp_path / ".env").write_text("MONGO_URI=mongodb://from-dotenv:1\nMONGO_DB=db_from_dotenv\n")
        env = {k: v for k, v in os.environ.items() if k not in ("MONGO_URI", "MONGO_DB")}
        env.update(env_extra)
        code = "import app.main as m; print(m.MONGO_URI, m.MONGO_DB)"
        out = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env, capture_output=True, text=True)
        assert out.returncode == 0, out.stderr
        return out.stdout.split()

    def test_real_environment_wins_over_dotenv(self, tmp_path):
        uri, name = self._run(tmp_path, {"MONGO_URI": "mongodb://from-env:2", "MONGO_DB": "db_from_env"})
        assert (uri, name) == ("mongodb://from-env:2", "db_from_env")

    def test_dotenv_is_used_when_variable_is_not_set(self, tmp_path):
        uri, name = self._run(tmp_path, {})
        assert (uri, name) == ("mongodb://from-dotenv:1", "db_from_dotenv")

    def test_partial_override(self, tmp_path):
        uri, name = self._run(tmp_path, {"MONGO_DB": "only_db_from_env"})
        assert (uri, name) == ("mongodb://from-dotenv:1", "only_db_from_env")

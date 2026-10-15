"""POST /attendance/punch-out. Expected numbers are worked out by hand from R1-R5 in the comments."""
import threading
from datetime import datetime, timedelta, timezone

import pytest

from app import main
from tests.conftest import employee_body

IST, UTC = main.IST, timezone.utc


def ist(d, h, m=0, s=0, month=7, year=2026):
    return datetime(year, month, d, h, m, s, tzinfo=IST)


def ms(dt):
    return main.dt_to_ms(dt)


@pytest.fixture()
def emps(api):
    api.post("/employees", json=employee_body("EMP0001"))                                           # 09:30-18:30
    api.post("/employees", json=employee_body("EMP0005", shift_start="22:00", shift_end="06:00"))   # overnight
    return api


def pin(api, code, when, **extra):
    r = api.post("/attendance/punch-in", json={"emp_code": code, "punched_at": ms(when) if isinstance(when, datetime) else when, **extra})
    assert r.status_code == 201, r.text
    return r.json()


def pout(api, code, when=None):
    body = {"emp_code": code}
    if when is not None:
        body["punched_at"] = ms(when) if isinstance(when, datetime) else when
    return api.post("/attendance/punch-out", json=body)


def stored(code, date):
    return main.db.attendance_logs.find_one({"emp_code": code, "date": date})


# --------------------------------------------------------------------------- #
class TestOrdinaryShift:
    def test_response_schema_and_values(self, emps):
        pin(emps, "EMP0001", ist(6, 9, 28))                       # on time
        r = pout(emps, "EMP0001", ist(6, 18, 35))                 # 9h07m = 32820 s -> (32820+18)//36 = 912 -> 9.12
        assert r.status_code == 200
        assert r.json() == {
            "emp_code": "EMP0001", "date": "2026-07-06", "status": "PRESENT",
            "punch_in": ms(ist(6, 9, 28)), "punch_out": ms(ist(6, 18, 35)),
            "work_hours": 9.12, "late_minutes": 0,
            "overtime_minutes": 0,                                # only 5 minutes after 18:30: below the 30 minimum
            "half_day": False, "history": [],
        }

    def test_late_stays_as_stored_and_overtime_counts(self, emps):
        pin(emps, "EMP0001", ist(6, 10, 5))                       # 35 minutes late
        r = pout(emps, "EMP0001", ist(6, 19, 10)).json()          # 9h05m = 32700 s -> 9.08; 40 min after 18:30
        assert (r["late_minutes"], r["work_hours"], r["overtime_minutes"], r["half_day"]) == (35, 9.08, 40, False)

    def test_stored_values_and_no_history_written(self, emps):
        pin(emps, "EMP0001", ist(6, 9, 28))
        pout(emps, "EMP0001", ist(6, 18, 35))
        doc = stored("EMP0001", "2026-07-06")
        assert doc["punch_out"] == datetime(2026, 7, 6, 13, 5, tzinfo=UTC)      # 18:35 IST stored as UTC
        assert doc["work_hours"] == 9.12 and doc["history"] == []               # punch-out is not a manual correction

    @pytest.mark.parametrize("out,expected", [
        (ist(6, 18, 29, 59), 0), (ist(6, 18, 59, 59), 0),         # 29 whole minutes: below the minimum
        (ist(6, 19, 0, 0), 30),                                   # exactly 30
        (ist(6, 19, 0, 59), 30),                                  # floor, not round
        (ist(6, 19, 30, 0), 60),
        (ist(6, 17, 0, 0), 0),                                    # left early
    ])
    def test_overtime_threshold(self, emps, out, expected):
        pin(emps, "EMP0001", ist(6, 9, 0))
        assert pout(emps, "EMP0001", out).json()["overtime_minutes"] == expected

    def test_milliseconds_are_truncated_before_calculating(self, emps):
        pin(emps, "EMP0001", ist(6, 9, 0))
        r = pout(emps, "EMP0001", ms(ist(6, 18, 59, 59)) + 900).json()           # 18:59:59.900 counts as 18:59:59
        assert r["overtime_minutes"] == 0 and r["punch_out"] == ms(ist(6, 18, 59, 59))
        assert main.dt_to_ms(stored("EMP0001", "2026-07-06")["punch_out"]) == ms(ist(6, 18, 59, 59))

    def test_work_hours_use_whole_seconds_of_both_instants(self, emps):
        pin(emps, "EMP0001", ms(ist(6, 9, 0)) + 999)                              # 09:00:00.999 -> 09:00:00
        r = pout(emps, "EMP0001", ms(ist(6, 9, 7, 30)) + 999).json()              # 09:07:30.999 -> 09:07:30 = 450 s
        assert r["work_hours"] == 0.13                                            # half-up on the 0.125 tie


class TestRoundingAndHalfDay:
    @pytest.mark.parametrize("out,hours,half", [
        (ist(6, 9, 7, 30), 0.13, True),       # 450 s = 0.125 h exactly: half-up -> 0.13 (round() would say 0.12)
        (ist(6, 9, 37, 30), 0.63, True),      # 2250 s = 0.625 h: half-up -> 0.63
        (ist(6, 13, 29, 41), 4.49, True),     # 16181 s = 4.4947 h -> 4.49 < 4.50
        (ist(6, 13, 29, 42), 4.50, False),    # 16182 s = 4.4950 h -> half-up 4.50 -> NOT a half day
        (ist(6, 13, 0, 0), 4.00, True),
        (ist(6, 13, 30, 0), 4.50, False),
        (ist(6, 9, 0, 1), 0.0, True),         # one second of work -> 0.00 hours
    ])
    def test_boundaries(self, emps, out, hours, half):
        pin(emps, "EMP0001", ist(6, 9, 0))
        r = pout(emps, "EMP0001", out).json()
        assert r["work_hours"] == hours and r["half_day"] is half

    def test_nine_hours_is_not_half_day(self, emps):
        pin(emps, "EMP0001", ist(6, 9, 0))
        assert pout(emps, "EMP0001", ist(6, 18, 0)).json()["half_day"] is False


class TestOvernight:
    def test_shift_that_ends_the_next_morning(self, emps):
        d = pin(emps, "EMP0005", ist(6, 21, 55))                  # before 22:00: not late
        assert d["date"] == "2026-07-06" and d["late_minutes"] == 0
        r = pout(emps, "EMP0005", ist(7, 6, 40))                  # across midnight
        assert r.status_code == 200
        b = r.json()
        # 21:55 -> 06:40 = 8h45m = 31500 s -> 8.75 ; shift ends 06:00 on the 7th -> 40 min overtime
        assert (b["date"], b["work_hours"], b["overtime_minutes"], b["half_day"]) == ("2026-07-06", 8.75, 40, False)
        assert b["punch_out"] == ms(ist(7, 6, 40))

    def test_overtime_is_measured_against_next_day_shift_end(self, emps):
        pin(emps, "EMP0005", ist(6, 21, 55))
        b = pout(emps, "EMP0005", ist(7, 5, 30)).json()           # leaves 30 min early: no overtime
        assert (b["work_hours"], b["overtime_minutes"]) == (7.58, 0)       # 27300 s -> (27318)//36 = 758

    def test_punch_in_after_midnight_belongs_to_the_previous_day(self, emps):
        d = pin(emps, "EMP0005", ist(7, 0, 30))
        assert d["date"] == "2026-07-06" and d["late_minutes"] == 150      # 150 min after 22:00 on the 6th
        b = pout(emps, "EMP0005", ist(7, 7, 0)).json()                     # 6h30m ; 60 min after 06:00
        assert (b["date"], b["work_hours"], b["overtime_minutes"], b["late_minutes"]) == ("2026-07-06", 6.5, 60, 150)

    def test_day_shift_worked_past_midnight_measures_overtime_from_its_own_day(self, emps):
        pin(emps, "EMP0001", ist(6, 9, 0))
        b = pout(emps, "EMP0001", ist(7, 1, 0)).json()            # 16 h ; 18:30 -> 01:00 = 390 min
        assert (b["date"], b["work_hours"], b["overtime_minutes"]) == ("2026-07-06", 16.0, 390)

    def test_overnight_record_is_found_although_punch_out_is_on_the_next_ist_date(self, emps):
        pin(emps, "EMP0005", ist(6, 22, 0))
        assert pout(emps, "EMP0005", ist(7, 6, 0)).status_code == 200
        assert stored("EMP0005", "2026-07-06")["punch_out"] is not None
        assert stored("EMP0005", "2026-07-07") is None            # nothing was created for the 7th


class TestChoosingTheRecord:
    def test_closes_the_most_recent_punch_in_at_or_before_punched_at(self, emps):
        pin(emps, "EMP0001", ist(6, 9, 0))
        pin(emps, "EMP0001", ist(7, 9, 0))
        r = pout(emps, "EMP0001", ist(6, 18, 0))                  # before the 7th's punch-in -> the 6th
        assert r.status_code == 200 and r.json()["date"] == "2026-07-06"
        assert stored("EMP0001", "2026-07-07")["punch_out"] is None
        r = pout(emps, "EMP0001", ist(7, 18, 0))
        assert r.status_code == 200 and r.json()["date"] == "2026-07-07"

    def test_most_recent_record_already_closed_is_409_even_if_an_older_one_is_open(self, emps):
        pin(emps, "EMP0001", ist(6, 9, 0))
        pin(emps, "EMP0001", ist(7, 9, 0))
        assert pout(emps, "EMP0001", ist(7, 18, 0)).status_code == 200      # closes the 7th; the 6th stays open
        r = pout(emps, "EMP0001", ist(7, 20, 0))
        assert r.status_code == 409 and isinstance(r.json()["detail"], str)
        assert stored("EMP0001", "2026-07-06")["punch_out"] is None

    def test_other_employees_are_never_touched(self, emps):
        pin(emps, "EMP0001", ist(6, 9, 0))
        pin(emps, "EMP0005", ist(6, 21, 55))
        pout(emps, "EMP0001", ist(6, 18, 0))
        assert stored("EMP0005", "2026-07-06")["punch_out"] is None

    def test_default_punched_at_is_now(self, emps):
        pin(emps, "EMP0001", ms(datetime.now(UTC)) - 3_600_000)             # an hour ago
        r = emps.post("/attendance/punch-out", json={"emp_code": "EMP0001"})
        assert r.status_code == 200
        assert 0.99 <= r.json()["work_hours"] <= 1.02 and r.json()["punch_out"] % 1000 == 0


class TestErrors:
    def test_unknown_employee_404(self, api):
        r = pout(api, "EMP9999", ist(6, 18, 0))
        assert r.status_code == 404 and isinstance(r.json()["detail"], str)

    def test_no_punch_in_at_all_404(self, emps):
        assert pout(emps, "EMP0001", ist(6, 18, 0)).status_code == 404

    def test_punched_at_before_every_punch_in_404(self, emps):
        pin(emps, "EMP0001", ist(6, 9, 0))
        assert pout(emps, "EMP0001", ist(6, 8, 0)).status_code == 404

    def test_only_an_absent_record_is_not_a_punch_in_404(self, emps):
        main.db.attendance_logs.insert_one({"emp_code": "EMP0001", "date": "2026-07-06", "status": "ABSENT",
                                            "punch_in": None, "punch_out": None, "work_hours": None})
        assert pout(emps, "EMP0001", ist(6, 18, 0)).status_code == 404

    def test_record_of_an_employee_that_does_not_exist_is_404(self, api):
        main.db.attendance_logs.insert_one({"emp_code": "EMP0777", "date": "2026-07-06", "status": "PRESENT",
                                            "punch_in": ist(6, 9, 0).astimezone(UTC), "punch_out": None})
        assert pout(api, "EMP0777", ist(6, 18, 0)).status_code == 404       # no employee -> no shift to calculate with
        assert stored("EMP0777", "2026-07-06")["punch_out"] is None

    def test_already_punched_out_is_409_even_when_the_new_time_is_also_invalid(self, emps):
        pin(emps, "EMP0001", ist(6, 9, 0))
        assert pout(emps, "EMP0001", ist(6, 18, 0)).status_code == 200
        assert pout(emps, "EMP0001", ist(6, 9, 0)).status_code == 409       # == punch_in would be 422 on an open record

    def test_second_punch_out_is_409_and_changes_nothing(self, emps):
        pin(emps, "EMP0001", ist(6, 9, 0))
        assert pout(emps, "EMP0001", ist(6, 18, 0)).status_code == 200
        before = stored("EMP0001", "2026-07-06")
        r = pout(emps, "EMP0001", ist(6, 19, 0))
        assert r.status_code == 409 and isinstance(r.json()["detail"], str)
        assert stored("EMP0001", "2026-07-06") == before

    def test_equal_to_punch_in_is_422(self, emps):
        pin(emps, "EMP0001", ist(6, 9, 0))
        r = pout(emps, "EMP0001", ist(6, 9, 0))
        assert r.status_code == 422 and isinstance(r.json()["detail"], list)
        assert stored("EMP0001", "2026-07-06")["punch_out"] is None

    def test_sub_second_difference_truncates_to_equal_and_is_422(self, emps):
        pin(emps, "EMP0001", ist(6, 9, 0))
        assert pout(emps, "EMP0001", ms(ist(6, 9, 0)) + 999).status_code == 422

    def test_exactly_24_hours_is_allowed_more_is_422(self, emps):
        pin(emps, "EMP0001", ist(6, 9, 0))
        assert pout(emps, "EMP0001", ist(6, 9, 0) + timedelta(hours=24, seconds=1)).status_code == 422
        assert stored("EMP0001", "2026-07-06")["punch_out"] is None
        r = pout(emps, "EMP0001", ist(7, 9, 0))                              # exactly 24 h
        assert r.status_code == 200 and r.json()["work_hours"] == 24.0
        assert r.json()["overtime_minutes"] == 870                           # 18:30 -> 09:00 next day

    @pytest.mark.parametrize("bad", [1783312500, 99_999_999_999, 4_102_444_800_001, 0, -1,
                                     1783312500000.0, "1783312500000", True])
    def test_invalid_punched_at_is_422(self, emps, bad):
        pin(emps, "EMP0001", ist(6, 9, 0))
        assert emps.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": bad}).status_code == 422

    def test_missing_emp_code_is_422(self, api):
        assert api.post("/attendance/punch-out", json={}).status_code == 422


class TestLegacyAndIndexes:
    def test_legacy_open_record_without_optional_fields(self, emps):
        main.db.attendance_logs.insert_one({"emp_code": "EMP0001", "date": "2026-07-06", "status": "PRESENT",
                                            "punch_in": datetime(2026, 7, 6, 3, 30, tzinfo=UTC)})   # 09:00 IST, nothing else
        r = pout(emps, "EMP0001", ist(6, 19, 0))
        assert r.status_code == 200
        b = r.json()
        assert (b["work_hours"], b["overtime_minutes"], b["half_day"], b["late_minutes"], b["history"]) == (10.0, 30, False, 0, [])

    def test_lookup_index_exists(self, api):
        main.ensure_indexes()
        assert main.db.attendance_logs.index_information()["ix_emp_code_punch_in_desc"]["key"] == [("emp_code", 1), ("punch_in", -1)]

    def test_lookup_uses_the_punch_in_sort_in_the_database(self, emps, commands):
        pin(emps, "EMP0001", ist(6, 9, 0))
        commands.clear()
        pout(emps, "EMP0001", ist(6, 18, 0))
        f = [c for n, c in commands if n == "find" and c["find"] == "attendance_logs"][0]
        assert list(f["sort"].items()) == [("punch_in", -1)] and f["limit"] == 1
        assert f["filter"]["emp_code"] == "EMP0001" and "$lte" in f["filter"]["punch_in"]


class TestConcurrency:
    def test_simultaneous_punch_outs_exactly_one_200(self, emps, atomic_updates):
        pin(emps, "EMP0001", ist(6, 9, 0))
        results, barrier = [], threading.Barrier(12)

        def go():
            barrier.wait()
            results.append(pout(emps, "EMP0001", ist(6, 18, 0)).status_code)

        ts = [threading.Thread(target=go) for _ in range(12)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert sorted(results) == [200] + [409] * 11
        assert stored("EMP0001", "2026-07-06")["work_hours"] == 9.0

    def test_simultaneous_punch_outs_with_different_times_keep_one_consistent_result(self, emps, atomic_updates):
        pin(emps, "EMP0001", ist(6, 9, 0))
        results, barrier = {}, threading.Barrier(8)

        def go(i):
            barrier.wait()
            results[i] = pout(emps, "EMP0001", ist(6, 17, i)).status_code

        ts = [threading.Thread(target=go, args=(i,)) for i in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert sorted(results.values()) == [200] + [409] * 7
        winner = [i for i, c in results.items() if c == 200][0]
        doc = stored("EMP0001", "2026-07-06")
        assert doc["punch_out"] == ist(6, 17, winner).astimezone(UTC)
        assert doc["work_hours"] == main.compute_work_hours(ist(6, 9, 0), ist(6, 17, winner))   # values match the winner

    def test_losing_the_race_between_read_and_write_is_409(self, emps, monkeypatch):
        pin(emps, "EMP0001", ist(6, 9, 0))
        original = main.close_record

        def rival_wins_first(rec, values):
            main.db.attendance_logs.update_one({"_id": rec["_id"]}, {"$set": {"punch_out": ist(6, 17, 0).astimezone(UTC), "work_hours": 8.0}})
            return original(rec, values)

        monkeypatch.setattr(main, "close_record", rival_wins_first)
        assert pout(emps, "EMP0001", ist(6, 18, 0)).status_code == 409
        assert stored("EMP0001", "2026-07-06")["work_hours"] == 8.0          # the rival's values survive

    def test_punch_in_corrected_between_read_and_write_is_409_not_stale_values(self, emps, monkeypatch):
        pin(emps, "EMP0001", ist(6, 9, 0))
        original = main.close_record

        def punch_in_corrected_meanwhile(rec, values):
            main.db.attendance_logs.update_one({"_id": rec["_id"]}, {"$set": {"punch_in": ist(6, 10, 0).astimezone(UTC)}})
            return original(rec, values)

        monkeypatch.setattr(main, "close_record", punch_in_corrected_meanwhile)
        assert pout(emps, "EMP0001", ist(6, 18, 0)).status_code == 409
        assert stored("EMP0001", "2026-07-06")["punch_out"] is None

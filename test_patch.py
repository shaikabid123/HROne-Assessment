"""PATCH /attendance/{emp_code}/{date}. Expected numbers are worked out by hand from R1-R5."""
import threading
from datetime import datetime, timedelta, timezone

import pytest

from app import main
from tests.conftest import employee_body

IST, UTC = main.IST, timezone.utc
D6 = "2026-07-06"


def ist(d, h, m=0, s=0):
    return datetime(2026, 7, d, h, m, s, tzinfo=IST)


def ms(dt):
    return main.dt_to_ms(dt)


@pytest.fixture()
def emps(api):
    api.post("/employees", json=employee_body("EMP0001"))                                           # 09:30-18:30
    api.post("/employees", json=employee_body("EMP0005", shift_start="22:00", shift_end="06:00"))   # overnight
    return api


def punch_in(api, code, when):
    r = api.post("/attendance/punch-in", json={"emp_code": code, "punched_at": ms(when)})
    assert r.status_code == 201, r.text


def punch_out(api, code, when):
    r = api.post("/attendance/punch-out", json={"emp_code": code, "punched_at": ms(when)})
    assert r.status_code == 200, r.text


def patch(api, code="EMP0001", date=D6, reason="fix punch", by="hr.admin", **body):
    return api.patch(f"/attendance/{code}/{date}", json={"reason": reason, "regularized_by": by, **body})


def stored(code="EMP0001", date=D6):
    return main.db.attendance_logs.find_one({"emp_code": code, "date": date})


@pytest.fixture()
def closed(emps):
    """EMP0001, 10:05 -> 18:35: late 35, work_hours 8.50, overtime 0 (5 min), half_day false."""
    punch_in(emps, "EMP0001", ist(6, 10, 5))
    punch_out(emps, "EMP0001", ist(6, 18, 35))
    return emps


@pytest.fixture()
def open_rec(emps):
    punch_in(emps, "EMP0001", ist(6, 9, 0))
    return emps


@pytest.fixture()
def overnight(emps):
    """EMP0005, 21:55 (6th) -> 06:40 (7th): late 0, work_hours 8.75, overtime 40."""
    punch_in(emps, "EMP0005", ist(6, 21, 55))
    punch_out(emps, "EMP0005", ist(7, 6, 40))
    return emps


def absent_record(**over):
    doc = {"emp_code": "EMP0001", "date": D6, "status": "ABSENT", "punch_in": None, "punch_out": None,
           "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": []}
    doc.update(over)
    main.db.attendance_logs.insert_one(doc)


# --------------------------------------------------------------------------- #
class TestCorrectingTimes:
    def test_the_contract_example_punch_in_earlier(self, closed):
        r = patch(closed, punch_in=ms(ist(6, 9, 28)), reason="biometric glitch")
        assert r.status_code == 200
        b = r.json()
        # 09:28 -> 18:35 = 9h07m = 32820 s -> 9.12 ; 09:28 is before 09:30 -> late 0 ; overtime still 5 min -> 0
        assert (b["punch_in"], b["late_minutes"], b["work_hours"], b["overtime_minutes"], b["half_day"]) == (ms(ist(6, 9, 28)), 0, 9.12, 0, False)
        assert (b["emp_code"], b["date"], b["status"], b["punch_out"]) == ("EMP0001", D6, "PRESENT", ms(ist(6, 18, 35)))
        assert len(b["history"]) == 1
        h = b["history"][0]
        assert h["by"] == "hr.admin" and h["reason"] == "biometric glitch" and isinstance(h["at"], int)
        assert h["changes"] == {
            "punch_in": {"from": ms(ist(6, 10, 5)), "to": ms(ist(6, 9, 28))},
            "late_minutes": {"from": 35, "to": 0},
            "work_hours": {"from": 8.5, "to": 9.12},
        }                                                       # overtime / half_day did not change: not listed

    def test_history_is_stored_with_bson_dates_for_instants(self, closed):
        patch(closed, punch_in=ms(ist(6, 9, 28)))
        e = stored()["history"][0]
        assert isinstance(e["at"], datetime) and e["at"].tzinfo is not None
        assert e["changes"]["punch_in"]["from"] == ist(6, 10, 5).astimezone(UTC)
        assert e["changes"]["punch_in"]["to"] == ist(6, 9, 28).astimezone(UTC)
        assert e["changes"]["late_minutes"] == {"from": 35, "to": 0}
        assert abs((datetime.now(UTC) - e["at"]).total_seconds()) < 30

    def test_punch_out_corrected_recomputes_overtime(self, closed):
        b = patch(closed, punch_out=ms(ist(6, 19, 10))).json()
        # 10:05 -> 19:10 = 9h05m -> 9.08 ; 40 min after 18:30
        assert (b["work_hours"], b["overtime_minutes"], b["late_minutes"]) == (9.08, 40, 35)
        assert set(b["history"][0]["changes"]) == {"punch_out", "work_hours", "overtime_minutes"}
        assert b["history"][0]["changes"]["overtime_minutes"] == {"from": 0, "to": 40}

    def test_punch_out_added_to_an_open_record(self, open_rec):
        b = patch(open_rec, punch_out=ms(ist(6, 13, 0))).json()
        assert (b["work_hours"], b["overtime_minutes"], b["half_day"]) == (4.0, 0, True)
        assert b["history"][0]["changes"] == {
            "punch_out": {"from": None, "to": ms(ist(6, 13, 0))},
            "work_hours": {"from": None, "to": 4.0},
            "half_day": {"from": False, "to": True},
        }

    @pytest.mark.parametrize("out,hours,half", [
        (ist(6, 13, 29, 41), 4.49, True),        # 16181 s
        (ist(6, 13, 29, 42), 4.50, False),       # 16182 s = 4.495 h -> half-up 4.50 -> not a half day
        (ist(6, 13, 30, 0), 4.50, False),
        (ist(6, 9, 7, 30), 0.13, True),          # 450 s = 0.125 h -> 0.13
    ])
    def test_rounding_and_half_day_boundaries(self, open_rec, out, hours, half):
        b = patch(open_rec, punch_out=ms(out)).json()
        assert b["work_hours"] == hours and b["half_day"] is half

    @pytest.mark.parametrize("h,m,s,late", [(9, 40, 0, 0), (9, 40, 1, 10), (10, 15, 59, 45), (9, 30, 0, 0)])
    def test_late_rule_through_patch(self, closed, h, m, s, late):
        assert patch(closed, punch_in=ms(ist(6, h, m, s))).json()["late_minutes"] == late

    def test_instants_are_truncated_stored_returned_and_audited(self, closed):
        b = patch(closed, punch_in=ms(ist(6, 9, 28)) + 900, punch_out=ms(ist(6, 18, 35)) + 999).json()
        assert b["punch_in"] == ms(ist(6, 9, 28)) and b["history"][0]["changes"]["punch_in"]["to"] == ms(ist(6, 9, 28))
        assert main.dt_to_ms(stored()["punch_in"]) == ms(ist(6, 9, 28))
        assert b["work_hours"] == 9.12

    def test_overtime_threshold_through_patch(self, closed):
        assert patch(closed, punch_out=ms(ist(6, 18, 59, 59))).json()["overtime_minutes"] == 0
        assert patch(closed, punch_out=ms(ist(6, 19, 0, 0))).json()["overtime_minutes"] == 30


class TestOvernightCorrections:
    def test_punch_in_moved_later_the_same_evening(self, overnight):
        b = patch(overnight, "EMP0005", punch_in=ms(ist(6, 22, 30))).json()
        # late: 30 min after 22:00 ; work 22:30 -> 06:40 = 8h10m = 29400 s -> 8.17 ; overtime (06:40 vs 06:00) unchanged
        assert (b["late_minutes"], b["work_hours"], b["overtime_minutes"]) == (30, 8.17, 40)
        assert set(b["history"][0]["changes"]) == {"punch_in", "late_minutes", "work_hours"}

    def test_punch_out_corrected_across_midnight(self, overnight):
        b = patch(overnight, "EMP0005", punch_out=ms(ist(7, 7, 10))).json()
        # 21:55 -> 07:10 = 9h15m = 33300 s -> 9.25 ; 70 min after 06:00 on the 7th
        assert (b["work_hours"], b["overtime_minutes"]) == (9.25, 70)

    def test_punch_in_after_midnight_still_belongs_to_the_record_date(self, overnight):
        b = patch(overnight, "EMP0005", punch_in=ms(ist(7, 0, 30))).json()   # 00:30 on the 7th is the shift of the 6th
        assert (b["late_minutes"], b["work_hours"]) == (150, 6.17)           # 22:00 -> 00:30 ; 00:30 -> 06:40 = 22200 s

    def test_punch_in_that_would_move_to_the_next_attendance_date_is_422(self, overnight):
        r = patch(overnight, "EMP0005", punch_in=ms(ist(7, 7, 0)))           # 07:00 on the 7th is the shift of the 7th
        assert r.status_code == 422 and isinstance(r.json()["detail"], list)
        assert patch(overnight, "EMP0005", punch_in=ms(ist(5, 22, 0))).status_code == 422   # the shift of the 5th

    def test_exactly_at_shift_end_belongs_to_the_next_day(self, overnight):
        assert patch(overnight, "EMP0005", punch_in=ms(ist(7, 5, 59, 59))).status_code == 200
        assert patch(overnight, "EMP0005", punch_in=ms(ist(7, 6, 0, 0))).status_code == 422


class TestRulesFor422:
    def test_punch_in_must_stay_on_the_attendance_date(self, closed):
        assert patch(closed, punch_in=ms(ist(7, 9, 0))).status_code == 422
        assert patch(closed, punch_in=ms(ist(5, 9, 0))).status_code == 422
        assert patch(closed, punch_in=ms(datetime(2026, 7, 5, 20, 0, tzinfo=UTC))).status_code == 200   # 01:30 IST on the 6th
        assert stored()["history"] and len(stored()["history"]) == 1                                    # only the success was logged

    def test_punch_out_must_be_after_punch_in_and_within_24_hours(self, open_rec):
        for bad in (ist(6, 9, 0), ist(6, 8, 0), ist(6, 9, 0) + timedelta(hours=24, seconds=1)):
            r = patch(open_rec, punch_out=ms(bad))
            assert r.status_code == 422, bad
        assert stored()["punch_out"] is None and stored()["history"] == []
        b = patch(open_rec, punch_out=ms(ist(7, 9, 0))).json()               # exactly 24 h
        assert b["work_hours"] == 24.0 and b["overtime_minutes"] == 870

    def test_new_punch_in_after_the_existing_punch_out_is_422(self, closed):
        assert patch(closed, punch_in=ms(ist(6, 18, 35))).status_code == 422     # equal
        assert patch(closed, punch_in=ms(ist(6, 18, 36))).status_code == 422     # after
        assert stored()["history"] == []

    def test_punch_in_moved_so_the_gap_exceeds_24_hours_is_422(self, emps):
        punch_in(emps, "EMP0001", ist(6, 9, 0))
        punch_out(emps, "EMP0001", ist(7, 8, 0))                              # 23 h
        assert patch(emps, punch_in=ms(ist(6, 7, 0))).status_code == 422      # 25 h

    def test_absent_or_leave_with_punch_times_is_422(self, closed):
        for status in ("ABSENT", "LEAVE"):
            assert patch(closed, status=status, punch_in=ms(ist(6, 9, 0))).status_code == 422
            assert patch(closed, status=status, punch_out=ms(ist(6, 18, 0))).status_code == 422
        assert stored()["status"] == "PRESENT" and stored()["history"] == []

    def test_punch_times_on_a_record_that_stays_absent_are_422(self, emps):
        absent_record()
        assert patch(emps, punch_in=ms(ist(6, 9, 0))).status_code == 422
        assert patch(emps, status="LEAVE", punch_in=ms(ist(6, 9, 0))).status_code == 422

    def test_presence_status_requires_a_punch_in(self, emps):
        absent_record()
        assert patch(emps, status="PRESENT").status_code == 422
        assert patch(emps, status="WFH", punch_out=ms(ist(6, 18, 0))).status_code == 422
        assert stored()["status"] == "ABSENT" and stored()["history"] == []

    @pytest.mark.parametrize("body", [
        {},                                                  # nothing to change
        {"status": "PRESENT"},                               # same status
        {"punch_in": ms(ist(6, 10, 5))},                     # same punch_in
        {"punch_out": ms(ist(6, 18, 35))},                   # same punch_out
        {"punch_in": ms(ist(6, 10, 5)) + 400, "punch_out": ms(ist(6, 18, 35)) + 400, "status": "PRESENT"},   # same after truncation
        {"work_hours": 99.0, "late_minutes": 99, "overtime_minutes": 99, "half_day": True},                  # derived fields are ignored
    ])
    def test_a_request_that_changes_nothing_is_422(self, closed, body):
        r = patch(closed, **body)
        assert r.status_code == 422 and isinstance(r.json()["detail"], list)
        assert r.json()["detail"][0]["loc"] == ["body"]
        assert stored()["history"] == []

    @pytest.mark.parametrize("over", [
        {"reason": "abcd"}, {"reason": ""}, {"reason": "x" * 201},
        {"by": ""}, {"by": "y" * 51},
        {"status": "BOGUS"}, {"status": "present"}, {"status": 1},
        {"punch_in": 1783312500}, {"punch_in": 99_999_999_999}, {"punch_in": 4_102_444_800_001},
        {"punch_in": 1783312500000.0}, {"punch_in": "1783312500000"}, {"punch_in": True},
        {"punch_out": 1783312500}, {"punch_out": "x"}, {"punch_out": -1},
    ])
    def test_invalid_input_is_422(self, closed, over):
        body = {"punch_in": ms(ist(6, 9, 28))}
        body.update({k: v for k, v in over.items() if k != "by"})
        if "by" in over:
            body["by"] = over["by"]
        r = patch(closed, **body)
        assert r.status_code == 422, over
        assert stored()["history"] == []

    def test_reason_and_actor_length_limits_are_inclusive(self, closed):
        assert patch(closed, punch_in=ms(ist(6, 9, 28)), reason="abcde", by="y").status_code == 200
        assert patch(closed, punch_in=ms(ist(6, 9, 20)), reason="x" * 200, by="y" * 50).status_code == 200

    def test_reason_and_regularized_by_are_required(self, closed):
        base = {"punch_in": ms(ist(6, 9, 28))}
        assert closed.patch(f"/attendance/EMP0001/{D6}", json={**base, "regularized_by": "a"}).status_code == 422
        assert closed.patch(f"/attendance/EMP0001/{D6}", json={**base, "reason": "long enough"}).status_code == 422
        assert closed.patch(f"/attendance/EMP0001/{D6}", json={}).status_code == 422

    @pytest.mark.parametrize("date", ["2026-13-01", "2026-02-30", "20260706", "abc", "2026-7-6"])
    def test_malformed_date_in_the_path_is_422(self, closed, date):
        assert patch(closed, date=date, punch_in=ms(ist(6, 9, 28))).status_code == 422


class TestStatusChanges:
    def test_status_only(self, closed):
        b = patch(closed, status="WFH").json()
        assert b["status"] == "WFH" and b["punch_in"] == ms(ist(6, 10, 5)) and b["work_hours"] == 8.5
        assert b["history"][0]["changes"] == {"status": {"from": "PRESENT", "to": "WFH"}}

    def test_absent_clears_times_and_derived_fields(self, closed):
        b = patch(closed, status="ABSENT").json()
        assert (b["status"], b["punch_in"], b["punch_out"], b["work_hours"], b["late_minutes"],
                b["overtime_minutes"], b["half_day"]) == ("ABSENT", None, None, None, 0, 0, False)
        assert b["history"][0]["changes"] == {         # overtime 0 -> 0 and half_day false -> false are not changes
            "status": {"from": "PRESENT", "to": "ABSENT"},
            "punch_in": {"from": ms(ist(6, 10, 5)), "to": None},
            "punch_out": {"from": ms(ist(6, 18, 35)), "to": None},
            "work_hours": {"from": 8.5, "to": None},
            "late_minutes": {"from": 35, "to": 0},
        }
        d = stored()
        assert d["punch_in"] is None and d["punch_out"] is None and d["work_hours"] is None

    def test_leave_resets_overtime_and_half_day_when_they_were_set(self, emps):
        punch_in(emps, "EMP0001", ist(6, 9, 0))
        punch_out(emps, "EMP0001", ist(6, 13, 0))                          # 4.00 h -> half day
        b = patch(emps, status="LEAVE").json()
        assert b["half_day"] is False and b["history"][0]["changes"]["half_day"] == {"from": True, "to": False}
        punch_in(emps, "EMP0001", ist(7, 9, 30))
        punch_out(emps, "EMP0001", ist(7, 19, 30))                         # 10.00 h, 60 min overtime
        b = patch(emps, date="2026-07-07", status="ABSENT").json()
        assert b["history"][0]["changes"]["overtime_minutes"] == {"from": 60, "to": 0}
        assert b["history"][0]["changes"]["work_hours"] == {"from": 10.0, "to": None}

    def test_absent_to_present_with_a_punch_in(self, emps):
        absent_record()
        b = patch(emps, status="PRESENT", punch_in=ms(ist(6, 10, 5))).json()
        assert (b["status"], b["late_minutes"], b["punch_out"], b["work_hours"]) == ("PRESENT", 35, None, None)
        assert b["history"][0]["changes"] == {
            "status": {"from": "ABSENT", "to": "PRESENT"},
            "punch_in": {"from": None, "to": ms(ist(6, 10, 5))},
            "late_minutes": {"from": 0, "to": 35},
        }

    def test_absent_to_leave_and_back_on_the_same_status_is_422(self, emps):
        absent_record()
        assert patch(emps, status="LEAVE").status_code == 200
        assert patch(emps, status="LEAVE").status_code == 422

    def test_status_and_times_in_one_request(self, closed):
        b = patch(closed, status="ON_DUTY", punch_out=ms(ist(6, 19, 10))).json()
        assert b["status"] == "ON_DUTY" and b["overtime_minutes"] == 40
        assert set(b["history"][0]["changes"]) == {"status", "punch_out", "work_hours", "overtime_minutes"}


class TestRequestHandling:
    def test_derived_and_identity_fields_in_the_body_are_ignored(self, closed):
        r = closed.patch(f"/attendance/EMP0001/{D6}", json={
            "punch_in": ms(ist(6, 9, 28)), "reason": "fix punch", "regularized_by": "hr",
            "work_hours": 99, "late_minutes": 99, "overtime_minutes": 99, "half_day": True,
            "history": [], "date": "2026-07-01", "emp_code": "EMP0002"})
        assert r.status_code == 200
        b = r.json()
        assert (b["emp_code"], b["date"], b["work_hours"], b["late_minutes"], b["overtime_minutes"], b["half_day"]) == ("EMP0001", D6, 9.12, 0, 0, False)
        assert len(b["history"]) == 1

    def test_explicit_nulls_count_as_omitted(self, closed):
        r = closed.patch(f"/attendance/EMP0001/{D6}", json={"status": None, "punch_out": None, "punch_in": ms(ist(6, 9, 28)),
                                                            "reason": "fix punch", "regularized_by": "hr"})
        assert r.status_code == 200 and r.json()["punch_out"] == ms(ist(6, 18, 35))

    def test_unknown_employee_404(self, emps):
        r = patch(emps, "EMP9999", punch_in=ms(ist(6, 9, 28)))
        assert r.status_code == 404 and isinstance(r.json()["detail"], str)

    def test_no_record_for_that_date_404(self, closed):
        assert patch(closed, date="2026-07-07", punch_in=ms(ist(7, 9, 28))).status_code == 404
        assert patch(closed, "EMP0005", punch_in=ms(ist(6, 9, 28))).status_code == 404   # employee exists, no record

    def test_validation_comes_before_lookup_for_bad_input(self, emps):
        assert patch(emps, "EMP9999", reason="x").status_code == 422

    def test_the_corrected_record_shows_up_in_the_list(self, closed):
        patch(closed, punch_in=ms(ist(6, 9, 28)))
        item = closed.get("/attendance", params={"emp_code": "EMP0001"}).json()["items"][0]
        assert item["late_minutes"] == 0 and item["work_hours"] == 9.12 and len(item["history"]) == 1


class TestHistoryIsAppendOnly:
    def test_second_correction_appends_and_leaves_the_first_untouched(self, closed):
        first = patch(closed, punch_in=ms(ist(6, 9, 28)), reason="first fix", by="a.b").json()["history"][0]
        r = patch(closed, punch_out=ms(ist(6, 19, 10)), reason="second fix", by="c.d").json()
        assert len(r["history"]) == 2 and r["history"][0] == first               # entry 0 byte-for-byte unchanged
        assert (r["history"][1]["by"], r["history"][1]["reason"]) == ("c.d", "second fix")
        assert r["history"][1]["changes"]["punch_out"] == {"from": ms(ist(6, 18, 35)), "to": ms(ist(6, 19, 10))}
        assert r["history"][1]["changes"]["work_hours"] == {"from": 9.12, "to": 9.7}     # 09:28 -> 19:10 = 9h42m
        assert r["history"][1]["at"] >= first["at"]

    def test_punch_in_and_punch_out_never_write_history(self, closed):
        assert stored()["history"] == []
        assert closed.get("/attendance").json()["items"][0]["history"] == []

    def test_a_failed_correction_does_not_touch_the_history(self, closed):
        patch(closed, punch_in=ms(ist(6, 9, 28)))
        before = stored()
        assert patch(closed, punch_in=ms(ist(7, 9, 0))).status_code == 422
        assert patch(closed, punch_in=ms(ist(6, 9, 28))).status_code == 422
        assert stored() == before

    def test_punch_out_after_a_correction_keeps_the_history(self, open_rec):
        patch(open_rec, punch_in=ms(ist(6, 8, 55)), reason="clock was wrong")
        punch_out(open_rec, "EMP0001", ist(6, 18, 0))
        d = stored()
        assert len(d["history"]) == 1 and d["history"][0]["reason"] == "clock was wrong"


class TestLegacyDocuments:
    def legacy(self, **over):
        doc = {"emp_code": "EMP0001", "date": D6, "status": "PRESENT",
               "punch_in": ist(6, 9, 0).astimezone(UTC), "punch_out": ist(6, 18, 0).astimezone(UTC), "work_hours": 9.0}
        doc.update(over)
        main.db.attendance_logs.insert_one(doc)

    def test_missing_history_half_day_late_and_overtime(self, emps):
        self.legacy()
        r = patch(emps, status="WFH")
        assert r.status_code == 200
        b = r.json()
        assert b["history"][0]["changes"] == {"status": {"from": "PRESENT", "to": "WFH"}}   # defaults are not 'changes'
        assert (b["half_day"], b["late_minutes"], b["overtime_minutes"]) == (False, 0, 0)
        assert isinstance(stored()["history"], list) and len(stored()["history"]) == 1       # array created by the append

    def test_history_stored_as_null(self, emps):
        self.legacy(history=None)
        assert patch(emps, status="WFH").status_code == 200
        assert len(stored()["history"]) == 1

    def test_second_correction_on_a_legacy_record_appends(self, emps):
        self.legacy()
        patch(emps, status="WFH")
        r = patch(emps, status="ON_DUTY").json()
        assert [e["changes"]["status"]["to"] for e in r["history"]] == ["WFH", "ON_DUTY"]


# --------------------------------------------------------------------------- #
# Concurrent corrections
# --------------------------------------------------------------------------- #
class TestLostUpdates:
    def _two_plans(self, rec, emp, first, second):
        return (main.plan_regularization(emp, rec, None, first, None), main.plan_regularization(emp, rec, None, second, None))

    def test_a_stale_write_is_rejected_and_the_winners_entry_survives(self, closed):
        emp = main.db.employees.find_one({"emp_code": "EMP0001"})
        rec = stored()                                                    # both "requests" read this state
        (new_a, ch_a), (new_b, ch_b) = self._two_plans(rec, emp, ist(6, 9, 28).astimezone(UTC), ist(6, 9, 15).astimezone(UTC))
        entry = lambda by, ch: {"at": main.now_utc_ms_precision(), "by": by, "reason": "race", "changes": ch}
        assert main.write_correction(rec, new_a, entry("A", ch_a)) is not None
        assert main.write_correction(rec, new_b, entry("B", ch_b)) is None       # B was planned from the old state
        d = stored()
        assert [e["by"] for e in d["history"]] == ["A"] and d["punch_in"] == ist(6, 9, 28).astimezone(UTC)

    def test_endpoint_returns_409_when_a_rival_correction_lands_between_read_and_write(self, closed, monkeypatch):
        original = main.write_correction
        emp = main.db.employees.find_one({"emp_code": "EMP0001"})

        def rival_first(rec, new, entry):
            new_a, ch_a = main.plan_regularization(emp, rec, None, ist(6, 9, 28).astimezone(UTC), None)
            original(rec, new_a, {"at": main.now_utc_ms_precision(), "by": "rival", "reason": "rival fix", "changes": ch_a})
            return original(rec, new, entry)                              # this request now holds a stale view

        monkeypatch.setattr(main, "write_correction", rival_first)
        r = patch(closed, punch_in=ms(ist(6, 9, 15)), by="me")
        assert r.status_code == 409 and isinstance(r.json()["detail"], str)
        d = stored()
        assert [e["by"] for e in d["history"]] == ["rival"]               # nothing lost, nothing overwritten
        assert d["punch_in"] == ist(6, 9, 28).astimezone(UTC)

    def test_a_rival_history_only_change_is_detected_by_the_history_length(self, closed, monkeypatch):
        original = main.write_correction

        def rival_appends(rec, new, entry):
            main.db.attendance_logs.update_one({"_id": rec["_id"]}, {"$push": {"history": {"at": main.now_utc_ms_precision(), "by": "x", "reason": "y", "changes": {}}}})
            return original(rec, new, entry)

        monkeypatch.setattr(main, "write_correction", rival_appends)
        assert patch(closed, punch_in=ms(ist(6, 9, 28))).status_code == 409
        assert len(stored()["history"]) == 1 and stored()["punch_in"] == ist(6, 10, 5).astimezone(UTC)

    def test_punch_out_landing_between_read_and_write_makes_the_patch_409(self, open_rec, monkeypatch):
        original = main.write_correction

        def punch_out_wins(rec, new, entry):
            main.db.attendance_logs.update_one({"_id": rec["_id"]}, {"$set": {"punch_out": ist(6, 17, 0).astimezone(UTC), "work_hours": 8.0}})
            return original(rec, new, entry)

        monkeypatch.setattr(main, "write_correction", punch_out_wins)
        assert patch(open_rec, status="WFH").status_code == 409
        d = stored()
        assert d["status"] == "PRESENT" and d["history"] == [] and d["work_hours"] == 8.0

    def test_legacy_record_without_history_is_also_guarded(self, emps, monkeypatch):
        main.db.attendance_logs.insert_one({"emp_code": "EMP0001", "date": D6, "status": "PRESENT",
                                            "punch_in": ist(6, 9, 0).astimezone(UTC), "punch_out": ist(6, 18, 0).astimezone(UTC), "work_hours": 9.0})
        original = main.write_correction

        def rival_creates_history(rec, new, entry):
            main.db.attendance_logs.update_one({"_id": rec["_id"]}, {"$push": {"history": {"at": main.now_utc_ms_precision(), "by": "x", "reason": "y", "changes": {}}}})
            return original(rec, new, entry)

        monkeypatch.setattr(main, "write_correction", rival_creates_history)
        assert patch(emps, status="WFH").status_code == 409
        assert len(stored()["history"]) == 1


class TestSimultaneousCorrections:
    """Real threads. Need a server with atomic conditional updates (MongoDB); skipped on FerretDB."""

    def test_different_corrections_no_history_entry_is_lost(self, closed, atomic_updates):
        results, barrier = {}, threading.Barrier(10)

        def go(i):
            barrier.wait()
            results[i] = patch(closed, punch_in=ms(ist(6, 9, i)), reason=f"race {i}", by=f"user{i}").status_code

        ts = [threading.Thread(target=go, args=(i,)) for i in range(10)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert set(results.values()) <= {200, 409}, results
        ok = sorted(i for i, c in results.items() if c == 200)
        assert ok, "at least one correction must win"
        history = stored()["history"]
        assert len(history) == len(ok)                                    # every 200 is in the audit trail, nothing extra
        assert sorted(int(e["reason"].split()[1]) for e in history) == ok
        for earlier, later in zip(history, history[1:]):                  # the entries form an unbroken chain
            assert earlier["changes"]["punch_in"]["to"] == later["changes"]["punch_in"]["from"]
        assert stored()["punch_in"] == history[-1]["changes"]["punch_in"]["to"]

    def test_identical_corrections_exactly_one_wins(self, closed, atomic_updates):
        results, barrier = [], threading.Barrier(10)

        def go():
            barrier.wait()
            results.append(patch(closed, punch_in=ms(ist(6, 9, 28))).status_code)

        ts = [threading.Thread(target=go) for _ in range(10)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert results.count(200) == 1 and set(results) <= {200, 409, 422}     # late arrivals see "nothing to change"
        assert len(stored()["history"]) == 1

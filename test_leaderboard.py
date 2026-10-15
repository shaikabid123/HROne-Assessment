"""GET /analytics/leaderboard/late (July 2026, see analytics_helpers)."""
import json
import random

import pytest

from app import main
from tests.analytics_helpers import JUL, add_emp, add_rec, days_of

ROW_KEYS = ["rank", "emp_code", "name", "department", "total_late_minutes", "late_count"]


def get(api, month=JUL, **params):
    return api.get("/analytics/leaderboard/late", params={"month": month, **params})


def rows(api, month=JUL, **params):
    r = get(api, month, **params)
    assert r.status_code == 200, r.text
    return r.json()["items"]


def board(api, month=JUL, **params):
    """[(rank, emp_code)] in the order returned."""
    return [(r["rank"], r["emp_code"]) for r in rows(api, month, **params)]


# --------------------------------------------------------------------------- independent oracle
def oracle(employees, logs, month, limit=10, department=None):
    first, last = days_of(month)[0].isoformat(), days_of(month)[-1].isoformat()
    known = {e["emp_code"]: e for e in employees}
    total, count = {}, {}
    for r in logs:
        minutes = r.get("late_minutes") or 0
        if minutes > 0 and first <= r["date"] <= last and r["emp_code"] in known:
            total[r["emp_code"]] = total.get(r["emp_code"], 0) + minutes
            count[r["emp_code"]] = count.get(r["emp_code"], 0) + 1
    if department is not None:
        total = {c: t for c, t in total.items() if known[c]["department"] == department}
    out = []
    for code, t in total.items():
        rank = 1 + sum(1 for other in total.values() if other > t)            # 1 + how many are strictly worse off
        if rank <= limit:
            out.append({"rank": rank, "emp_code": code, "name": known[code]["name"], "department": known[code]["department"],
                        "total_late_minutes": int(t), "late_count": count[code]})
    return sorted(out, key=lambda r: (-r["total_late_minutes"], r["emp_code"]))


# =========================================================================== a board worked out by hand
@pytest.fixture()
def populated(analytics_api):
    names = {"A1": "Engineering", "A2": "Engineering", "A3": "Engineering", "A4": "Engineering", "B1": "Sales", "B2": "Sales",
             "Z1": "Engineering", "Z2": "Engineering", "Z3": "Sales"}
    for code in reversed(list(names)):                                      # inserted in reverse order on purpose
        add_emp(code, names[code])
    add_rec("A1", "2026-07-06", late=60)
    add_rec("A1", "2026-07-07", late=40)                                    # 100 in 2 records
    add_rec("A2", "2026-07-06", late=50)
    add_rec("A2", "2026-07-07", late=30)
    add_rec("A2", "2026-07-08", late=0)                                     # not late
    add_rec("A2", "2026-07-09")                                             # no late_minutes at all -> 80 in 2 records
    add_rec("A3", "2026-07-04", late=30)                                    # a Saturday counts
    add_rec("A3", "2026-07-06", late=50)                                    # 80 in 2 records
    add_rec("A4", "2026-07-06", late=50)                                    # 50 in 1
    add_rec("A4", "2026-06-30", late=1000)                                  # other months must not count
    add_rec("A4", "2026-08-01", late=1000)
    add_rec("B1", "2026-07-06", late=90)
    add_rec("B2", "2026-07-06", "ABSENT", late=80)                          # stored late minutes count whatever the status
    add_rec("Z2", "2026-07-06", late=0)                                     # logs, but never late
    add_rec("Z3", "2026-07-06")
    add_rec("GHOST", "2026-07-06", late=500)                                # no such employee: ignored
    return analytics_api


class TestWorkedBoard:
    def test_exact_response(self, populated):
        r = get(populated)
        assert r.status_code == 200
        body = r.json()
        assert list(body) == ["month", "items"] and body["month"] == JUL
        # totals 100, 90, 80, 80, 80, 50 -> competition ranks 1, 2, 3, 3, 3, 6 (never 4 or 5)
        assert body["items"] == [
            {"rank": 1, "emp_code": "A1", "name": "Name A1", "department": "Engineering", "total_late_minutes": 100, "late_count": 2},
            {"rank": 2, "emp_code": "B1", "name": "Name B1", "department": "Sales", "total_late_minutes": 90, "late_count": 1},
            {"rank": 3, "emp_code": "A2", "name": "Name A2", "department": "Engineering", "total_late_minutes": 80, "late_count": 2},
            {"rank": 3, "emp_code": "A3", "name": "Name A3", "department": "Engineering", "total_late_minutes": 80, "late_count": 2},
            {"rank": 3, "emp_code": "B2", "name": "Name B2", "department": "Sales", "total_late_minutes": 80, "late_count": 1},
            {"rank": 6, "emp_code": "A4", "name": "Name A4", "department": "Engineering", "total_late_minutes": 50, "late_count": 1},
        ]
        assert all(list(item) == ROW_KEYS for item in body["items"])
        assert all(type(i[k]) is int for i in body["items"] for k in ("rank", "total_late_minutes", "late_count"))

    def test_tied_rows_are_ordered_by_emp_code_although_inserted_in_reverse(self, populated):
        assert [c for rk, c in board(populated) if rk == 3] == ["A2", "A3", "B2"]

    def test_ranks_come_from_the_values_not_from_positions(self, populated):
        # positions would be 1..6; the next rank after the three-way tie is 6
        assert [rk for rk, _ in board(populated)] == [1, 2, 3, 3, 3, 6]

    @pytest.mark.parametrize("limit,expected", [
        (1, ["A1"]),
        (2, ["A1", "B1"]),
        (3, ["A1", "B1", "A2", "A3", "B2"]),     # the tie at rank 3 straddles the boundary: all three stay
        (4, ["A1", "B1", "A2", "A3", "B2"]),     # ranks 4 and 5 do not exist
        (5, ["A1", "B1", "A2", "A3", "B2"]),
        (6, ["A1", "B1", "A2", "A3", "B2", "A4"]),
        (50, ["A1", "B1", "A2", "A3", "B2", "A4"]),
    ])
    def test_limit_is_applied_after_ranking(self, populated, limit, expected):
        assert [c for _, c in board(populated, limit=limit)] == expected

    def test_default_limit_is_ten(self, populated):
        assert len(rows(populated)) == 6 == len(rows(populated, limit=10))

    def test_employees_that_were_never_late_are_not_listed(self, populated):
        codes = {c for _, c in board(populated, limit=50)}
        assert codes.isdisjoint({"Z1", "Z2", "Z3", "GHOST"})                # no logs / late 0 / no late field / no employee

    def test_a_ghost_with_a_huge_total_does_not_shift_anybody_down(self, populated):
        assert board(populated, limit=1) == [(1, "A1")]

    def test_department_filter_ranks_inside_the_department_only(self, populated):
        # Engineering: 100, 80, 80, 50 -> 1, 2, 2, 4
        assert board(populated, department="Engineering", limit=50) == [(1, "A1"), (2, "A2"), (2, "A3"), (4, "A4")]
        # Sales: 90, 80 -> 1, 2 (they were 2 and 3 on the global board)
        assert board(populated, department="Sales", limit=50) == [(1, "B1"), (2, "B2")]

    @pytest.mark.parametrize("limit,expected", [
        (1, [(1, "A1")]),
        (2, [(1, "A1"), (2, "A2"), (2, "A3")]),                             # the tie at rank 2 at the boundary
        (3, [(1, "A1"), (2, "A2"), (2, "A3")]),                             # there is no rank 3
        (4, [(1, "A1"), (2, "A2"), (2, "A3"), (4, "A4")]),
    ])
    def test_limit_and_ties_inside_a_department(self, populated, limit, expected):
        assert board(populated, department="Engineering", limit=limit) == expected

    def test_department_filter_is_exact_and_case_sensitive(self, populated):
        assert rows(populated, department="engineering") == [] and rows(populated, department="Eng") == []
        assert rows(populated, department="Nope") == [] and rows(populated, department="") == []

    def test_department_and_name_come_from_the_employee_document(self, populated):
        row = rows(populated, department="Sales")[0]
        assert (row["name"], row["department"]) == ("Name B1", "Sales")


# =========================================================================== rank shapes
class TestRanking:
    @staticmethod
    def seed(api, totals):
        for n, t in enumerate(totals):
            add_emp(f"E{n:02d}")
            if t:
                add_rec(f"E{n:02d}", "2026-07-06", late=t)

    @pytest.mark.parametrize("totals,ranks", [
        ([100, 80, 80, 50], [1, 2, 2, 4]),
        ([90, 70, 70, 70, 10], [1, 2, 2, 2, 5]),
        ([50, 50, 50, 10], [1, 1, 1, 4]),
        ([40, 40], [1, 1]),
        ([7], [1]),
        ([10, 20, 20, 30, 30, 30, 40], [1, 2, 2, 2, 5, 5, 7]),      # 40 | 30 30 30 | 20 20 | 10
        ([60, 50, 50, 40, 40, 40, 30, 30, 30, 30], [1, 2, 2, 4, 4, 4, 7, 7, 7, 7]),
    ])
    def test_competition_ranking(self, analytics_api, totals, ranks):
        self.seed(analytics_api, totals)
        assert [rk for rk, _ in board(analytics_api, limit=50)] == ranks

    def test_everyone_tied_is_rank_one_for_everybody_and_limit_one_keeps_them_all(self, analytics_api):
        self.seed(analytics_api, [25] * 30)
        got = board(analytics_api, limit=1)
        assert len(got) == 30 and {rk for rk, _ in got} == {1}
        assert [c for _, c in got] == sorted(c for _, c in got)             # ordered by emp_code

    def test_tie_exactly_at_the_default_boundary(self, analytics_api):
        self.seed(analytics_api, list(range(120, 30, -10)) + [30, 30, 30, 5])   # 9 distinct, then 3 tied at rank 10, then 5
        got = board(analytics_api)                                          # default limit 10
        assert [rk for rk, _ in got] == list(range(1, 10)) + [10, 10, 10] and len(got) == 12

    def test_twelve_distinct_values_with_the_default_limit_stop_at_ten(self, analytics_api):
        self.seed(analytics_api, [100 - n for n in range(12)])
        assert [rk for rk, _ in board(analytics_api)] == list(range(1, 11))

    def test_a_tie_between_an_int_and_a_float_valued_total(self, analytics_api):
        add_emp("E1")
        add_emp("E2")
        add_rec("E1", "2026-07-06", late=35)
        add_rec("E2", "2026-07-06", late=35.0)
        got = rows(analytics_api)
        assert [r["rank"] for r in got] == [1, 1] and all(type(r["total_late_minutes"]) is int for r in got)

    def test_sums_over_several_records_decide_the_order(self, analytics_api):
        add_emp("E1")
        add_emp("E2")
        add_rec("E1", "2026-07-06", late=60)                                # one big record
        for d in ("2026-07-06", "2026-07-07", "2026-07-08", "2026-07-09"):
            add_rec("E2", d, late=20)                                       # four small ones, 80 in total
        assert [(r["rank"], r["emp_code"], r["total_late_minutes"], r["late_count"]) for r in rows(analytics_api)] == [(1, "E2", 80, 4), (2, "E1", 60, 1)]


# =========================================================================== empty results, month isolation, validation
class TestEdges:
    def test_no_employees_no_logs(self, analytics_api):
        r = get(analytics_api)
        assert r.status_code == 200 and r.json() == {"month": JUL, "items": []}

    def test_nobody_was_late(self, analytics_api):
        add_emp("E1")
        add_rec("E1", "2026-07-06", late=0)
        add_rec("E1", "2026-07-07")
        assert rows(analytics_api) == []

    def test_lateness_only_in_other_months(self, analytics_api):
        add_emp("E1")
        add_rec("E1", "2026-06-30", late=50)
        add_rec("E1", "2026-08-01", late=50)
        assert rows(analytics_api) == [] and len(rows(analytics_api, "2026-06")) == 1 and len(rows(analytics_api, "2026-08")) == 1

    def test_department_without_lateness(self, populated):
        add_emp("Q1", "Quiet")
        add_rec("Q1", "2026-07-06", late=0)
        assert rows(populated, department="Quiet") == []

    def test_first_and_last_day_of_the_month_are_included(self, analytics_api):
        add_emp("E1")
        add_rec("E1", "2026-07-01", late=1)
        add_rec("E1", "2026-07-31", late=2)
        add_rec("E1", "2026-06-30", late=100)
        add_rec("E1", "2026-08-01", late=100)
        (row,) = rows(analytics_api)
        assert (row["total_late_minutes"], row["late_count"]) == (3, 2)

    def test_a_ghost_alone_gives_an_empty_board(self, analytics_api):
        add_rec("GHOST", "2026-07-06", late=500)
        assert rows(analytics_api) == []

    @pytest.mark.parametrize("limit", ["0", "-1", "51", "100", "abc", "", "1.5", "1e1", "٣"])
    def test_invalid_limit_is_422(self, analytics_api, limit):
        r = get(analytics_api, limit=limit)
        assert r.status_code == 422 and isinstance(r.json()["detail"], list)

    @pytest.mark.parametrize("limit", ["1", "10", "50"])
    def test_limit_bounds_are_inclusive(self, analytics_api, limit):
        assert get(analytics_api, limit=limit).status_code == 200

    @pytest.mark.parametrize("month", ["2026-13", "2026-00", "2026-7", "26-07", "202607", "2026-07-01", "", " 2026-07", "２０２６-07", "2026-07\n"])
    def test_invalid_month_is_422(self, analytics_api, month):
        assert get(analytics_api, month).status_code == 422

    def test_missing_month_is_422(self, analytics_api):
        assert analytics_api.get("/analytics/leaderboard/late").status_code == 422

    @pytest.mark.parametrize("month", ["0001-01", "0000-07", "2026-12", "9999-12"])
    def test_every_pattern_valid_month_is_200(self, analytics_api, month):
        assert get(analytics_api, month).status_code == 200

    def test_the_sample_data_of_the_kit(self, analytics_api):
        from tests.conftest import load_sample

        emps, logs = load_sample("employees"), load_sample("attendance_logs")
        main.db.employees.insert_many([dict(e) for e in emps])
        main.db.attendance_logs.insert_many([dict(l) for l in logs])
        assert rows(analytics_api, limit=50) == oracle(emps, logs, JUL, limit=50)


# =========================================================================== pipeline shape
class TestPipeline:
    def test_stages(self):
        pipe = main.build_late_leaderboard_pipeline(JUL, 7)
        assert [next(iter(s)) for s in pipe] == ["$match", "$group", "$lookup", "$unwind", "$setWindowFields", "$match", "$sort", "$project"]
        assert pipe[0]["$match"] == {"date": {"$gte": "2026-07-01", "$lte": "2026-07-31"}, "late_minutes": {"$gt": 0}}
        lookup = pipe[2]["$lookup"]
        assert (lookup["from"], lookup["localField"], lookup["foreignField"]) == ("employees", "_id", "emp_code")
        assert pipe[3] == {"$unwind": "$employee"}                           # no preserveNullAndEmptyArrays: ghosts are dropped

    def test_rank_stage_uses_one_sort_field_and_the_limit_filter_comes_after_it(self):
        pipe = main.build_late_leaderboard_pipeline(JUL, 7)
        window = pipe[4]["$setWindowFields"]
        assert window["sortBy"] == {"total_late_minutes": -1} and window["output"] == {"rank": {"$rank": {}}}
        assert "partitionBy" not in window
        assert pipe[5] == {"$match": {"rank": {"$lte": 7}}}
        assert pipe[6] == {"$sort": {"total_late_minutes": -1, "_id": 1}}      # ties: emp_code ascending

    def test_no_positional_ranking_or_limit_stage(self):
        text = json.dumps(main.build_late_leaderboard_pipeline(JUL, 7, "Sales"))
        for forbidden in ("$limit", "$skip", "$push", "$arrayElemAt", "$indexOfArray", "$documentNumber"):
            assert forbidden not in text, forbidden

    def test_department_match_precedes_the_ranking(self):
        pipe = main.build_late_leaderboard_pipeline(JUL, 7, "Sales")
        names = [next(iter(s)) for s in pipe]
        assert names.index("$setWindowFields") == names.index("$unwind") + 2
        assert pipe[4] == {"$match": {"employee.department": "Sales"}}

    def test_the_endpoint_makes_exactly_one_aggregation_and_fetches_no_records(self, populated, monkeypatch):
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
        assert get(populated).status_code == 200
        assert calls == [("attendance_logs", "aggregate")]

    def test_the_index_behind_the_first_stage_exists(self, analytics_api):
        keys = [v["key"] for v in main.db.attendance_logs.index_information().values()]
        assert [("date", -1), ("emp_code", 1)] in keys


# =========================================================================== independent oracle
class TestAgainstTheOracle:
    DEPTS = ["Engineering", "Sales", "HR"]

    def test_randomised_boards_limits_and_departments(self, analytics_api):
        rng = random.Random(20260712)
        for n in range(60):
            main.db.employees.delete_many({})
            main.db.attendance_logs.delete_many({})
            month = rng.choice(["2026-07", "2026-02", "2024-02"])
            first = days_of(month)[0]
            employees, logs = [], []
            for e in range(rng.randint(1, 18)):
                code = f"EMP{e:04d}"
                emp = {"emp_code": code, "name": f"Name {code}", "department": rng.choice(self.DEPTS)}
                employees.append(emp)
                add_emp(code, emp["department"], name=emp["name"])
                for k in range(-3, len(days_of(month)) + 3):
                    if rng.random() > 0.5:
                        continue
                    rec = {"emp_code": code, "date": first.fromordinal(first.toordinal() + k).isoformat(), "status": rng.choice(["PRESENT", "WFH", "ABSENT", "LEAVE"])}
                    if rng.random() < 0.8:
                        rec["late_minutes"] = rng.choice([0, 0, 5, 10, 10, 20, 35, 35.0])        # many ties on purpose
                    logs.append(rec)
                    main.db.attendance_logs.insert_one(dict(rec))
            for g in range(3):
                add_rec(f"GHOST{g}", first.isoformat(), late=rng.choice([10, 500]))
            limit, dept = rng.choice([1, 2, 3, 5, 10, 50]), rng.choice([None, *self.DEPTS])
            got = rows(analytics_api, month, limit=limit, **({"department": dept} if dept else {}))
            assert got == oracle(employees, logs, month, limit, dept), (n, month, limit, dept)


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
    """On a real server every test above already runs against the real $setWindowFields / $rank / $lookup;
    these add the planner and the volume."""

    def test_explain_uses_the_date_index_and_never_scans(self, api, real_mongo):
        for e in range(300):
            add_emp(f"EMP{e:04d}")
        main.db.attendance_logs.insert_many([{"emp_code": f"EMP{e:04d}", "date": d.isoformat(), "status": "PRESENT", "late_minutes": (e + d.day) % 7}
                                             for e in range(300) for m in ("2026-06", JUL, "2026-08") for d in days_of(m)])
        plan = main.db.command({"explain": {"aggregate": "attendance_logs", "pipeline": main.build_late_leaderboard_pipeline(JUL, 10), "cursor": {}},
                                "verbosity": "executionStats"})
        text = json.dumps(plan, default=str)
        assert "COLLSCAN" not in text and "IXSCAN" in text
        assert all(v == 0 for v in _values_of(plan, "collectionScans"))
        assert max(_values_of(plan, "totalDocsExamined")) <= 300 * 31          # one month of logs, not all three

    def test_large_collection_matches_the_oracle(self, api, real_mongo):
        rng = random.Random(9)
        employees, logs = [], []
        for e in range(500):
            code = f"EMP{e:04d}"
            employees.append({"emp_code": code, "name": f"Name {code}", "department": rng.choice(TestAgainstTheOracle.DEPTS)})
            add_emp(code, employees[-1]["department"], name=employees[-1]["name"])
        for emp in employees:
            for d in days_of(JUL):
                if rng.random() < 0.6:
                    rec = {"emp_code": emp["emp_code"], "date": d.isoformat(), "status": "PRESENT", "late_minutes": rng.choice([0, 0, 10, 20, 35])}
                    logs.append(rec)
        main.db.attendance_logs.insert_many([dict(r) for r in logs])
        for limit in (1, 3, 10, 50):
            assert rows(api, limit=limit) == oracle(employees, logs, JUL, limit)
        assert rows(api, limit=5, department="Sales") == oracle(employees, logs, JUL, 5, "Sales")

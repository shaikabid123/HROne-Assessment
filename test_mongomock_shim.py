"""The shim is test infrastructure that the analytics tests rely on, so its two handlers are pinned here against the
documented MongoDB behaviour, with expectations computed independently of the shim's own algorithm."""
import mongomock
import pytest

from tests import mongomock_shim

mongomock_shim.install()


def db_with(**collections):
    db = mongomock.MongoClient(tz_aware=True)["shim"]            # like the application's own client
    for name, docs in collections.items():
        if docs:
            db[name].insert_many([dict(d) for d in docs])
    return db


def rank_rows(values, direction=-1, **extra):
    db = db_with(t=[{"v": v, "i": i} for i, v in enumerate(values)])
    pipe = [{"$setWindowFields": {"sortBy": {"v": direction}, "output": {"rank": {"$rank": {}}}, **extra}}]
    return list(db.t.aggregate(pipe))


def expected_ranks(values, direction=-1):
    """Competition rank = 1 + how many values come strictly BEFORE this one in the sort order."""
    before = (lambda a, b: a > b) if direction < 0 else (lambda a, b: a < b)
    return {i: 1 + sum(1 for other in values if before(other, v)) for i, v in enumerate(values)}


class TestRank:
    @pytest.mark.parametrize("values,direction", [
        ([100, 80, 80, 50], -1), ([90, 70, 70, 70, 10], -1), ([5, 5, 5], -1), ([7], -1), ([1, 2, 3, 4], -1),
        ([10, 20, 20, 30, 30, 30, 40], -1), ([1, 2, 2, 3], 1), ([3, 3, 1], 1), ([50, 50, 40, 40, 30], -1),
        ([35, 35.0, 12], -1), ([0.5, 0.25, 0.5], -1),
    ])
    def test_matches_the_definition(self, values, direction):
        rows = rank_rows(values, direction)
        assert len(rows) == len(values)
        assert {r["i"]: r["rank"] for r in rows} == expected_ranks(values, direction)

    def test_the_documented_example_has_a_gap(self):
        assert [r["rank"] for r in rank_rows([100, 80, 80, 50])] == [1, 2, 2, 4]       # tied at 2, 3 is skipped
        assert [r["rank"] for r in rank_rows([90, 70, 70, 70, 10])] == [1, 2, 2, 2, 5]
        assert [r["rank"] for r in rank_rows([7, 9, 9, 10], direction=1)] == [1, 2, 2, 4]   # the docs' 7, 9, 9, 10

    def test_output_is_in_sort_order_and_ties_keep_their_input_order(self):
        rows = rank_rows([10, 30, 20, 30, 20])
        assert [r["v"] for r in rows] == [30, 30, 20, 20, 10]
        assert [r["i"] for r in rows] == [1, 3, 2, 4, 0]            # stable among equals
        assert [r["rank"] for r in rows] == [1, 1, 3, 3, 5]

    def test_empty_input(self):
        assert rank_rows([]) == []

    def test_other_fields_survive(self):
        rows = rank_rows([5, 1])
        assert {r["i"] for r in rows} == {0, 1} and all({"v", "i", "rank", "_id"} <= set(r) for r in rows)

    def test_documents_missing_the_field_sort_last_when_descending(self):
        db = db_with(t=[{"v": 5}, {"x": 1}, {"v": 9}])
        rows = list(db.t.aggregate([{"$setWindowFields": {"sortBy": {"v": -1}, "output": {"rank": {"$rank": {}}}}}]))
        assert [r.get("v") for r in rows] == [9, 5, None] and [r["rank"] for r in rows] == [1, 2, 3]

    @pytest.mark.parametrize("spec", [
        {"sortBy": {"v": -1, "i": 1}, "output": {"rank": {"$rank": {}}}},     # "sortBy can only take one field"
        {"output": {"rank": {"$rank": {}}}},
    ])
    def test_rank_needs_exactly_one_sort_field(self, spec):
        db = db_with(t=[{"v": 1, "i": 1}])
        with pytest.raises(ValueError):
            list(db.t.aggregate([{"$setWindowFields": spec}]))

    @pytest.mark.parametrize("spec", [
        {"partitionBy": "$g", "sortBy": {"v": 1}, "output": {"rank": {"$rank": {}}}},
        {"sortBy": {"v": 1}, "output": {"rank": {"$denseRank": {}}}},
        {"sortBy": {"v": 1}, "output": {"n": {"$max": "$v"}}},
        {"sortBy": {"v": 1}, "output": {"n": {"$sum": "$v", "window": {"range": [-1, 0]}}}},     # only `documents` windows
    ])
    def test_everything_not_implemented_fails_loudly(self, spec):
        db = db_with(t=[{"v": 1, "g": 1}])
        with pytest.raises(NotImplementedError):
            list(db.t.aggregate([{"$setWindowFields": spec}]))


class TestLookupWithPipeline:
    EMP = [{"emp_code": "A"}, {"emp_code": "B"}, {"emp_code": "C"}]
    LOGS = [
        {"emp_code": "A", "date": "2026-07-01", "late": 5}, {"emp_code": "A", "date": "2026-07-02", "late": 7},
        {"emp_code": "A", "date": "2026-06-30", "late": 100},   # outside the month
        {"emp_code": "B", "date": "2026-07-03", "late": 1}, {"emp_code": "Z", "date": "2026-07-03", "late": 9},
    ]
    RANGE = {"$match": {"date": {"$gte": "2026-07-01", "$lte": "2026-07-31"}}}

    def lookup(self, pipeline, db=None):
        db = db or db_with(emp=self.EMP, logs=self.LOGS)
        stage = {"$lookup": {"from": "logs", "localField": "emp_code", "foreignField": "emp_code", "pipeline": pipeline, "as": "j"}}
        return {d["emp_code"]: d["j"] for d in db.emp.aggregate([stage])}

    def test_the_sub_pipeline_is_bounded_and_run_per_employee(self):
        out = self.lookup([self.RANGE])
        assert sorted(x["late"] for x in out["A"]) == [5, 7]               # the June record is excluded
        assert [x["late"] for x in out["B"]] == [1] and out["C"] == []     # no logs -> an empty array

    def test_inner_stages_are_executed_by_mongomock(self):
        out = self.lookup([self.RANGE, {"$group": {"_id": None, "total": {"$sum": "$late"}, "n": {"$sum": 1}}}])
        assert [(x["total"], x["n"]) for x in out["A"]] == [(12, 2)]
        assert [(x["total"], x["n"]) for x in out["B"]] == [(1, 1)]
        assert out["C"] == []                                              # real MongoDB: $group over nothing -> no row

    def test_a_group_with_input_still_returns_its_row_and_other_last_stages_are_untouched(self):
        out = self.lookup([self.RANGE, {"$group": {"_id": "$emp_code", "n": {"$sum": 1}}}, {"$sort": {"_id": 1}}])
        assert out["A"] == [{"_id": "A", "n": 2}] and out["C"] == []
        out = self.lookup([{"$match": {"late": {"$gt": 1000}}}])           # filtered to nothing, no $group involved
        assert out == {"A": [], "B": [], "C": []}

    def test_only_the_matching_employee_key_is_joined(self):
        out = self.lookup([])
        assert len(out["A"]) == 3 and len(out["B"]) == 1 and all(x["emp_code"] == "A" for x in out["A"])

    def test_a_missing_local_field_matches_foreign_documents_without_the_field(self):
        db = db_with(emp=[{"name": "no code"}], logs=[{"emp_code": "A"}, {"other": 1}])
        stage = {"$lookup": {"from": "logs", "localField": "emp_code", "foreignField": "emp_code", "pipeline": [], "as": "j"}}
        (doc,) = list(db.emp.aggregate([stage]))
        assert len(doc["j"]) == 1 and "emp_code" not in doc["j"][0]

    def test_the_input_collections_are_not_modified(self):
        db = db_with(emp=self.EMP, logs=self.LOGS)
        self.lookup([self.RANGE], db)
        assert db.emp.count_documents({}) == 3 and db.logs.count_documents({}) == 5
        assert all("j" not in d for d in db.emp.find())

    def test_the_plain_form_still_works(self):
        db = db_with(emp=self.EMP, logs=self.LOGS)
        stage = {"$lookup": {"from": "logs", "localField": "emp_code", "foreignField": "emp_code", "as": "j"}}
        assert {d["emp_code"]: len(d["j"]) for d in db.emp.aggregate([stage])} == {"A": 3, "B": 1, "C": 0}

    def test_the_let_form_is_not_pretended_to_work(self):
        db = db_with(emp=self.EMP, logs=self.LOGS)
        stage = {"$lookup": {"from": "logs", "let": {"c": "$emp_code"}, "localField": "emp_code", "foreignField": "emp_code", "pipeline": [], "as": "j"}}
        with pytest.raises(NotImplementedError):
            list(db.emp.aggregate([stage]))


# ===================================================================================== windows ($sum / $avg)
import datetime as dt


def windows(values, output, sort=1):
    """Run $setWindowFields over documents {i, v} and return the output column in sort order."""
    db = db_with(t=[{"i": i, "v": v} for i, v in enumerate(values)])
    rows = list(db.t.aggregate([{"$setWindowFields": {"sortBy": {"i": sort}, "output": {"out": output}}}]))
    return [r["out"] for r in rows]


class TestWindows:
    def test_running_total_unbounded_to_current(self):
        assert windows([1, 2, 3, 4], {"$sum": "$v", "window": {"documents": ["unbounded", "current"]}}) == [1, 3, 6, 10]

    def test_current_row_plus_six_preceding_rows(self):
        # ten ones: the window grows 1..7 and then stays at 7 rows
        assert windows([1] * 10, {"$sum": "$v", "window": {"documents": [-6, 0]}}) == [1, 2, 3, 4, 5, 6, 7, 7, 7, 7]

    def test_a_window_of_the_previous_row_only_and_forward_windows(self):
        assert windows([1, 2, 4, 8], {"$sum": "$v", "window": {"documents": [-1, -1]}}) == [0, 1, 2, 4]
        assert windows([1, 2, 4, 8], {"$sum": "$v", "window": {"documents": ["current", 1]}}) == [3, 6, 12, 8]

    def test_the_default_window_is_the_whole_partition(self):
        assert windows([1, 2, 3], {"$sum": "$v"}) == [6, 6, 6]
        assert windows([1, 2, 3], {"$sum": "$v", "window": {"documents": ["unbounded", "unbounded"]}}) == [6, 6, 6]

    def test_avg_ignores_null_and_missing_and_is_null_when_nothing_is_left(self):
        values = [4, None, 8, None, None]
        assert windows(values, {"$avg": "$v", "window": {"documents": [-1, 0]}}) == [4, 4, 8, 8, None]
        assert windows([10, 20, 60], {"$avg": "$v", "window": {"documents": [-6, 0]}}) == [10, 15, 30]

    def test_sum_of_nothing_is_zero(self):
        assert windows([None, None], {"$sum": "$v", "window": {"documents": [-6, 0]}}) == [0, 0]

    def test_documents_missing_the_field_are_ignored_like_nulls(self):
        db = db_with(t=[{"i": 0, "v": 5}, {"i": 1}, {"i": 2, "v": 7}])
        out = [r["out"] for r in db.t.aggregate([{"$setWindowFields": {"sortBy": {"i": 1}, "output": {"out": {"$avg": "$v", "window": {"documents": [-6, 0]}}}}}])]
        assert out == [5, 5, 6]

    def test_the_operand_may_be_an_expression_and_the_output_follows_the_sort(self):
        db = db_with(t=[{"i": 2, "v": 3}, {"i": 0, "v": 1}, {"i": 1, "v": 2}])
        stage = {"$setWindowFields": {"sortBy": {"i": 1}, "output": {"out": {"$sum": {"$cond": [{"$gt": ["$v", 1]}, 10, 0]}, "window": {"documents": ["unbounded", "current"]}}}}}
        rows = list(db.t.aggregate([stage]))
        assert [r["i"] for r in rows] == [0, 1, 2] and [r["out"] for r in rows] == [0, 10, 20]

    def test_several_outputs_in_one_stage_each_read_the_input_fields(self):
        db = db_with(t=[{"i": i, "v": v} for i, v in enumerate([1, 2, 3])])
        stage = {"$setWindowFields": {"sortBy": {"i": 1}, "output": {
            "total": {"$sum": "$v", "window": {"documents": ["unbounded", "current"]}},
            "last2": {"$sum": "$v", "window": {"documents": [-1, 0]}}}}}
        rows = list(db.t.aggregate([stage]))
        assert [(r["total"], r["last2"]) for r in rows] == [(1, 1), (3, 3), (6, 5)]

    def test_descending_sort_windows_follow_the_sorted_order(self):
        assert windows([1, 2, 3, 4], {"$sum": "$v", "window": {"documents": [-1, 0]}}, sort=-1) == [4, 7, 5, 3]

    def test_a_documents_window_needs_a_sort(self):
        db = db_with(t=[{"v": 1}])
        with pytest.raises(ValueError):
            list(db.t.aggregate([{"$setWindowFields": {"output": {"out": {"$sum": "$v", "window": {"documents": [-1, 0]}}}}}]))

    def test_empty_input(self):
        db = db_with()
        assert list(db.t.aggregate([{"$setWindowFields": {"sortBy": {"i": 1}, "output": {"out": {"$sum": "$v"}}}}])) == []


# ============================================================================================ $densify
D = lambda day: dt.datetime(2026, 7, day, tzinfo=dt.timezone.utc)


def densified(values, bounds, field="d", step=1, unit=None):
    db = db_with(t=[{field: v, "keep": n} for n, v in enumerate(values)])
    rng = {"step": step, "bounds": bounds}
    if unit:
        rng["unit"] = unit
    rows = list(db.t.aggregate([{"$densify": {"field": field, "range": rng}}]))
    return rows


class TestDensify:
    def test_fills_the_gaps_and_the_upper_bound_is_exclusive(self):
        rows = densified([0, 3], [0, 5])
        assert [r["d"] for r in rows] == [0, 1, 2, 3, 4]                    # 5 is not generated

    def test_generates_before_the_first_and_after_the_last_document(self):
        assert [r["d"] for r in densified([2], [0, 4])] == [0, 1, 2, 3]

    def test_lower_bound_is_inclusive_and_defines_the_start_whatever_the_data(self):
        assert [r["d"] for r in densified([3], [1, 4])] == [1, 2, 3]

    def test_documents_outside_the_bounds_are_kept_not_filtered(self):
        assert [r["d"] for r in densified([-5, 2, 9], [0, 4])] == [-5, 0, 1, 2, 3, 9]

    def test_existing_documents_keep_their_fields_and_new_ones_hold_only_the_field(self):
        rows = densified([1, 3], [1, 4])
        by = {r["d"]: r for r in rows}
        assert by[1]["keep"] == 0 and by[3]["keep"] == 1
        assert set(by[2]) == {"d"}

    def test_an_existing_value_is_not_duplicated(self):
        assert [r["d"] for r in densified([1, 2, 3], [1, 4])] == [1, 2, 3]

    def test_steps_larger_than_one(self):
        assert [r["d"] for r in densified([0], [0, 10], step=3)] == [0, 3, 6, 9]

    def test_dates_by_the_day_with_an_exclusive_upper_bound(self):
        rows = densified([D(2), D(4)], [D(1), D(6)], unit="day")
        assert [r["d"] for r in rows] == [D(1), D(2), D(3), D(4), D(5)]       # the 6th is the exclusive bound

    def test_the_last_day_is_included_by_an_upper_bound_of_23_59_59_999_on_that_day(self):
        last = dt.datetime(2026, 7, 6, 23, 59, 59, 999000, tzinfo=dt.timezone.utc)
        rows = densified([D(4)], [D(4), last], unit="day")
        assert [r["d"] for r in rows] == [D(4), D(5), D(6)]                   # 6th included, the 7th is not

    def test_one_day_range(self):
        last = dt.datetime(2026, 7, 6, 23, 59, 59, 999000, tzinfo=dt.timezone.utc)
        assert [r["d"] for r in densified([D(6)], [D(6), last], unit="day")] == [D(6)]
        assert [r["d"] for r in densified([], [D(6), last], unit="day")] == [D(6)]   # nothing to anchor on, still one row

    def test_the_last_representable_day_can_be_densified(self):
        last_day = dt.datetime(9999, 12, 31, tzinfo=dt.timezone.utc)
        upper = dt.datetime(9999, 12, 31, 23, 59, 59, 999000, tzinfo=dt.timezone.utc)
        assert [r["d"] for r in densified([], [last_day - dt.timedelta(days=2), upper], unit="day")] == [last_day - dt.timedelta(days=2), last_day - dt.timedelta(days=1), last_day]

    def test_a_date_step_must_be_an_integer(self):
        with pytest.raises(ValueError):
            densified([D(1)], [D(1), D(3)], step=1.5, unit="day")

    @pytest.mark.parametrize("kwargs", [dict(bounds="full"), dict(bounds="partition")])
    def test_non_explicit_bounds_are_not_pretended(self, kwargs):
        db = db_with(t=[{"d": 1}])
        with pytest.raises(NotImplementedError):
            list(db.t.aggregate([{"$densify": {"field": "d", "range": {"step": 1, **kwargs}}}]))

    def test_partitioning_and_other_units_are_not_pretended(self):
        db = db_with(t=[{"d": D(1)}])
        with pytest.raises(NotImplementedError):
            list(db.t.aggregate([{"$densify": {"field": "d", "partitionByFields": ["x"], "range": {"step": 1, "unit": "day", "bounds": [D(1), D(3)]}}}]))
        with pytest.raises(NotImplementedError):
            list(db.t.aggregate([{"$densify": {"field": "d", "range": {"step": 1, "unit": "hour", "bounds": [D(1), D(3)]}}}]))

    def test_a_document_without_the_field_is_rejected(self):
        db = db_with(t=[{"other": 1}])
        with pytest.raises(ValueError):
            list(db.t.aggregate([{"$densify": {"field": "d", "range": {"step": 1, "bounds": [0, 3]}}}]))


# =================================================================================== date expressions
class TestDateOperators:
    def run(self, expression, doc=None):
        db = db_with(t=[doc or {"s": "2026-07-06"}])
        return list(db.t.aggregate([{"$project": {"_id": 0, "x": expression}}]))[0]["x"]

    def test_date_from_string(self):
        got = self.run({"$dateFromString": {"dateString": "$s", "format": "%Y-%m-%d"}})
        assert got == dt.datetime(2026, 7, 6, tzinfo=dt.timezone.utc)

    def test_date_from_string_of_null_is_null(self):
        assert self.run({"$dateFromString": {"dateString": "$s", "format": "%Y-%m-%d"}}, {"s": None}) is None

    def test_date_from_string_rejects_what_it_does_not_implement_or_cannot_parse(self):
        with pytest.raises(NotImplementedError):
            self.run({"$dateFromString": {"dateString": "$s", "format": "%Y-%m-%d", "timezone": "UTC"}})
        with pytest.raises(ValueError):
            self.run({"$dateFromString": {"dateString": "$s", "format": "%Y-%m-%d"}}, {"s": "2026-02-30"})

    @pytest.mark.parametrize("day,expected", [(6, 1), (7, 2), (8, 3), (9, 4), (10, 5), (11, 6), (12, 7)])
    def test_iso_day_of_week_monday_is_1_and_sunday_is_7(self, day, expected):
        assert self.run({"$isoDayOfWeek": dt.datetime(2026, 7, day, tzinfo=dt.timezone.utc)}) == expected

    def test_round_trip_with_date_to_string(self):
        parsed = {"$dateFromString": {"dateString": "$s", "format": "%Y-%m-%d"}}
        assert self.run({"$dateToString": {"date": parsed, "format": "%Y-%m-%d"}}) == "2026-07-06"

    def test_the_other_date_operators_still_work(self):
        assert self.run({"$dayOfWeek": dt.datetime(2026, 7, 6)}) == 2

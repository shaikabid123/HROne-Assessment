"""GET /admin/explain/{endpoint}.

Two kinds of tests:
  * contract tests with a RECORDING stand-in for MongoDB's `explain` command: they check WHAT is asked of the database
    (the envelope, that the explained command is the one the real endpoint runs, validation that never reaches the
    database, the JSON conversion). The stand-in returns a canned document, which is only ever used to check plumbing.
  * tests against a REAL MongoDB server (skipped on FerretDB, which cannot explain these pipelines): they load a
    ~100,000-record dataset and assert on the real plans (IXSCAN, no COLLSCAN, bounded documents examined, no sort).
    Pipelines that need MongoDB 5.0+/6.0+ are skipped on an older real server."""
import inspect
import json
from datetime import date, datetime, timezone

import pytest
from bson import Decimal128, Int64, ObjectId, Timestamp, encode
from fastapi.testclient import TestClient
from pymongo.errors import OperationFailure

from app import main
from tests import big_dataset
from tests.analytics_helpers import JUL, SpyDB, add_emp, add_rec

_REAL_CLIENT = main.client   # the real client, kept before any fixture swaps in mongomock


def _build_info():
    return _REAL_CLIENT.admin.command("buildInfo")


def on_ferretdb():
    return "ferretdbVersion" in _build_info()


def server_version():
    return tuple(_build_info()["versionArray"][:2])


ENDPOINTS = ["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]
COLLECTION = {"attendance_list": "attendance_logs", "employee_monthly": "attendance_logs", "department_summary": "employees",
              "late_leaderboard": "attendance_logs", "department_trend": "employees"}
VALID = {
    "attendance_list": {"emp_code": "EMP0001", "date_from": "2026-07-01", "date_to": "2026-07-31", "status": "PRESENT", "page": 3, "page_size": 10},
    "employee_monthly": {"emp_code": "EMP0001", "month": JUL},
    "department_summary": {"month": JUL, "department": "Engineering"},
    "late_leaderboard": {"month": JUL, "limit": 5, "department": "Engineering"},
    "department_trend": {"department": "Engineering", "from": "2026-07-01", "to": "2026-07-14"},
}
CANNED = {
    "explainVersion": "1",
    "queryPlanner": {"winningPlan": {"stage": "FETCH", "inputStage": {"stage": "IXSCAN", "indexName": "uq_emp_code_date"}}},
    "executionStats": {"nReturned": 20, "totalKeysExamined": Int64(20), "totalDocsExamined": Int64(20)},
    "serverInfo": {"version": "7.0.0"},
    "ok": 1.0,
}


class RecordingDB:
    """Stands in for main.db: records every command; answers `explain` with a canned document (or raises)."""

    def __init__(self, document=None, error=None):
        self.commands, self._document, self._error = [], CANNED if document is None else document, error

    def command(self, command, *args, **kwargs):
        self.commands.append(command)
        if self._error:
            raise self._error
        return self._document


@pytest.fixture()
def recorder(monkeypatch):
    rec = RecordingDB()
    monkeypatch.setattr(main, "db", rec)
    return rec


@pytest.fixture()
def client(recorder):
    return TestClient(main.app, raise_server_exceptions=False)   # no `with`: no startup, the endpoint only needs db.command


def explain(client, endpoint, **params):
    return client.get(f"/admin/explain/{endpoint}", params=params)


# =========================================================================== envelope and response format
class TestEnvelopeAndResponse:
    @pytest.mark.parametrize("endpoint", ENDPOINTS)
    def test_runs_mongodb_explain_with_execution_stats_and_returns_the_three_fields(self, client, recorder, endpoint):
        r = explain(client, endpoint, **VALID[endpoint])
        assert r.status_code == 200, r.text
        body = r.json()
        assert list(body) == ["endpoint", "collection", "explain"]
        assert body["endpoint"] == endpoint and body["collection"] == COLLECTION[endpoint]
        assert len(recorder.commands) == 1
        sent = recorder.commands[0]
        assert list(sent)[0] == "explain" and sent["verbosity"] == "executionStats" and set(sent) == {"explain", "verbosity"}
        inner = sent["explain"]
        assert next(iter(inner)) in ("find", "aggregate") and inner[next(iter(inner))] == COLLECTION[endpoint]

    @pytest.mark.parametrize("endpoint", ENDPOINTS)
    def test_the_explain_field_is_the_servers_document_converted_not_invented(self, client, recorder, endpoint):
        body = explain(client, endpoint, **VALID[endpoint]).json()
        assert body["explain"] == main.to_json_document(CANNED)
        assert body["explain"]["queryPlanner"]["winningPlan"]["inputStage"]["indexName"] == "uq_emp_code_date"   # passed through verbatim
        assert body["explain"]["executionStats"]["totalKeysExamined"] == 20

    def test_a_different_server_answer_gives_a_different_response(self, monkeypatch):
        other = {"queryPlanner": {"winningPlan": {"stage": "COLLSCAN"}}, "executionStats": {"nReturned": 1}, "ok": 1.0}
        monkeypatch.setattr(main, "db", RecordingDB(other))
        body = explain(TestClient(main.app), "attendance_list").json()
        assert body["explain"] == other                                     # nothing is added, hard-coded or filtered

    def test_the_source_contains_no_hard_coded_plan_or_index_name(self):
        source = "".join(inspect.getsource(f) for f in (main.explain_endpoint, main.explain_command, main.run_explain, main.find_command, main.aggregate_command, main.to_json_document))
        for forbidden in ("IXSCAN", "COLLSCAN", "uq_emp_code", "ix_date", "ix_status", "ix_joined", "ix_department", "winningPlan"):
            assert forbidden not in source, forbidden

    def test_a_server_error_is_not_papered_over(self, monkeypatch):
        monkeypatch.setattr(main, "db", RecordingDB(error=OperationFailure("Unrecognized pipeline stage name: '$setWindowFields'")))
        r = explain(TestClient(main.app, raise_server_exceptions=False), "late_leaderboard", **VALID["late_leaderboard"])
        assert r.status_code == 500
        assert "Unrecognized pipeline stage name: '$setWindowFields'" in r.json()["detail"] and "late_leaderboard" in r.json()["detail"]
        assert "explain" not in r.json()                                    # no plan of any kind is offered instead


# =========================================================================== the explained command is the real one
class TestSameQueryAsTheEndpoint:
    def test_attendance_list_command_literally(self, client, recorder):
        explain(client, "attendance_list", **VALID["attendance_list"])
        assert recorder.commands[0]["explain"] == {
            "find": "attendance_logs",
            "filter": {"emp_code": "EMP0001", "date": {"$gte": "2026-07-01", "$lte": "2026-07-31"}, "status": "PRESENT"},
            "projection": {"_id": 0}, "sort": {"date": -1}, "skip": 20, "limit": 10}
        assert list(recorder.commands[0]["explain"]["sort"].items()) == [("date", -1)]

    def test_attendance_list_without_an_employee_sorts_by_date_then_emp_code(self, client, recorder):
        explain(client, "attendance_list", status="LEAVE")
        cmd = recorder.commands[0]["explain"]
        assert list(cmd["sort"].items()) == [("date", -1), ("emp_code", 1)] and cmd["filter"] == {"status": "LEAVE"}
        assert (cmd["skip"], cmd["limit"]) == (0, 20)                       # the endpoint's defaults: page 1, page_size 20

    def test_pagination_arithmetic(self, client, recorder):
        for page, size, skip in ((1, 20, 0), (2, 20, 20), (5, 50, 200), (7, 100, 600)):
            explain(client, "attendance_list", page=page, page_size=size)
            cmd = recorder.commands[-1]["explain"]
            assert (cmd["skip"], cmd["limit"]) == (skip, size)

    def test_a_page_far_beyond_mongodbs_integers_is_still_explainable(self, client, recorder):
        r = explain(client, "attendance_list", page=10 ** 30, page_size=100)
        assert r.status_code == 200
        cmd = recorder.commands[0]["explain"]
        assert cmd["skip"] == main.MAX_EXPLAIN_SKIP and encode(cmd)         # fits a BSON int64

    def test_what_the_real_attendance_endpoint_sends_is_what_is_explained(self, analytics_api, monkeypatch):
        add_emp("EMP0001")
        for d in range(1, 31):
            add_rec("EMP0001", f"2026-07-{d:02d}")
        params = {"emp_code": "EMP0001", "date_from": "2026-07-01", "date_to": "2026-07-31", "status": "PRESENT", "page": 3, "page_size": 10}
        spy = SpyDB(main.db)
        monkeypatch.setattr(main, "db", spy)
        assert analytics_api.get("/attendance", params=params).status_code == 200
        finds = [kwargs for (coll, method, args, kwargs) in spy.calls if (coll, method) == ("attendance_logs", "find")]
        assert len(finds) == 1
        real = finds[0]
        rec = RecordingDB()
        monkeypatch.setattr(main, "db", rec)
        explain(TestClient(main.app), "attendance_list", **params)
        cmd = rec.commands[0]["explain"]
        assert cmd["filter"] == real["filter"] and cmd["projection"] == real["projection"]
        assert list(cmd["sort"].items()) == list(real["sort"]) and (cmd["skip"], cmd["limit"]) == (real["skip"], real["limit"])

    @pytest.mark.parametrize("endpoint,collection,seed", [
        ("employee_monthly", "attendance_logs", "monthly"),
        ("department_summary", "employees", "dept"),
        ("late_leaderboard", "attendance_logs", "dept"),
        ("department_trend", "employees", "dept"),
    ])
    def test_the_pipeline_the_real_endpoint_runs_is_the_pipeline_explained(self, analytics_api, monkeypatch, endpoint, collection, seed):
        if not on_ferretdb() and server_version() < (6, 0):
            pytest.skip("these pipelines need MongoDB 6.0+")
        add_emp("EMP0001", "Engineering")
        add_rec("EMP0001", "2026-07-06", late=5)
        urls = {"employee_monthly": "/analytics/employees/EMP0001/monthly", "department_summary": "/analytics/departments/summary",
                "late_leaderboard": "/analytics/leaderboard/late", "department_trend": "/analytics/departments/Engineering/trend"}
        params = dict(VALID[endpoint])
        params.pop("emp_code", None)
        if endpoint == "employee_monthly":
            params = {"month": JUL}
        if endpoint == "department_trend":
            params = {"from": "2026-07-01", "to": "2026-07-14"}
        spy = SpyDB(main.db)
        monkeypatch.setattr(main, "db", spy)
        assert analytics_api.get(urls[endpoint], params=params).status_code == 200
        seen = [args[0] for (coll, method, args, kwargs) in spy.calls if (coll, method) == (collection, "aggregate")]
        assert seen, "the endpoint made no aggregation"
        rec = RecordingDB()
        monkeypatch.setattr(main, "db", rec)
        explain(TestClient(main.app), endpoint, **VALID[endpoint])
        sent = rec.commands[0]["explain"]
        assert sent["aggregate"] == collection and sent["cursor"] == {}
        assert sent["pipeline"] == seen[0]                                  # stage for stage, constant for constant

    def test_each_pipeline_is_the_one_from_the_builder(self, client, recorder):
        explain(client, "late_leaderboard", month=JUL)
        assert recorder.commands[-1]["explain"]["pipeline"] == main.build_late_leaderboard_pipeline(JUL, 10, None)      # limit defaults to 10
        explain(client, "department_summary", month=JUL)
        assert recorder.commands[-1]["explain"]["pipeline"] == main.build_department_summary_pipeline(JUL, None)
        explain(client, "department_trend", department="Sales", **{"from": "2026-07-01", "to": "2026-07-03"})
        assert recorder.commands[-1]["explain"]["pipeline"] == main.build_department_trend_pipeline("Sales", date(2026, 7, 1), date(2026, 7, 3))
        explain(client, "employee_monthly", emp_code="EMP0009", month="2026-02")
        assert recorder.commands[-1]["explain"]["pipeline"] == main.build_employee_monthly_pipeline("EMP0009", "2026-02")

    def test_leaderboard_limit_and_department_reach_the_pipeline(self, client, recorder):
        explain(client, "late_leaderboard", month=JUL, limit=3, department="Sales")
        pipe = recorder.commands[0]["explain"]["pipeline"]
        assert {"$match": {"rank": {"$lte": 3}}} in pipe and {"$match": {"employee.department": "Sales"}} in pipe

    def test_an_unknown_employee_or_department_is_still_explained_not_a_404(self, client, recorder):
        assert explain(client, "employee_monthly", emp_code="NOBODY", month=JUL).status_code == 200
        assert explain(client, "department_trend", department="Nope", **{"from": "2026-07-01", "to": "2026-07-02"}).status_code == 200
        assert len(recorder.commands) == 2

    USED = {"attendance_list": {"emp_code", "date_from", "date_to", "status", "page", "page_size"}, "employee_monthly": {"emp_code", "month"},
            "department_summary": {"month", "department"}, "late_leaderboard": {"month", "limit", "department"},
            "department_trend": {"department", "from", "to"}}

    @pytest.mark.parametrize("endpoint", ENDPOINTS)
    def test_parameters_that_an_endpoint_does_not_use_change_nothing(self, client, recorder, endpoint):
        everything = {}
        for params in VALID.values():
            everything.update(params)
        extras = {name: value for name, value in everything.items() if name not in self.USED[endpoint]}
        assert extras, "the test needs parameters this endpoint does not use"
        assert explain(client, endpoint, **VALID[endpoint]).status_code == 200
        assert explain(client, endpoint, **VALID[endpoint], **extras).status_code == 200
        assert recorder.commands[0] == recorder.commands[1]


# =========================================================================== validation: errors never reach the database
class TestValidation:
    @pytest.mark.parametrize("name", ["nope", "Attendance_List", "attendance-list", "attendance_list ", "monthly", "employee_monthly/", "late", "department_trends", "trend"])
    def test_unsupported_endpoint_names_are_422(self, client, recorder, name):
        r = client.get(f"/admin/explain/{name}", params={"month": JUL})
        assert r.status_code in (422, 404)
        if "/" not in name:
            assert r.status_code == 422 and isinstance(r.json()["detail"], list)
        assert recorder.commands == []

    @pytest.mark.parametrize("endpoint,expected", [
        ("employee_monthly", ["emp_code", "month"]),
        ("department_summary", ["month"]),
        ("late_leaderboard", ["month"]),
        ("department_trend", ["department", "from", "to"]),
    ])
    def test_missing_required_parameters_are_422_and_all_are_listed(self, client, recorder, endpoint, expected):
        r = explain(client, endpoint)
        assert r.status_code == 422
        assert [e["loc"] for e in r.json()["detail"]] == [["query", name] for name in expected]
        assert recorder.commands == []

    @pytest.mark.parametrize("endpoint,present,missing", [
        ("employee_monthly", {"month": JUL}, "emp_code"), ("employee_monthly", {"emp_code": "E1"}, "month"),
        ("department_trend", {"from": "2026-07-01", "to": "2026-07-02"}, "department"),
        ("department_trend", {"department": "D", "to": "2026-07-02"}, "from"), ("department_trend", {"department": "D", "from": "2026-07-01"}, "to"),
    ])
    def test_one_missing_parameter_is_named(self, client, recorder, endpoint, present, missing):
        r = explain(client, endpoint, **present)
        assert r.status_code == 422 and [e["loc"] for e in r.json()["detail"]] == [["query", missing]]
        assert recorder.commands == []

    def test_attendance_list_needs_nothing(self, client, recorder):
        assert explain(client, "attendance_list").status_code == 200

    @pytest.mark.parametrize("endpoint,params", [
        ("employee_monthly", {"emp_code": "E1", "month": "2026-13"}), ("department_summary", {"month": "2026-00"}),
        ("late_leaderboard", {"month": "2026-7"}), ("employee_monthly", {"emp_code": "E1", "month": "202607"}),
        ("department_summary", {"month": "2026-07-01"}), ("late_leaderboard", {"month": ""}), ("department_summary", {"month": "２０２６-07"}),
        ("late_leaderboard", {"month": JUL, "limit": 0}), ("late_leaderboard", {"month": JUL, "limit": 51}), ("late_leaderboard", {"month": JUL, "limit": "x"}),
        ("attendance_list", {"status": "BOGUS"}), ("attendance_list", {"status": "present"}),
        ("attendance_list", {"page": 0}), ("attendance_list", {"page": -1}), ("attendance_list", {"page_size": 0}), ("attendance_list", {"page_size": 101}),
        ("attendance_list", {"date_from": "2026-02-30"}), ("attendance_list", {"date_to": "20260706"}), ("attendance_list", {"date_from": "yesterday"}),
        ("attendance_list", {"date_from": "2026-07-09", "date_to": "2026-07-08"}),
        ("department_trend", {"department": "D", "from": "2026-07-09", "to": "2026-07-08"}),
        ("department_trend", {"department": "D", "from": "2026-01-01", "to": "2026-04-03"}),
        ("department_trend", {"department": "D", "from": "bad", "to": "2026-07-08"}), ("department_trend", {"department": "D", "from": "2026-07-01", "to": "2026-13-01"}),
    ])
    def test_invalid_parameters_are_422_exactly_like_the_real_endpoint(self, client, recorder, endpoint, params):
        r = explain(client, endpoint, **params)
        assert r.status_code == 422 and isinstance(r.json()["detail"], list)
        assert recorder.commands == []

    @pytest.mark.parametrize("endpoint,params", [
        ("attendance_list", {"limit": 50}), ("attendance_list", {"page_size": 100}), ("attendance_list", {"page": 1}),
        ("attendance_list", {"date_from": "2026-07-08", "date_to": "2026-07-08"}),
        ("late_leaderboard", {"month": JUL, "limit": 1}), ("late_leaderboard", {"month": JUL, "limit": 50}),
        ("department_trend", {"department": "D", "from": "2026-01-01", "to": "2026-04-02"}),     # 92 days
        ("department_trend", {"department": "D", "from": "2026-07-06", "to": "2026-07-06"}),      # one day
    ])
    def test_boundaries_that_are_allowed(self, client, recorder, endpoint, params):
        assert explain(client, endpoint, **params).status_code == 200

    def test_the_range_limit_is_the_same_as_the_trend_endpoints(self, client, recorder):
        assert explain(client, "department_trend", department="D", **{"from": "2026-01-01", "to": "2026-04-02"}).status_code == 200
        assert explain(client, "department_trend", department="D", **{"from": "2026-01-01", "to": "2026-04-03"}).status_code == 422

    def test_the_parameters_and_limits_match_openapi_yaml(self):
        yaml = pytest.importorskip("yaml")
        import pathlib

        contract = yaml.safe_load((pathlib.Path(main.__file__).parent.parent / "openapi.yaml").read_text())
        spec = contract["paths"]["/admin/explain/{endpoint}"]["get"]["parameters"]
        resolved = [contract["components"]["parameters"][p["$ref"].split("/")[-1]] if "$ref" in p else p for p in spec]
        wanted = {p["name"]: p for p in resolved}
        ours = {p["name"]: p for p in main.app.openapi()["paths"]["/admin/explain/{endpoint}"]["get"]["parameters"]}
        assert set(ours) == set(wanted)
        assert set(ours["endpoint"]["schema"]["enum"]) == set(wanted["endpoint"]["schema"]["enum"]) == set(ENDPOINTS)
        for name in ("limit", "page", "page_size"):
            for bound in ("minimum", "maximum"):
                got = next((s.get(bound) for s in ours[name]["schema"].get("anyOf", [ours[name]["schema"]]) if bound in s), None)
                assert got == wanted[name]["schema"].get(bound), (name, bound)
        assert ours["endpoint"]["in"] == "path" and all(ours[n]["in"] == "query" for n in ours if n != "endpoint")


# =========================================================================== BSON -> JSON
class TestJsonConversion:
    RAW = {"when": datetime(2026, 7, 6, 4, 35, tzinfo=timezone.utc), "id": ObjectId("64b64c2d9f1b2a3c4d5e6f70"), "n": Int64(5), "small": 3,
           "dec": Decimal128("1.50"), "ts": Timestamp(1700000000, 7), "nan": float("nan"), "inf": float("inf"), "pi": 3.25,
           "nested": {"list": [datetime(2026, 7, 1, tzinfo=timezone.utc), {"deep": Int64(2 ** 40)}]}, "text": "IXSCAN", "none": None, "flag": True}

    def test_raw_explain_output_is_not_json_serialisable_which_is_why_it_is_converted(self):
        with pytest.raises(TypeError):
            json.dumps(self.RAW)

    def test_conversion_keeps_everything_and_is_valid_strict_json(self):
        out = main.to_json_document(self.RAW)
        json.dumps(out, allow_nan=False)                                   # no NaN / Infinity literals left
        assert out["when"] == {"$date": "2026-07-06T04:35:00Z"} and out["id"] == {"$oid": "64b64c2d9f1b2a3c4d5e6f70"}
        assert out["n"] == 5 and out["small"] == 3 and out["pi"] == 3.25 and out["text"] == "IXSCAN" and out["none"] is None and out["flag"] is True
        assert out["dec"] == {"$numberDecimal": "1.50"} and out["ts"] == {"$timestamp": {"t": 1700000000, "i": 7}}
        assert out["nan"] == {"$numberDouble": "NaN"} and out["inf"] == {"$numberDouble": "Infinity"}
        assert out["nested"]["list"][0] == {"$date": "2026-07-01T00:00:00Z"} and out["nested"]["list"][1] == {"deep": 2 ** 40}

    def test_the_endpoint_serialises_such_a_document(self, monkeypatch):
        monkeypatch.setattr(main, "db", RecordingDB(self.RAW))
        r = explain(TestClient(main.app), "attendance_list")
        assert r.status_code == 200 and r.json()["explain"]["when"] == {"$date": "2026-07-06T04:35:00Z"}

    def test_a_real_query_with_dates_in_it_explains_to_valid_json(self, monkeypatch):
        """The commands themselves carry datetimes (the trend's $densify bounds): they must survive the round trip."""
        cmd = main.aggregate_command("employees", main.build_department_trend_pipeline("D", date(2026, 7, 1), date(2026, 7, 3)))
        out = main.to_json_document({"command": cmd})
        densify = next(s for s in out["command"]["pipeline"] if "$densify" in s)["$densify"]["range"]["bounds"]
        assert densify == [{"$date": "2026-07-01T00:00:00Z"}, {"$date": "2026-07-03T23:59:59.999Z"}]


# =========================================================================== real MongoDB: the actual plans
def stages_of(node):
    found, stack = [], [node]
    while stack:
        n = stack.pop()
        if isinstance(n, dict):
            if "stage" in n:
                found.append(n["stage"])
            stack.extend(n.values())
        elif isinstance(n, list):
            stack.extend(n)
    return found


def values_of(node, key):
    found, stack = [], [node]
    while stack:
        n = stack.pop()
        if isinstance(n, dict):
            for k, v in n.items():
                if k == key and isinstance(v, (int, float)) and not isinstance(v, bool):
                    found.append(v)
                stack.append(v)
        elif isinstance(n, list):
            stack.extend(n)
    return found


def cursor_node(explain_doc):
    """The part of an explain output that holds the main query's plan and statistics."""
    return explain_doc["stages"][0]["$cursor"] if "stages" in explain_doc else explain_doc


@pytest.fixture(scope="module")
def big():
    if on_ferretdb():
        pytest.skip("needs a real MongoDB: FerretDB cannot explain these queries/pipelines")
    main.db.employees.drop()
    main.db.attendance_logs.drop()
    with TestClient(main.app) as c:                                         # entering the context runs the startup index creation
        counts = big_dataset.populate(main.db)
        yield c, counts


def need(minimum):
    if server_version() < minimum:
        pytest.skip(f"this pipeline needs MongoDB {minimum[0]}.{minimum[1]}+ (the README's minimum is 6.0); this server is {server_version()[0]}.{server_version()[1]}")


class TestOnRealMongo:
    def test_the_dataset_is_representative(self, big):
        _, (n_emp, n_log) = big
        assert n_emp >= 1400 and n_log >= 95_000

    def test_indexes_were_created_by_the_application_at_startup(self, big):
        names = set(main.db.attendance_logs.index_information()) | set(main.db.employees.index_information())
        assert {"uq_emp_code", "ix_department_emp_code", "ix_joined_on", "uq_emp_code_date", "ix_date_desc_emp_code", "ix_status_date_emp_code"} <= names

    @pytest.mark.parametrize("label,params", [
        ("no filters", {}),
        ("emp_code", {"emp_code": "EMP00042"}),
        ("emp_code + dates", {"emp_code": "EMP00042", "date_from": "2026-05-01", "date_to": "2026-05-31"}),
        ("dates", {"date_from": "2026-06-01", "date_to": "2026-06-07"}),
        ("status", {"status": "LEAVE"}),
        ("status + dates, deep page", {"status": "WFH", "date_from": "2026-05-01", "date_to": "2026-06-30", "page": 5, "page_size": 50}),
        ("emp_code + status", {"emp_code": "EMP00042", "status": "PRESENT"}),
    ])
    def test_attendance_list_plans(self, big, label, params):
        client, (_, n_log) = big
        r = client.get("/admin/explain/attendance_list", params=params)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["endpoint"] == "attendance_list" and body["collection"] == "attendance_logs"
        text = json.dumps(body["explain"])
        assert "IXSCAN" in text and "COLLSCAN" not in text, label
        assert "executionStats" in body["explain"]
        node = cursor_node(body["explain"])
        assert "SORT" not in stages_of(node["queryPlanner"]["winningPlan"]), label            # the index delivers the order
        stats = node["executionStats"]
        size, skip = params.get("page_size", 20), (params.get("page", 1) - 1) * params.get("page_size", 20)
        assert stats["nReturned"] == size
        assert stats["totalDocsExamined"] <= 3 * (skip + size) and stats["totalDocsExamined"] < n_log / 100

    def test_employee_monthly_reads_exactly_the_months_documents(self, big):
        client, _ = big
        body = client.get("/admin/explain/employee_monthly", params={"emp_code": "EMP00042", "month": "2026-06"}).json()
        text = json.dumps(body["explain"])
        assert body["collection"] == "attendance_logs" and "IXSCAN" in text and "COLLSCAN" not in text
        stats = cursor_node(body["explain"])["executionStats"]
        assert 15 <= stats["totalDocsExamined"] <= 30 and stats["totalDocsExamined"] == stats["nReturned"]

    @pytest.mark.parametrize("endpoint,params,collection", [
        ("department_summary", {"month": "2026-06"}, "employees"),
        ("department_summary", {"month": "2026-06", "department": "Sales"}, "employees"),
        ("late_leaderboard", {"month": "2026-06", "limit": 5}, "attendance_logs"),
        ("late_leaderboard", {"month": "2026-06", "department": "Sales"}, "attendance_logs"),
        ("department_trend", {"department": "Engineering", "from": "2026-06-01", "to": "2026-06-30"}, "employees"),
        ("department_trend", {"department": "Engineering", "from": "2026-04-01", "to": "2026-07-01"}, "employees"),
    ])
    def test_the_advanced_pipelines_are_explained_with_indexes_only(self, big, endpoint, params, collection):
        need((6, 0))
        client, _ = big
        r = client.get(f"/admin/explain/{endpoint}", params=params)
        assert r.status_code == 200, r.text
        body = r.json()
        text = json.dumps(body["explain"])
        assert body["collection"] == collection and "IXSCAN" in text and "COLLSCAN" not in text
        assert "executionStats" in text
        assert all(v == 0 for v in values_of(body["explain"], "collectionScans"))      # $lookup sub-queries: no collection scan

    @pytest.mark.parametrize("endpoint,builder,collection", [
        ("department_summary", lambda: main.build_department_summary_pipeline("2026-06", None), "employees"),
        ("department_summary", lambda: main.build_department_summary_pipeline("2026-06", "Sales"), "employees"),
        ("late_leaderboard", lambda: main.build_late_leaderboard_pipeline("2026-06", 5, None), "attendance_logs"),
        ("department_trend", lambda: main.build_department_trend_pipeline("Engineering", date(2026, 6, 1), date(2026, 6, 30)), "employees"),
    ])
    def test_the_leading_stage_of_every_pipeline_is_an_index_scan_on_any_real_server(self, big, endpoint, builder, collection):
        """What MongoDB turns into the pipeline's cursor plan is its leading $match. Explained through the same
        run_explain() as the endpoint, so it also works on servers too old to run the whole pipeline."""
        out = main.to_json_document(main.run_explain(main.aggregate_command(collection, builder()[:1])))
        text = json.dumps(out)
        assert "IXSCAN" in text and "COLLSCAN" not in text

    @pytest.mark.parametrize("builder", [
        lambda: main.build_department_summary_pipeline("2026-06", None)[1]["$lookup"]["pipeline"][0]["$match"],
        lambda: main.build_department_trend_pipeline("Engineering", date(2026, 6, 1), date(2026, 6, 30))[1]["$lookup"]["pipeline"][0]["$match"],
    ])
    def test_the_lookup_sub_query_is_one_index_range_per_employee(self, big, builder):
        match = builder()
        out = main.to_json_document(main.run_explain({"find": "attendance_logs", "filter": {"emp_code": "EMP00042", **match}}))
        text = json.dumps(out)
        assert "IXSCAN" in text and "COLLSCAN" not in text and "uq_emp_code_date" in text
        assert 15 <= out["executionStats"]["totalDocsExamined"] <= 30

    def test_the_employees_side_of_the_leaderboard_lookup_uses_the_unique_index(self, big):
        out = main.to_json_document(main.run_explain({"find": "employees", "filter": {"emp_code": "EMP00042"}}))
        assert "IXSCAN" in json.dumps(out) and "uq_emp_code" in json.dumps(out) and out["executionStats"]["totalDocsExamined"] == 1

    def test_the_check_can_fail_without_the_index_so_the_assertions_above_mean_something(self, big):
        client, _ = big
        main.db.attendance_logs.drop_index("ix_date_desc_emp_code")
        try:
            text = json.dumps(client.get("/admin/explain/attendance_list").json()["explain"])
            assert "COLLSCAN" in text                                             # the endpoint reports what the server really does
        finally:
            main.ensure_indexes()
        assert "COLLSCAN" not in json.dumps(client.get("/admin/explain/attendance_list").json()["explain"])

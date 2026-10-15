"""Test setup. Needs a reachable MongoDB (MONGO_URI, default mongodb://localhost:27017).
Tests use their own database (MONGO_DB defaults to 'hr_assessment_test') and wipe it.

    pip install pytest httpx          # test-only dependencies
    pytest -q tests
"""
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("MONGO_DB", "hr_assessment_test")

import pytest
from bson import json_util
from fastapi.testclient import TestClient
from pymongo import monitoring


class _Listener(monitoring.CommandListener):
    """Records every command sent to MongoDB so tests can check HOW the app queries."""

    def __init__(self):
        self.commands = []

    def started(self, event):
        self.commands.append((event.command_name, dict(event.command)))

    def succeeded(self, event):
        pass

    def failed(self, event):
        pass


LISTENER = _Listener()
monitoring.register(LISTENER)  # must happen before app.main creates its MongoClient

from app import main  # noqa: E402


@pytest.fixture()
def commands():
    LISTENER.commands.clear()
    return LISTENER.commands


@pytest.fixture()
def api():
    main.db.employees.drop()
    main.db.attendance_logs.drop()
    main.ensure_indexes()
    with TestClient(main.app) as c:
        yield c


def load_sample(name):
    opts = json_util.JSONOptions(tz_aware=True)
    return json_util.loads((ROOT / "sample_data" / f"{name}.json").read_text(), json_options=opts)


@pytest.fixture()
def seeded(api):
    """The nine sample attendance documents and six sample employees."""
    main.db.employees.insert_many(load_sample("employees"))
    main.db.attendance_logs.insert_many(load_sample("attendance_logs"))
    return api


def employee_body(code="EMP0001", **over):
    body = {"emp_code": code, "name": "Test User", "email": "t@example.com",
            "department": "Engineering", "joined_on": "2026-01-05"}
    body.update(over)
    return body


@pytest.fixture()
def atomic_updates():
    """Threaded 'exactly one winner' tests need a server whose filtered updates are atomic (MongoDB's are).
    FerretDB 1.x is not: I measured 2-8 'winners' per document for 8 racers on both of its backends.
    The deterministic interleaving tests cover the same guard logic on any server."""
    if engine() == "ferretdb1":
        pytest.skip("FerretDB 1.x does not make conditional updates atomic; use FerretDB 2.x or real MongoDB")


def engine():
    """'ferretdb1' (limited: no $cond in $group, non-atomic conditional updates), 'ferretdb2' (FerretDB 2.x on
    Microsoft's DocumentDB engine: runs $lookup pipelines, $setWindowFields, atomic updates) or 'mongodb'."""
    info = main.client.admin.command("buildInfo")
    if "ferretdbVersion" in info:
        return "ferretdb1"
    return "ferretdb2" if "ferretdb" in info else "mongodb"


def pytest_report_header(config):
    try:
        info = main.client.admin.command("buildInfo")
        kind = engine()
    except Exception as exc:  # no database reachable
        return f"database: unreachable ({type(exc).__name__})"
    names = {"ferretdb1": "FerretDB 1.x (limited)", "ferretdb2": "FerretDB 2.x on DocumentDB", "mongodb": "MongoDB"}
    note = " -> analytics tests use mongomock" if kind == "ferretdb1" else ""
    return f"database: {names[kind]} (reports version {info['version']}){note}"


@pytest.fixture()
def analytics_api(monkeypatch):
    """TestClient for the analytics endpoints. On real MongoDB they run against the real server. FerretDB 1.x
    cannot evaluate $cond / $in / $ifNull / $eq inside $group, so there the app is pointed at mongomock, an
    independent implementation of MongoDB's aggregation semantics (it checks the pipeline's logic, not the
    server's query planner)."""
    if engine() == "ferretdb1":
        import mongomock

        from tests import mongomock_shim

        mongomock_shim.install()  # $lookup + sub-pipeline and $setWindowFields/$rank; everything else is mongomock's own
        client = mongomock.MongoClient(tz_aware=True)
        monkeypatch.setattr(main, "client", client)
        monkeypatch.setattr(main, "db", client["analytics_test"])
    else:
        main.db.employees.drop()
        main.db.attendance_logs.drop()
    main.ensure_indexes()
    with TestClient(main.app) as c:
        yield c


@pytest.fixture()
def real_mongo():
    """For tests about the query planner or data volume, which only mean something on a real MongoDB."""
    if engine() != "mongodb":
        pytest.skip("needs real MongoDB (its explain output / planner); FerretDB's engines plan queries differently")

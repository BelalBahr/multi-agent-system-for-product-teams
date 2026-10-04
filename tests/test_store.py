import sqlite3

import pytest

from product_agents.models import Evidence
from product_agents.store import Store


def ev(i="e1", text="hello"):
    return Evidence(i, "jsonl", i, f"http://x/{i}", "2026-10-01T00:00:00Z", "smb", text)


def test_add_evidence_is_idempotent(tmp_path):
    s = Store(tmp_path / "a.db")
    assert s.add_evidence(ev()) is True
    assert s.add_evidence(ev()) is False
    assert s.count_evidence() == 1


def test_evidence_is_immutable(tmp_path):
    s = Store(tmp_path / "a.db")
    s.add_evidence(ev())
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        s.conn.execute("UPDATE evidence SET text = 'changed' WHERE id = 'e1'")
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        s.conn.execute("DELETE FROM evidence WHERE id = 'e1'")


def test_one_theme_per_evidence(tmp_path):
    s = Store(tmp_path / "a.db")
    s.add_evidence(ev())
    s.create_theme("t1", "One", "")
    s.create_theme("t2", "Two", "")
    assert s.assign_evidence("e1", "t1") is True
    assert s.assign_evidence("e1", "t2") is False
    assert s.assignments() == {"e1": "t1"}
    assert s.count_unassigned() == 0


def test_unassigned_and_theme_lookup(tmp_path):
    s = Store(tmp_path / "a.db")
    s.add_evidence(ev("e1"))
    s.add_evidence(ev("e2"))
    s.create_theme("t1", "Export Fails", "x")
    s.assign_evidence("e1", "t1")
    assert [e.id for e in s.unassigned_evidence(10)] == ["e2"]
    assert s.find_theme_by_title("export fails").id == "t1"


def test_cursor_meta(tmp_path):
    s = Store(tmp_path / "a.db")
    assert s.get_meta("k") is None
    s.set_meta("k", "v")
    assert s.get_meta("k") == "v"

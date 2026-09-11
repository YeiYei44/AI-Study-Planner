import json

from app.ingest.normalize import normalize_assignment, normalize_course


def test_normalize_course_basic():
    raw = {
        "id": 101,
        "name": "AP Biology",
        "course_code": "BIO201",
        "term": {"name": "Fall 2026"},
        "workflow_state": "available",
    }
    row = normalize_course(raw, "2026-09-11T00:00:00+00:00")
    assert row["id"] == 101
    assert row["name"] == "AP Biology"
    assert row["term"] == "Fall 2026"
    assert row["source"] == "canvas_session"
    assert json.loads(row["raw_json"]) == raw


def test_normalize_course_falls_back_when_name_missing():
    row = normalize_course({"id": 5, "course_code": "X1"}, "t")
    assert row["name"] == "X1"
    row2 = normalize_course({"id": 6}, "t")
    assert row2["name"] == "Course 6"


def test_normalize_assignment_basic():
    raw = {
        "id": 55,
        "name": "Essay 1",
        "description": "<p>Write about...</p>",
        "due_at": "2026-09-20T23:59:00Z",
        "points_possible": 100,
        "submission_types": ["online_upload"],
        "html_url": "https://x/courses/1/assignments/55",
        "workflow_state": "published",
    }
    row = normalize_assignment(raw, course_id=101, fetched_at="t")
    assert row["id"] == 55
    assert row["course_id"] == 101
    assert row["due_at"] == "2026-09-20T23:59:00Z"
    assert json.loads(row["submission_types"]) == ["online_upload"]
    assert json.loads(row["raw_json"]) == raw


def test_normalize_assignment_missing_name():
    row = normalize_assignment({"id": 9}, course_id=1, fetched_at="t")
    assert row["name"] == "Assignment 9"
    assert row["due_at"] is None

import time
from unittest.mock import MagicMock, patch
import pytest
from datetime import datetime, timezone


def test_health_endpoint_healthy(client):
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_health_endpoint_db_failure(client):
    mock_db = MagicMock()
    mock_db.client.admin.command.side_effect = Exception("DB Down")
    with patch("app.main.get_db", return_value=mock_db):
        res = client.get("/health")
        assert res.status_code == 503
        assert res.json()["detail"] == "Database unavailable"


def test_create_employee_success(client, test_db):
    test_db.employees.delete_many({})
    payload = {
        "emp_code": "EMP0100",
        "name": "Ananya Sharma",
        "email": "ananya@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05"
    }
    res = client.post("/employees", json=payload)
    assert res.status_code == 201
    data = res.json()
    assert data["emp_code"] == "EMP0100"
    assert data["name"] == "Ananya Sharma"
    assert "created_at" in data
    assert isinstance(data["created_at"], int)
    assert "_id" not in data


def test_create_employee_duplicate_conflict(client):
    payload = {
        "emp_code": "EMP0100",
        "name": "Duplicate Ananya",
        "email": "ananya2@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05"
    }
    res = client.post("/employees", json=payload)
    assert res.status_code == 409
    assert res.json()["detail"] == "emp_code already exists"


def test_create_employee_validation_errors(client):
    # Invalid emp_code pattern
    res1 = client.post("/employees", json={
        "emp_code": "INVALID",
        "name": "Test",
        "email": "test@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-05"
    })
    assert res1.status_code == 422

    # Shift start equals shift end
    res2 = client.post("/employees", json={
        "emp_code": "EMP0101",
        "name": "Test",
        "email": "test@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "09:30",
        "joined_on": "2026-01-05"
    })
    assert res2.status_code == 422

    # Invalid email
    res3 = client.post("/employees", json={
        "emp_code": "EMP0101",
        "name": "Test",
        "email": "not-an-email",
        "department": "Engineering",
        "joined_on": "2026-01-05"
    })
    assert res3.status_code == 422

    # Invalid joined_on date format
    res4 = client.post("/employees", json={
        "emp_code": "EMP0101",
        "name": "Test",
        "email": "test@example.com",
        "department": "Engineering",
        "joined_on": "not-a-date"
    })
    assert res4.status_code == 422


def test_list_employees_pagination_and_filter(client, test_db):
    # Ensure second employee exists
    client.post("/employees", json={
        "emp_code": "EMP0102",
        "name": "Dev B",
        "email": "devb@example.com",
        "department": "Design",
        "joined_on": "2026-02-01"
    })

    # Page 1
    res = client.get("/employees?page=1&page_size=1")
    assert res.status_code == 200
    data = res.json()
    assert len(data["items"]) == 1
    assert data["total"] >= 2
    assert data["page"] == 1
    assert data["page_size"] == 1

    # Filter by department
    res_dept = client.get("/employees?department=Design")
    assert res_dept.status_code == 200
    data_dept = res_dept.json()
    assert data_dept["total"] == 1
    assert data_dept["items"][0]["emp_code"] == "EMP0102"


def test_punch_in_and_duplicate(client, test_db):
    test_db.attendance_logs.delete_many({})
    # Punch in 09:28 IST on 2026-07-06 (epoch ms: 1783310280000)
    punch_ms = 1783310280000
    payload = {
        "emp_code": "EMP0100",
        "punched_at": punch_ms,
        "status": "PRESENT"
    }
    res = client.post("/attendance/punch-in", json=payload)
    assert res.status_code == 201
    record = res.json()
    assert record["emp_code"] == "EMP0100"
    assert record["date"] == "2026-07-06"
    assert record["punch_in"] == punch_ms
    assert record["punch_out"] is None
    assert record["work_hours"] is None
    assert record["late_minutes"] == 0
    assert record["overtime_minutes"] == 0
    assert record["half_day"] is False
    assert record["history"] == []
    assert "_id" not in record

    # Duplicate punch in for same day
    res_dup = client.post("/attendance/punch-in", json=payload)
    assert res_dup.status_code == 409
    assert "already punched in" in res_dup.json()["detail"]


def test_punch_in_omitted_timestamp(client, test_db):
    test_db.attendance_logs.delete_many({"emp_code": "EMP0102"})
    res = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0102"
    })
    assert res.status_code == 201
    assert res.json()["punch_in"] is not None


def test_punch_in_unknown_employee(client):
    res = client.post("/attendance/punch-in", json={
        "emp_code": "EMP9999",
        "punched_at": 1783310280000
    })
    assert res.status_code == 404
    assert res.json()["detail"] == "Employee not found"


def test_punch_out_success_and_validation(client, test_db):
    # Punch out at 18:35 IST on 2026-07-06 (epoch ms: 1783343100000)
    out_ms = 1783343100000
    res = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0100",
        "punched_at": out_ms
    })
    assert res.status_code == 200
    data = res.json()
    assert data["punch_out"] == out_ms
    assert data["work_hours"] == 9.12
    assert data["overtime_minutes"] == 0
    assert data["half_day"] is False

    # Second punch out should return 409
    res_dup = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0100",
        "punched_at": out_ms
    })
    assert res_dup.status_code == 409
    assert res_dup.json()["detail"] == "already punched out"


def test_punch_out_omitted_timestamp(client, test_db):
    # Create punch in 1 hour ago
    test_db.attendance_logs.delete_many({"emp_code": "EMP0102"})
    now_sec = int(time.time())
    one_hour_ago_ms = (now_sec - 3600) * 1000
    client.post("/attendance/punch-in", json={
        "emp_code": "EMP0102",
        "punched_at": one_hour_ago_ms
    })
    res = client.post("/attendance/punch-out", json={"emp_code": "EMP0102"})
    assert res.status_code == 200
    assert res.json()["punch_out"] is not None


def test_punch_out_unknown_employee(client):
    res = client.post("/attendance/punch-out", json={"emp_code": "EMP9999"})
    assert res.status_code == 404
    assert res.json()["detail"] == "Employee not found"


def test_punch_out_no_punch_in(client, test_db):
    client.post("/employees", json={
        "emp_code": "EMP0105",
        "name": "No Punch",
        "email": "nopunch@example.com",
        "department": "Ops",
        "joined_on": "2026-01-01"
    })
    res = client.post("/attendance/punch-out", json={"emp_code": "EMP0105"})
    assert res.status_code == 404
    assert res.json()["detail"] == "No punch-in found"


def test_punch_out_timing_validations(client, test_db):
    # Create new punch in for another day
    punch_in_ms = 1783396680000  # 2026-07-07 09:28 IST
    client.post("/attendance/punch-in", json={
        "emp_code": "EMP0100",
        "punched_at": punch_in_ms
    })

    # punch_out <= punch_in
    res_early = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0100",
        "punched_at": punch_in_ms - 1000
    })
    assert res_early.status_code == 422
    assert "must be after" in res_early.json()["detail"]

    # punch_out > 24 hours
    res_too_late = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0100",
        "punched_at": punch_in_ms + (25 * 3600 * 1000)
    })
    assert res_too_late.status_code == 422
    assert "24 hours" in res_too_late.json()["detail"]


def test_list_attendance_endpoint_filters(client):
    # Filter by emp_code and status
    res = client.get("/attendance?emp_code=EMP0100&status=PRESENT&date_from=2026-07-01&date_to=2026-07-31")
    assert res.status_code == 200
    data = res.json()
    assert "items" in data
    assert data["total"] >= 1
    dates = [item["date"] for item in data["items"]]
    assert dates == sorted(dates, reverse=True)


def test_list_attendance_date_validation(client):
    res = client.get("/attendance?date_from=2026-07-10&date_to=2026-07-01")
    assert res.status_code == 422
    assert res.json()["detail"] == "date_from must be <= date_to"


def test_regularize_attendance_success(client, test_db):
    # Regularize 2026-07-06 record
    new_in = 1783312500000   # 10:05 IST
    new_out = 1783345200000  # 19:10 IST

    payload = {
        "status": "PRESENT",
        "punch_in": new_in,
        "punch_out": new_out,
        "reason": "biometric correction for shift",
        "regularized_by": "hr.manager"
    }

    res = client.patch("/attendance/EMP0100/2026-07-06", json=payload)
    assert res.status_code == 200
    data = res.json()
    assert data["late_minutes"] == 35
    assert data["overtime_minutes"] == 40
    assert len(data["history"]) == 1
    h = data["history"][0]
    assert h["by"] == "hr.manager"
    assert h["reason"] == "biometric correction for shift"
    assert "punch_in" in h["changes"]
    assert "late_minutes" in h["changes"]
    assert h["changes"]["late_minutes"]["from"] == 0
    assert h["changes"]["late_minutes"]["to"] == 35


def test_regularize_set_leave_or_absent(client, test_db):
    res = client.patch("/attendance/EMP0100/2026-07-06", json={
        "status": "LEAVE",
        "reason": "Employee took approved leave",
        "regularized_by": "hr.admin"
    })
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "LEAVE"
    assert data["punch_in"] is None
    assert data["punch_out"] is None
    assert data["work_hours"] is None
    assert data["late_minutes"] == 0
    assert len(data["history"]) == 2


def test_regularize_validations(client):
    # Unknown employee
    res1 = client.patch("/attendance/EMP9999/2026-07-06", json={
        "reason": "test reason",
        "regularized_by": "admin"
    })
    assert res1.status_code == 404

    # Nonexistent attendance date
    res2 = client.patch("/attendance/EMP0100/2026-01-01", json={
        "reason": "test reason",
        "regularized_by": "admin"
    })
    assert res2.status_code == 404

    # Supply punch times when setting ABSENT
    res3 = client.patch("/attendance/EMP0100/2026-07-06", json={
        "status": "ABSENT",
        "punch_in": 1783312500000,
        "reason": "marking absent with times",
        "regularized_by": "admin"
    })
    assert res3.status_code == 422

    # Request changing nothing
    res4 = client.patch("/attendance/EMP0100/2026-07-06", json={
        "status": "LEAVE",
        "reason": "no changes submitted",
        "regularized_by": "admin"
    })
    assert res4.status_code == 422
    assert "nothing" in res4.json()["detail"]

    # Punch in date mismatch with attendance date
    res5 = client.patch("/attendance/EMP0100/2026-07-06", json={
        "status": "PRESENT",
        "punch_in": 1783396680000,  # 2026-07-07 date
        "reason": "wrong date punch in",
        "regularized_by": "admin"
    })
    assert res5.status_code == 422
    assert "date" in res5.json()["detail"]

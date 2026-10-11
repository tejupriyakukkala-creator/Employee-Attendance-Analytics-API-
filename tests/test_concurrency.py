import concurrent.futures
import pytest
from app.main import get_db, create_indexes


def test_concurrent_punch_in_race(client, test_db):
    # Ensure clean employee and logs
    test_db.employees.delete_many({"emp_code": "EMP0200"})
    test_db.attendance_logs.delete_many({"emp_code": "EMP0200"})
    create_indexes()

    client.post("/employees", json={
        "emp_code": "EMP0200",
        "name": "Race Tester",
        "email": "race@example.com",
        "department": "QA",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })

    punch_ms = 1783310280000  # 2026-07-06 09:28 IST
    payload = {
        "emp_code": "EMP0200",
        "punched_at": punch_ms,
        "status": "PRESENT"
    }

    # Fire 5 concurrent punch-in requests
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(client.post, "/attendance/punch-in", json=payload) for _ in range(5)]
        for f in concurrent.futures.as_completed(futures):
            results.append(f.result().status_code)

    assert results.count(201) == 1
    assert results.count(409) == 4


def test_concurrent_punch_out_race(client, test_db):
    test_db.employees.delete_many({"emp_code": "EMP0201"})
    test_db.attendance_logs.delete_many({"emp_code": "EMP0201"})
    create_indexes()

    client.post("/employees", json={
        "emp_code": "EMP0201",
        "name": "Race Tester Out",
        "email": "raceout@example.com",
        "department": "QA",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })

    punch_in_ms = 1783310280000
    client.post("/attendance/punch-in", json={
        "emp_code": "EMP0201",
        "punched_at": punch_in_ms,
        "status": "PRESENT"
    })

    punch_out_ms = 1783343100000
    payload = {
        "emp_code": "EMP0201",
        "punched_at": punch_out_ms
    }

    # Fire 5 concurrent punch-out requests
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(client.post, "/attendance/punch-out", json=payload) for _ in range(5)]
        for f in concurrent.futures.as_completed(futures):
            results.append(f.result().status_code)

    assert results.count(200) == 1
    assert results.count(409) == 4


def test_concurrent_employee_creation_race(client, test_db):
    test_db.employees.delete_many({"emp_code": "EMP0202"})
    create_indexes()

    payload = {
        "emp_code": "EMP0202",
        "name": "Concurrent Employee",
        "email": "conc@example.com",
        "department": "QA",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    }

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(client.post, "/employees", json=payload) for _ in range(5)]
        for f in concurrent.futures.as_completed(futures):
            results.append(f.result().status_code)

    assert results.count(201) == 1
    assert results.count(409) == 4

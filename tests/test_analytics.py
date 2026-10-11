import pathlib
import pytest
from bson import json_util
from app.main import get_db, create_indexes


@pytest.fixture(autouse=True)
def load_sample_dataset(test_db):
    test_db.employees.delete_many({})
    test_db.attendance_logs.delete_many({})
    create_indexes()

    here = pathlib.Path(__file__).parent.parent / "sample_data"
    opts = json_util.JSONOptions(tz_aware=True)
    for name in ("employees", "attendance_logs"):
        path = here / f"{name}.json"
        if path.exists():
            docs = json_util.loads(path.read_text(), json_options=opts)
            test_db[name].insert_many(docs)


def test_employee_monthly_analytics(client):
    # EMP0001 (Asha Rao) joined 2026-01-05
    # In July 2026: total weekdays = 23
    # Logs in sample:
    # 2026-07-06: PRESENT, work_hours 9.12, late 0
    # 2026-07-07: PRESENT, work_hours 9.03, late 0
    # Both are weekdays, present_days = 2.0
    res = client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["emp_code"] == "EMP0001"
    assert data["month"] == "2026-07"
    assert data["working_days"] == 23
    assert data["present_days"] == 2.0
    assert data["leave_days"] == 0
    assert data["late_count"] == 0
    assert data["total_late_minutes"] == 0
    # 2.0 / 23 * 100 = 8.69565... -> 8.70%
    assert data["attendance_pct"] == 8.70


def test_employee_monthly_mid_month_joiner(client):
    # EMP0002 joined 2026-07-13
    # Weekdays from 2026-07-13 to 2026-07-31 = 15 working days
    # Has 1 log on 2026-07-14 (PRESENT) -> present_days = 1.0
    res = client.get("/analytics/employees/EMP0002/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["working_days"] == 15
    assert data["present_days"] == 1.0
    # 1.0 / 15 * 100 = 6.666... -> 6.67%
    assert data["attendance_pct"] == 6.67


def test_employee_monthly_unknown_employee(client):
    res = client.get("/analytics/employees/EMP9999/monthly?month=2026-07")
    assert res.status_code == 404
    assert res.json()["detail"] == "Employee not found"


def test_department_summary_analytics(client):
    res = client.get("/analytics/departments/summary?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["month"] == "2026-07"
    items = {item["department"]: item for item in data["items"]}

    # Engineering: 2 employees (EMP0001, EMP0002)
    eng = items["Engineering"]
    assert eng["headcount"] == 2
    assert eng["present_days"] == 3.0
    assert eng["avg_work_hours"] == 9.04
    assert eng["late_count"] == 0
    assert eng["total_late_minutes"] == 0

    # Sales: 2 employees (EMP0003, EMP0004)
    sales = items["Sales"]
    assert sales["headcount"] == 2
    assert sales["present_days"] == 1.5
    assert sales["avg_work_hours"] == 6.29
    assert sales["late_count"] == 1
    assert sales["total_late_minutes"] == 35
    assert sales["leave_count"] == 1

    # Support: 2 employees (EMP0005, EMP0006)
    supp = items["Support"]
    assert supp["headcount"] == 2
    assert supp["present_days"] == 2.0
    assert supp["avg_work_hours"] == 8.75


def test_department_summary_filter(client):
    res = client.get("/analytics/departments/summary?month=2026-07&department=Sales")
    assert res.status_code == 200
    data = res.json()
    assert len(data["items"]) == 1
    assert data["items"][0]["department"] == "Sales"


def test_late_leaderboard_ranking_and_ties(client, test_db):
    # Test standard competition ranking (1, 2, 2, 4) with ties
    # Insert extra logs with ties into test_db
    test_db.attendance_logs.insert_many([
        {
            "emp_code": "EMP0001",
            "date": "2026-07-10",
            "status": "PRESENT",
            "late_minutes": 50
        },
        {
            "emp_code": "EMP0002",
            "date": "2026-07-10",
            "status": "PRESENT",
            "late_minutes": 35  # Tied with EMP0003 who has 35 min
        },
        {
            "emp_code": "EMP0004",
            "date": "2026-07-10",
            "status": "PRESENT",
            "late_minutes": 10
        }
    ])

    res = client.get("/analytics/leaderboard/late?month=2026-07&limit=10")
    assert res.status_code == 200
    data = res.json()
    items = data["items"]

    # Rank 1: EMP0001 (50 min)
    assert items[0]["emp_code"] == "EMP0001"
    assert items[0]["rank"] == 1

    # Rank 2 & 2 (Tie): EMP0002 and EMP0003 (both 35 min)
    # Sorted by total_late_minutes desc, then emp_code asc
    assert items[1]["rank"] == 2
    assert items[2]["rank"] == 2
    assert items[1]["emp_code"] == "EMP0002"
    assert items[2]["emp_code"] == "EMP0003"

    # Rank 4 (Rank 3 was skipped due to competition ranking!)
    assert items[3]["rank"] == 4
    assert items[3]["emp_code"] == "EMP0004"


def test_department_trend_gap_filling_and_moving_average(client):
    res = client.get("/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-07-07")
    assert res.status_code == 200
    data = res.json()
    assert data["department"] == "Engineering"
    items = data["items"]
    # Exactly 7 days (2026-07-01 to 2026-07-07)
    assert len(items) == 7

    # Jul 1 (Wed): working day, headcount 1, present 0, rate 0.0, moving avg 0.0
    assert items[0]["date"] == "2026-07-01"
    assert items[0]["is_working_day"] is True
    assert items[0]["attendance_rate"] == 0.0
    assert items[0]["moving_avg_7d"] == 0.0

    # Jul 4 (Sat): weekend, is_working_day False, rate None
    assert items[3]["date"] == "2026-07-04"
    assert items[3]["is_working_day"] is False
    assert items[3]["attendance_rate"] is None
    assert items[3]["moving_avg_7d"] == 0.0

    # Jul 6 (Mon): working day, headcount 1, present 1.0, rate 1.0, moving avg 0.25
    assert items[5]["date"] == "2026-07-06"
    assert items[5]["is_working_day"] is True
    assert items[5]["attendance_rate"] == 1.0
    assert items[5]["moving_avg_7d"] == 0.25

    # Jul 7 (Tue): working day, headcount 1, present 1.0, rate 1.0, moving avg 0.4
    assert items[6]["date"] == "2026-07-07"
    assert items[6]["is_working_day"] is True
    assert items[6]["attendance_rate"] == 1.0
    assert items[6]["moving_avg_7d"] == 0.4


def test_department_trend_validations(client):
    # Unknown department -> 404
    res1 = client.get("/analytics/departments/NonexistentDept/trend?from=2026-07-01&to=2026-07-07")
    assert res1.status_code == 404
    assert res1.json()["detail"] == "Department not found"

    # to < from -> 422
    res2 = client.get("/analytics/departments/Engineering/trend?from=2026-07-07&to=2026-07-01")
    assert res2.status_code == 422
    assert ">= from date" in res2.json()["detail"]

    # Range > 92 days -> 422
    res3 = client.get("/analytics/departments/Engineering/trend?from=2026-01-01&to=2026-05-01")
    assert res3.status_code == 422
    assert "92 days" in res3.json()["detail"]


def test_admin_explain_all_endpoints(client):
    # 1. attendance_list
    r1 = client.get("/admin/explain/attendance_list?date_from=2026-07-01&date_to=2026-07-31")
    assert r1.status_code == 200
    d1 = r1.json()
    assert d1["collection"] == "attendance_logs"
    assert "explain" in d1

    # 2. employee_monthly
    r2 = client.get("/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07")
    assert r2.status_code == 200
    d2 = r2.json()
    assert d2["collection"] == "attendance_logs"

    # 3. department_summary
    r3 = client.get("/admin/explain/department_summary?month=2026-07")
    assert r3.status_code == 200
    d3 = r3.json()
    assert d3["collection"] == "employees"

    # 4. late_leaderboard
    r4 = client.get("/admin/explain/late_leaderboard?month=2026-07")
    assert r4.status_code == 200
    d4 = r4.json()
    assert d4["collection"] == "attendance_logs"

    # 5. department_trend
    r5 = client.get("/admin/explain/department_trend?department=Engineering&from=2026-07-01&to=2026-07-07")
    assert r5.status_code == 200
    d5 = r5.json()
    assert d5["collection"] == "attendance_logs"


def test_admin_explain_missing_param_validation(client):
    # Missing required month for employee_monthly
    r = client.get("/admin/explain/employee_monthly?emp_code=EMP0001")
    assert r.status_code == 422

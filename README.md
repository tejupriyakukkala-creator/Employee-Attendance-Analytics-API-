# Employee Attendance & Analytics API

A FastAPI and MongoDB service for employee attendance tracking, manual corrections with audit trails, and aggregation-based analytics.

## Tech Stack
- **Python:** 3.11+ (Tested on Python 3.12 and 3.14)
- **Framework:** FastAPI
- **Database:** MongoDB 6.0+ (Tested on MongoDB 8.3)
- **Validation:** Pydantic V2
- **Driver:** PyMongo 4.x

---

## 1. Environment Configuration

The application reads database configuration from environment variables (a local `.env` is supported as fallback; system environment variables take precedence):

```env
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

No secrets or connection credentials are hardcoded.

---

## 2. Local Setup & Installation

### Step 1: Create and activate virtual environment
```bash
python -m venv .venv
# On Windows:
.venv\Scripts\activate
# On Linux / macOS:
source .venv/bin/activate
```

### Step 2: Install dependencies
```bash
pip install -r requirements.txt
```

### Step 3: (Optional) Load sample data
```bash
python sample_seed.py
```

---

## 3. Running the Application

Start the server using Uvicorn:
```bash
uvicorn app.main:app --port 8000 --reload
```

- **Interactive API Documentation (Swagger UI):** `http://localhost:8000/docs`
- **ReDoc Documentation:** `http://localhost:8000/redoc`
- **OpenAPI JSON Contract:** `http://localhost:8000/openapi.json`
- **Health Check:** `http://localhost:8000/health`

All indexes are automatically and idempotently created on application startup via the FastAPI lifespan event.

---

## 4. Running Automated Tests

Run the full automated test suite with coverage:

```bash
pytest -v
```

To run with coverage report:
```bash
python -m coverage run --source=app -m pytest
python -m coverage report -m
```

The test suite covers:
- Unit business rules (R1 through R8, timezones, overnight shifts, rounding)
- REST API endpoint operations and status codes
- Concurrency race conditions (punch-in, punch-out, regularization)
- Aggregation pipelines with boundary conditions and ties
- Index usage verification (`IXSCAN` without `COLLSCAN`)

---

## 5. API Endpoints Overview

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/health` | Service and MongoDB readiness check (200 / 503) |
| `POST` | `/employees` | Register new employee (unique `emp_code`, 409 conflict) |
| `GET` | `/employees` | List employees (paginated, sorted by `emp_code` ASC) |
| `POST` | `/attendance/punch-in` | Record daily punch-in (R1 date, R2 late minutes, race-safe) |
| `POST` | `/attendance/punch-out` | Record punch-out (R3 overtime, R4 work hours, R5 half-day) |
| `GET` | `/attendance` | List attendance logs (paginated, sorted by date DESC, emp_code ASC) |
| `PATCH` | `/attendance/{emp_code}/{date}` | Regularize attendance record with audit trail (`history`) |
| `GET` | `/analytics/employees/{emp_code}/monthly` | Monthly employee attendance metrics (aggregation) |
| `GET` | `/analytics/departments/summary` | Monthly department attendance & headcount summary (cross-collection) |
| `GET` | `/analytics/leaderboard/late` | Late leaderboard with standard competition ranking (1, 2, 2, 4) |
| `GET` | `/analytics/departments/{department}/trend` | Daily attendance trend with gap filling (`$densify`) and 7-day moving avg |
| `GET` | `/admin/explain/{endpoint}` | Query execution plan inspection (`IXSCAN` validation) |

---

## 6. Architecture & Deliverables

- `app/main.py`: Complete application codebase.
- `requirements.txt`: Python package requirements.
- `REVIEW.md`: Comprehensive review of starter defects, reproduction steps, and fixes.
- `DECISIONS.md`: Architectural decisions regarding indexing, concurrency, and scaling.
- `TEST_REPORT.md`: Requirement-by-requirement audit and test execution evidence.

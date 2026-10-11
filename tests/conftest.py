import os
import sys
import pathlib
import pytest
from fastapi.testclient import TestClient

# Ensure project root is in sys.path
root_dir = pathlib.Path(__file__).parent.parent
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))

# Use dedicated test database for automated test suite
os.environ["MONGO_URI"] = os.getenv("MONGO_URI", "mongodb://localhost:27017")
os.environ["MONGO_DB"] = "test_attendance_qa_db"

from app.main import app, get_db, create_indexes


@pytest.fixture(scope="session", autouse=True)
def setup_test_db():
    database = get_db()
    # Clean test collections before test session
    database.employees.delete_many({})
    database.attendance_logs.delete_many({})
    create_indexes()
    yield
    # Cleanup after session
    database.employees.delete_many({})
    database.attendance_logs.delete_many({})


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def test_db():
    return get_db()

import pytest
from fastapi.testclient import TestClient
from app.main import create_app
from app.models import JobStatus


@pytest.fixture
def client(settings):
    app = create_app()
    with TestClient(app) as c:
        yield c


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "app": "GovFlow AI"}


def test_create_job_and_delete(client):
    # Create job with direct URL
    r = client.post("/jobs", json={"url": "http://127.0.0.1:8080/form.html"})
    assert r.status_code == 200
    data = r.json()
    job_id = data["job_id"]
    assert job_id
    assert data["status"] in (JobStatus.RESOLVING.value, JobStatus.PLANNING.value)

    # Get job status
    r = client.get(f"/jobs/{job_id}")
    assert r.status_code == 200
    assert r.json()["job_id"] == job_id

    # Get profile
    r = client.get(f"/jobs/{job_id}/profile")
    assert r.status_code == 200

    # Delete job data
    r = client.delete(f"/jobs/{job_id}/data")
    assert r.status_code == 200
    assert r.json()["deleted"] is True


def test_websocket_stream(client):
    r = client.post("/jobs", json={"url": "http://127.0.0.1:8080/form.html"})
    job_id = r.json()["job_id"]

    with client.websocket_connect(f"/jobs/{job_id}/stream") as ws:
        ws.send_json({"type": "ping"})
        data = ws.receive_json()
        assert data == {"type": "pong"}


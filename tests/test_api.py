import time

from fastapi.testclient import TestClient

from pha_core.api import create_app


def test_api_snapshot_job_and_editor(tmp_path, raw):
    with TestClient(create_app(tmp_path / "api.sqlite")) as client:
        assert client.get("/").status_code == 200
        assert "PHA" in client.get("/").text
        assert client.get("/api/schema").json()["$defs"]["Datum"]
        assert client.get("/api/example").json()["project_id"] == "PHA-DEMO"
        validation = client.post("/api/validate", json=raw)
        assert validation.status_code == 200
        saved = client.post("/api/baselines", json=raw).json()
        sha = saved["digest"]
        assert client.get(f"/api/baselines/{sha}/tables/assets").json()["rows"][0]["id"] == "AST-TK01"
        job = client.post(f"/api/baselines/{sha}/analyze").json()["job_id"]
        for _ in range(100):
            row = client.get(f"/api/runs/{job}").json()
            if row["status"] != "RUNNING":
                break
            time.sleep(.01)
        assert row["status"] == "SUCCEEDED", row
        assert row["result"]["result"]["sil"]["SIF-TK01"]["pfdavg"] > 0
        assert client.get("/api/prism/capabilities").status_code == 422
        raw["gates"][0]["inputs"] = ["missing"]
        assert client.post("/api/validate", json=raw).status_code == 422


def test_api_failure_and_revision_errors(tmp_path, raw):
    with TestClient(create_app(tmp_path / "api.sqlite")) as client:
        sha = client.post("/api/baselines", json=raw).json()["digest"]
        assert client.post(f"/api/baselines/{sha}/preview", json=raw).status_code == 422
        raw["revision"] = 2
        raw["change_reason"] = "updated input"
        preview = client.post(f"/api/baselines/{sha}/preview", json=raw)
        assert preview.json()["committed"] is False
        assert len(client.get("/api/baselines").json()) == 1
        assert client.get("/api/runs/missing").status_code == 404

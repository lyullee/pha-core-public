"""Local API. Jobs run outside the request handler; PRISM uses its own process."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import ValidationError

from .application import analyze, run_study
from .domain import StudySnapshot, canonical
from .prism import PrismClient, PrismError
from .storage import Store


def create_app(db_path="pha.sqlite", *, prism_root=None):
    store = Store(db_path)
    executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="pha-job")

    @asynccontextmanager
    async def lifespan(app):
        yield
        executor.shutdown(wait=True, cancel_futures=False)

    app = FastAPI(title="PHA — FTA / LOPA / SIL / PRISM", version="0.1.0", lifespan=lifespan)
    app.state.store = store

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse(status_code=422, content={"error": str(exc)})

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return JSONResponse(status_code=404, content={"error": str(exc)})

    @app.exception_handler(PrismError)
    async def prism_failed(request, exc):
        return JSONResponse(status_code=422, content={"error": str(exc), "status": "BLOCKED"})

    def prism_client():
        if not prism_root:
            raise ValueError("Start server with --prism-root to enable PRISM integration")
        return PrismClient(prism_root)

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (Path(__file__).parent / "web/index.html").read_text(encoding="utf-8")

    @app.get("/api/health")
    def health():
        return {"status": "ok", "version": "0.1.0", "prism_configured": prism_root is not None}

    @app.get("/api/schema")
    def schema():
        return StudySnapshot.model_json_schema()

    @app.get("/api/example")
    def example():
        return json.loads((Path(__file__).parent / "resources/tank_overpressure.snapshot.json").read_text(encoding="utf-8"))

    @app.post("/api/validate")
    def validate(payload: dict):
        snapshot = StudySnapshot.model_validate_json(canonical(payload))
        return {"status": "VALID", "digest": snapshot.digest(), "mode": snapshot.mode,
                "entity_counts": {name: len(getattr(snapshot, name)) for name in type(snapshot).model_fields if isinstance(getattr(snapshot, name), tuple)}}

    @app.get("/api/baselines")
    def baselines():
        return store.list_baselines()

    @app.post("/api/baselines")
    def save(payload: dict):
        snapshot = StudySnapshot.model_validate_json(canonical(payload))
        sha = store.save(snapshot)
        return {"digest": sha, "status": "SAVED", "revision": snapshot.revision}

    @app.get("/api/baselines/{sha}")
    def baseline(sha: str):
        return store.load(sha).model_dump(mode="json")

    @app.get("/api/baselines/{sha}/tables/{table}")
    def table(sha: str, table: str):
        data = store.load(sha).model_dump(mode="json")
        if not isinstance(data.get(table), list):
            raise ValueError("unknown entity table")
        return {"table": table, "rows": data[table], "baseline_digest": sha}

    @app.post("/api/baselines/{sha}/preview")
    def preview(sha: str, payload: dict):
        previous = store.load(sha)
        candidate = StudySnapshot.model_validate_json(canonical(payload))
        if candidate.project_id != previous.project_id or candidate.study_id != previous.study_id:
            raise ValueError("preview must retain project/study identity")
        if candidate.revision <= previous.revision:
            raise ValueError("preview needs a greater baseline revision")
        changes = []
        for name, old in previous.model_dump(mode="json").items():
            new = candidate.model_dump(mode="json")[name]
            if old != new:
                changes.append(name)
        return {"candidate_digest": candidate.digest(), "changed_fields": changes, "committed": False}

    def submit(sha, module, operation):
        job_id = store.start_run(sha, module)

        def work():
            try:
                store.finish_run(job_id, result=operation(job_id))
            except Exception as exc:
                store.finish_run(job_id, error=f"{type(exc).__name__}: {exc}")

        executor.submit(work)
        return {"job_id": job_id, "status": "RUNNING"}

    @app.post("/api/baselines/{sha}/analyze", status_code=202)
    def calculate(sha: str):
        store.load(sha)
        return submit(sha, "analysis_job", lambda job_id: run_study(store, sha))

    @app.get("/api/runs/{run_id}")
    def run(run_id: str):
        return store.run(run_id)

    @app.get("/api/prism/capabilities")
    def capabilities():
        return prism_client().capabilities()

    def prepare(sha, payload, run_id=None):
        if not {"scenario_id", "port_ids", "release", "facility", "request"} <= payload.keys():
            raise ValueError("scenario_id, port_ids, release, facility and request are required")
        client = prism_client()
        snapshot = store.load(sha)
        analysis = analyze(snapshot)
        event = client.build_event(snapshot, analysis, payload["scenario_id"], port_ids=tuple(payload["port_ids"]),
                                   release=payload["release"], run_id=run_id)
        return client, event, payload["facility"], payload["request"]

    @app.post("/api/baselines/{sha}/prism/preflight")
    def preflight(sha: str, payload: dict):
        client, event, facility, request = prepare(sha, payload)
        return client.preflight(event, facility, request)

    @app.post("/api/baselines/{sha}/prism/execute", status_code=202)
    def execute(sha: str, payload: dict):
        # Prepare and validate before submitting, so missing provider histories
        # and contradictory identities are visible immediately in the editor.
        client, event, facility, request = prepare(sha, payload)
        client.preflight(event, facility, request)
        return submit(sha, "prism_consequences", lambda job_id: client.evaluate(
            {**event, "analysis_run_id": job_id}, facility, request))

    return app

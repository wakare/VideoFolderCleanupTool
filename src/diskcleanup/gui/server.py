from __future__ import annotations

import argparse
import json
import webbrowser
from importlib.resources import files
from pathlib import Path
from typing import Any, Callable

from starlette.responses import Response

try:
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import FileResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles

    HAS_FASTAPI = True
except ImportError:
    from starlette.applications import Starlette
    from starlette.exceptions import HTTPException
    from starlette.requests import Request
    from starlette.responses import FileResponse, JSONResponse
    from starlette.routing import Mount, Route
    from starlette.staticfiles import StaticFiles

    FastAPI = None  # type: ignore[assignment]
    HAS_FASTAPI = False

from .jobs import JobManager
from .workflows import (
    build_evidence,
    build_plan,
    evidence_settings_from_payload,
    list_cached_profiles,
    move_items,
    move_settings_from_payload,
    plan_settings_from_payload,
    read_evidence_preview,
    read_plan_preview,
    scan_settings_from_payload,
    scan_videos,
)


Handler = Callable[[Request], Any]


def static_directory() -> Path:
    return Path(str(files("diskcleanup.gui").joinpath("static")))


def create_handlers(manager: JobManager, static_dir: Path) -> dict[str, Handler]:
    async def index(_request: Request) -> FileResponse:
        return FileResponse(static_dir / "index.html")

    async def health(_request: Request) -> dict[str, object]:
        return {"ok": True, "backend": "fastapi" if HAS_FASTAPI else "starlette"}

    async def list_jobs(_request: Request) -> dict[str, object]:
        return {"jobs": [job.to_dict() for job in manager.list()]}

    async def get_job(request: Request) -> dict[str, object]:
        job = manager.get(request.path_params["job_id"])
        if not job:
            raise HTTPException(status_code=404, detail="job not found")
        return job.to_dict()

    async def profiles(request: Request) -> dict[str, object]:
        db = request.query_params.get("db", ".diskcleanup/cache.sqlite")
        return {"profiles": list_cached_profiles(Path(db))}

    async def submit_scan(request: Request) -> dict[str, object]:
        settings = scan_settings_from_payload(await request.json())
        job = manager.submit("scan", lambda progress: scan_videos(settings, progress))
        return job.to_dict()

    async def submit_plan(request: Request) -> dict[str, object]:
        settings = plan_settings_from_payload(await request.json())
        job = manager.submit("plan", lambda progress: build_plan(settings, progress))
        return job.to_dict()

    async def submit_evidence(request: Request) -> dict[str, object]:
        settings = evidence_settings_from_payload(await request.json())
        job = manager.submit("evidence", lambda progress: build_evidence(settings, progress))
        return job.to_dict()

    async def submit_move(request: Request) -> dict[str, object]:
        settings = move_settings_from_payload(await request.json())
        job = manager.submit("move", lambda progress: move_items(settings, progress))
        return job.to_dict()

    async def plan_preview(request: Request) -> dict[str, object]:
        path = request.query_params.get("path")
        if not path:
            raise HTTPException(status_code=400, detail="path is required")
        plan_path = Path(path)
        if not plan_path.exists():
            raise HTTPException(status_code=404, detail="plan not found")
        return read_plan_preview(plan_path)

    async def evidence_preview(request: Request) -> dict[str, object]:
        path = request.query_params.get("path")
        if not path:
            raise HTTPException(status_code=400, detail="path is required")
        output_dir = Path(path)
        if not output_dir.exists():
            raise HTTPException(status_code=404, detail="evidence output directory not found")
        return read_evidence_preview(output_dir)

    async def artifact(request: Request) -> FileResponse:
        path = request.query_params.get("path")
        if not path:
            raise HTTPException(status_code=400, detail="path is required")
        artifact_path = Path(path)
        if not artifact_path.exists() or not artifact_path.is_file():
            raise HTTPException(status_code=404, detail="artifact not found")
        return FileResponse(artifact_path)

    return {
        "index": index,
        "health": health,
        "list_jobs": list_jobs,
        "get_job": get_job,
        "profiles": profiles,
        "submit_scan": submit_scan,
        "submit_plan": submit_plan,
        "submit_evidence": submit_evidence,
        "submit_move": submit_move,
        "plan_preview": plan_preview,
        "evidence_preview": evidence_preview,
        "artifact": artifact,
    }


async def value_error_handler(_request: Request, exc: ValueError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


async def json_error_handler(_request: Request, exc: json.JSONDecodeError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": f"invalid JSON: {exc}"})


def as_starlette_endpoint(handler: Handler) -> Handler:
    async def endpoint(request: Request) -> Response:
        result = await handler(request)
        if isinstance(result, Response):
            return result
        return JSONResponse(result)

    return endpoint


def create_app():
    manager = JobManager(max_workers=2)
    static_dir = static_directory()
    handlers = create_handlers(manager, static_dir)

    if HAS_FASTAPI:
        app = FastAPI(title="DiskCleanUp GUI")
        app.mount("/static", StaticFiles(directory=static_dir), name="static")
        app.add_api_route("/", handlers["index"], methods=["GET"])
        app.add_api_route("/api/health", handlers["health"], methods=["GET"])
        app.add_api_route("/api/jobs", handlers["list_jobs"], methods=["GET"])
        app.add_api_route("/api/jobs/{job_id}", handlers["get_job"], methods=["GET"])
        app.add_api_route("/api/profiles", handlers["profiles"], methods=["GET"])
        app.add_api_route("/api/scan", handlers["submit_scan"], methods=["POST"])
        app.add_api_route("/api/plan", handlers["submit_plan"], methods=["POST"])
        app.add_api_route("/api/evidence", handlers["submit_evidence"], methods=["POST"])
        app.add_api_route("/api/move", handlers["submit_move"], methods=["POST"])
        app.add_api_route("/api/plan-preview", handlers["plan_preview"], methods=["GET"])
        app.add_api_route("/api/evidence-preview", handlers["evidence_preview"], methods=["GET"])
        app.add_api_route("/api/artifact", handlers["artifact"], methods=["GET"])
        app.add_exception_handler(ValueError, value_error_handler)
        app.add_exception_handler(json.JSONDecodeError, json_error_handler)
        return app

    routes = [
        Mount("/static", StaticFiles(directory=static_dir), name="static"),
        Route("/", as_starlette_endpoint(handlers["index"]), methods=["GET"]),
        Route("/api/health", as_starlette_endpoint(handlers["health"]), methods=["GET"]),
        Route("/api/jobs", as_starlette_endpoint(handlers["list_jobs"]), methods=["GET"]),
        Route("/api/jobs/{job_id}", as_starlette_endpoint(handlers["get_job"]), methods=["GET"]),
        Route("/api/profiles", as_starlette_endpoint(handlers["profiles"]), methods=["GET"]),
        Route("/api/scan", as_starlette_endpoint(handlers["submit_scan"]), methods=["POST"]),
        Route("/api/plan", as_starlette_endpoint(handlers["submit_plan"]), methods=["POST"]),
        Route("/api/evidence", as_starlette_endpoint(handlers["submit_evidence"]), methods=["POST"]),
        Route("/api/move", as_starlette_endpoint(handlers["submit_move"]), methods=["POST"]),
        Route("/api/plan-preview", as_starlette_endpoint(handlers["plan_preview"]), methods=["GET"]),
        Route("/api/evidence-preview", as_starlette_endpoint(handlers["evidence_preview"]), methods=["GET"]),
        Route("/api/artifact", as_starlette_endpoint(handlers["artifact"]), methods=["GET"]),
    ]
    return Starlette(
        routes=routes,
        exception_handlers={
            ValueError: value_error_handler,
            json.JSONDecodeError: json_error_handler,
        },
    )


app = create_app()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="diskcleanup-gui")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true", help="do not open a browser tab automatically")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    url = f"http://{args.host}:{args.port}"
    if not args.no_open:
        webbrowser.open(url)

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

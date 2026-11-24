"""Command-line entry point, also available as python -m pha_core."""
import argparse
import json
from pathlib import Path
import sys

from .application import analyze, run_study
from .domain import StudySnapshot
from .prism import PrismClient
from .storage import Store
from .tables import export_table, preview_table


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="pha", description="Traceable FTA → LOPA → SIL → PRISM")
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate")
    validate.add_argument("snapshot")
    run = sub.add_parser("run")
    run.add_argument("snapshot")
    run.add_argument("--db")
    run.add_argument("--output")
    schema = sub.add_parser("schema")
    schema.add_argument("--output")
    serve = sub.add_parser("serve")
    serve.add_argument("--db", default="pha.sqlite")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--prism-root")
    prism = sub.add_parser("prism")
    prism.add_argument("operation", choices=["capabilities", "preflight", "evaluate", "campaign"])
    prism.add_argument("--prism-root", required=True)
    prism.add_argument("--snapshot")
    prism.add_argument("--input", help="Explicit physical integration JSON: scenario_id, port_ids, release, facility, request")
    prism.add_argument("--output")
    tables = sub.add_parser("table")
    tables.add_argument("operation", choices=["export", "preview"])
    tables.add_argument("snapshot")
    tables.add_argument("table")
    tables.add_argument("path")
    tables.add_argument("--revision", type=int)
    tables.add_argument("--reason")
    tables.add_argument("--output", help="Write validated candidate JSON after preview; no database commit")
    evidence = sub.add_parser("evidence")
    evidence.add_argument("path")
    evidence.add_argument("--db", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "serve":
            import uvicorn
            from .api import create_app
            uvicorn.run(create_app(args.db, prism_root=args.prism_root), host="127.0.0.1", port=args.port)
            return 0
        if args.command == "schema":
            result = StudySnapshot.model_json_schema()
        elif args.command == "evidence":
            result = Store(args.db).ingest_evidence(args.path)
        elif args.command == "prism":
            client = PrismClient(args.prism_root)
            if args.operation == "capabilities":
                result = client.capabilities()
            else:
                payload = read(args.input)
                if args.operation == "campaign":
                    result = client.campaign(payload["facility"], payload["cases"])
                else:
                    s = StudySnapshot.model_validate_json(Path(args.snapshot).read_text(encoding="utf-8-sig"))
                    event = client.build_event(s, analyze(s), payload["scenario_id"], port_ids=tuple(payload["port_ids"]), release=payload["release"])
                    result = (client.preflight if args.operation == "preflight" else client.evaluate)(event, payload["facility"], payload["request"])
        else:
            s = StudySnapshot.model_validate_json(Path(args.snapshot).read_text(encoding="utf-8-sig"))
            if args.command == "validate":
                result = {"status": "VALID", "input_digest": s.digest(), "mode": s.mode}
            elif args.command == "run":
                if args.db:
                    store = Store(args.db)
                    result = run_study(store, store.save(s))
                else:
                    result = analyze(s)
            elif args.operation == "export":
                export_table(s, args.table, args.path)
                result = {"status": "EXPORTED", "path": str(Path(args.path).resolve())}
            else:
                candidate, result = preview_table(s, args.table, args.path, new_revision=args.revision, reason=args.reason)
                if args.output:
                    Path(args.output).write_text(candidate.model_dump_json(indent=2) + "\n", encoding="utf-8")
                    args.output = None
        output = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        if getattr(args, "output", None):
            target = Path(args.output)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(output, encoding="utf-8")
            print(f"Written: {target}")
        else:
            print(output)
        return 0
    except (ValueError, KeyError, OSError, RuntimeError, TypeError) as exc:
        print(json.dumps({"status": "BLOCKED", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

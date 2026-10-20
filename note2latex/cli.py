import argparse
import json
import shutil
from pathlib import Path

from .config import Settings
from .engine import Engine
from .ingest import MAX_PAGES, ingest


def main():
    parser = argparse.ArgumentParser(description="Math notes: vision API + bounded review agent")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor")
    serve = commands.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    convert = commands.add_parser("convert")
    convert.add_argument("files", nargs="+", type=Path)
    convert.add_argument("--title", default="Math notes")
    convert.add_argument("--template", choices=["notes", "article", "homework"], default="notes")
    args = parser.parse_args()
    settings = Settings.load()
    if args.command == "doctor":
        try:
            settings.require_provider()
            provider = "configured (not live-tested)"
        except ValueError as exc:
            provider = str(exc)
        print(json.dumps({"provider": provider, "data_dir": str(settings.data_dir),
                          "docker_available": bool(shutil.which("docker")),
                          "pdf_enabled": settings.compile_pdf}, indent=2))
        return
    if args.command == "serve":
        if args.host not in {"127.0.0.1", "localhost", "::1"} and not settings.service_token:
            parser.error("Non-loopback binding requires SERVICE_TOKEN")
        import uvicorn
        from .api import create_app
        uvicorn.run(create_app(settings), host=args.host, port=args.port, workers=1)
        return
    try:
        settings.require_provider()
        if not 1 <= len(args.title) <= 200:
            raise ValueError("Title must contain 1 to 200 characters")
        pages = []
        for path in args.files:
            from .ingest import MAX_BYTES
            if path.stat().st_size > MAX_BYTES:
                raise ValueError("Each file must be at most 25 MB")
            pages.extend(ingest(path.name, path.read_bytes()))
            if len(pages) > MAX_PAGES:
                raise ValueError("Maximum 12 pages per job")
        engine = Engine(settings)
        try:
            meta = engine.store.create(args.title, args.template, pages)
            engine.run(meta["id"])
            result = engine.store.get(meta["id"])
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if result["status"] == "failed":
                raise SystemExit(1)
        finally:
            engine.close()
    except (ValueError, OSError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()

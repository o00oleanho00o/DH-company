from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from .config import DB_PATH, ROOT_DIR, STORAGE_DIR
from .db import init_db
from .ingest import ingest_directory, ingest_workbook
from .pricing import catalog_stats, run_pricing


DEFAULT_HOLDOUT = "BOQ-HỆ THỐNG ĐIỆN TRẠI LƠN HẢI HÀ-DH290124.xlsx"


def _print(value) -> None:
    # Keep CLI commands usable on Windows consoles that default to cp1252;
    # source filenames and report payloads may contain Vietnamese characters.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def cmd_init_db(_: argparse.Namespace) -> None:
    init_db()
    _print({"status": "ok", "database": str(DB_PATH)})


def cmd_ingest(args: argparse.Namespace) -> None:
    init_db()
    path = Path(args.path)
    if path.is_dir():
        result = ingest_directory(path, holdout_filename=args.holdout, force=args.force)
    else:
        result = ingest_workbook(
            path,
            confirmed_type=args.type,
            exclude_prices=args.exclude_prices,
            force=args.force,
        )
    _print(result)


def cmd_seed(args: argparse.Namespace) -> None:
    init_db()
    input_dir = Path(args.input_dir)
    holdout = DEFAULT_HOLDOUT if args.holdout else None
    result = ingest_directory(
        input_dir,
        holdout_filename=holdout,
        # The default seed is the operational catalog only.  The holdout is
        # reserved for ``benchmark`` or an explicit quotation upload.
        skip_holdout=args.holdout,
        force=args.force,
    )
    _print({"results": result, "catalog": catalog_stats()})


def cmd_price(args: argparse.Namespace) -> None:
    init_db()
    _print(run_pricing(args.project_id, force=args.force))


def cmd_stats(_: argparse.Namespace) -> None:
    init_db()
    _print(catalog_stats())


def cmd_reset(_: argparse.Namespace) -> None:
    if STORAGE_DIR.exists():
        # The target is a fixed workspace-local path from app.config, not a
        # user-computed path.
        shutil.rmtree(STORAGE_DIR)
    init_db()
    _print({"status": "reset", "database": str(DB_PATH)})


def cmd_benchmark(args: argparse.Namespace) -> None:
    from benchmarks.holdout import run_benchmark

    result = run_benchmark(
        input_dir=Path(args.input_dir),
        holdout_filename=args.holdout,
        output_dir=Path(args.output_dir),
    )
    _print(result)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="DH M&E pricing platform CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init-db", help="Khởi tạo database")
    init.set_defaults(func=cmd_init_db)

    ingest = sub.add_parser("ingest", help="Import một workbook hoặc cả thư mục")
    ingest.add_argument("path")
    ingest.add_argument("--type", choices=["SUPPLIER_PRICE", "LABOR", "HISTORICAL_BOQ", "NEW_BOQ", "PANEL_BOM", "MIXED"])
    ingest.add_argument("--exclude-prices", action="store_true")
    ingest.add_argument("--holdout", default=None, help="Tên file giữ làm holdout khi ingest thư mục")
    ingest.add_argument("--force", action="store_true")
    ingest.set_defaults(func=cmd_ingest)

    seed = sub.add_parser("seed", help="Import toàn bộ input mẫu")
    seed.add_argument("--input-dir", default=str(ROOT_DIR / "input"))
    seed.add_argument("--holdout", action=argparse.BooleanOptionalAction, default=True)
    seed.add_argument("--force", action="store_true")
    seed.set_defaults(func=cmd_seed)

    price = sub.add_parser("price", help="Chạy pricing cho project")
    price.add_argument("project_id", type=int)
    price.add_argument("--force", action="store_true")
    price.set_defaults(func=cmd_price)

    stats = sub.add_parser("stats", help="Thống kê operational database")
    stats.set_defaults(func=cmd_stats)

    reset = sub.add_parser("reset", help="Xóa database/storage local và tạo lại")
    reset.set_defaults(func=cmd_reset)

    benchmark = sub.add_parser("benchmark", help="Chạy holdout benchmark")
    benchmark.add_argument("--input-dir", default=str(ROOT_DIR / "input"))
    benchmark.add_argument("--holdout", default=DEFAULT_HOLDOUT)
    benchmark.add_argument("--output-dir", default=str(ROOT_DIR / "benchmarks"))
    benchmark.set_defaults(func=cmd_benchmark)
    return parser


def main(argv: list[str] | None = None) -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()

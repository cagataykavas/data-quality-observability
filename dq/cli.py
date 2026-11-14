from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

import pandas as pd

from dq.contracts import ContractError, load_contract, run_contract
from dq.history import QualityHistory


def _write_atomically(path: str | Path, content: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            temporary_path = Path(handle.name)
        os.replace(temporary_path, target)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def read_frame(path: str | Path) -> pd.DataFrame:
    source = Path(path)
    suffix = source.suffix.lower()
    if suffix == ".csv":
        return pd.read_csv(source)
    if suffix in {".parquet", ".pq"}:
        return pd.read_parquet(source)
    raise ValueError("supported input formats are CSV and Parquet")


def command_check(args: argparse.Namespace) -> int:
    try:
        contract = load_contract(args.contract)
        frame = read_frame(args.data)
        result = run_contract(frame, contract)
    except (ValueError, ContractError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"passed": False, "error": str(exc)}, indent=2))
        return 2

    payload = result.as_dict()
    if args.history:
        history = QualityHistory(args.history)
        payload["run_id"] = history.record(result)

    rendered = json.dumps(payload, indent=2)
    if args.output:
        _write_atomically(args.output, rendered + "\n")
    print(rendered)
    return 0 if result.passed else 1


def command_history(args: argparse.Namespace) -> int:
    history = QualityHistory(args.database)
    print(json.dumps(history.recent(args.dataset, args.limit), indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="data-quality",
        description="Validate tabular datasets against declarative data contracts.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser("check")
    check.add_argument("--data", required=True)
    check.add_argument("--contract", required=True)
    check.add_argument("--output")
    check.add_argument("--history", help="SQLite path used to persist run history")
    check.set_defaults(handler=command_check)

    history = subparsers.add_parser("history")
    history.add_argument("--database", required=True)
    history.add_argument("--dataset", required=True)
    history.add_argument("--limit", type=int, default=20)
    history.set_defaults(handler=command_history)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())

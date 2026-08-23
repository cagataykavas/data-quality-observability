from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from dq.contracts import ContractError, load_contract, run_contract
from dq.history import QualityHistory


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
        frame = read_frame(args.data)
        contract = load_contract(args.contract)
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
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
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

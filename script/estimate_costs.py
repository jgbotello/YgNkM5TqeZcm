#!/usr/bin/env python3
"""Run the retrospective cost estimate for all four study conditions."""

from __future__ import annotations

import argparse
import ast
import json
import math
from pathlib import Path
from typing import Any

import pandas as pd
import tiktoken


ROOT = Path(__file__).resolve().parents[1]
NOTEBOOK = Path(__file__).resolve().with_name("code.ipynb")
DATASETS = ROOT / "datasets"

RUNS = (
    ("tatavla_context", "tatavla", "with_context", "movements_no_na_with_context_tatavla.xlsx"),
    ("tatavla_no_context", "tatavla", "no_context", "movements_no_na_no_context_tatavla.xlsx"),
    ("fener_context", "fener", "with_context", "movements_no_na_with_context_fener.xlsx"),
    ("fener_no_context", "fener", "no_context", "movements_no_na_no_context_fener.xlsx"),
)


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-price", type=float, default=2.50,
                        help="USD per 1M input tokens (default: 2.50)")
    parser.add_argument("--output-price", type=float, default=10.00,
                        help="USD per 1M output tokens (default: 10.00)")
    parser.add_argument("--model", default="gpt-4o")
    parser.add_argument("--batch-size", type=int, default=15)
    parser.add_argument(
        "--results-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "cost_estimates",
    )
    return parser.parse_args()


def extract_prompts() -> tuple[str, str]:
    notebook = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
    for cell in notebook["cells"]:
        source = "".join(cell.get("source", []))
        if "def build_batch_prompt" not in source:
            continue
        tree = ast.parse(source)
        values: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                if isinstance(target, ast.Name) and target.id in {
                    "header_with_context", "header_no_context"
                }:
                    values[target.id] = ast.literal_eval(node.value)
        if len(values) == 2:
            return values["header_with_context"], values["header_no_context"]
    raise RuntimeError(f"Could not extract both prompts from {NOTEBOOK}")


def clean_value(value: Any) -> Any:
    if pd.isna(value):
        return None
    return value.item() if hasattr(value, "item") else value


def estimate_run(
    *, sheet: str, output_name: str, prompt: str, model: str,
    batch_size: int, input_price: float, output_price: float,
) -> dict[str, Any]:
    input_df = pd.read_excel(DATASETS / "datasets.xlsx", sheet_name=sheet)
    input_df = input_df[input_df["Migration"].notna()].reset_index(drop=True)
    output_df = pd.read_excel(DATASETS / output_name)

    prefixes = ("location_", "latitude_", "longitude_", "date_hijri_", "date_gregorian_")
    output_columns = [column for column in output_df.columns if str(column).startswith(prefixes)]
    if not output_columns:
        raise ValueError(f"No model-output columns found in {output_name}")

    row_count = min(len(input_df), len(output_df))
    input_df = input_df.iloc[:row_count]
    output_df = output_df.iloc[:row_count]
    try:
        encoding = tiktoken.encoding_for_model(model)
    except KeyError:
        encoding = tiktoken.get_encoding("o200k_base")

    def count(text: str) -> int:
        return len(encoding.encode(text))

    system = (
        "You are a precise extractor of historical migration paths. "
        "Think step-by-step INTERNALLY and output valid JSON only."
    )
    input_tokens = 0
    output_tokens = 0
    for start in range(0, row_count, batch_size):
        stop = min(start + batch_size, row_count)
        records = []
        results = []
        for index in range(start, stop):
            row = input_df.iloc[index]
            records.append(
                f"Defter: {clean_value(row['Defter'])}\n"
                f"Place of origin: {clean_value(row['Place of origin'])}\n"
                f"Migration: {clean_value(row['Migration'])}"
            )
            output_row = output_df.iloc[index]
            result = {
                column: clean_value(output_row[column])
                for column in output_columns
                if clean_value(output_row[column]) is not None
            }
            results.append(result)

        # Six tokens approximate the chat-message framing for each API call.
        input_tokens += count(system) + count(prompt) + count("\n\n".join(records)) + 6
        output_tokens += count(json.dumps(
            {"results": results}, ensure_ascii=False, separators=(",", ":")
        ))

    calls = math.ceil(row_count / batch_size)
    input_cost = input_tokens / 1_000_000 * input_price
    output_cost = output_tokens / 1_000_000 * output_price
    return {
        "method": "retrospective tokenizer-based estimate; no API call made",
        "model_string": model,
        "tokenizer": encoding.name,
        "records": row_count,
        "batch_size": batch_size,
        "estimated_api_calls": calls,
        "estimated_input_tokens": input_tokens,
        "estimated_output_tokens": output_tokens,
        "input_price_usd_per_million_tokens": input_price,
        "output_price_usd_per_million_tokens": output_price,
        "estimated_input_cost_usd": input_cost,
        "estimated_output_cost_usd": output_cost,
        "estimated_total_cost_usd": input_cost + output_cost,
        "limitations": [
            "Chat-message framing is approximated at six tokens per API call.",
            "Tabular outputs are re-serialized as compact JSON.",
            "Retries, failed calls, cached-token discounts, development runs, and the response schema are excluded.",
        ],
    }


def main() -> None:
    args = arguments()
    results_dir = args.results_dir.resolve()
    results_dir.mkdir(parents=True, exist_ok=True)

    with_context, no_context = extract_prompts()
    prompts = {"with_context": with_context, "no_context": no_context}
    summaries = []
    for run_name, sheet, mode, output_name in RUNS:
        item = estimate_run(
            sheet=sheet,
            output_name=output_name,
            prompt=prompts[mode],
            model=args.model,
            batch_size=args.batch_size,
            input_price=args.input_price,
            output_price=args.output_price,
        )
        item["run"] = run_name
        summary = results_dir / f"{run_name}_summary.json"
        summary.write_text(json.dumps(item, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        summaries.append(item)

    totals = {
        "model_string": args.model,
        "batch_size": args.batch_size,
        "input_price_usd_per_million_tokens": args.input_price,
        "output_price_usd_per_million_tokens": args.output_price,
        "runs": len(summaries),
        "records_across_runs": sum(item["records"] for item in summaries),
        "estimated_api_calls": sum(item["estimated_api_calls"] for item in summaries),
        "estimated_input_tokens": sum(item["estimated_input_tokens"] for item in summaries),
        "estimated_output_tokens": sum(item["estimated_output_tokens"] for item in summaries),
        "estimated_input_cost_usd": sum(item["estimated_input_cost_usd"] for item in summaries),
        "estimated_output_cost_usd": sum(item["estimated_output_cost_usd"] for item in summaries),
        "estimated_total_cost_usd": sum(item["estimated_total_cost_usd"] for item in summaries),
        "individual_summaries": [f"{item['run']}_summary.json" for item in summaries],
        "pricing_note": "Replace prices and rerun if a different historical pricing date applies.",
    }
    total_file = results_dir / "all_experiments_summary.json"
    total_file.write_text(json.dumps(totals, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print("\nCOMBINED TOTAL")
    print(json.dumps(totals, indent=2, ensure_ascii=False))
    print(f"\nAll files saved in: {results_dir}")


if __name__ == "__main__":
    main()

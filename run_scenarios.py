"""Evaluate an agent or application using explicit scenario evidence."""

import argparse
import asyncio
import json
import os
from pathlib import Path

from app.scenarios.adapters import FixtureAdapter, HttpAdapter, LegalRagAdapter
from app.scenarios.runner import load_scenarios, run_scenarios


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--adapter", choices=["fixture", "http", "legal-rag"], required=True)
    parser.add_argument("--fixtures")
    parser.add_argument("--url")
    parser.add_argument("--token-env", help="Name of environment variable containing a bearer credential")
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--output-dir", required=True, help="New directory; existing results are never overwritten")
    parser.add_argument("--target-label", default="application", help="Nonsecret target/version label")
    parser.add_argument("--ready-path", default="/workflow_status/status", help="Legal RAG readiness JSON Pointer")
    parser.add_argument("--ready-value", default="human_in_loop", help="Legal RAG ready status")
    args = parser.parse_args()
    scenarios, digest = load_scenarios(args.dataset)
    if args.adapter == "fixture":
        if not args.fixtures:
            parser.error("--fixtures is required in fixture mode")
        adapter = FixtureAdapter(json.loads(Path(args.fixtures).read_text(encoding="utf-8")))
    else:
        if not args.url:
            parser.error("--url is required for live targets")
        token = os.environ.get(args.token_env) if args.token_env else None
        if args.token_env and not token:
            parser.error("The configured credential environment variable is empty")
        adapter = (LegalRagAdapter(args.url, token, ready_path=args.ready_path, ready_value=args.ready_value)
                   if args.adapter == "legal-rag" else HttpAdapter(args.url, token))
    manifest = await run_scenarios(scenarios, adapter, args.output_dir,
                                   timeout_seconds=args.timeout, dataset_sha256=digest,
                                   target_label=args.target_label)
    print(json.dumps({k: manifest[k] for k in ("run_id", "status", "simulated", "metrics")}, indent=2))
    return {"passed": 0, "regressed": 1, "inconclusive": 2}[manifest["metrics"]["decision"]]


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

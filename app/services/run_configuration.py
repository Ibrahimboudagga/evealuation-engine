"""Build safe, immutable execution-configuration snapshots for run records."""

from __future__ import annotations

from copy import deepcopy
import json
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable, Optional

from app.providers.base import BaseProvider


CONFIGURATION_SCHEMA_VERSION = 1
_SECRET_KEY_PARTS = ("api_key", "apikey", "authorization", "password", "secret", "token")


def _redact_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if any(part in key.lower() for part in _SECRET_KEY_PARTS)
            else _redact_secrets(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_secrets(item) for item in value]
    return value


def provider_snapshot(provider: BaseProvider) -> dict[str, Any]:
    """Describe a provider without retaining its credentials."""
    snapshot: dict[str, Any] = {
        "implementation": f"{type(provider).__module__}.{type(provider).__name__}",
        "model": getattr(provider, "model_name", "unknown-model"),
        "simulated": bool(getattr(provider, "is_mock", False)),
    }
    base_url = getattr(provider, "base_url", None)
    if base_url:
        snapshot["base_url"] = base_url
    if getattr(provider, "allow_unauthenticated", False):
        snapshot["allow_unauthenticated"] = True
    return snapshot


def evaluator_snapshots(evaluators: Iterable[Any]) -> list[dict[str, Any]]:
    """Capture evaluator identities and their effective prompt settings."""
    snapshots = []
    for evaluator in evaluators:
        snapshot: dict[str, Any] = {
            "name": evaluator.name,
            "implementation": f"{type(evaluator).__module__}.{type(evaluator).__name__}",
            "version": "1",
        }
        if hasattr(evaluator, "model_name"):
            snapshot["model_name"] = evaluator.model_name
        if hasattr(evaluator, "prompt_template"):
            snapshot["prompt_template"] = evaluator.prompt_template
        if hasattr(evaluator, "provider"):
            snapshot["provider"] = provider_snapshot(evaluator.provider)
        snapshots.append(snapshot)
    return snapshots


def dataset_path_snapshot(dataset_path: str) -> dict[str, Any]:
    """Record a legacy file dataset by resolved path and stable content hash."""
    path = Path(dataset_path).resolve()
    content_hash: Optional[str] = None
    if path.exists():
        content_hash = sha256(path.read_bytes()).hexdigest()
    snapshot = {
        "source": "path",
        "path": str(path),
        "content_sha256": content_hash,
        "version_id": None,
        "version_number": None,
    }
    if path.exists():
        snapshot.update(case_manifest(path.read_text(encoding="utf-8")))
    return snapshot


def case_manifest(content: str) -> dict[str, Any]:
    from app.services.dataset_service import DatasetService
    examples = DatasetService._parse_jsonl(content)
    if not examples:
        raise ValueError("Dataset is empty.")
    ids = sorted(e.id for e in examples)
    return {"expected_case_ids": ids, "example_count": len(ids),
            "expected_case_ids_sha256": sha256(json.dumps(ids).encode()).hexdigest()}


def dataset_version_snapshot(dataset: Any, version: Any) -> dict[str, Any]:
    return {
        "source": "dataset_registry",
        "dataset_id": dataset.id,
        "dataset_name": dataset.name,
        "project_id": dataset.project_id,
        "version_id": version.id,
        "version_number": version.version_number,
        "example_count": version.example_count,
        "content_sha256": sha256(version.content.encode("utf-8")).hexdigest(),
        **case_manifest(version.content),
    }


def build_single_run_configuration(
    *,
    dataset: dict[str, Any],
    provider: BaseProvider,
    evaluators: Iterable[Any],
    concurrency_limit: int,
    execution_timeout_seconds: float,
    result_batch_size: int,
    is_simulated: bool,
    requested_configuration: Optional[dict[str, Any]],
) -> dict[str, Any]:
    config = {
        "schema_version": CONFIGURATION_SCHEMA_VERSION,
        "run_type": "single_model",
        "dataset": dataset,
        "candidate": provider_snapshot(provider),
        "evaluators": evaluator_snapshots(evaluators),
        "execution": {
            "concurrency_limit": concurrency_limit,
            "timeout_seconds": execution_timeout_seconds,
            "result_batch_size": result_batch_size,
        },
        "is_simulated": is_simulated,
        "request": _redact_secrets(deepcopy(requested_configuration or {})),
    }
    config["compatibility_fingerprint"] = compatibility_fingerprint(config)
    return config


def compatibility_fingerprint(config: dict[str, Any] | None) -> str | None:
    if not config:
        return None
    dataset = config.get("dataset") or {}
    if not dataset.get("content_sha256") or not dataset.get("expected_case_ids") or not config.get("evaluators"):
        return None
    contract = {"dataset_sha256": dataset["content_sha256"],
                "case_ids": sorted(dataset["expected_case_ids"]),
                "evaluators": sorted(config["evaluators"], key=lambda e: e["name"]),
                "is_simulated": config.get("is_simulated"), "run_type": config.get("run_type")}
    return sha256(json.dumps(contract, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def build_pairwise_run_configuration(
    *,
    dataset: dict[str, Any],
    provider_a: BaseProvider,
    provider_b: BaseProvider,
    evaluator: Any,
    concurrency_limit: int,
    execution_timeout_seconds: float,
    result_batch_size: int,
    is_simulated: bool,
    requested_configuration: Optional[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": CONFIGURATION_SCHEMA_VERSION,
        "run_type": "pairwise",
        "dataset": dataset,
        "model_a": provider_snapshot(provider_a),
        "model_b": provider_snapshot(provider_b),
        "judge": {
            "name": evaluator.name,
            "implementation": f"{type(evaluator).__module__}.{type(evaluator).__name__}",
            "prompt_template": getattr(evaluator, "prompt_template", None),
            "provider": provider_snapshot(evaluator.provider),
        },
        "execution": {
            "concurrency_limit": concurrency_limit,
            "timeout_seconds": execution_timeout_seconds,
            "result_batch_size": result_batch_size,
        },
        "is_simulated": is_simulated,
        "request": _redact_secrets(deepcopy(requested_configuration or {})),
    }

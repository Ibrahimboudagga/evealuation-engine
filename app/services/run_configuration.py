"""Build safe, immutable execution-configuration snapshots for run records."""

from __future__ import annotations

from copy import deepcopy
import json
import inspect
from importlib.metadata import PackageNotFoundError, version
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable, Optional

from app.providers.base import BaseProvider


CONFIGURATION_SCHEMA_VERSION = 2
MEASUREMENT_CONTRACT_VERSION = 2
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


def implementation_digest(implementation: type) -> str | None:
    """Hash executable source (including base classes), never a literal version label."""
    modules = {}
    for cls in implementation.__mro__:
        if cls is object:
            continue
        module = inspect.getmodule(cls)
        path = getattr(module, "__file__", None)
        if not path or not Path(path).is_file():
            return None
        try:
            modules[module.__name__] = Path(path).read_text(encoding="utf-8").replace("\r\n", "\n")
        except (OSError, UnicodeError):
            return None
    return sha256(json.dumps(modules, sort_keys=True).encode()).hexdigest()


def dependency_versions() -> dict[str, str | None]:
    versions = {}
    for package in ("pydantic", "json-repair", "sentence-transformers", "torch", "openai", "anthropic", "cohere", "google-generativeai", "numpy"):
        try:
            versions[package] = version(package)
        except PackageNotFoundError:
            versions[package] = None
    return versions


def provider_snapshot(provider: BaseProvider) -> dict[str, Any]:
    """Describe a provider without retaining its credentials."""
    snapshot: dict[str, Any] = {
        "implementation": f"{type(provider).__module__}.{type(provider).__name__}",
        "model": getattr(provider, "model_name", "unknown-model"),
        "simulated": bool(getattr(provider, "is_mock", False)),
        "implementation_sha256": implementation_digest(type(provider)),
        # The implementation digest also pins SDK call defaults. Only an explicit
        # allowlist is retained; provider clients and credentials never enter snapshots.
        "generation_settings": _redact_secrets(deepcopy(getattr(provider, "generation_settings", {}))),
    }
    if type(provider).__module__ in ("app.providers.openai", "app.providers.anthropic", "app.providers.gemini"):
        snapshot["generation_settings"] = {"temperature": 0.0}
        if type(provider).__module__ == "app.providers.anthropic":
            snapshot["generation_settings"]["max_tokens"] = 1024
    elif type(provider).__module__ == "app.providers.cohere":
        snapshot["generation_settings"] = {"temperature": "provider_default"}
    base_url = getattr(provider, "base_url", None)
    if base_url:
        snapshot["base_url"] = base_url
    if getattr(provider, "allow_unauthenticated", False):
        snapshot["allow_unauthenticated"] = True
    return snapshot


def evaluator_snapshots(evaluators: Iterable[Any], settings: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Capture evaluator identities and their effective prompt settings."""
    from app.schemas.release import EvaluatorSettings
    snapshots = []
    settings = settings or {}
    evaluators = list(evaluators)
    unknown = set(settings) - {e.name for e in evaluators}
    if unknown:
        raise ValueError("Settings refer to unknown evaluators: " + ", ".join(sorted(unknown)))
    for evaluator in evaluators:
        snapshot: dict[str, Any] = {
            "name": evaluator.name,
            "implementation": f"{type(evaluator).__module__}.{type(evaluator).__name__}",
            "contract_version": MEASUREMENT_CONTRACT_VERSION,
            "implementation_sha256": implementation_digest(type(evaluator)),
            "dependencies": dependency_versions(),
            "settings": EvaluatorSettings.model_validate(settings.get(evaluator.name, {})).model_dump(),
            "calibration": {"status": "unverified", "reviewed_cases": 0},
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
            "expected_case_ids_sha256": sha256(json.dumps(ids).encode()).hexdigest(),
            "case_metadata": {e.id: _redact_secrets(e.metadata or {}) for e in examples}}


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
        "evaluators": evaluator_snapshots(evaluators, (requested_configuration or {}).get("evaluator_settings")),
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
    if not config or config.get("schema_version") != CONFIGURATION_SCHEMA_VERSION:
        return None
    dataset = config.get("dataset") or {}
    if not dataset.get("content_sha256") or not dataset.get("expected_case_ids") or not config.get("evaluators"):
        return None
    evaluators = config["evaluators"]
    if any(e.get("contract_version") != MEASUREMENT_CONTRACT_VERSION
           or not e.get("implementation_sha256") or not e.get("settings")
           or (e.get("provider") is not None and not e["provider"].get("implementation_sha256"))
           for e in evaluators):
        return None
    contract = {"contract_version": MEASUREMENT_CONTRACT_VERSION, "dataset_sha256": dataset["content_sha256"],
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
        "judge": evaluator_snapshots([evaluator])[0],
        "execution": {
            "concurrency_limit": concurrency_limit,
            "timeout_seconds": execution_timeout_seconds,
            "result_batch_size": result_batch_size,
        },
        "is_simulated": is_simulated,
        "request": _redact_secrets(deepcopy(requested_configuration or {})),
    }


def assert_execution_contract(saved: dict[str, Any] | None, effective: dict[str, Any]) -> None:
    """Fail closed when queued work would run a different measurement or target."""
    if not saved or saved.get("schema_version") != CONFIGURATION_SCHEMA_VERSION:
        raise ValueError("Run execution contract is legacy or unverified; submit a fresh run.")
    sections = ("candidate", "evaluators") if effective["run_type"] == "single_model" else ("model_a", "model_b", "judge")
    for key in ("run_type", "is_simulated", "execution", *sections):
        if saved.get(key) != effective.get(key):
            raise ValueError(f"Run execution contract changed ({key}); submit a fresh run with the current configuration.")

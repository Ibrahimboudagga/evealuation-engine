"""Persist and compare immutable imported agent evidence in project scope."""
from __future__ import annotations

import hashlib
import html
import json
import secrets
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import func

from app.database.connection import get_db
from app.database.models import AuditEventDB, ScenarioSuiteDB, ScenarioRunDB, ScenarioShareDB
from app.errors import redact_secrets_text
from app.scenarios import evaluation as scoring
from app.scenarios.schemas import Scenario, Evidence, ScenarioResult
from app.scenarios.runner import summarize
from app.scenarios.report import render_report
from app.services.usage_service import UsageService


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def evaluator_identity():
    directory = Path(scoring.__file__).parent
    return digest("".join((directory / name).read_text(encoding="utf-8")
                         for name in ("evaluation.py", "schemas.py", "runner.py", "assertions.py", "trace.py", "revenue_ops.py")))


def redact(value):
    """Remove recognizable credentials while preserving observable text formatting."""
    if isinstance(value, dict):
        return {key: ("[REDACTED]" if re.sub(r"[^a-z0-9]", "", key.lower()) in
            {"authorization", "apikey", "token", "accesstoken", "refreshtoken", "idtoken",
             "password", "secret", "clientsecret", "cookie", "setcookie"}
            else redact(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return redact_secrets_text(value)
    return value


def audit(db, context, action, entity_type, entity_id, project_id):
    db.add(AuditEventDB(id=str(uuid.uuid4()), workspace_id=context.workspace_id,
        actor_user_id=context.user_id, actor_email=context.email, action=action,
        entity_type=entity_type, entity_id=entity_id, project_id=project_id, created_at=now()))


def validate_suite_content(content):
    """Return parsed scenarios and stable row/field errors without persisting input."""
    scenarios, scenario_rows, errors = [], [], []
    for row, line in enumerate(content.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            scenarios.append(Scenario.model_validate_json(line))
            scenario_rows.append(row)
        except ValidationError as error:
            for item in error.errors(include_url=False, include_context=False, include_input=False):
                errors.append({"row": row, "field": ".".join(map(str, item.get("loc", ()))) or "$",
                               "message": item["msg"]})
        except ValueError:
            errors.append({"row": row, "field": "$", "message": "Invalid JSON"})
    ids = {}
    for row, scenario in zip(scenario_rows, scenarios):
        if scenario.id in ids:
            errors.append({"row": row, "field": "id",
                           "message": f"Duplicate scenario ID also appears in parsed row {ids[scenario.id]}"})
        ids.setdefault(scenario.id, row)
    if not scenarios and not errors:
        errors.append({"row": 1, "field": "$", "message": "Upload at least one scenario"})
    if len(scenarios) > 1000:
        errors.append({"row": 1001, "field": "$", "message": "A suite may contain at most 1000 scenarios"})
    return scenarios, errors


def suite_payload(suite, include_content=False):
    data = {key: getattr(suite, key) for key in
            ("id", "project_id", "name", "version", "sha256", "created_at")}
    data["case_count"] = len(json.loads(suite.content_json))
    if include_content:
        data["scenarios"] = json.loads(suite.content_json)
    return data


def run_payload(run, include_results=True):
    configuration = json.loads(run.configuration_json)
    metrics = json.loads(run.metrics_json)
    imported_unverified = (
        configuration.get("evidence_source") == "imported"
        and configuration.get("provenance_verified") is False
    )
    if imported_unverified:
        metrics["quality_decision"] = metrics.get("quality_decision", metrics.get("decision", "inconclusive"))
        metrics["decision"] = "inconclusive"
    data = {key: getattr(run, key) for key in
            ("id", "project_id", "suite_id", "target_build", "is_simulated", "created_at", "finished_at")}
    data.update(status="completed", evidence_source="imported",
                provenance_verified=False, metrics=metrics,
                configuration=configuration)
    if include_results:
        results = json.loads(run.results_json)
        if imported_unverified:
            for result in results:
                result["quality_decision"] = result.get("quality_decision", result.get("decision", "inconclusive"))
                result["decision"] = "inconclusive"
        data["results"] = results
    return data


class ScenarioService:
    def preview_suite(self, content):
        if len(content.encode("utf-8")) > 5_000_000:
            return {"valid": False, "case_count": 0, "errors": [
                {"row": 1, "field": "$", "message": "Scenario suite upload must be at most 5 MB"}]}
        scenarios, errors = validate_suite_content(content)
        payload = [scenario.model_dump(mode="json") for scenario in scenarios]
        if not errors and redact(payload) != payload:
            errors.append({"row": 1, "field": "$",
                           "message": "Remove credential-bearing fields or text before uploading"})
        return {"valid": not errors, "case_count": len(scenarios), "errors": errors,
                "preview": payload[:10], "truncated": len(payload) > 10}

    def create_suite(self, context, project_id, name, content):
        if len(content.encode('utf-8')) > 5_000_000:
            raise ValueError('Scenario suite upload must be at most 5 MB.')
        preview = self.preview_suite(content)
        if not preview["valid"]:
            first = preview["errors"][0]
            raise ValueError(f"Row {first['row']}, field {first['field']}: {first['message']}")
        scenarios, _ = validate_suite_content(content)
        scenario_payload = [s.model_dump(mode="json") for s in scenarios]
        canonical = encoded(scenario_payload)
        UsageService().assert_capacity(context.workspace_id, storage_bytes=len(canonical.encode("utf-8")))
        with get_db() as db:
            version = (db.query(func.max(ScenarioSuiteDB.version)).filter_by(
                project_id=project_id, name=name).scalar() or 0) + 1
            suite = ScenarioSuiteDB(id=str(uuid.uuid4()), workspace_id=context.workspace_id,
                project_id=project_id, name=name, version=version, content_json=canonical,
                sha256=digest(canonical), created_at=now())
            db.add(suite)
            audit(db, context, "scenario_suite.created", "scenario_suite", suite.id, project_id)
            db.commit()
            db.refresh(suite)
            return suite_payload(suite, True)

    def import_evidence(self, context, suite, target_build, evidence, policy, release_policy=None):
        scenarios = [Scenario.model_validate(s) for s in json.loads(suite.content_json)]
        if set(evidence) - {s.id for s in scenarios}:
            raise ValueError("Evidence contains IDs outside the pinned scenario suite.")
        if len(encoded(evidence).encode("utf-8")) > 5_000_000:
            raise ValueError("Evidence upload must be at most 5 MB.")
        started = now()
        results = []
        for scenario in scenarios:
            if scenario.id not in evidence:
                result = ScenarioResult(scenario_id=scenario.id, outcome="evaluation_error",
                    decision="inconclusive", simulated=False, error_message="No evidence was submitted for this case")
            else:
                try:
                    observed = Evidence.model_validate(evidence[scenario.id])
                    if observed.case_id is not None and observed.case_id != scenario.id:
                        raise ValueError("Evidence case_id does not match its suite key")
                    observed.case_id = scenario.id
                    observed.source = "imported"
                    result = scoring.evaluate(scenario, observed)
                except (ValidationError, ValueError):
                    result = ScenarioResult(scenario_id=scenario.id, outcome="evaluation_error",
                        decision="inconclusive", simulated=evidence[scenario.id].get("simulated") is True,
                        error_message="Submitted evidence does not match the versioned trace contract")
            results.append(result)
        metrics = summarize(results, len(scenarios))
        # Completeness is mandatory, even when a lower policy coverage threshold is configured.
        if metrics["valid_evaluations"] < policy["minimum_valid_cases"]:
            metrics["decision"] = "inconclusive"
        metrics["quality_decision"] = metrics["decision"]
        metrics["decision"] = "inconclusive"
        configuration = {"schema_version": 1, "suite_sha256": suite.sha256,
            "suite_version": suite.version, "evaluator_sha256": evaluator_identity(),
            "target_build": target_build, "evidence_source": "imported",
            "provenance_verified": False, "release_rules": policy,
            "release_policy": release_policy or {"governance_verified": False}}
        original_results = [r.model_dump(mode="json") for r in results]
        for result in original_results:
            result["quality_decision"] = result["decision"]
            result["decision"] = "inconclusive"
        retained_results = redact(original_results)
        was_redacted = retained_results != original_results
        configuration["redaction"] = {"applied": was_redacted, "scores_use_original_observations": True,
                                      "retained_evidence_replayable": not was_redacted}
        result_json = encoded(retained_results)
        run = ScenarioRunDB(id=str(uuid.uuid4()), workspace_id=context.workspace_id,
            project_id=suite.project_id, suite_id=suite.id, target_build=target_build,
            release_policy_revision_id=(release_policy or {}).get("id"),
            is_simulated=any(r.simulated for r in results), configuration_json=encoded(configuration),
            results_json=result_json, metrics_json=encoded(metrics), created_at=started, finished_at=now())
        UsageService().assert_capacity(context.workspace_id, runs=1,
            evaluated_cases=metrics["valid_evaluations"],
            storage_bytes=len(result_json.encode("utf-8")) + len(run.configuration_json.encode("utf-8")) +
                          len(run.metrics_json.encode("utf-8")))
        with get_db() as db:
            db.add(run)
            audit(db, context, "scenario_run.completed", "scenario_run", run.id, run.project_id)
            db.commit()
            db.refresh(run)
            return run_payload(run)

    def compare(self, run, baseline):
        current, previous = run_payload(run), run_payload(baseline)
        config, prior = current["configuration"], previous["configuration"]
        policy = config["release_rules"]
        reasons = []
        if run.project_id != baseline.project_id:
            reasons.append("Runs belong to different projects.")
        for field in ("suite_sha256", "evaluator_sha256", "evidence_source"):
            if not config.get(field) or config.get(field) != prior.get(field):
                reasons.append(f"Incompatible {field}.")
        if run.is_simulated != baseline.is_simulated:
            reasons.append("Simulation modes differ.")
        current_modes = {r["scenario_id"]: r["simulated"] for r in current["results"]}
        prior_modes = {r["scenario_id"]: r["simulated"] for r in previous["results"]}
        if current_modes != prior_modes:
            reasons.append("Per-case simulation modes differ.")
        for label, report in (("Run", current), ("Baseline", previous)):
            metrics = report["metrics"]
            quality = metrics.get("quality_decision", metrics["decision"])
            if metrics["valid_evaluations"] < policy["minimum_valid_cases"]:
                reasons.append(f"{label} has too few valid cases.")
            if metrics["coverage"] < policy["coverage_minimum"] or quality == "inconclusive":
                reasons.append(f"{label} has incomplete evidence.")
            if metrics["pass_rate"] is None:
                reasons.append(f"{label} has no quality score.")
        quality_decision = "inconclusive"
        if not reasons:
            drop = previous["metrics"]["pass_rate"] - current["metrics"]["pass_rate"]
            current_quality = current["metrics"].get("quality_decision", current["metrics"]["decision"])
            quality_decision = "regressed" if (current_quality == "regressed" or
                current["metrics"]["pass_rate"] < policy["pass_rate_minimum"] or
                drop > policy["pass_rate_max_drop"] + 1e-12) else "passed"
            dimension_reasons = []
            for name, rule in policy.get("dimensions", {}).items():
                current_dimension = current["metrics"]["dimensions"].get(name, {})
                prior_dimension = previous["metrics"]["dimensions"].get(name, {})
                if current_dimension.get("average_score") is None or current_dimension.get("pass_rate") is None:
                    quality_decision = "inconclusive"
                    dimension_reasons.append(f"Dimension {name} has insufficient evidence.")
                    continue
                needs_baseline = rule.get("average_score_max_drop") is not None or rule.get("pass_rate_max_drop") is not None
                if needs_baseline and (prior_dimension.get("average_score") is None or prior_dimension.get("pass_rate") is None):
                    quality_decision = "inconclusive"
                    dimension_reasons.append(f"Baseline dimension {name} has insufficient evidence.")
                    continue
                breached = ((rule.get("average_score_minimum") is not None and
                             current_dimension["average_score"] < rule["average_score_minimum"]) or
                            (rule.get("pass_rate_minimum") is not None and
                             current_dimension["pass_rate"] < rule["pass_rate_minimum"]) or
                            (rule.get("average_score_max_drop") is not None and
                             prior_dimension.get("average_score") is not None and
                             prior_dimension["average_score"] - current_dimension["average_score"] > rule["average_score_max_drop"] + 1e-12) or
                            (rule.get("pass_rate_max_drop") is not None and
                             prior_dimension.get("pass_rate") is not None and
                             prior_dimension["pass_rate"] - current_dimension["pass_rate"] > rule["pass_rate_max_drop"] + 1e-12))
                if breached:
                    quality_decision = "regressed"
                    dimension_reasons.append(f"Dimension {name} breached its release threshold.")
            reasons = dimension_reasons or (["Explicit scenario checks failed or a release threshold was breached."]
                if quality_decision == "regressed" else ["All explicit checks and configured release thresholds passed."])
        release_policy = config.get("release_policy") or {"governance_verified": False}
        if not release_policy.get("governance_verified"):
            reasons.append("No owner-approved project scenario policy is bound to this run.")
        reasons.append("Imported evidence provenance is not independently verified.")
        return {"run_id": run.id, "baseline_run_id": baseline.id, "decision": "inconclusive",
            "quality_decision": quality_decision,
            "reasons": reasons, "rules": policy, "current": current["metrics"],
            "baseline": previous["metrics"], "provenance_verified": False,
            "release_policy": release_policy}

    def html(self, run, project, comparison=None):
        payload = run_payload(run)
        manifest = {"run_id": run.id, "status": "completed", "target_label": run.target_build,
                    "simulated": run.is_simulated, "metrics": payload["metrics"],
                    "provenance_verified": False}
        results = [ScenarioResult.model_validate(item) for item in payload["results"]]
        document = render_report(manifest, results)
        note = ("<p><strong>Imported evidence — target execution and provenance are not independently verified.</strong></p>"
            f"<p>Client: {html.escape(project.client_name)} · Project: {html.escape(project.name)}</p>"
            "<details open><summary>Versioned execution and scoring configuration</summary><pre>" +
            html.escape(json.dumps(payload["configuration"], indent=2)) + "</pre></details>")
        if comparison is not None:
            note += ("<h2>Baseline release decision</h2><pre>" +
                     html.escape(json.dumps(comparison, indent=2)) + "</pre>")
        return document.replace("</html>", note + "</html>")

    def share(self, context, run, project, hours, baseline=None):
        token = secrets.token_urlsafe(32)
        document = self.html(run, project, self.compare(run, baseline) if baseline is not None else None)
        UsageService().assert_capacity(context.workspace_id, report_shares=1,
                                       storage_bytes=len(document.encode("utf-8")))
        shared = ScenarioShareDB(id=str(uuid.uuid4()), workspace_id=context.workspace_id,
            run_id=run.id, token_hash=digest(token), html_snapshot=document, created_at=now(),
            expires_at=now() + timedelta(hours=hours))
        with get_db() as db:
            db.add(shared)
            audit(db, context, "report_share.created", "scenario_share", shared.id, run.project_id)
            db.commit()
            return {"id": shared.id, "expires_at": shared.expires_at,
                    "url": "/public/scenario-reports/" + token}

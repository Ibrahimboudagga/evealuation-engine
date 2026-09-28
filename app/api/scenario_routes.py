"""Project-authorized scenario upload, evidence review and fixed report links."""
from typing import Literal
from fastapi import Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.exc import IntegrityError

from app.database.connection import get_db
from app.database.models import ScenarioSuiteDB, ScenarioRunDB, ScenarioShareDB
from app.services.scenario_service import ScenarioService, suite_payload, run_payload, digest, now, audit
from app.services.identity_service import IdentityService, WRITE_ROLES
from app.services.usage_service import WorkspaceLimitExceeded


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, str_strip_whitespace=True)


class SuiteUpload(StrictRequest):
    project_id: str
    name: str = Field(min_length=1, max_length=255)
    content: str = Field(min_length=1, max_length=5_000_000)


class ScenarioPolicy(StrictRequest):
    coverage_minimum: float = Field(default=1, ge=0, le=1)
    minimum_valid_cases: int = Field(default=1, ge=1, le=1000)
    pass_rate_minimum: float = Field(default=1, ge=0, le=1)
    pass_rate_max_drop: float = Field(default=0, ge=0, le=1)


class EvidenceUpload(StrictRequest):
    suite_id: str
    target_build: str = Field(min_length=1, max_length=255)
    evidence: dict[str, dict]
    release_rules: ScenarioPolicy = Field(default_factory=ScenarioPolicy)


class ShareCreate(StrictRequest):
    expires_in_hours: int = Field(default=24, ge=1, le=720)
    baseline_run_id: str | None = None


def register_scenario_routes(app, auth_dependency, project_access):
    service = ScenarioService()

    def require_context(context, write=False):
        if context is None:
            raise HTTPException(401, "Sign in to a workspace first.")
        if write and context.role not in WRITE_ROLES:
            raise HTTPException(403, "Owner or editor access is required.")
        return context

    def accessible(model, record_id, context, write=False):
        require_context(context, write)
        with get_db() as db:
            record = db.query(model).filter_by(id=record_id, workspace_id=context.workspace_id).first()
            if not record:
                raise HTTPException(404, "Scenario record not found.")
            project_access(record.project_id, context, write=write)
            return record

    def list_records(model, context, project_id):
        require_context(context)
        allowed = IdentityService().accessible_project_ids(context)
        if project_id:
            project_access(project_id, context)
            allowed = [project_id]
        with get_db() as db:
            return db.query(model).filter(model.workspace_id == context.workspace_id,
                model.project_id.in_(allowed)).order_by(model.created_at.desc()).limit(200).all()

    @app.post("/scenario-suites", status_code=201)
    def create_suite(req: SuiteUpload, context=Depends(auth_dependency)):
        require_context(context, True)
        project_access(req.project_id, context, write=True)
        try:
            return service.create_suite(context, req.project_id, req.name, req.content)
        except WorkspaceLimitExceeded as error:
            raise HTTPException(429, str(error))
        except (ValidationError, ValueError):
            raise HTTPException(422, "Invalid suite. Use 1–1000 unique scenario IDs and the documented scenario schema.")
        except IntegrityError:
            raise HTTPException(409, "A suite version was created concurrently. Retry the upload.")

    @app.get("/scenario-suites")
    def list_suites(project_id: str | None = None, context=Depends(auth_dependency)):
        return [suite_payload(s) for s in list_records(ScenarioSuiteDB, context, project_id)]

    @app.get("/scenario-suites/{suite_id}")
    def get_suite(suite_id: str, context=Depends(auth_dependency)):
        return suite_payload(accessible(ScenarioSuiteDB, suite_id, context), True)

    @app.post("/scenario-runs", status_code=201)
    def import_evidence(req: EvidenceUpload, context=Depends(auth_dependency)):
        suite = accessible(ScenarioSuiteDB, req.suite_id, context, True)
        try:
            return service.import_evidence(context, suite, req.target_build,
                                           req.evidence, req.release_rules.model_dump())
        except WorkspaceLimitExceeded as error:
            raise HTTPException(429, str(error))
        except ValueError as error:
            raise HTTPException(422, str(error))

    @app.get("/scenario-runs")
    def list_runs(project_id: str | None = None, context=Depends(auth_dependency)):
        return [run_payload(r, False) for r in list_records(ScenarioRunDB, context, project_id)]

    @app.get("/scenario-runs/{run_id}")
    def get_run(run_id: str, context=Depends(auth_dependency)):
        return run_payload(accessible(ScenarioRunDB, run_id, context))

    @app.get("/scenario-runs/{run_id}/compare")
    def compare(run_id: str, baseline_run_id: str = Query(...), context=Depends(auth_dependency)):
        if run_id == baseline_run_id:
            raise HTTPException(422, "Select two different runs.")
        run = accessible(ScenarioRunDB, run_id, context)
        baseline = accessible(ScenarioRunDB, baseline_run_id, context)
        return service.compare(run, baseline)

    @app.get("/scenario-runs/{run_id}/export")
    def export(run_id: str, format: Literal["html", "json"] = "html", baseline_run_id: str | None = None, context=Depends(auth_dependency)):
        run = accessible(ScenarioRunDB, run_id, context)
        project = project_access(run.project_id, context)
        comparison = service.compare(run, accessible(ScenarioRunDB, baseline_run_id, context)) if baseline_run_id else None
        with get_db() as db:
            audit(db, context, "report.exported", "scenario_run", run_id, run.project_id)
            db.commit()
        if format == "json":
            return {**run_payload(run), "baseline_comparison": comparison}
        return Response(service.html(run, project, comparison), media_type="text/html",
                        headers={"Cache-Control": "no-store"})

    @app.post("/scenario-runs/{run_id}/shares", status_code=201)
    def share(run_id: str, req: ShareCreate, context=Depends(auth_dependency)):
        run = accessible(ScenarioRunDB, run_id, context, True)
        project = project_access(run.project_id, context)
        baseline = accessible(ScenarioRunDB, req.baseline_run_id, context) if req.baseline_run_id else None
        try:
            return service.share(context, run, project, req.expires_in_hours, baseline)
        except WorkspaceLimitExceeded as error:
            raise HTTPException(429, str(error))

    @app.delete("/scenario-shares/{share_id}", status_code=204)
    def revoke(share_id: str, context=Depends(auth_dependency)):
        require_context(context, True)
        with get_db() as db:
            share = db.query(ScenarioShareDB).filter_by(id=share_id, workspace_id=context.workspace_id).first()
            if not share:
                raise HTTPException(404, "Share not found.")
            run = accessible(ScenarioRunDB, share.run_id, context, True)
            share.revoked_at = now()
            audit(db, context, "report_share.revoked", "scenario_share", share_id, run.project_id)
            db.commit()
        return Response(status_code=204)

    @app.get("/public/scenario-reports/{token}", include_in_schema=False)
    def public_report(token: str):
        with get_db() as db:
            share = db.query(ScenarioShareDB).filter(
                ScenarioShareDB.token_hash == digest(token),
                ScenarioShareDB.revoked_at.is_(None), ScenarioShareDB.expires_at > now()).first()
            if not share:
                raise HTTPException(404, "Report unavailable.")
            return Response(share.html_snapshot, media_type="text/html", headers={
                "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'"})

    @app.delete("/scenario-runs/{run_id}", status_code=204)
    def delete_run(run_id: str, context=Depends(auth_dependency)):
        run = accessible(ScenarioRunDB, run_id, context, True)
        if context.role != "owner":
            raise HTTPException(403, "Only owners can delete retained evidence.")
        with get_db() as db:
            db.query(ScenarioShareDB).filter_by(run_id=run_id).delete()
            db.query(ScenarioRunDB).filter_by(id=run_id).delete()
            audit(db, context, "scenario_run.deleted", "scenario_run", run_id, run.project_id)
            db.commit()
        return Response(status_code=204)


    @app.delete("/scenario-suites/{suite_id}", status_code=204)
    def delete_suite(suite_id: str, context=Depends(auth_dependency)):
        suite = accessible(ScenarioSuiteDB, suite_id, context, True)
        if context.role != "owner":
            raise HTTPException(403, "Only owners can delete retained suites.")
        with get_db() as db:
            if db.query(ScenarioRunDB.id).filter_by(suite_id=suite_id).first():
                raise HTTPException(409, "Delete retained scenario runs first.")
            db.query(ScenarioSuiteDB).filter_by(id=suite_id).delete()
            audit(db, context, "scenario_suite.deleted", "scenario_suite", suite_id, suite.project_id)
            db.commit()
        return Response(status_code=204)

"""Gradio workflow for uploaded scenario suites and imported application traces."""
import json
import os
from pathlib import Path
import tempfile

import gradio as gr
import httpx

from app.ui.session import api_headers, bind_session


def build_scenario_tab(session, api_base):
    async def request(method, path, **kwargs):
        async with httpx.AsyncClient(timeout=60, headers=api_headers()) as client:
            response = await client.request(method, api_base + path, **kwargs)
            response.raise_for_status()
            return response

    def read_upload(path):
        if not path:
            raise ValueError("Choose an uploaded file.")
        file = Path(path)
        if file.stat().st_size > 5_000_000:
            raise ValueError("Upload at most 5 MB.")
        return file.read_text(encoding="utf-8-sig")

    @bind_session
    async def refresh():
        try:
            projects = (await request("GET", "/projects")).json()["projects"]
            suites = (await request("GET", "/scenario-suites")).json()
            runs = (await request("GET", "/scenario-runs")).json()
            run_choices = [(f"{r['target_build']} · {r['metrics']['decision']} · {r['id'][:8]}", r["id"]) for r in runs]
            return (gr.update(choices=[(f"{p['client_name']} / {p['name']}", p["id"]) for p in projects], value=None),
                    gr.update(choices=[(f"{s['name']} v{s['version']}", s["id"]) for s in suites], value=None),
                    gr.update(choices=run_choices, value=None), gr.update(choices=run_choices, value=None),
                    {"message": "Workspace scenario lists refreshed."})
        except (ValueError, httpx.HTTPError):
            return (gr.update(choices=[], value=None), gr.update(choices=[], value=None),
                    gr.update(choices=[], value=None), gr.update(choices=[], value=None),
                    {"error": "Sign in and refresh your workspace."})

    @bind_session
    async def upload(project_id, name, file):
        try:
            return (await request("POST", "/scenario-suites", json={
                "project_id": project_id, "name": name, "content": read_upload(file)})).json()
        except httpx.HTTPStatusError as error:
            return {"error": error.response.json().get("detail")}
        except (ValueError, OSError, httpx.HTTPError) as error:
            return {"error": str(error)}

    @bind_session
    async def import_evidence(suite_id, target_build, file):
        try:
            if not suite_id or not target_build:
                raise ValueError("Select a suite and enter the target build.")
            evidence = json.loads(read_upload(file))
            return (await request("POST", "/scenario-runs", json={
                "suite_id": suite_id, "target_build": target_build, "evidence": evidence})).json()
        except httpx.HTTPStatusError as error:
            return {"error": error.response.json().get("detail")}
        except (ValueError, OSError, httpx.HTTPError) as error:
            return {"error": str(error)}

    @bind_session
    async def review(run_id, decision):
        try:
            if not run_id:
                raise ValueError("Select a run.")
            data = (await request("GET", f"/scenario-runs/{run_id}")).json()
            if decision != "all":
                data["results"] = [r for r in data["results"] if r["decision"] == decision]
                data["result_filter"] = decision
            return data
        except httpx.HTTPStatusError as error:
            return {"error": error.response.json().get("detail")}
        except (ValueError, httpx.HTTPError) as error:
            return {"error": str(error)}

    @bind_session
    async def compare(run_id, baseline_id):
        try:
            if not run_id or not baseline_id:
                raise ValueError("Select the current and baseline runs.")
            return (await request("GET", f"/scenario-runs/{run_id}/compare",
                                  params={"baseline_run_id": baseline_id})).json()
        except httpx.HTTPStatusError as error:
            return {"error": error.response.json().get("detail")}
        except (ValueError, httpx.HTTPError) as error:
            return {"error": str(error)}

    @bind_session
    async def export(run_id, format, baseline_id):
        try:
            if not run_id:
                raise ValueError("Select a run.")
            response = await request("GET", f"/scenario-runs/{run_id}/export",
                                     params={"format": format, **({"baseline_run_id": baseline_id} if baseline_id else {})})
            with tempfile.NamedTemporaryFile(mode="w", suffix="." + format, prefix="scenario-report-",
                                             encoding="utf-8", delete=False) as stream:
                stream.write(response.text)
                return stream.name
        except (ValueError, httpx.HTTPError):
            return None

    @bind_session
    async def share(run_id, hours, baseline_id):
        try:
            if not run_id:
                raise ValueError("Select a run.")
            data = (await request("POST", f"/scenario-runs/{run_id}/shares",
                                  json={"expires_in_hours": int(hours), "baseline_run_id": baseline_id or None})).json()
            data["url"] = os.getenv("PUBLIC_API_BASE", api_base).rstrip("/") + data["url"]
            return data
        except httpx.HTTPStatusError as error:
            return {"error": error.response.json().get("detail")}
        except (ValueError, httpx.HTTPError) as error:
            return {"error": str(error)}

    @bind_session
    async def revoke(share_id):
        try:
            if not share_id:
                raise ValueError("Enter the share ID returned when creating a link.")
            await request("DELETE", "/scenario-shares/" + share_id.strip())
            return {"message": "Shared report revoked."}
        except httpx.HTTPStatusError as error:
            return {"error": error.response.json().get("detail")}
        except (ValueError, httpx.HTTPError) as error:
            return {"error": str(error)}

    with gr.Tab("Agent & App Scenarios"):
        gr.Markdown("Upload a scenario suite, then score evidence collected from your application. "
                    "Each run pins the suite and scorer. Simulation labels are retained; imported traces "
                    "are not independently verified as live execution.")
        refresh_button = gr.Button("Refresh workspace scenarios")
        with gr.Row():
            project = gr.Dropdown(label="Client project", choices=[])
            name = gr.Textbox(label="Suite name (uploading again creates a version)")
            suite_file = gr.File(label="Scenario dataset", file_types=[".jsonl"], type="filepath")
        upload_button = gr.Button("Save suite version")
        with gr.Row():
            suite = gr.Dropdown(label="Pinned suite version", choices=[])
            build = gr.Textbox(label="Target build / commit")
            evidence_file = gr.File(label="Evidence JSON keyed by scenario ID", file_types=[".json"], type="filepath")
        import_button = gr.Button("Evaluate imported evidence", variant="primary")
        with gr.Row():
            selected_run = gr.Dropdown(label="Run to review", choices=[])
            baseline = gr.Dropdown(label="Baseline run", choices=[])
            decision = gr.Dropdown(label="Example filter", choices=["all", "passed", "regressed", "inconclusive"], value="all")
        with gr.Row():
            review_button = gr.Button("Review checks and evidence")
            compare_button = gr.Button("Compare with baseline")
            format = gr.Dropdown(label="Export format", choices=["html", "json"], value="html")
            export_button = gr.Button("Export report")
        download = gr.File(label="Download scenario report")
        with gr.Row():
            hours = gr.Number(label="Share expires in hours", value=24, minimum=1, maximum=720, precision=0)
            share_button = gr.Button("Create read-only share")
            share_id = gr.Textbox(label="Share ID to revoke")
            revoke_button = gr.Button("Revoke share")
        output = gr.JSON(label="Configuration, coverage, checks and evidence")
        refresh_button.click(refresh, inputs=[session], outputs=[project, suite, selected_run, baseline, output])
        upload_button.click(upload, inputs=[project, name, suite_file, session], outputs=output)
        import_button.click(import_evidence, inputs=[suite, build, evidence_file, session], outputs=output)
        review_button.click(review, inputs=[selected_run, decision, session], outputs=output)
        compare_button.click(compare, inputs=[selected_run, baseline, session], outputs=output)
        export_button.click(export, inputs=[selected_run, format, baseline, session], outputs=download)
        share_button.click(share, inputs=[selected_run, hours, baseline, session], outputs=output)
        revoke_button.click(revoke, inputs=[share_id, session], outputs=output)

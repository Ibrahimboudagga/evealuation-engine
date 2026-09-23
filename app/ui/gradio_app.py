import gradio as gr
import httpx
import os
import tempfile
from pathlib import Path

API_BASE = os.getenv("API_BASE", "http://localhost:8000")

PROVIDER_CHOICES = ["openai", "anthropic", "cohere", "gemini", "mock"]


def _api_headers():
    """Use the deployment's workspace token without placing it in Gradio state or outputs."""
    token = os.getenv("WORKSPACE_API_TOKEN")
    return {"Authorization": f"Bearer {token}"} if token else {}


async def submit_run(
    dataset_id,
    dataset_version_id,
    candidate_connection_id,
    candidate_model,
    evaluator_connection_id,
    evaluator_model,
    concurrency,
    judge_prompt_template,
):
    if not dataset_id or not dataset_version_id:
        return {"error": "Select a dataset and version before submitting a run."}
    payload = {
        "dataset_id": dataset_id,
        "dataset_version_id": dataset_version_id,
        "candidate_connection_id": candidate_connection_id,
        "candidate_model": candidate_model or None,
        "evaluator_connection_id": evaluator_connection_id,
        "evaluator_model": evaluator_model or None,
        "concurrency": int(concurrency),
        "judge_prompt_template": judge_prompt_template or None,
    }
    try:
        async with httpx.AsyncClient(timeout=30.0, headers=_api_headers()) as client:
            resp = await client.post(f"{API_BASE}/runs", json=payload)
            resp.raise_for_status()
            data = resp.json()
            if data.get("is_simulated"):
                data["notice"] = "SIMULATED RUN: mock or demo provider output was used."
            return data
    except httpx.HTTPStatusError as e:
        return {"error": f"HTTP {e.response.status_code}: {e.response.text}"}
    except Exception as e:
        return {"error": str(e)}


async def setup_workspace(email, display_name, workspace_name):
    if not email or not display_name or not workspace_name:
        return {"error": "Enter an owner email, display name, and agency workspace name."}
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(f"{API_BASE}/auth/bootstrap", json={
                "email": email, "display_name": display_name, "workspace_name": workspace_name,
            })
            response.raise_for_status()
        data = response.json()
        return {
            "message": "Workspace created. Save api_token in WORKSPACE_API_TOKEN, restart Gradio, then use Seed Agency Demo.",
            "workspace_id": data["workspace_id"], "api_token": data["api_token"],
        }
    except httpx.HTTPStatusError as error:
        return {"error": f"HTTP {error.response.status_code}: {error.response.text}"}
    except Exception as error:
        return {"error": str(error)}


async def get_run_status(run_id):
    if not run_id or not run_id.strip():
        return "Please enter a Run ID.", None, None
    try:
        async with httpx.AsyncClient(timeout=10.0, headers=_api_headers()) as client:
            resp = await client.get(f"{API_BASE}/runs/{run_id.strip()}")
            resp.raise_for_status()
            data = resp.json()
            status = data.get("status", "unknown")
            if data.get("is_simulated"):
                status = f"SIMULATED — {status}"
            metrics = data.get("metrics")
            error = data.get("error")
            if error:
                status = f"{status} — {error}"
            configuration = {
                "project_id": data.get("project_id"),
                "configuration_verified": data.get("configuration_verified", False),
                "configuration": data.get("run_configuration"),
            }
            if not metrics:
                return status, None, configuration
            rows = []
            for m in metrics:
                has_valid_evaluations = m["mean_score"] is not None
                rows.append([
                    m["evaluator"],
                    f"{m['mean_score']:.3f}" if has_valid_evaluations else "No valid evaluations.",
                    f"{m['pass_rate']:.1%}" if m["pass_rate"] is not None else "—",
                    f"{m['valid_evaluations']}/{m['total_cases']}",
                    f"{m['evaluation_coverage']:.1%}",
                    m["generation_errors"],
                    m["evaluation_errors"],
                    m["passing_evaluations"],
                ])
            return status, rows, configuration
    except httpx.HTTPStatusError as e:
        return f"HTTP {e.response.status_code}: {e.response.text}", None, None
    except Exception as e:
        return f"Error: {e}", None, None


async def review_run_results(run_id, evaluator, outcomes, score_min, score_max):
    """Load filtered per-example results for the Gradio review page."""
    if not run_id or not run_id.strip():
        return (
            "Enter a Run ID to review its results.",
            [],
            gr.update(choices=[], value=None),
            [],
            None,
            gr.update(choices=["All"], value="All"),
        )

    params = []
    if evaluator and evaluator != "All":
        params.append(("evaluator", evaluator))
    for outcome in outcomes or []:
        params.append(("outcome", outcome))
    if score_min is not None:
        params.append(("score_min", score_min))
    if score_max is not None:
        params.append(("score_max", score_max))

    try:
        async with httpx.AsyncClient(timeout=30.0, headers=_api_headers()) as client:
            response = await client.get(f"{API_BASE}/runs/{run_id.strip()}/results", params=params)
            response.raise_for_status()
        data = response.json()
        results = data["results"]
        rows = [
            [
                item["example_id"],
                item["evaluator_name"],
                item["outcome"],
                f"{item['score']:.3f}" if item["score"] is not None else "—",
                (item["judge_explanation"] or "—")[:100],
                (item["error_message"] or "—")[:100],
            ]
            for item in results
        ]
        result_choices = [
            (
                f"{item['example_id']} / {item['evaluator_name']} / {item['outcome']}",
                str(item["id"]),
            )
            for item in results
        ]
        evaluator_choices = ["All", *data["available_evaluators"]]
        selected_evaluator = evaluator if evaluator in evaluator_choices else "All"
        summary = f"Showing {data['filtered_count']} of {data['total_count']} persisted results."
        return (
            summary,
            rows,
            gr.update(choices=result_choices, value=None),
            results,
            None,
            gr.update(choices=evaluator_choices, value=selected_evaluator),
        )
    except httpx.HTTPStatusError as error:
        return (
            f"HTTP {error.response.status_code}: {error.response.text}",
            [],
            gr.update(choices=[], value=None),
            [],
            None,
            gr.update(choices=["All"], value="All"),
        )
    except Exception as error:
        return (
            f"Error: {error}",
            [],
            gr.update(choices=[], value=None),
            [],
            None,
            gr.update(choices=["All"], value="All"),
        )


def show_review_result(result_id, results):
    if not result_id:
        return None
    return next((item for item in results or [] if str(item["id"]) == str(result_id)), None)


async def export_run_report(run_id, report_format):
    """Download a client-ready report from the API into Gradio's file output."""
    if not run_id or not run_id.strip():
        return "Enter a Run ID before exporting.", None
    try:
        async with httpx.AsyncClient(timeout=60.0, headers=_api_headers()) as client:
            response = await client.get(
                f"{API_BASE}/runs/{run_id.strip()}/export",
                params={"format": report_format},
            )
            response.raise_for_status()
        with tempfile.NamedTemporaryFile(
            mode="wb", suffix=f".{report_format}", prefix="evaluation-report-", delete=False
        ) as report_file:
            report_file.write(response.content)
            report_path = report_file.name
        return f"{report_format.upper()} report is ready to download.", report_path
    except httpx.HTTPStatusError as error:
        return f"HTTP {error.response.status_code}: {error.response.text}", None
    except Exception as error:
        return f"Error: {error}", None


async def seed_agency_demo():
    try:
        async with httpx.AsyncClient(timeout=30.0, headers=_api_headers()) as client:
            response = await client.post(f"{API_BASE}/demo/seed")
            response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as error:
        return {"error": f"HTTP {error.response.status_code}: {error.response.text}"}
    except Exception as error:
        return {"error": str(error)}


async def baseline_choice_update():
    try:
        async with httpx.AsyncClient(timeout=10.0, headers=_api_headers()) as client:
            response = await client.get(f"{API_BASE}/runs", params={"baseline_only": True})
            response.raise_for_status()
        choices = [
            (f"{run['run_id']} ({run['status']})", run["run_id"])
            for run in response.json()["runs"]
        ]
        return gr.update(choices=choices, value=None)
    except Exception:
        return gr.update(choices=[], value=None)


async def mark_run_as_baseline(run_id):
    if not run_id or not run_id.strip():
        return {"error": "Enter the completed Run ID to mark as a baseline."}
    try:
        async with httpx.AsyncClient(timeout=10.0, headers=_api_headers()) as client:
            response = await client.put(f"{API_BASE}/runs/{run_id.strip()}/baseline")
            response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as error:
        return {"error": f"HTTP {error.response.status_code}: {error.response.text}"}
    except Exception as error:
        return {"error": str(error)}


async def compare_run_with_baseline(run_id, baseline_run_id, coverage_minimum, max_pass_rate_drop):
    if not run_id or not run_id.strip() or not baseline_run_id:
        return "Enter a current Run ID and select a baseline.", None
    params = {
        "baseline_run_id": baseline_run_id,
        "coverage_minimum": coverage_minimum,
        "exact_match_pass_rate_max_drop": max_pass_rate_drop,
    }
    try:
        async with httpx.AsyncClient(timeout=30.0, headers=_api_headers()) as client:
            response = await client.get(f"{API_BASE}/runs/{run_id.strip()}/comparison", params=params)
            response.raise_for_status()
        data = response.json()
        reasons = " ".join(data.get("reasons", []))
        return f"### Release check: **{data['status'].upper()}**\n\n{reasons}", data
    except httpx.HTTPStatusError as error:
        return f"### Release check: **INCONCLUSIVE**\n\nHTTP {error.response.status_code}: {error.response.text}", None
    except Exception as error:
        return f"### Release check: **INCONCLUSIVE**\n\n{error}", None


async def submit_pairwise_run(
    dataset_id,
    dataset_version_id,
    model_a_connection_id,
    model_a_model,
    model_b_connection_id,
    model_b_model,
    judge_connection_id,
    judge_model,
    concurrency,
):
    if not dataset_id or not dataset_version_id:
        return {"error": "Select a dataset and version before submitting a pairwise run."}
    payload = {
        "dataset_id": dataset_id,
        "dataset_version_id": dataset_version_id,
        "model_a_connection_id": model_a_connection_id,
        "model_a_model": model_a_model or None,
        "model_b_connection_id": model_b_connection_id,
        "model_b_model": model_b_model or None,
        "judge_connection_id": judge_connection_id,
        "judge_model": judge_model or None,
        "concurrency": int(concurrency),
    }
    try:
        async with httpx.AsyncClient(timeout=30.0, headers=_api_headers()) as client:
            resp = await client.post(f"{API_BASE}/pairwise-runs", json=payload)
            resp.raise_for_status()
            data = resp.json()
            if data.get("is_simulated"):
                data["notice"] = "SIMULATED RUN: mock or demo provider output was used."
            return data
    except httpx.HTTPStatusError as e:
        return {"error": f"HTTP {e.response.status_code}: {e.response.text}"}
    except Exception as e:
        return {"error": str(e)}


async def dataset_choice_update():
    """Return the current dataset choices for one dropdown."""
    try:
        async with httpx.AsyncClient(timeout=10.0, headers=_api_headers()) as client:
            response = await client.get(f"{API_BASE}/datasets")
            response.raise_for_status()
        datasets = response.json()["datasets"]
        choices = [
            (
                f"{dataset.get('client_name') or 'Unassigned'} / "
                f"{dataset.get('project_name') or 'No project'} — "
                f"{dataset['name']} (latest v{dataset['latest_version_number']})",
                dataset["id"],
            )
            for dataset in datasets
        ]
        return gr.update(choices=choices, value=None)
    except Exception:
        return gr.update(choices=[], value=None)


async def provider_connection_choice_update():
    """Load safe connection labels; credentials never enter the browser UI."""
    try:
        async with httpx.AsyncClient(timeout=10.0, headers=_api_headers()) as client:
            response = await client.get(f"{API_BASE}/provider-connections")
            response.raise_for_status()
        choices = [
            (f"{item['name']} — {item['provider']} / {item['default_model']}", item["id"])
            for item in response.json()
        ]
        return gr.update(choices=choices, value=None)
    except Exception:
        return gr.update(choices=[], value=None)


async def all_provider_connection_choice_updates():
    update = await provider_connection_choice_update()
    return update, update, update, update, update


async def run_provider_connection_choice_updates():
    update = await provider_connection_choice_update()
    return update, update


async def pairwise_provider_connection_choice_updates():
    update = await provider_connection_choice_update()
    return update, update, update


async def all_dataset_choice_updates():
    update = await dataset_choice_update()
    return update, update, update


async def project_choice_update():
    """Return agency project choices for dataset creation."""
    try:
        async with httpx.AsyncClient(timeout=10.0, headers=_api_headers()) as client:
            response = await client.get(f"{API_BASE}/projects")
            response.raise_for_status()
        choices = [
            (f"{project['client_name']} / {project['name']}", project["id"])
            for project in response.json()["projects"]
        ]
        return gr.update(choices=choices, value=None)
    except Exception:
        return gr.update(choices=[], value=None)


async def create_project(name, client_name, description, tags):
    if not name or not name.strip() or not client_name or not client_name.strip():
        return {"error": "Enter both a project name and client name."}
    payload = {
        "name": name.strip(),
        "client_name": client_name.strip(),
        "description": description.strip() if description and description.strip() else None,
        "tags": [tag.strip() for tag in tags.split(",") if tag.strip()] if tags else [],
    }
    try:
        async with httpx.AsyncClient(timeout=10.0, headers=_api_headers()) as client:
            response = await client.post(f"{API_BASE}/projects", json=payload)
            response.raise_for_status()
        return {"message": "Project created.", "project": response.json()}
    except httpx.HTTPStatusError as error:
        return {"error": f"HTTP {error.response.status_code}: {error.response.text}"}
    except Exception as error:
        return {"error": str(error)}


async def create_provider_connection(name, provider, model, api_key, base_url, allow_unauthenticated):
    if not name or not model:
        return {"error": "Enter a connection name and default model."}
    payload = {
        "name": name.strip(), "provider": provider, "default_model": model.strip(),
        "api_key": api_key or None, "base_url": base_url or None,
        "allow_unauthenticated": bool(allow_unauthenticated),
    }
    try:
        async with httpx.AsyncClient(timeout=15.0, headers=_api_headers()) as client:
            response = await client.post(f"{API_BASE}/provider-connections", json=payload)
            response.raise_for_status()
        return {"message": "Provider connection saved. Its credential cannot be retrieved.", "connection": response.json()}
    except httpx.HTTPStatusError as error:
        return {"error": f"HTTP {error.response.status_code}: {error.response.text}"}
    except Exception as error:
        return {"error": str(error)}


async def create_evaluation_template(name, candidate_connection, judge_connection, prompt, concurrency, timeout, coverage, max_drop):
    if not name or not candidate_connection or not judge_connection:
        return {"error": "Enter a name and select candidate and judge connections."}
    payload = {
        "name": name.strip(), "candidate_connection_id": candidate_connection,
        "evaluator_connection_id": judge_connection, "judge_prompt_template": prompt or None,
        "concurrency": int(concurrency), "timeout_seconds": float(timeout),
        "coverage_minimum": float(coverage), "exact_match_pass_rate_max_drop": float(max_drop),
        "report_preferences": {},
    }
    try:
        async with httpx.AsyncClient(timeout=15.0, headers=_api_headers()) as client:
            response = await client.post(f"{API_BASE}/evaluation-templates", json=payload)
            response.raise_for_status()
        return {"message": "Template saved.", "template": response.json()}
    except httpx.HTTPStatusError as error:
        return {"error": f"HTTP {error.response.status_code}: {error.response.text}"}
    except Exception as error:
        return {"error": str(error)}


async def template_choice_update():
    try:
        async with httpx.AsyncClient(timeout=10.0, headers=_api_headers()) as client:
            response = await client.get(f"{API_BASE}/evaluation-templates")
            response.raise_for_status()
        return gr.update(choices=[(item["name"], item["id"]) for item in response.json()], value=None)
    except Exception:
        return gr.update(choices=[], value=None)


async def launch_template(template_id, dataset_id, dataset_version_id):
    if not template_id or not dataset_id:
        return {"error": "Select a template and dataset."}
    try:
        async with httpx.AsyncClient(timeout=30.0, headers=_api_headers()) as client:
            response = await client.post(
                f"{API_BASE}/evaluation-templates/{template_id}/launch",
                json={"dataset_id": dataset_id, "dataset_version_id": dataset_version_id or None},
            )
            response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as error:
        return {"error": f"HTTP {error.response.status_code}: {error.response.text}"}
    except Exception as error:
        return {"error": str(error)}


async def create_report_share(run_id, hours, agency_name, report_title):
    if not run_id:
        return {"error": "Enter a run ID to share."}
    try:
        async with httpx.AsyncClient(timeout=15.0, headers=_api_headers()) as client:
            response = await client.post(f"{API_BASE}/report-shares", json={
                "run_id": run_id.strip(), "expires_in_hours": int(hours),
                "branding": {"agency_name": agency_name or "Evaluation Summary", "report_title": report_title or "Client Report"},
            })
            response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as error:
        return {"error": f"HTTP {error.response.status_code}: {error.response.text}"}
    except Exception as error:
        return {"error": str(error)}


async def get_project_dashboard(project_id):
    if not project_id:
        return {"error": "Select a project."}
    try:
        async with httpx.AsyncClient(timeout=20.0, headers=_api_headers()) as client:
            response = await client.get(f"{API_BASE}/projects/{project_id}/dashboard")
            response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as error:
        return {"error": f"HTTP {error.response.status_code}: {error.response.text}"}
    except Exception as error:
        return {"error": str(error)}


async def refresh_version_choices(dataset_id):
    if not dataset_id:
        return gr.update(choices=[], value=None)
    try:
        async with httpx.AsyncClient(timeout=10.0, headers=_api_headers()) as client:
            response = await client.get(f"{API_BASE}/datasets/{dataset_id}")
            response.raise_for_status()
        dataset = response.json()
        choices = [
            (
                f"v{version['version_number']} — {version['example_count']} cases"
                + (" (active)" if version["is_active"] else ""),
                version["id"],
            )
            for version in dataset["versions"]
        ]
        active = next((version["id"] for version in dataset["versions"] if version["is_active"]), None)
        return gr.update(choices=choices, value=active)
    except Exception:
        return gr.update(choices=[], value=None)


async def upload_dataset(file_path, name, description, tags, project_id):
    if not file_path:
        return {"error": "Choose a UTF-8 .jsonl file to upload."}
    path = Path(file_path)
    dataset_name = name.strip() if name and name.strip() else path.stem
    try:
        file_content = path.read_bytes()
        async with httpx.AsyncClient(timeout=30.0, headers=_api_headers()) as client:
            response = await client.post(
                f"{API_BASE}/datasets/upload",
                data={
                    "name": dataset_name,
                    "description": description or "",
                    "tags": tags or "",
                    "project_id": project_id or "",
                },
                files={"file": (path.name, file_content, "application/jsonl")},
            )
            response.raise_for_status()
        return {"message": "Dataset uploaded and version 1 is active.", "dataset": response.json()}
    except httpx.HTTPStatusError as error:
        return {"error": f"HTTP {error.response.status_code}: {error.response.text}"}
    except Exception as error:
        return {"error": str(error)}


async def add_dataset_version(dataset_id, file_path):
    if not dataset_id or not file_path:
        return {"error": "Select a dataset and a .jsonl file for the new version."}
    try:
        content = Path(file_path).read_text(encoding="utf-8")
        async with httpx.AsyncClient(timeout=30.0, headers=_api_headers()) as client:
            response = await client.post(
                f"{API_BASE}/datasets/{dataset_id}/versions",
                json={"content": content},
            )
            response.raise_for_status()
        return {"message": "New version created and set active.", "version": response.json()}
    except httpx.HTTPStatusError as error:
        return {"error": f"HTTP {error.response.status_code}: {error.response.text}"}
    except UnicodeDecodeError:
        return {"error": "File must be UTF-8 encoded."}
    except Exception as error:
        return {"error": str(error)}


async def set_active_dataset_version(dataset_id, version_id):
    if not dataset_id or not version_id:
        return {"error": "Select a dataset and version."}
    try:
        async with httpx.AsyncClient(timeout=10.0, headers=_api_headers()) as client:
            response = await client.put(
                f"{API_BASE}/datasets/{dataset_id}/active-version",
                json={"version_id": version_id},
            )
            response.raise_for_status()
        return {"message": "Active version updated.", "version": response.json()}
    except httpx.HTTPStatusError as error:
        return {"error": f"HTTP {error.response.status_code}: {error.response.text}"}
    except Exception as error:
        return {"error": str(error)}


async def get_pairwise_status(run_id):
    if not run_id or not run_id.strip():
        return "Please enter a Run ID.", None, None, None
    try:
        async with httpx.AsyncClient(timeout=10.0, headers=_api_headers()) as client:
            resp = await client.get(
                f"{API_BASE}/pairwise-runs/{run_id.strip()}",
                params={"include_comparisons": True},
            )
            resp.raise_for_status()
            data = resp.json()
            status = data.get("status", "unknown")
            if data.get("is_simulated"):
                status = f"SIMULATED — {status}"
            model_a = data.get("model_a_name", "?")
            model_b = data.get("model_b_name", "?")
            error = data.get("error")
            if error:
                status = f"{status} — {error}"
            metrics = data.get("metrics")
            comparisons = data.get("comparisons")
            configuration = {
                "project_id": data.get("project_id"),
                "configuration_verified": data.get("configuration_verified", False),
                "configuration": data.get("run_configuration"),
            }
            if not metrics:
                return f"{status} ({model_a} vs {model_b})", None, None, configuration

            m = metrics
            metrics_rows = [
                ["Total Comparisons", m["total_comparisons"]],
                ["Valid Comparisons", m["valid_comparisons"]],
                ["Evaluation Coverage", f"{m['evaluation_coverage']:.1%}"],
                ["Generation Errors", m["generation_errors"]],
                ["Evaluation Errors", m["evaluation_errors"]],
                ["Quality", "Available" if m["valid_comparisons"] else "No valid evaluations."],
                ["Win Rate A", f"{m['win_rate_a']:.1%}" if m["win_rate_a"] is not None else "—"],
                ["Win Rate B", f"{m['win_rate_b']:.1%}" if m["win_rate_b"] is not None else "—"],
                ["Tie Rate", f"{m['tie_rate']:.1%}" if m["tie_rate"] is not None else "—"],
                ["Elo A", m["elo_a"] if m["elo_a"] is not None else "—"],
                ["Elo B", m["elo_b"] if m["elo_b"] is not None else "—"],
                ["Avg Score A", f"{m['avg_score_a']:.3f}" if m["avg_score_a"] is not None else "—"],
                ["Avg Score B", f"{m['avg_score_b']:.3f}" if m["avg_score_b"] is not None else "—"],
            ]

            comp_rows = None
            if comparisons:
                comp_rows = [
                    [
                        c["example_id"],
                        c["winner"],
                        f"{c['score_a']:.2f}" if c["score_a"] is not None else "—",
                        f"{c['score_b']:.2f}" if c["score_b"] is not None else "—",
                        c["judge_reason"][:80],
                    ]
                    for c in comparisons
                ]

            return f"{status} ({model_a} vs {model_b})", metrics_rows, comp_rows, configuration
    except httpx.HTTPStatusError as e:
        return f"HTTP {e.response.status_code}: {e.response.text}", None, None, None
    except Exception as e:
        return f"Error: {e}", None, None, None


with gr.Blocks(title="LLM Evaluation Engine") as demo:
    gr.Markdown("# LLM Evaluation Engine")

    with gr.Tab("Setup Wizard"):
        gr.Markdown("### Hosted-pilot checklist\n1. Set `WORKSPACE_ENCRYPTION_KEY` and a PostgreSQL `DATABASE_URL` for production.\n2. Create the first owner below and copy the token once.\n3. Set `WORKSPACE_API_TOKEN`, restart Gradio, then use **Seed Agency Demo**, run a mock evaluation, and create an expiring client link.")
        with gr.Row():
            wizard_email = gr.Textbox(label="Owner Email")
            wizard_name = gr.Textbox(label="Owner Display Name")
            wizard_workspace = gr.Textbox(label="Agency Workspace Name")
        wizard_button = gr.Button("Create Agency Workspace", variant="primary")
        wizard_output = gr.JSON(label="One-time Setup Result")
        wizard_button.click(fn=setup_workspace, inputs=[wizard_email, wizard_name, wizard_workspace], outputs=wizard_output)

    with gr.Tab("Provider Connections"):
        gr.Markdown("Workspace owners configure a provider once. The credential is encrypted by the API and is never returned to the browser, reports, or other members.")
        with gr.Row():
            with gr.Column():
                connection_name = gr.Textbox(label="Connection Name")
                connection_provider = gr.Dropdown(choices=PROVIDER_CHOICES, label="Provider", value="openai")
                connection_model = gr.Textbox(label="Default Model", value="gpt-4o")
                connection_api_key = gr.Textbox(label="API Key", type="password")
                connection_base_url = gr.Textbox(label="Compatible Endpoint Base URL (optional)")
                connection_unauthenticated = gr.Checkbox(label="Intentional unauthenticated endpoint", value=False)
                connection_save_button = gr.Button("Save Provider Connection", variant="primary")
            with gr.Column():
                connection_output = gr.JSON(label="Connection Result")
        connection_save_button.click(
            fn=create_provider_connection,
            inputs=[connection_name, connection_provider, connection_model, connection_api_key, connection_base_url, connection_unauthenticated],
            outputs=connection_output,
        )

    with gr.Tab("Evaluation Templates"):
        gr.Markdown("Save the standard candidate, judge, rubric, timeout, release-rule, and report settings for a client workflow, then launch it from a dataset version.")
        with gr.Row():
            with gr.Column():
                template_name = gr.Textbox(label="Template Name")
                template_candidate_connection = gr.Dropdown(label="Candidate Connection", choices=[])
                template_judge_connection = gr.Dropdown(label="Judge Connection", choices=[])
                template_prompt = gr.Textbox(label="Judge Rubric / Prompt (optional)", lines=3)
                template_concurrency = gr.Slider(minimum=1, maximum=20, value=5, step=1, label="Concurrency")
                template_timeout = gr.Slider(minimum=1, maximum=300, value=60, step=1, label="Timeout (seconds)")
                template_coverage = gr.Number(label="Minimum Coverage", value=0.95, minimum=0, maximum=1)
                template_max_drop = gr.Number(label="Maximum Pass-Rate Drop", value=0.05, minimum=0, maximum=1)
                template_save_button = gr.Button("Save Template", variant="primary")
            with gr.Column():
                launch_template_choice = gr.Dropdown(label="Saved Template", choices=[])
                launch_template_dataset = gr.Dropdown(label="Dataset", choices=[])
                launch_template_version = gr.Dropdown(label="Dataset Version", choices=[])
                refresh_templates_button = gr.Button("Refresh Templates and Datasets")
                template_launch_button = gr.Button("Launch Standard Evaluation", variant="primary")
                template_output = gr.JSON(label="Template Result")
        template_save_button.click(
            fn=create_evaluation_template,
            inputs=[template_name, template_candidate_connection, template_judge_connection, template_prompt, template_concurrency, template_timeout, template_coverage, template_max_drop],
            outputs=template_output,
        ).then(fn=template_choice_update, outputs=launch_template_choice)
        refresh_templates_button.click(fn=template_choice_update, outputs=launch_template_choice)
        refresh_templates_button.click(fn=dataset_choice_update, outputs=launch_template_dataset)
        refresh_templates_button.click(fn=run_provider_connection_choice_updates, outputs=[template_candidate_connection, template_judge_connection])
        launch_template_dataset.change(fn=refresh_version_choices, inputs=launch_template_dataset, outputs=launch_template_version)
        template_launch_button.click(
            fn=launch_template,
            inputs=[launch_template_choice, launch_template_dataset, launch_template_version],
            outputs=template_output,
        )

    with gr.Tab("Projects"):
        gr.Markdown("Create a workspace for each client product before uploading its evaluation datasets. Seed the mock-only demo to start a credential-free walkthrough.")
        with gr.Row():
            with gr.Column():
                project_name = gr.Textbox(label="Project / Product Name")
                project_client_name = gr.Textbox(label="Client Name")
                project_description = gr.Textbox(label="Description (optional)", lines=3)
                project_tags = gr.Textbox(label="Tags (optional, comma-separated)")
                create_project_button = gr.Button("Create Project", variant="primary")
                seed_demo_button = gr.Button("Seed Agency Demo")
            with gr.Column():
                project_output = gr.JSON(label="Project Result")
                demo_seed_output = gr.JSON(label="Demo Setup")

    with gr.Tab("Datasets"):
        gr.Markdown("Upload and version JSONL datasets. Assign each dataset to a client project; runs inherit that project.")
        with gr.Row():
            with gr.Column():
                gr.Markdown("### Upload a new dataset")
                upload_file = gr.File(label="JSONL File", file_types=[".jsonl"], type="filepath")
                upload_name = gr.Textbox(label="Dataset Name (optional)")
                upload_description = gr.Textbox(label="Description (optional)")
                upload_tags = gr.Textbox(label="Tags (optional, comma-separated)")
                upload_project = gr.Dropdown(label="Client Project (optional)", choices=[])
                upload_button = gr.Button("Upload Dataset", variant="primary")
                upload_output = gr.JSON(label="Upload Result")
            with gr.Column():
                gr.Markdown("### Create or select a version")
                manage_dataset = gr.Dropdown(label="Dataset", choices=[])
                refresh_datasets_button = gr.Button("Refresh Datasets")
                version_file = gr.File(label="New JSONL Version", file_types=[".jsonl"], type="filepath")
                add_version_button = gr.Button("Create New Version")
                manage_version = gr.Dropdown(label="Version", choices=[])
                refresh_versions_button = gr.Button("Refresh Versions")
                set_active_button = gr.Button("Set Active Version")
                version_output = gr.JSON(label="Version Result")

        upload_button.click(
            fn=upload_dataset,
            inputs=[upload_file, upload_name, upload_description, upload_tags, upload_project],
            outputs=upload_output,
        ).then(
            fn=dataset_choice_update,
            outputs=manage_dataset,
        )
        refresh_datasets_button.click(
            fn=dataset_choice_update,
            outputs=manage_dataset,
        )
        manage_dataset.change(fn=refresh_version_choices, inputs=manage_dataset, outputs=manage_version)
        refresh_versions_button.click(fn=refresh_version_choices, inputs=manage_dataset, outputs=manage_version)
        add_version_button.click(
            fn=add_dataset_version,
            inputs=[manage_dataset, version_file],
            outputs=version_output,
        ).then(fn=refresh_version_choices, inputs=manage_dataset, outputs=manage_version)
        set_active_button.click(
            fn=set_active_dataset_version,
            inputs=[manage_dataset, manage_version],
            outputs=version_output,
        ).then(fn=refresh_version_choices, inputs=manage_dataset, outputs=manage_version)

    create_project_button.click(
        fn=create_project,
        inputs=[project_name, project_client_name, project_description, project_tags],
        outputs=project_output,
    ).then(fn=project_choice_update, outputs=upload_project)
    seed_demo_button.click(fn=seed_agency_demo, outputs=demo_seed_output).then(
        fn=project_choice_update, outputs=upload_project
    ).then(fn=dataset_choice_update, outputs=manage_dataset)

    with gr.Tab("Run Evaluation"):
        gr.Markdown("Choose a stored dataset version and workspace provider connections. Credentials are configured once by an owner and never entered here.")
        with gr.Row():
            with gr.Column():
                dataset_id = gr.Dropdown(label="Dataset", choices=[])
                refresh_run_datasets = gr.Button("Refresh Datasets")
                dataset_version_id = gr.Dropdown(label="Dataset Version", choices=[])
                candidate_connection = gr.Dropdown(label="Candidate Provider Connection", choices=[])
                candidate_model = gr.Textbox(label="Candidate Model Override (optional)")
                concurrency = gr.Slider(minimum=1, maximum=20, value=5, step=1, label="Concurrency")
            with gr.Column():
                evaluator_connection = gr.Dropdown(label="Judge Provider Connection", choices=[])
                evaluator_model = gr.Textbox(label="Judge Model Override (optional)")
                judge_prompt_template = gr.Textbox(
                    label="Judge Prompt Template (optional)",
                    lines=4,
                    placeholder="Leave empty to use default judge prompt",
                )

        submit_btn = gr.Button("Submit Run", variant="primary")
        run_output = gr.JSON(label="Run Response")

        submit_btn.click(
            fn=submit_run,
            inputs=[
                dataset_id,
                dataset_version_id,
                candidate_connection,
                candidate_model,
                evaluator_connection,
                evaluator_model,
                concurrency,
                judge_prompt_template,
            ],
            outputs=run_output,
        )
        refresh_run_datasets.click(fn=dataset_choice_update, outputs=dataset_id)
        refresh_run_datasets.click(fn=run_provider_connection_choice_updates, outputs=[candidate_connection, evaluator_connection])
        dataset_id.change(fn=refresh_version_choices, inputs=dataset_id, outputs=dataset_version_id)

    with gr.Tab("View Results"):
        gr.Markdown("Look up an evaluation run by its ID.")
        run_id_input = gr.Textbox(label="Run ID")
        fetch_btn = gr.Button("Fetch Results", variant="primary")
        status_output = gr.Textbox(label="Status")
        metrics_output = gr.Dataframe(
            headers=[
                "Evaluator", "Mean Score", "Pass Rate", "Valid / Total", "Coverage",
                "Generation Errors", "Evaluation Errors", "Passing Valid",
            ],
            label="Metrics",
        )
        configuration_output = gr.JSON(label="Execution Configuration")

        fetch_btn.click(
            fn=get_run_status,
            inputs=[run_id_input],
            outputs=[status_output, metrics_output, configuration_output],
        )

    with gr.Tab("Review Results"):
        gr.Markdown("Inspect each persisted evaluation result and filter for low scores or provider and judge failures.")
        with gr.Row():
            review_run_id = gr.Textbox(label="Run ID")
            review_evaluator = gr.Dropdown(label="Evaluator", choices=["All"], value="All")
            review_outcomes = gr.CheckboxGroup(
                label="Outcomes",
                choices=["evaluated", "generation_error", "evaluation_error"],
                value=["evaluated", "generation_error", "evaluation_error"],
            )
        with gr.Row():
            review_score_min = gr.Number(label="Minimum Score (optional)", minimum=0, maximum=1, precision=3)
            review_score_max = gr.Number(label="Maximum Score (optional)", minimum=0, maximum=1, precision=3)
            review_button = gr.Button("Load Results", variant="primary")
        review_summary = gr.Markdown("Enter a run ID, choose filters, then load results.")
        review_table = gr.Dataframe(
            headers=["Example", "Evaluator", "Outcome", "Score", "Judge Explanation", "Sanitized Error"],
            label="Filtered Results",
        )
        review_selector = gr.Dropdown(label="Select a result for full details", choices=[])
        review_detail = gr.JSON(label="Prompt, Output, Expected Answer, and Review Details")
        review_state = gr.State([])

        review_button.click(
            fn=review_run_results,
            inputs=[review_run_id, review_evaluator, review_outcomes, review_score_min, review_score_max],
            outputs=[
                review_summary,
                review_table,
                review_selector,
                review_state,
                review_detail,
                review_evaluator,
            ],
        )
        review_selector.change(fn=show_review_result, inputs=[review_selector, review_state], outputs=review_detail)

    with gr.Tab("Client Reports"):
        gr.Markdown("Export a transparent run summary to share with a client. HTML is formatted for browser review or printing.")
        with gr.Row():
            report_run_id = gr.Textbox(label="Run ID")
            report_format = gr.Dropdown(label="Export Format", choices=["html", "json", "csv"], value="html")
            report_button = gr.Button("Export Report", variant="primary")
        report_export_status = gr.Markdown()
        report_download = gr.File(label="Client-ready Report")
        report_button.click(
            fn=export_run_report,
            inputs=[report_run_id, report_format],
            outputs=[report_export_status, report_download],
        )
        gr.Markdown("### Read-only client link")
        with gr.Row():
            share_run_id = gr.Textbox(label="Completed Run ID")
            share_hours = gr.Slider(label="Expires In Hours", minimum=1, maximum=720, value=168, step=1)
            share_agency_name = gr.Textbox(label="Agency Branding", value="Evaluation Summary")
            share_title = gr.Textbox(label="Report Title", value="Client Evaluation Report")
        share_button = gr.Button("Create Expiring Client Link")
        share_output = gr.JSON(label="Share Link — copy it now; it is shown only at creation")
        share_button.click(fn=create_report_share, inputs=[share_run_id, share_hours, share_agency_name, share_title], outputs=share_output)

    with gr.Tab("Project Dashboard"):
        gr.Markdown("Review the latest release state, coverage and quality trends, and recent evaluation failures for one client project.")
        dashboard_project = gr.Dropdown(label="Client Project", choices=[])
        dashboard_refresh = gr.Button("Refresh Projects")
        dashboard_load = gr.Button("Load Selected Dashboard", variant="primary")
        dashboard_output = gr.JSON(label="Project Health")
        dashboard_refresh.click(fn=project_choice_update, outputs=dashboard_project)
        dashboard_load.click(fn=get_project_dashboard, inputs=dashboard_project, outputs=dashboard_output)

    with gr.Tab("Release Checks"):
        gr.Markdown("Mark a completed run as the baseline, then compare a later completed run against the configured release rules.")
        with gr.Row():
            baseline_mark_run_id = gr.Textbox(label="Completed Run ID to Mark as Baseline")
            baseline_mark_button = gr.Button("Mark Baseline")
            baseline_mark_output = gr.JSON(label="Baseline Result")
        with gr.Row():
            release_run_id = gr.Textbox(label="Current Completed Run ID")
            release_baseline_id = gr.Dropdown(label="Baseline Run", choices=[])
            refresh_baselines_button = gr.Button("Refresh Baselines")
        with gr.Row():
            release_coverage_minimum = gr.Number(label="Minimum Coverage", value=0.95, minimum=0, maximum=1, precision=3)
            release_pass_rate_drop = gr.Number(label="Maximum Exact-Match Pass-Rate Drop", value=0.05, minimum=0, maximum=1, precision=3)
            release_check_button = gr.Button("Run Release Check", variant="primary")
        release_check_summary = gr.Markdown("Choose a baseline and current run to evaluate the release rules.")
        release_check_detail = gr.JSON(label="Evaluator Comparison")

        baseline_mark_button.click(
            fn=mark_run_as_baseline,
            inputs=baseline_mark_run_id,
            outputs=baseline_mark_output,
        ).then(fn=baseline_choice_update, outputs=release_baseline_id)
        refresh_baselines_button.click(fn=baseline_choice_update, outputs=release_baseline_id)
        release_check_button.click(
            fn=compare_run_with_baseline,
            inputs=[release_run_id, release_baseline_id, release_coverage_minimum, release_pass_rate_drop],
            outputs=[release_check_summary, release_check_detail],
        )

    # ── Pairwise Evaluation Tab ──────────────────────────────

    with gr.Tab("Pairwise Evaluation"):
        gr.Markdown("Compare two models side-by-side on a stored dataset version.")
        with gr.Row():
            with gr.Column():
                gr.Markdown("### Model A")
                pw_dataset_id = gr.Dropdown(label="Dataset", choices=[])
                pw_refresh_datasets = gr.Button("Refresh Datasets")
                pw_dataset_version_id = gr.Dropdown(label="Dataset Version", choices=[])
                pw_model_a_connection = gr.Dropdown(label="Provider Connection", choices=[])
                pw_model_a_model = gr.Textbox(label="Model Override (optional)")
                pw_concurrency = gr.Slider(minimum=1, maximum=20, value=5, step=1, label="Concurrency")
            with gr.Column():
                gr.Markdown("### Model B")
                pw_model_b_connection = gr.Dropdown(label="Provider Connection", choices=[])
                pw_model_b_model = gr.Textbox(label="Model Override (optional)")
                gr.Markdown("### Judge")
                pw_judge_connection = gr.Dropdown(label="Judge Provider Connection", choices=[])
                pw_judge_model = gr.Textbox(label="Judge Model Override (optional)")

        pw_submit_btn = gr.Button("Submit Pairwise Run", variant="primary")
        pw_run_output = gr.JSON(label="Pairwise Run Response")

        pw_submit_btn.click(
            fn=submit_pairwise_run,
            inputs=[
                pw_dataset_id,
                pw_dataset_version_id,
                pw_model_a_connection,
                pw_model_a_model,
                pw_model_b_connection,
                pw_model_b_model,
                pw_judge_connection,
                pw_judge_model,
                pw_concurrency,
            ],
            outputs=pw_run_output,
        )
        pw_refresh_datasets.click(fn=dataset_choice_update, outputs=pw_dataset_id)
        pw_refresh_datasets.click(
            fn=pairwise_provider_connection_choice_updates,
            outputs=[pw_model_a_connection, pw_model_b_connection, pw_judge_connection],
        )
        pw_dataset_id.change(fn=refresh_version_choices, inputs=pw_dataset_id, outputs=pw_dataset_version_id)

    demo.load(
        fn=all_dataset_choice_updates,
        outputs=[manage_dataset, dataset_id, pw_dataset_id],
    )
    demo.load(
        fn=all_provider_connection_choice_updates,
        outputs=[candidate_connection, evaluator_connection, pw_model_a_connection, pw_model_b_connection, pw_judge_connection],
    )
    demo.load(fn=project_choice_update, outputs=upload_project)
    demo.load(fn=template_choice_update, outputs=launch_template_choice)
    demo.load(fn=project_choice_update, outputs=dashboard_project)
    demo.load(fn=dataset_choice_update, outputs=launch_template_dataset)
    demo.load(fn=run_provider_connection_choice_updates, outputs=[template_candidate_connection, template_judge_connection])

    with gr.Tab("Pairwise Results"):
        gr.Markdown("Look up a pairwise evaluation run by its ID.")
        pw_run_id_input = gr.Textbox(label="Pairwise Run ID")
        pw_fetch_btn = gr.Button("Fetch Results", variant="primary")
        pw_status_output = gr.Textbox(label="Status")
        pw_metrics_output = gr.Dataframe(
            headers=["Metric", "Value"],
            label="Pairwise Metrics",
        )
        pw_comparisons_output = gr.Dataframe(
            headers=["Example ID", "Winner", "Score A", "Score B", "Reason"],
            label="Individual Comparisons",
        )
        pw_configuration_output = gr.JSON(label="Execution Configuration")

        pw_fetch_btn.click(
            fn=get_pairwise_status,
            inputs=[pw_run_id_input],
            outputs=[pw_status_output, pw_metrics_output, pw_comparisons_output, pw_configuration_output],
        )


if __name__ == "__main__":
    demo.launch(server_name="0.0.0.0", server_port=7860)

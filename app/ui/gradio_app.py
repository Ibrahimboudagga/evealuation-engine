import gradio as gr
import httpx

API_BASE = "http://localhost:8000"

PROVIDER_CHOICES = ["openai", "anthropic", "cohere", "gemini", "mock"]


async def submit_run(
    dataset_path,
    candidate_provider,
    candidate_model,
    candidate_api_key,
    evaluator_provider,
    evaluator_model,
    evaluator_api_key,
    concurrency,
    judge_prompt_template,
):
    payload = {
        "dataset_path": dataset_path,
        "candidate_provider": candidate_provider,
        "candidate_model": candidate_model,
        "candidate_api_key": candidate_api_key or None,
        "evaluator_provider": evaluator_provider,
        "evaluator_model": evaluator_model,
        "evaluator_api_key": evaluator_api_key or None,
        "concurrency": int(concurrency),
        "judge_prompt_template": judge_prompt_template or None,
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(f"{API_BASE}/runs", json=payload)
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPStatusError as e:
        return {"error": f"HTTP {e.response.status_code}: {e.response.text}"}
    except Exception as e:
        return {"error": str(e)}


async def get_run_status(run_id):
    if not run_id or not run_id.strip():
        return "Please enter a Run ID.", None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{API_BASE}/runs/{run_id.strip()}")
            resp.raise_for_status()
            data = resp.json()
            status = data.get("status", "unknown")
            metrics = data.get("metrics")
            error = data.get("error")
            if error:
                status = f"{status} — {error}"
            if not metrics:
                return status, None
            rows = []
            for m in metrics:
                rows.append([m["evaluator"], m["mean_score"], m["pass_rate"], m["n"]])
            return status, rows
    except httpx.HTTPStatusError as e:
        return f"HTTP {e.response.status_code}: {e.response.text}", None
    except Exception as e:
        return f"Error: {e}", None


async def submit_pairwise_run(
    dataset_path,
    model_a_provider,
    model_a_model,
    model_a_api_key,
    model_a_base_url,
    model_b_provider,
    model_b_model,
    model_b_api_key,
    model_b_base_url,
    judge_provider,
    judge_model,
    judge_api_key,
    concurrency,
):
    payload = {
        "dataset_path": dataset_path,
        "model_a_provider": model_a_provider,
        "model_a_model": model_a_model,
        "model_a_api_key": model_a_api_key or None,
        "model_a_base_url": model_a_base_url or None,
        "model_b_provider": model_b_provider,
        "model_b_model": model_b_model,
        "model_b_api_key": model_b_api_key or None,
        "model_b_base_url": model_b_base_url or None,
        "judge_provider": judge_provider,
        "judge_model": judge_model,
        "judge_api_key": judge_api_key or None,
        "concurrency": int(concurrency),
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(f"{API_BASE}/pairwise-runs", json=payload)
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPStatusError as e:
        return {"error": f"HTTP {e.response.status_code}: {e.response.text}"}
    except Exception as e:
        return {"error": str(e)}


async def get_pairwise_status(run_id):
    if not run_id or not run_id.strip():
        return "Please enter a Run ID.", None, None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{API_BASE}/pairwise-runs/{run_id.strip()}",
                params={"include_comparisons": True},
            )
            resp.raise_for_status()
            data = resp.json()
            status = data.get("status", "unknown")
            model_a = data.get("model_a_name", "?")
            model_b = data.get("model_b_name", "?")
            error = data.get("error")
            if error:
                status = f"{status} — {error}"
            metrics = data.get("metrics")
            comparisons = data.get("comparisons")
            if not metrics:
                return f"{status} ({model_a} vs {model_b})", None, None

            m = metrics
            metrics_rows = [
                ["Win Rate A", f"{m['win_rate_a']:.1%}"],
                ["Win Rate B", f"{m['win_rate_b']:.1%}"],
                ["Tie Rate", f"{m['tie_rate']:.1%}"],
                ["Elo A", m["elo_a"]],
                ["Elo B", m["elo_b"]],
                ["Avg Score A", f"{m['avg_score_a']:.3f}"],
                ["Avg Score B", f"{m['avg_score_b']:.3f}"],
                ["Total Comparisons", m["total_comparisons"]],
            ]

            comp_rows = None
            if comparisons:
                comp_rows = [
                    [
                        c["example_id"],
                        c["winner"],
                        f"{c['score_a']:.2f}",
                        f"{c['score_b']:.2f}",
                        c["judge_reason"][:80],
                    ]
                    for c in comparisons
                ]

            return f"{status} ({model_a} vs {model_b})", metrics_rows, comp_rows
    except httpx.HTTPStatusError as e:
        return f"HTTP {e.response.status_code}: {e.response.text}", None, None
    except Exception as e:
        return f"Error: {e}", None, None


with gr.Blocks(title="LLM Evaluation Engine") as demo:
    gr.Markdown("# LLM Evaluation Engine")

    with gr.Tab("Run Evaluation"):
        gr.Markdown("Configure and submit a new evaluation run.")
        with gr.Row():
            with gr.Column():
                dataset_path = gr.Textbox(label="Dataset Path", value="datasets/sample.jsonl")
                candidate_provider = gr.Dropdown(choices=PROVIDER_CHOICES, label="Candidate Provider", value="openai")
                candidate_model = gr.Textbox(label="Candidate Model", value="gpt-4o")
                candidate_api_key = gr.Textbox(label="Candidate API Key", type="password")
                concurrency = gr.Slider(minimum=1, maximum=20, value=5, step=1, label="Concurrency")
            with gr.Column():
                evaluator_provider = gr.Dropdown(choices=PROVIDER_CHOICES, label="Evaluator Provider", value="openai")
                evaluator_model = gr.Textbox(label="Evaluator Model", value="gpt-4o")
                evaluator_api_key = gr.Textbox(label="Evaluator API Key", type="password")
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
                dataset_path,
                candidate_provider,
                candidate_model,
                candidate_api_key,
                evaluator_provider,
                evaluator_model,
                evaluator_api_key,
                concurrency,
                judge_prompt_template,
            ],
            outputs=run_output,
        )

    with gr.Tab("View Results"):
        gr.Markdown("Look up an evaluation run by its ID.")
        run_id_input = gr.Textbox(label="Run ID")
        fetch_btn = gr.Button("Fetch Results", variant="primary")
        status_output = gr.Textbox(label="Status")
        metrics_output = gr.Dataframe(headers=["Evaluator", "Mean Score", "Pass Rate", "N"], label="Metrics")

        fetch_btn.click(
            fn=get_run_status,
            inputs=[run_id_input],
            outputs=[status_output, metrics_output],
        )

    # ── Pairwise Evaluation Tab ──────────────────────────────

    with gr.Tab("Pairwise Evaluation"):
        gr.Markdown("Compare two models side-by-side on the same dataset.")
        with gr.Row():
            with gr.Column():
                gr.Markdown("### Model A")
                pw_dataset_path = gr.Textbox(label="Dataset Path", value="datasets/sample.jsonl")
                pw_model_a_provider = gr.Dropdown(choices=PROVIDER_CHOICES, label="Provider", value="openai")
                pw_model_a_model = gr.Textbox(label="Model", value="gpt-4o")
                pw_model_a_api_key = gr.Textbox(label="API Key", type="password")
                pw_model_a_base_url = gr.Textbox(label="Base URL (optional)")
                pw_concurrency = gr.Slider(minimum=1, maximum=20, value=5, step=1, label="Concurrency")
            with gr.Column():
                gr.Markdown("### Model B")
                pw_model_b_provider = gr.Dropdown(choices=PROVIDER_CHOICES, label="Provider", value="openai")
                pw_model_b_model = gr.Textbox(label="Model", value="gpt-4o-mini")
                pw_model_b_api_key = gr.Textbox(label="API Key", type="password")
                pw_model_b_base_url = gr.Textbox(label="Base URL (optional)")
                gr.Markdown("### Judge")
                pw_judge_provider = gr.Dropdown(choices=PROVIDER_CHOICES, label="Judge Provider", value="openai")
                pw_judge_model = gr.Textbox(label="Judge Model", value="gpt-4o")
                pw_judge_api_key = gr.Textbox(label="Judge API Key", type="password")

        pw_submit_btn = gr.Button("Submit Pairwise Run", variant="primary")
        pw_run_output = gr.JSON(label="Pairwise Run Response")

        pw_submit_btn.click(
            fn=submit_pairwise_run,
            inputs=[
                pw_dataset_path,
                pw_model_a_provider,
                pw_model_a_model,
                pw_model_a_api_key,
                pw_model_a_base_url,
                pw_model_b_provider,
                pw_model_b_model,
                pw_model_b_api_key,
                pw_model_b_base_url,
                pw_judge_provider,
                pw_judge_model,
                pw_judge_api_key,
                pw_concurrency,
            ],
            outputs=pw_run_output,
        )

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

        pw_fetch_btn.click(
            fn=get_pairwise_status,
            inputs=[pw_run_id_input],
            outputs=[pw_status_output, pw_metrics_output, pw_comparisons_output],
        )


if __name__ == "__main__":
    demo.launch()

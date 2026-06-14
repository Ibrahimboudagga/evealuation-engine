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


if __name__ == "__main__":
    demo.launch()

# Five-minute agency demo

This walkthrough uses only the explicit `mock` provider. It needs no API keys and every generated run is visibly labelled **SIMULATED**.

1. Install dependencies and start the API:

   ```powershell
   .\.venv\Scripts\python.exe -m uvicorn app.api.main:app --port 8000
   ```

2. In a second terminal, start Gradio:

   ```powershell
   .\.venv\Scripts\python.exe app/ui/gradio_app.py
   ```

3. Open the **Projects** tab and click **Seed Agency Demo**. This idempotently creates the `Northstar Demo Client / Customer Support Copilot` project and two small JSONL datasets.

4. Open **Run Evaluation**, click **Refresh Datasets**, choose a demo dataset and its active version, then select `mock` for both Candidate Provider and Evaluator Provider. Use `mock` as both model names and click **Submit Run**.

5. Copy the returned run ID. In **View Results**, fetch the run and confirm its **SIMULATED** label. In **Review Results**, inspect individual prompt, output, expected-answer, judge, and error evidence.

6. Open **Client Reports**, enter the same run ID, select HTML, and download the report. It includes configuration, coverage, quality metrics, failure counts, and failed examples with sanitized errors.

7. Optional release check: in **Release Checks**, mark that completed run as a baseline. Run another mock evaluation, then compare it to the baseline using the default 95% coverage and 5% exact-match pass-rate-drop rules.

The two source sample files are [agency_demo_support.jsonl](datasets/agency_demo_support.jsonl) and [agency_demo_retrieval.jsonl](datasets/agency_demo_retrieval.jsonl). The seed endpoint is `POST /demo/seed`; it is safe to call more than once.

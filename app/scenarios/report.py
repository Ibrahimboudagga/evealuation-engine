"""Local HTML review of scenario evidence. All application content is escaped."""

import html
import json


def render_report(manifest, results):
    def esc(value):
        return html.escape(str(value))

    rows = []
    for result in results:
        checks = "".join(f"<li>{esc(c.name)}: <strong>{esc(c.status)}</strong> — {esc(c.explanation)}</li>" for c in result.checks)
        output = json.dumps(result.evidence.output, indent=2, ensure_ascii=False) if result.evidence else "No output"
        rows.append(f"<section><h2>{esc(result.scenario_id)}</h2><p>{esc(result.outcome)} / {esc(result.decision)}"
                    f" — {'SIMULATED' if result.simulated else 'LIVE TARGET'}</p>"
                    f"<p>{esc(result.error_message or '')}</p><ul>{checks}</ul><details><summary>Output</summary>"
                    f"<pre>{esc(output)}</pre></details></section>")
    metrics = esc(json.dumps(manifest["metrics"], indent=2))
    return ("<!doctype html><html lang='en'><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<meta http-equiv='Content-Security-Policy' content=\"default-src 'none'; style-src 'unsafe-inline'\">"
            "<title>Scenario evaluation</title><style>body{font:16px system-ui;max-width:960px;margin:40px auto;padding:20px}"
            "section{border-top:1px solid #ddd;padding:16px 0}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style>"
            f"<h1>Scenario evaluation {'— SIMULATED' if manifest['simulated'] else ''}</h1>"
            f"<p>Run {esc(manifest['run_id'])} · {esc(manifest['status'])} · {esc(manifest['target_label'])}</p>"
            "<p>Coverage denominator: all expected cases. Average score and pass rate denominator: valid evaluations. "
            "Scores measure explicit checks only; they are not a general judgment of correctness.</p>"
            f"<pre>{metrics}</pre>" + "".join(rows) + "</html>")

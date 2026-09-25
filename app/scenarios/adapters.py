"""Trusted operator-configured adapters; do not expose URL selection to public users."""

import asyncio
import time
from typing import Protocol
from urllib.parse import quote, urlsplit

import httpx

from app.scenarios.schemas import Evidence, Scenario
from app.scenarios.evaluation import resolve_pointer


class ExecutionFailure(Exception):
    """Safe, deliberately non-sensitive adapter error."""


class Adapter(Protocol):
    simulated: bool

    async def execute(self, scenario: Scenario, request_id: str) -> Evidence: ...


class FixtureAdapter:
    simulated = True

    def __init__(self, fixtures: dict):
        self.fixtures = fixtures

    async def execute(self, scenario, request_id):
        if scenario.id not in self.fixtures:
            raise ExecutionFailure("No fixture for this scenario")
        return Evidence.model_validate({**self.fixtures[scenario.id], "simulated": True})


class HttpAdapter:
    simulated = False

    def __init__(self, url: str, token: str | None = None, *, transport=None):
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Use an HTTP(S) endpoint without credentials, query, or fragment")
        if token and parsed.scheme != "https" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("Authenticated remote endpoints require HTTPS")
        self.url = url.rstrip("/")
        self.token = token
        self.transport = transport

    def client(self):
        return httpx.AsyncClient(transport=self.transport, follow_redirects=False, timeout=30,
                                headers={"Authorization": "Bearer " + self.token} if self.token else {})

    async def request(self, client, method, url, **kwargs):
        try:
            response = await client.request(method, url, **kwargs)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            raise ExecutionFailure(f"Target returned HTTP {exc.response.status_code}") from None
        except httpx.TimeoutException:
            raise ExecutionFailure("Target request timed out; remote execution may still be active") from None
        except httpx.HTTPError:
            raise ExecutionFailure("Target connection failed") from None
        except ValueError:
            raise ExecutionFailure("Target returned malformed JSON") from None

    async def execute(self, scenario, request_id):
        started = time.monotonic()
        async with self.client() as client:
            body = await self.request(client, "POST", self.url,
                                      json={"schema_version": 1, "request_id": request_id,
                                            "task": scenario.task, "inputs": scenario.inputs},
                                      headers={"Idempotency-Key": request_id})
        evidence = Evidence.model_validate(body)
        evidence.latency_ms = (time.monotonic() - started) * 1000
        return evidence


class LegalRagAdapter(HttpAdapter):
    """Uses the reference app's real start/status/report routes.

    A report is available while the workflow awaits human review. Do not wait
    for COMPLETED: the reference API returns no report after that transition.
    """

    def __init__(self, *args, poll_seconds=1.0, ready_path="/workflow_status/status", ready_value="human_in_loop", **kwargs):
        super().__init__(*args, **kwargs)
        if poll_seconds <= 0:
            raise ValueError("Poll interval must be positive")
        self.poll_seconds = poll_seconds
        self.ready_path = ready_path
        self.ready_value = ready_value

    async def execute(self, scenario, request_id):
        started = time.monotonic()
        async with self.client() as client:
            response = await self.request(client, "POST", self.url + "/agent-review/start",
                                          json={"query": scenario.task,
                                                "s3_paths": scenario.inputs.get("s3_paths", []),
                                                "top_k": scenario.inputs.get("top_k", 5)},
                                          headers={"Idempotency-Key": request_id})
            workflow_id = response.get("workflow_id") if isinstance(response, dict) else None
            if not isinstance(workflow_id, str) or not workflow_id:
                raise ExecutionFailure("Target start response has no workflow ID")
            path = self.url + "/agent-review/" + quote(workflow_id, safe="")
            while True:
                status = await self.request(client, "GET", path + "/status")
                if not isinstance(status, dict):
                    raise ExecutionFailure("Invalid target status response")
                state = status.get("desc_status")
                if state in ("FAILED", "CANCELED", "CANCELLED", "TERMINATED", "TIMED_OUT"):
                    raise ExecutionFailure("Remote workflow did not complete successfully")
                body = await self.request(client, "GET", path + "/report")
                report = body.get("workflow_report") if isinstance(body, dict) else None
                try:
                    ready = resolve_pointer(status, self.ready_path) == self.ready_value
                except (KeyError, IndexError):
                    ready = False
                if ready and report and isinstance(report, dict) and "error" not in report:
                    return Evidence(output=report, simulated=False, external_run_id=workflow_id,
                                    latency_ms=(time.monotonic() - started) * 1000)
                if state == "COMPLETED":
                    raise ExecutionFailure("Workflow completed but its report is unavailable from the target API")
                await asyncio.sleep(self.poll_seconds)

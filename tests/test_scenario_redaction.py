"""Credential redaction preserves unmodified scoring inputs and text formatting."""
import copy
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api.main import app
from app.services.scenario_service import ScenarioService, redact


def test_redaction_preserves_whitespace_and_does_not_mutate_original():
    data = {"output": {"code": "line 1\n    line 2\t\n", "apiKey": "private-api-key",
                       "refresh_token": "private-refresh", "refreshToken": "private-refresh-2"},
            "log": "  first line\nAuthorization: Bearer private-bearer\n  last line\n"}
    original = copy.deepcopy(data)
    result = redact(data)
    assert result["output"]["code"] == data["output"]["code"]
    assert all(result["output"][key] == "[REDACTED]" for key in ("apiKey", "refresh_token", "refreshToken"))
    assert result["log"] == "  first line\nAuthorization: Bearer [REDACTED]\n  last line\n"
    assert data == original
    assert redact(result) == result


@pytest.mark.parametrize("inputs", [{"apiKey": "private"}, {"nested": {"refresh_token": "private"}},
                                   {"message": "apiKey=private"}, {"message": 'header {"password": "private"}'}])
def test_suites_with_obvious_credentials_are_rejected_before_storage(inputs):
    case = {"id": "s", "task": "test", "inputs": inputs,
            "assertions": [{"name": "answer", "path": "/answer", "value": "yes"}]}
    # An empty context proves rejection happens before quota or database access.
    with pytest.raises(ValueError, match="credential") as error:
        ScenarioService().create_suite(SimpleNamespace(), "p", "secret-free suite", json.dumps(case))
    assert "private" not in str(error.value)


def test_retained_redaction_is_explicit_and_scoring_uses_original_observations():
    with TestClient(app) as client:
        owner = client.post("/auth/bootstrap", json={"email": "redaction@example.com",
            "display_name": "Owner", "workspace_name": "Redaction workspace"}).json()
        headers = {"Authorization": "Bearer " + owner["api_token"]}
        project = client.post("/projects", headers=headers, json={"name": "Agent", "client_name": "Client"}).json()
        original = "def answer():\n    return 4\n"
        case = {"id": "s", "task": "Return code", "assertions": [{"name": "code", "path": "/code", "value": original}]}
        suite = client.post("/scenario-suites", headers=headers, json={"project_id": project["id"],
            "name": "Code", "content": json.dumps(case)}).json()
        result = client.post("/scenario-runs", headers=headers, json={"suite_id": suite["id"],
            "target_build": "v1", "evidence": {"s": {"output": {"code": original, "apiKey": "private"},
                                                   "simulated": True}}})
        assert result.status_code == 201, result.text
        data = result.json()
        assert data["metrics"]["decision"] == "passed"
        assert data["results"][0]["evidence"]["output"]["code"] == original
        assert data["results"][0]["evidence"]["output"]["apiKey"] == "[REDACTED]"
        assert data["configuration"]["redaction"]["applied"] is True
        assert data["configuration"]["redaction"]["scores_use_original_observations"] is True
        assert data["configuration"]["redaction"]["retained_evidence_replayable"] is False

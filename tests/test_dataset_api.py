import json
import io
import pytest
from fastapi.testclient import TestClient

from app.api.main import app
from app.services.dataset_service import DatasetService


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def service():
    return DatasetService()


@pytest.fixture
def sample_jsonl():
    data = [
        {"id": "a1", "input": "What is 1+1?", "expected_output": "2"},
        {"id": "a2", "input": "What is 2+2?", "expected_output": "4"},
    ]
    return "\n".join(json.dumps(item) for item in data)


@pytest.fixture
def sample_jsonl_v2():
    data = [
        {"id": "a1", "input": "What is 1+1?", "expected_output": "2"},
        {"id": "a2", "input": "What is 2+2?", "expected_output": "4"},
        {"id": "a3", "input": "What is 3+3?", "expected_output": "6"},
    ]
    return "\n".join(json.dumps(item) for item in data)


class TestListDatasetsAPI:
    def test_list_empty(self, client):
        resp = client.get("/datasets")
        assert resp.status_code == 200
        body = resp.json()
        assert body["datasets"] == []

    def test_list_returns_datasets(self, client, service, sample_jsonl):
        service.create_dataset(name="api-list", content_jsonl=sample_jsonl)
        resp = client.get("/datasets")
        assert resp.status_code == 200
        assert len(resp.json()["datasets"]) == 1
        assert resp.json()["datasets"][0]["name"] == "api-list"

    def test_list_filter_tag(self, client, service, sample_jsonl):
        service.create_dataset(name="tagged", content_jsonl=sample_jsonl, tags=["qa"])
        service.create_dataset(name="other", content_jsonl=sample_jsonl)
        resp = client.get("/datasets", params={"tag": "qa"})
        assert resp.status_code == 200
        assert len(resp.json()["datasets"]) == 1

    def test_list_search(self, client, service, sample_jsonl):
        service.create_dataset(name="geography-101", content_jsonl=sample_jsonl)
        service.create_dataset(name="math-101", content_jsonl=sample_jsonl)
        resp = client.get("/datasets", params={"search": "geo"})
        assert resp.status_code == 200
        assert len(resp.json()["datasets"]) == 1


class TestGetDatasetAPI:
    def test_get_existing(self, client, service, sample_jsonl):
        dataset = service.create_dataset(name="api-get", content_jsonl=sample_jsonl)
        resp = client.get(f"/datasets/{dataset.id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "api-get"
        assert len(body["versions"]) == 1
        assert body["versions"][0]["version_number"] == 1
        assert body["versions"][0]["is_active"] is True

    def test_get_nonexistent(self, client):
        resp = client.get("/datasets/nonexistent")
        assert resp.status_code == 404

    def test_get_includes_versions(self, client, service, sample_jsonl, sample_jsonl_v2):
        dataset = service.create_dataset(name="api-vers", content_jsonl=sample_jsonl)
        service.add_version(dataset.id, sample_jsonl_v2)
        resp = client.get(f"/datasets/{dataset.id}")
        body = resp.json()
        assert len(body["versions"]) == 2
        assert body["versions"][0]["version_number"] == 1
        assert body["versions"][1]["version_number"] == 2
        assert body["versions"][1]["is_active"] is True


class TestCreateDatasetAPI:
    def test_create_basic(self, client, sample_jsonl):
        resp = client.post("/datasets", json={
            "name": "api-create",
            "content": sample_jsonl,
        })
        assert resp.status_code == 201
        body = resp.json()
        assert body["name"] == "api-create"
        assert body["latest_version_number"] == 1
        assert body["id"] is not None

    def test_create_with_description_and_tags(self, client, sample_jsonl):
        resp = client.post("/datasets", json={
            "name": "api-full",
            "content": sample_jsonl,
            "description": "Full dataset",
            "tags": ["test", "qa"],
        })
        assert resp.status_code == 201
        body = resp.json()
        assert body["description"] == "Full dataset"
        assert body["tags"] == ["test", "qa"]

    def test_create_empty_content_fails(self, client):
        resp = client.post("/datasets", json={
            "name": "empty",
            "content": "",
        })
        assert resp.status_code in (400, 422)

    def test_create_invalid_json_fails(self, client):
        resp = client.post("/datasets", json={
            "name": "bad",
            "content": "not json",
        })
        assert resp.status_code == 400


class TestUploadDatasetAPI:
    def test_upload_basic(self, client, sample_jsonl):
        resp = client.post(
            "/datasets/upload",
            files={"file": ("data.jsonl", sample_jsonl.encode("utf-8"), "application/jsonl")},
            data={"name": "api-upload"},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["name"] == "api-upload"
        assert body["latest_version_number"] == 1

    def test_upload_with_tags(self, client, sample_jsonl):
        resp = client.post(
            "/datasets/upload",
            files={"file": ("data.jsonl", sample_jsonl.encode("utf-8"), "application/jsonl")},
            data={
                "name": "api-upload-tags",
                "tags": "qa,test",
            },
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["tags"] == ["qa", "test"]

    def test_upload_non_jsonl_fails(self, client):
        resp = client.post(
            "/datasets/upload",
            files={"file": ("data.csv", b"id,input,output", "text/csv")},
            data={"name": "bad"},
        )
        assert resp.status_code == 400

    def test_upload_empty_file_fails(self, client):
        resp = client.post(
            "/datasets/upload",
            files={"file": ("data.jsonl", b"", "application/jsonl")},
            data={"name": "empty"},
        )
        assert resp.status_code == 400


class TestAddVersionAPI:
    def test_add_version(self, client, service, sample_jsonl, sample_jsonl_v2):
        dataset = service.create_dataset(name="api-addv", content_jsonl=sample_jsonl)
        resp = client.post(f"/datasets/{dataset.id}/versions", json={
            "content": sample_jsonl_v2,
        })
        assert resp.status_code == 201
        body = resp.json()
        assert body["version_number"] == 2
        assert body["example_count"] == 3
        assert body["is_active"] is True

    def test_add_version_nonexistent_dataset(self, client, sample_jsonl):
        resp = client.post("/datasets/nonexistent/versions", json={
            "content": sample_jsonl,
        })
        assert resp.status_code == 400

    def test_add_version_empty_content(self, client, service, sample_jsonl):
        dataset = service.create_dataset(name="api-addv-empty", content_jsonl=sample_jsonl)
        resp = client.post(f"/datasets/{dataset.id}/versions", json={
            "content": "",
        })
        assert resp.status_code in (400, 422)


class TestSetActiveVersionAPI:
    def test_set_active(self, client, service, sample_jsonl, sample_jsonl_v2):
        dataset = service.create_dataset(name="api-setact", content_jsonl=sample_jsonl)
        v2 = service.add_version(dataset.id, sample_jsonl_v2)
        resp = client.put(f"/datasets/{dataset.id}/active-version", json={
            "version_id": dataset.versions[0].id,
        })
        assert resp.status_code == 200
        body = resp.json()
        assert body["version_number"] == 1
        assert body["is_active"] is True

    def test_set_active_nonexistent_version(self, client, service, sample_jsonl):
        dataset = service.create_dataset(name="api-setbad", content_jsonl=sample_jsonl)
        resp = client.put(f"/datasets/{dataset.id}/active-version", json={
            "version_id": "fake",
        })
        assert resp.status_code == 400


class TestDeleteDatasetAPI:
    def test_delete_existing(self, client, service, sample_jsonl):
        dataset = service.create_dataset(name="api-del", content_jsonl=sample_jsonl)
        resp = client.delete(f"/datasets/{dataset.id}")
        assert resp.status_code == 200
        assert resp.json()["message"] == "Dataset deleted successfully"
        # Verify it's gone
        resp2 = client.get(f"/datasets/{dataset.id}")
        assert resp2.status_code == 404

    def test_delete_nonexistent(self, client):
        resp = client.delete("/datasets/nonexistent")
        assert resp.status_code == 404


class TestBackwardCompatibility:
    def test_get_datasets_still_works(self, client, service, sample_jsonl):
        """The GET /datasets endpoint should still work as before."""
        service.create_dataset(name="compat", content_jsonl=sample_jsonl)
        resp = client.get("/datasets")
        assert resp.status_code == 200
        assert "datasets" in resp.json()

    def test_response_shape兼容(self, client, service, sample_jsonl):
        """Response should include both old and new fields."""
        dataset = service.create_dataset(name="shape", content_jsonl=sample_jsonl)
        resp = client.get(f"/datasets/{dataset.id}")
        body = resp.json()
        # Old fields
        assert "id" in body
        assert "name" in body
        # New fields
        assert "description" in body
        assert "tags" in body
        assert "versions" in body
        assert "active_version" in body
        assert "created_at" in body
        assert "updated_at" in body

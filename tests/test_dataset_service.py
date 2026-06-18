import json
import pytest
from app.services.dataset_service import DatasetService
from app.database.connection import get_db
from app.database.models import DatasetDB, DatasetVersionDB


@pytest.fixture
def service():
    return DatasetService()


@pytest.fixture
def sample_jsonl():
    data = [
        {"id": "s1", "input": "What is 2+2?", "expected_output": "4"},
        {"id": "s2", "input": "What is 3+3?", "expected_output": "6"},
    ]
    return "\n".join(json.dumps(item) for item in data)


@pytest.fixture
def sample_jsonl_v2():
    data = [
        {"id": "s1", "input": "What is 2+2?", "expected_output": "4"},
        {"id": "s2", "input": "What is 3+3?", "expected_output": "6"},
        {"id": "s3", "input": "What is 4+4?", "expected_output": "8"},
    ]
    return "\n".join(json.dumps(item) for item in data)


class TestCreateDataset:
    def test_create_basic(self, service, sample_jsonl):
        dataset = service.create_dataset(name="test-ds", content_jsonl=sample_jsonl)
        assert dataset.id is not None
        assert dataset.name == "test-ds"
        assert dataset.latest_version_number == 1
        assert dataset.tags == []
        assert dataset.description is None

    def test_create_with_metadata(self, service, sample_jsonl):
        dataset = service.create_dataset(
            name="meta-ds",
            content_jsonl=sample_jsonl,
            description="A test dataset",
            tags=["math", "qa"],
        )
        assert dataset.description == "A test dataset"
        assert dataset.tags == ["math", "qa"]

    def test_create_stores_content(self, service, sample_jsonl):
        dataset = service.create_dataset(name="content-ds", content_jsonl=sample_jsonl)
        fetched = service.get_dataset(dataset.id)
        version_id = fetched.versions[0].id
        content = service.get_version_content(version_id)
        assert content == sample_jsonl

    def test_create_empty_content_raises(self, service):
        with pytest.raises(ValueError, match="empty"):
            service.create_dataset(name="empty", content_jsonl="")

    def test_create_whitespace_only_raises(self, service):
        with pytest.raises(ValueError, match="empty"):
            service.create_dataset(name="ws", content_jsonl="   \n  \n  ")

    def test_create_invalid_json_raises(self, service):
        with pytest.raises(ValueError, match="Error parsing line"):
            service.create_dataset(name="bad", content_jsonl="not json")

    def test_create_missing_fields_raises(self, service):
        content = json.dumps({"id": "x", "input": "hello"})
        with pytest.raises(ValueError, match="Error parsing line"):
            service.create_dataset(name="bad", content_jsonl=content)

    def test_create_version_is_active(self, service, sample_jsonl):
        dataset = service.create_dataset(name="active-check", content_jsonl=sample_jsonl)
        active = service.get_active_version(dataset.id)
        assert active is not None
        assert active.version_number == 1
        assert active.is_active is True
        assert active.example_count == 2


class TestListDatasets:
    def test_list_empty(self, service):
        datasets = service.list_datasets()
        assert datasets == []

    def test_list_returns_all(self, service, sample_jsonl):
        service.create_dataset(name="ds1", content_jsonl=sample_jsonl)
        service.create_dataset(name="ds2", content_jsonl=sample_jsonl)
        datasets = service.list_datasets()
        assert len(datasets) == 2

    def test_list_filter_by_tag(self, service, sample_jsonl):
        service.create_dataset(name="tagged", content_jsonl=sample_jsonl, tags=["math"])
        service.create_dataset(name="untagged", content_jsonl=sample_jsonl)
        tagged = service.list_datasets(tag="math")
        assert len(tagged) == 1
        assert tagged[0].name == "tagged"

    def test_list_search_by_name(self, service, sample_jsonl):
        service.create_dataset(name="geography-questions", content_jsonl=sample_jsonl)
        service.create_dataset(name="math-questions", content_jsonl=sample_jsonl)
        results = service.list_datasets(search="geo")
        assert len(results) == 1
        assert results[0].name == "geography-questions"


class TestGetDataset:
    def test_get_existing(self, service, sample_jsonl):
        dataset = service.create_dataset(name="get-test", content_jsonl=sample_jsonl)
        fetched = service.get_dataset(dataset.id)
        assert fetched is not None
        assert fetched.id == dataset.id
        assert fetched.name == "get-test"

    def test_get_nonexistent(self, service):
        result = service.get_dataset("nonexistent-id")
        assert result is None


class TestVersioning:
    def test_add_version(self, service, sample_jsonl, sample_jsonl_v2):
        dataset = service.create_dataset(name="vtest", content_jsonl=sample_jsonl)
        version2 = service.add_version(dataset.id, sample_jsonl_v2)
        assert version2.version_number == 2
        assert version2.example_count == 3
        assert version2.is_active is True

    def test_add_version_deactivates_previous(self, service, sample_jsonl, sample_jsonl_v2):
        dataset = service.create_dataset(name="deact", content_jsonl=sample_jsonl)
        service.add_version(dataset.id, sample_jsonl_v2)
        active = service.get_active_version(dataset.id)
        assert active.version_number == 2

    def test_add_version_updates_counter(self, service, sample_jsonl, sample_jsonl_v2):
        dataset = service.create_dataset(name="counter", content_jsonl=sample_jsonl)
        service.add_version(dataset.id, sample_jsonl_v2)
        refreshed = service.get_dataset(dataset.id)
        assert refreshed.latest_version_number == 2

    def test_add_version_nonexistent_dataset_raises(self, service, sample_jsonl):
        with pytest.raises(ValueError, match="not found"):
            service.add_version("fake-id", sample_jsonl)

    def test_add_version_empty_content_raises(self, service, sample_jsonl):
        dataset = service.create_dataset(name="vempty", content_jsonl=sample_jsonl)
        with pytest.raises(ValueError, match="empty"):
            service.add_version(dataset.id, "")

    def test_immutable_versions(self, service, sample_jsonl, sample_jsonl_v2):
        dataset = service.create_dataset(name="imm", content_jsonl=sample_jsonl)
        fetched = service.get_dataset(dataset.id)
        v1_id = fetched.versions[0].id
        service.add_version(dataset.id, sample_jsonl_v2)
        v1_content = service.get_version_content(v1_id)
        assert v1_content == sample_jsonl


class TestSetActiveVersion:
    def test_set_active(self, service, sample_jsonl, sample_jsonl_v2):
        dataset = service.create_dataset(name="setact", content_jsonl=sample_jsonl)
        service.add_version(dataset.id, sample_jsonl_v2)
        fetched = service.get_dataset(dataset.id)
        v1_id = fetched.versions[0].id
        service.set_active_version(dataset.id, v1_id)
        active = service.get_active_version(dataset.id)
        assert active.version_number == 1

    def test_set_active_nonexistent_version_raises(self, service, sample_jsonl):
        dataset = service.create_dataset(name="setbad", content_jsonl=sample_jsonl)
        with pytest.raises(ValueError, match="not found"):
            service.set_active_version(dataset.id, "fake-version-id")

    def test_set_active_wrong_dataset_raises(self, service, sample_jsonl):
        ds1 = service.create_dataset(name="ds1", content_jsonl=sample_jsonl)
        ds2 = service.create_dataset(name="ds2", content_jsonl=sample_jsonl)
        ds2_fetched = service.get_dataset(ds2.id)
        with pytest.raises(ValueError, match="not found"):
            service.set_active_version(ds1.id, ds2_fetched.versions[0].id)


class TestDeleteDataset:
    def test_delete_existing(self, service, sample_jsonl):
        dataset = service.create_dataset(name="del", content_jsonl=sample_jsonl)
        result = service.delete_dataset(dataset.id)
        assert result is True
        assert service.get_dataset(dataset.id) is None

    def test_delete_nonexistent(self, service):
        result = service.delete_dataset("nope")
        assert result is False

    def test_delete_cascades_versions(self, service, sample_jsonl):
        dataset = service.create_dataset(name="delcasc", content_jsonl=sample_jsonl)
        fetched = service.get_dataset(dataset.id)
        version_id = fetched.versions[0].id
        service.delete_dataset(dataset.id)
        with get_db() as db:
            v = db.query(DatasetVersionDB).filter(DatasetVersionDB.id == version_id).first()
            assert v is None


class TestUploadDataset:
    def test_upload_jsonl(self, service, sample_jsonl):
        dataset = service.upload_dataset(
            name="upload",
            file_content=sample_jsonl.encode("utf-8"),
            filename="data.jsonl",
        )
        assert dataset.name == "upload"
        fetched = service.get_dataset(dataset.id)
        assert fetched.versions[0].example_count == 2

    def test_upload_non_jsonl_raises(self, service):
        with pytest.raises(ValueError, match="JSONL"):
            service.upload_dataset(
                name="bad",
                file_content=b"data",
                filename="data.csv",
            )

    def test_upload_non_utf8_raises(self, service):
        with pytest.raises(ValueError, match="UTF-8"):
            service.upload_dataset(
                name="bad",
                file_content=b"\xff\xfe",
                filename="data.jsonl",
            )


class TestLoadExamples:
    def test_load_from_version(self, service, sample_jsonl):
        dataset = service.create_dataset(name="loadtest", content_jsonl=sample_jsonl)
        fetched = service.get_dataset(dataset.id)
        examples = service.load_examples_from_version(fetched.versions[0].id)
        assert len(examples) == 2
        assert examples[0].id == "s1"
        assert examples[0].input == "What is 2+2?"

    def test_load_nonexistent_version_raises(self, service):
        with pytest.raises(ValueError, match="not found"):
            service.load_examples_from_version("fake-id")

    def test_auto_generated_ids(self, service):
        content = json.dumps({"input": "q1", "expected_output": "a1"})
        dataset = service.create_dataset(name="autoids", content_jsonl=content)
        fetched = service.get_dataset(dataset.id)
        examples = service.load_examples_from_version(fetched.versions[0].id)
        assert examples[0].id == "example_1"


class TestParseJsonl:
    def test_parse_basic(self):
        content = '{"id": "x", "input": "q", "expected_output": "a"}'
        examples = DatasetService._parse_jsonl(content)
        assert len(examples) == 1
        assert examples[0].id == "x"

    def test_parse_with_metadata(self):
        content = '{"input": "q", "expected_output": "a", "metadata": {"cat": "test"}}'
        examples = DatasetService._parse_jsonl(content)
        assert examples[0].metadata == {"cat": "test"}

    def test_parse_multiple_lines(self):
        content = '{"input": "q1", "expected_output": "a1"}\n{"input": "q2", "expected_output": "a2"}'
        examples = DatasetService._parse_jsonl(content)
        assert len(examples) == 2

    def test_parse_skips_blank_lines(self):
        content = '{"input": "q1", "expected_output": "a1"}\n\n\n{"input": "q2", "expected_output": "a2"}'
        examples = DatasetService._parse_jsonl(content)
        assert len(examples) == 2

    def test_parse_empty_string(self):
        examples = DatasetService._parse_jsonl("")
        assert examples == []

    def test_parse_auto_ids(self):
        content = '{"input": "q1", "expected_output": "a1"}\n{"input": "q2", "expected_output": "a2"}'
        examples = DatasetService._parse_jsonl(content)
        assert examples[0].id == "example_1"
        assert examples[1].id == "example_2"

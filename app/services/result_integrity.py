"""Expected-suite denominators and conservative handling of legacy duplicates."""

from collections import Counter

from app.database.models import DatasetVersionDB
from app.services.run_configuration import case_manifest


def expected_cases(db, run, records):
    snapshot = ((run.run_configuration or {}).get("dataset") or {}) if run else {}
    ids = snapshot.get("expected_case_ids")
    if ids is None and run and run.dataset_version_id:
        version = db.get(DatasetVersionDB, run.dataset_version_id)
        if version:
            try:
                ids = case_manifest(version.content)["expected_case_ids"]
            except ValueError:
                pass
    return set(ids) if ids is not None else {r.example_id for r in records}, ids is not None


def authoritative_records(records, expected):
    counts = Counter(r.example_id for r in records)
    valid = [r for r in records if r.example_id in expected and counts[r.example_id] == 1]
    ambiguous = sum(count > 1 for key, count in counts.items() if key in expected)
    unexpected = len(set(counts) - expected)
    return valid, ambiguous, unexpected

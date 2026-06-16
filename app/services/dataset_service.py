import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import structlog
from json_repair import repair_json
from sqlalchemy import or_
from sqlalchemy.orm import Session, selectinload

from app.database.connection import get_db
from app.database.models import DatasetDB, DatasetVersionDB
from app.schemas.example import EvaluationExample

log = structlog.get_logger()


class DatasetService:
    """Service layer for dataset management operations."""

    def list_datasets(
        self,
        tag: Optional[str] = None,
        search: Optional[str] = None,
    ) -> List[DatasetDB]:
        """List all datasets with optional tag filter and name search."""
        with get_db() as db:
            query = db.query(DatasetDB).options(selectinload(DatasetDB.versions))
            if tag:
                query = query.filter(DatasetDB.tags_json.contains(f'"{tag}"'))
            if search:
                query = query.filter(DatasetDB.name.ilike(f"%{search}%"))
            return query.order_by(DatasetDB.created_at.desc()).all()

    def get_dataset(self, dataset_id: str) -> Optional[DatasetDB]:
        """Get a single dataset by ID with its versions loaded."""
        with get_db() as db:
            return db.query(DatasetDB).options(
                selectinload(DatasetDB.versions)
            ).filter(DatasetDB.id == dataset_id).first()

    def create_dataset(
        self,
        name: str,
        content_jsonl: str,
        description: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ) -> DatasetDB:
        """
        Create a new dataset with its first version.

        Args:
            name: Human-readable dataset name.
            content_jsonl: JSONL string content (one JSON object per line).
            description: Optional description.
            tags: Optional list of tag strings.

        Returns:
            The created DatasetDB record.

        Raises:
            ValueError: If content_jsonl is empty or malformed.
        """
        examples = self._parse_jsonl(content_jsonl)
        if not examples:
            raise ValueError("Dataset content is empty or contains no valid examples.")

        dataset_id = str(uuid.uuid4())
        version_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)

        with get_db() as db:
            dataset = DatasetDB(
                id=dataset_id,
                name=name,
                description=description,
                latest_version_number=1,
                created_at=now,
                updated_at=now,
            )
            if tags:
                dataset.tags = tags
            db.add(dataset)

            version = DatasetVersionDB(
                id=version_id,
                dataset_id=dataset_id,
                version_number=1,
                content=content_jsonl,
                example_count=len(examples),
                is_active=True,
                created_at=now,
            )
            db.add(version)
            db.commit()
            db.refresh(dataset)
            log.info("dataset_created", dataset_id=dataset_id, name=name, version=1)

        # Re-fetch with eager loading for safe detached access
        return self.get_dataset(dataset_id)

    def upload_dataset(
        self,
        name: str,
        file_content: bytes,
        filename: str,
        description: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ) -> DatasetDB:
        """
        Create a dataset from an uploaded file.

        Args:
            name: Human-readable dataset name.
            file_content: Raw bytes of the uploaded file.
            filename: Original filename (used for validation).
            description: Optional description.
            tags: Optional list of tag strings.

        Returns:
            The created DatasetDB record.

        Raises:
            ValueError: If the file is not JSONL or contains no valid examples.
        """
        if not filename.endswith(".jsonl"):
            raise ValueError("Only JSONL files are supported. Please upload a .jsonl file.")

        try:
            content_str = file_content.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError("File must be UTF-8 encoded.")

        return self.create_dataset(
            name=name,
            content_jsonl=content_str,
            description=description,
            tags=tags,
        )

    def add_version(
        self,
        dataset_id: str,
        content_jsonl: str,
    ) -> DatasetVersionDB:
        """
        Add a new immutable version to an existing dataset.
        The new version becomes the active version.

        Args:
            dataset_id: ID of the dataset to add a version to.
            content_jsonl: JSONL string content for the new version.

        Returns:
            The created DatasetVersionDB record.

        Raises:
            ValueError: If dataset not found or content is empty.
        """
        examples = self._parse_jsonl(content_jsonl)
        if not examples:
            raise ValueError("Version content is empty or contains no valid examples.")

        with get_db() as db:
            dataset = db.query(DatasetDB).options(
                selectinload(DatasetDB.versions)
            ).filter(DatasetDB.id == dataset_id).first()
            if not dataset:
                raise ValueError(f"Dataset '{dataset_id}' not found.")

            # Deactivate previous versions
            for v in dataset.versions:
                v.is_active = False

            new_version_number = dataset.latest_version_number + 1
            version_id = str(uuid.uuid4())

            version = DatasetVersionDB(
                id=version_id,
                dataset_id=dataset_id,
                version_number=new_version_number,
                content=content_jsonl,
                example_count=len(examples),
                is_active=True,
                created_at=datetime.now(timezone.utc),
            )
            db.add(version)

            dataset.latest_version_number = new_version_number
            dataset.updated_at = datetime.now(timezone.utc)

            db.commit()
            db.refresh(version)
            log.info("dataset_version_added", dataset_id=dataset_id, version=new_version_number)
            return version

    def set_active_version(
        self,
        dataset_id: str,
        version_id: str,
    ) -> DatasetVersionDB:
        """
        Set a specific version as the active version for a dataset.

        Args:
            dataset_id: ID of the dataset.
            version_id: ID of the version to activate.

        Returns:
            The activated DatasetVersionDB record.

        Raises:
            ValueError: If dataset or version not found, or version doesn't belong to dataset.
        """
        with get_db() as db:
            dataset = db.query(DatasetDB).options(
                selectinload(DatasetDB.versions)
            ).filter(DatasetDB.id == dataset_id).first()
            if not dataset:
                raise ValueError(f"Dataset '{dataset_id}' not found.")

            version = db.query(DatasetVersionDB).filter(
                DatasetVersionDB.id == version_id,
                DatasetVersionDB.dataset_id == dataset_id,
            ).first()
            if not version:
                raise ValueError(f"Version '{version_id}' not found for dataset '{dataset_id}'.")

            # Deactivate all versions, activate the target
            for v in dataset.versions:
                v.is_active = False
            version.is_active = True

            db.commit()
            db.refresh(version)
            log.info("dataset_active_version_set", dataset_id=dataset_id, version_id=version_id)
            return version

    def delete_dataset(self, dataset_id: str) -> bool:
        """
        Delete a dataset and all its versions.

        Returns:
            True if deleted, False if not found.
        """
        with get_db() as db:
            dataset = db.query(DatasetDB).filter(DatasetDB.id == dataset_id).first()
            if not dataset:
                return False
            db.delete(dataset)
            db.commit()
            log.info("dataset_deleted", dataset_id=dataset_id)
            return True

    def get_version_content(self, version_id: str) -> Optional[str]:
        """Return the raw JSONL content string for a version."""
        with get_db() as db:
            version = db.query(DatasetVersionDB).filter(DatasetVersionDB.id == version_id).first()
            if not version:
                return None
            return version.content

    def get_active_version(self, dataset_id: str) -> Optional[DatasetVersionDB]:
        """Get the active version for a dataset."""
        with get_db() as db:
            return db.query(DatasetVersionDB).filter(
                DatasetVersionDB.dataset_id == dataset_id,
                DatasetVersionDB.is_active == True,
            ).first()

    def load_examples_from_version(self, version_id: str) -> List[EvaluationExample]:
        """
        Parse stored JSONL content into EvaluationExample objects.

        Args:
            version_id: ID of the version to load.

        Returns:
            List of EvaluationExample objects.

        Raises:
            ValueError: If version not found or content is malformed.
        """
        content = self.get_version_content(version_id)
        if content is None:
            raise ValueError(f"Version '{version_id}' not found.")
        return self._parse_jsonl(content)

    @staticmethod
    def _parse_jsonl(content: str) -> List[EvaluationExample]:
        """
        Parse a JSONL string into EvaluationExample objects.

        Each line must be a JSON object with at minimum 'input' and 'expected_output' fields.
        An 'id' field is auto-generated if missing.
        Uses json_repair to handle malformed JSON lines gracefully.
        """
        examples: List[EvaluationExample] = []
        for line_num, line in enumerate(content.strip().splitlines(), 1):
            line_str = line.strip()
            if not line_str:
                continue
            try:
                data = repair_json(line_str, return_objects=True)
                if not isinstance(data, dict):
                    raise ValueError(f"Expected a JSON object, got {type(data).__name__}")
                if "id" not in data:
                    data["id"] = f"example_{line_num}"
                if "input" not in data or "expected_output" not in data:
                    raise KeyError("Line must contain 'input' and 'expected_output' fields.")
                examples.append(EvaluationExample(**data))
            except Exception as e:
                raise ValueError(f"Error parsing line {line_num}: {e}")
        return examples

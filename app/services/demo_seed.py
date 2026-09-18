"""Idempotent mock-only demo data for a five-minute agency walkthrough."""

import uuid
from datetime import datetime, timezone

from app.database.connection import get_db
from app.database.models import DatasetDB, DatasetVersionDB, ProjectDB


DEMO_CLIENT_NAME = "Northstar Demo Client"
DEMO_PROJECT_NAME = "Customer Support Copilot"
DEMO_PROJECT_DESCRIPTION = "Seeded mock-only demo workspace for agency walkthroughs."
DEMO_DATASETS = {
    "Support Regression Set": """{\"id\": \"support-1\", \"input\": \"How do I reset my password?\", \"expected_output\": \"Use the password reset link on the sign-in page.\"}
{\"id\": \"support-2\", \"input\": \"Where can I download invoices?\", \"expected_output\": \"Invoices are available in Billing settings.\"}
{\"id\": \"support-3\", \"input\": \"How can I contact support?\", \"expected_output\": \"Contact support through the help center.\"}
""",
    "Knowledge Retrieval Set": """{\"id\": \"retrieval-1\", \"input\": \"What is the return period?\", \"expected_output\": \"The return period is 30 days.\"}
{\"id\": \"retrieval-2\", \"input\": \"Does the plan include analytics?\", \"expected_output\": \"Analytics are included on the Pro plan.\"}
{\"id\": \"retrieval-3\", \"input\": \"Can users be invited to a workspace?\", \"expected_output\": \"Workspace admins can invite users by email.\"}
""",
}


class DemoSeedService:
    def seed(self) -> dict[str, object]:
        """Create the demo project and datasets once; safe to call repeatedly."""
        now = datetime.now(timezone.utc)
        with get_db() as db:
            project = (
                db.query(ProjectDB)
                .filter(
                    ProjectDB.client_name == DEMO_CLIENT_NAME,
                    ProjectDB.name == DEMO_PROJECT_NAME,
                )
                .first()
            )
            if not project:
                project = ProjectDB(
                    id=str(uuid.uuid4()),
                    name=DEMO_PROJECT_NAME,
                    client_name=DEMO_CLIENT_NAME,
                    description=DEMO_PROJECT_DESCRIPTION,
                    created_at=now,
                    updated_at=now,
                )
                project.tags = ["demo", "mock-only", "agency"]
                db.add(project)
                db.flush()

            dataset_ids = []
            for name, content in DEMO_DATASETS.items():
                dataset = (
                    db.query(DatasetDB)
                    .filter(DatasetDB.project_id == project.id, DatasetDB.name == name)
                    .first()
                )
                if not dataset:
                    dataset = DatasetDB(
                        id=str(uuid.uuid4()),
                        name=name,
                        description="Seeded sample dataset for the agency demo.",
                        project_id=project.id,
                        latest_version_number=1,
                        created_at=now,
                        updated_at=now,
                    )
                    dataset.tags = ["demo", "mock-only"]
                    db.add(dataset)
                    db.flush()
                    db.add(
                        DatasetVersionDB(
                            id=str(uuid.uuid4()),
                            dataset_id=dataset.id,
                            version_number=1,
                            content=content,
                            example_count=len(content.strip().splitlines()),
                            is_active=True,
                            created_at=now,
                        )
                    )
                dataset_ids.append(dataset.id)
            db.commit()

            return {
                "project_id": project.id,
                "project_name": project.name,
                "client_name": project.client_name,
                "dataset_ids": dataset_ids,
                "message": "Demo workspace is ready. Use the mock provider for a clearly simulated run.",
            }

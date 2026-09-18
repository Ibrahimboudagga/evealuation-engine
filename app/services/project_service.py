"""Project and client management for agency evaluation workspaces."""

import uuid
from datetime import datetime, timezone
from typing import List, Optional

from app.database.connection import get_db
from app.database.models import DatasetDB, EvaluationRunDB, PairwiseRunDB, ProjectDB


class ProjectService:
    def list_projects(self) -> List[ProjectDB]:
        with get_db() as db:
            return db.query(ProjectDB).order_by(ProjectDB.client_name, ProjectDB.name).all()

    def get_project(self, project_id: str) -> Optional[ProjectDB]:
        with get_db() as db:
            return db.query(ProjectDB).filter(ProjectDB.id == project_id).first()

    def create_project(
        self,
        name: str,
        client_name: str,
        description: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ) -> ProjectDB:
        project = ProjectDB(
            id=str(uuid.uuid4()),
            name=name,
            client_name=client_name,
            description=description,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        project.tags = tags or []
        with get_db() as db:
            db.add(project)
            db.commit()
            db.refresh(project)
            return project

    def update_project(
        self,
        project_id: str,
        *,
        name: Optional[str] = None,
        client_name: Optional[str] = None,
        description: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ) -> Optional[ProjectDB]:
        with get_db() as db:
            project = db.query(ProjectDB).filter(ProjectDB.id == project_id).first()
            if not project:
                return None
            if name is not None:
                project.name = name
            if client_name is not None:
                project.client_name = client_name
            if description is not None:
                project.description = description
            if tags is not None:
                project.tags = tags
            project.updated_at = datetime.now(timezone.utc)
            db.commit()
            db.refresh(project)
            return project

    def delete_project(self, project_id: str) -> bool:
        """Unassign records before deletion so historical runs remain available."""
        with get_db() as db:
            project = db.query(ProjectDB).filter(ProjectDB.id == project_id).first()
            if not project:
                return False
            for model in (DatasetDB, EvaluationRunDB, PairwiseRunDB):
                db.query(model).filter(model.project_id == project_id).update(
                    {"project_id": None}, synchronize_session=False
                )
            db.delete(project)
            db.commit()
            return True

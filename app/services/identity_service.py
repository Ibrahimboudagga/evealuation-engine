"""Workspace membership and local bearer-token authentication."""

import hashlib
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from app.database.connection import get_db
from app.database.models import MembershipDB, ProjectDB, UserDB, WorkspaceDB


ROLE_OWNER = "owner"
ROLE_EDITOR = "editor"
ROLE_VIEWER = "viewer"
ROLE_CLIENT_VIEWER = "client_viewer"
VALID_ROLES = {ROLE_OWNER, ROLE_EDITOR, ROLE_VIEWER, ROLE_CLIENT_VIEWER}
WRITE_ROLES = {ROLE_OWNER, ROLE_EDITOR}
OWNER_ROLES = {ROLE_OWNER}


@dataclass(frozen=True)
class AuthContext:
    user_id: str
    email: str
    workspace_id: str
    role: str


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _slug(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return normalized[:100] or "workspace"


class IdentityService:
    def bootstrap(self, email: str, display_name: str, workspace_name: str) -> tuple[AuthContext, str]:
        """Create the initial owner and workspace; the token is returned only once."""
        with get_db() as db:
            if db.query(UserDB.id).first():
                raise ValueError("An owner already exists. Use a workspace membership endpoint instead.")
            token = secrets.token_urlsafe(32)
            now = datetime.now(timezone.utc)
            workspace = WorkspaceDB(id=str(uuid.uuid4()), name=workspace_name, slug=_slug(workspace_name), created_at=now)
            user = UserDB(
                id=str(uuid.uuid4()),
                email=email.lower().strip(),
                display_name=display_name.strip(),
                api_token_hash=_token_hash(token),
                created_at=now,
            )
            membership = MembershipDB(
                id=str(uuid.uuid4()),
                workspace_id=workspace.id,
                user_id=user.id,
                role=ROLE_OWNER,
                created_at=now,
            )
            db.add_all([workspace, user, membership])
            # The first owner claims pre-workspace records during migration.
            db.query(ProjectDB).filter(ProjectDB.workspace_id.is_(None)).update(
                {"workspace_id": workspace.id}, synchronize_session=False
            )
            db.commit()
            return AuthContext(user.id, user.email, workspace.id, ROLE_OWNER), token

    def authenticate(self, token: str, workspace_id: Optional[str] = None) -> AuthContext:
        with get_db() as db:
            user = db.query(UserDB).filter(UserDB.api_token_hash == _token_hash(token)).first()
            if not user:
                raise ValueError("Invalid API token")
            memberships = db.query(MembershipDB).filter(MembershipDB.user_id == user.id).all()
            if not memberships:
                raise ValueError("User has no workspace membership")
            if workspace_id:
                membership = next((item for item in memberships if item.workspace_id == workspace_id), None)
                if not membership:
                    raise ValueError("User is not a member of the selected workspace")
            elif len(memberships) == 1:
                membership = memberships[0]
            else:
                raise ValueError("X-Workspace-ID is required when a user belongs to multiple workspaces")
            return AuthContext(user.id, user.email, membership.workspace_id, membership.role)

    def has_users(self) -> bool:
        with get_db() as db:
            return db.query(UserDB.id).first() is not None

    def add_member(self, context: AuthContext, email: str, display_name: str, role: str) -> tuple[AuthContext, str]:
        if context.role not in OWNER_ROLES:
            raise PermissionError("Only workspace owners can add members")
        if role not in VALID_ROLES:
            raise ValueError(f"Role must be one of: {', '.join(sorted(VALID_ROLES))}")
        with get_db() as db:
            user = db.query(UserDB).filter(UserDB.email == email.lower().strip()).first()
            token = ""
            if not user:
                token = secrets.token_urlsafe(32)
                user = UserDB(
                    id=str(uuid.uuid4()),
                    email=email.lower().strip(),
                    display_name=display_name.strip(),
                    api_token_hash=_token_hash(token),
                    created_at=datetime.now(timezone.utc),
                )
                db.add(user)
                db.flush()
            membership = db.query(MembershipDB).filter(
                MembershipDB.workspace_id == context.workspace_id,
                MembershipDB.user_id == user.id,
            ).first()
            if membership:
                membership.role = role
            else:
                membership = MembershipDB(
                    id=str(uuid.uuid4()), workspace_id=context.workspace_id, user_id=user.id,
                    role=role, created_at=datetime.now(timezone.utc)
                )
                db.add(membership)
            db.commit()
            return AuthContext(user.id, user.email, context.workspace_id, role), token

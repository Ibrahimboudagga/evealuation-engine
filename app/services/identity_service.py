"""Workspace membership and local bearer-token authentication."""

import hashlib
import hmac
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.exc import IntegrityError

from app.database.connection import get_db
from app.config import get_settings
from app.database.models import (
    AuthThrottleDB,
    MembershipDB,
    ProjectAccessDB,
    ProjectDB,
    UserDB,
    UserSessionDB,
    WorkspaceDB,
)
from app.services.activation_service import ActivationService


ROLE_OWNER = "owner"
ROLE_EDITOR = "editor"
ROLE_VIEWER = "viewer"
ROLE_CLIENT_VIEWER = "client_viewer"
VALID_ROLES = {ROLE_OWNER, ROLE_EDITOR, ROLE_VIEWER, ROLE_CLIENT_VIEWER}
WRITE_ROLES = {ROLE_OWNER, ROLE_EDITOR}
OWNER_ROLES = {ROLE_OWNER}


class LastWorkspaceOwnerError(ValueError):
    """Raised when a membership mutation would leave a workspace ownerless."""


class AuthenticationRateLimitedError(ValueError):
    """Raised after repeated failed sign-in attempts for one identity."""


@dataclass(frozen=True)
class AuthContext:
    user_id: str
    email: str
    workspace_id: str
    role: str


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _normalized_email(email: str) -> str:
    return email.lower().strip()


def _auth_subject_hash(email: str) -> str:
    settings = get_settings()
    secret = settings.auth_throttle_secret or settings.bootstrap_secret
    if not secret:
        # Production validation rejects this fallback. It keeps local setup and
        # tests usable while still avoiding plaintext identity storage.
        secret = "evaluation-engine-development-auth-throttle"
    return hmac.new(secret.encode("utf-8"), _normalized_email(email).encode("utf-8"), hashlib.sha256).hexdigest()


def _slug(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return normalized[:100] or "workspace"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _password_hash(password: str, salt: Optional[str] = None) -> str:
    if len(password) < 12:
        raise ValueError("Password must contain at least 12 characters.")
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 600_000).hex()
    return f"pbkdf2_sha256$600000${salt}${digest}"


def _verify_password(password: str, encoded: Optional[str]) -> bool:
    if not encoded:
        return False
    try:
        _, _, salt, _ = encoded.split("$", 3)
        return secrets.compare_digest(_password_hash(password, salt), encoded)
    except ValueError:
        return False


_DUMMY_PASSWORD_HASH = _password_hash("invalid-identity-password")


class IdentityService:
    @staticmethod
    def _locked_throttle(db, subject_hash: str, current_time: datetime) -> AuthThrottleDB:
        """Create the privacy-safe bucket once, then lock it for atomic updates."""
        values = {
            "subject_hash": subject_hash,
            "failure_count": 0,
            "window_started_at": current_time,
            "updated_at": current_time,
        }
        dialect = db.get_bind().dialect.name
        if dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
            db.execute(insert(AuthThrottleDB).values(**values).on_conflict_do_nothing(
                index_elements=[AuthThrottleDB.subject_hash]
            ))
        elif dialect == "sqlite":
            from sqlalchemy.dialects.sqlite import insert
            db.execute(insert(AuthThrottleDB).values(**values).on_conflict_do_nothing(
                index_elements=[AuthThrottleDB.subject_hash]
            ))
        else:
            try:
                with db.begin_nested():
                    db.add(AuthThrottleDB(**values))
                    db.flush()
            except IntegrityError:
                pass
        return (
            db.query(AuthThrottleDB)
            .filter(AuthThrottleDB.subject_hash == subject_hash)
            .with_for_update()
            .one()
        )

    @staticmethod
    def _lock_workspace_owners(db, workspace_id: str) -> list[MembershipDB]:
        """Lock owner memberships before a role/removal decision.

        PostgreSQL serializes competing owner mutations through these row
        locks. SQLite serializes writes at the database level.
        """
        return (
            db.query(MembershipDB)
            .filter(MembershipDB.workspace_id == workspace_id, MembershipDB.role == ROLE_OWNER)
            .with_for_update()
            .all()
        )

    def _assert_owner_remains(self, db, membership: MembershipDB, next_role: Optional[str]) -> None:
        if membership.role != ROLE_OWNER or next_role == ROLE_OWNER:
            return
        owners = self._lock_workspace_owners(db, membership.workspace_id)
        if len(owners) <= 1:
            raise LastWorkspaceOwnerError(
                "Assign another workspace owner before demoting or removing the last owner."
            )

    def accessible_project_ids(self, context: AuthContext) -> list[str]:
        with get_db() as db:
            query = db.query(ProjectDB.id).filter(ProjectDB.workspace_id == context.workspace_id)
            if context.role == ROLE_CLIENT_VIEWER:
                query = query.join(ProjectAccessDB, ProjectAccessDB.project_id == ProjectDB.id).join(
                    MembershipDB, MembershipDB.id == ProjectAccessDB.membership_id).filter(
                    MembershipDB.user_id == context.user_id, MembershipDB.workspace_id == context.workspace_id)
            return [row[0] for row in query.all()]

    def members(self, workspace_id: str):
        with get_db() as db:
            members = db.query(MembershipDB).filter(MembershipDB.workspace_id == workspace_id).all()
            for member in members:
                _ = member.user.email
            return members

    def update_member_role(self, workspace_id: str, user_id: str, role: str) -> bool:
        with get_db() as db:
            item = db.query(MembershipDB).filter(MembershipDB.workspace_id == workspace_id, MembershipDB.user_id == user_id).first()
            if not item or role not in VALID_ROLES:
                return False
            self._assert_owner_remains(db, item, role)
            item.role = role
            db.commit()
            return True

    def remove_member(self, workspace_id: str, user_id: str) -> bool:
        with get_db() as db:
            item = db.query(MembershipDB).filter(MembershipDB.workspace_id == workspace_id, MembershipDB.user_id == user_id).first()
            if not item:
                return False
            self._assert_owner_remains(db, item, None)
            db.delete(item)
            db.commit()
            return True

    def grant_project_access(self, workspace_id: str, user_id: str, project_id: str) -> bool:
        with get_db() as db:
            member = db.query(MembershipDB).filter(MembershipDB.workspace_id == workspace_id, MembershipDB.user_id == user_id).first()
            project = db.query(ProjectDB).filter(ProjectDB.id == project_id, ProjectDB.workspace_id == workspace_id).first()
            if not member or not project: return False
            if not db.query(ProjectAccessDB).filter(ProjectAccessDB.membership_id == member.id, ProjectAccessDB.project_id == project_id).first():
                db.add(ProjectAccessDB(id=str(uuid.uuid4()), membership_id=member.id, project_id=project_id, created_at=_now()))
                db.commit()
            return True
    def bootstrap(self, email: str, display_name: str, workspace_name: str, password: Optional[str] = None) -> tuple[AuthContext, str]:
        """Create the initial owner and workspace; the token is returned only once."""
        with get_db() as db:
            if db.query(UserDB.id).first():
                raise ValueError("An owner already exists. Use a workspace membership endpoint instead.")
            token = secrets.token_urlsafe(32)
            now = _now()
            workspace = WorkspaceDB(id=str(uuid.uuid4()), name=workspace_name, slug=_slug(workspace_name), created_at=now)
            user = UserDB(
                id=str(uuid.uuid4()),
                email=_normalized_email(email),
                display_name=display_name.strip(),
                api_token_hash=_token_hash(token),
                password_hash=_password_hash(password) if password else None,
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
            db.add(UserSessionDB(id=str(uuid.uuid4()), user_id=user.id, token_hash=_token_hash(token),
                                 expires_at=now + timedelta(hours=12), created_at=now))
            # The first owner claims pre-workspace records during migration.
            db.query(ProjectDB).filter(ProjectDB.workspace_id.is_(None)).update(
                {"workspace_id": workspace.id}, synchronize_session=False
            )
            db.commit()
            context = AuthContext(user.id, user.email, workspace.id, ROLE_OWNER)
        ActivationService().record_first(context.workspace_id, "workspace_created")
        return context, token

    def authenticate(self, token: str, workspace_id: Optional[str] = None, allow_legacy: bool = False) -> AuthContext:
        with get_db() as db:
            session = db.query(UserSessionDB).filter(UserSessionDB.token_hash == _token_hash(token)).first()
            if session and (session.revoked_at is not None or session.expires_at <= _now()):
                raise ValueError("Session has expired. Sign in again.")
            user = session.user if session else db.query(UserDB).filter(UserDB.api_token_hash == _token_hash(token)).first()
            if not user:
                raise ValueError("Invalid API token")
            if not session and (not allow_legacy or user.password_hash):
                raise ValueError("Legacy tokens can only set an initial password; sign in for normal access.")
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

    def sign_in(self, email: str, password: str, workspace_id: Optional[str] = None) -> tuple[AuthContext, str, datetime]:
        with get_db() as db:
            settings = get_settings()
            current_time = _now()
            subject_hash = _auth_subject_hash(email)
            throttle = self._locked_throttle(db, subject_hash, current_time)
            if throttle and throttle.locked_until and throttle.locked_until > current_time:
                raise AuthenticationRateLimitedError("Too many sign-in attempts. Try again later.")
            if throttle and throttle.window_started_at <= current_time - timedelta(
                seconds=settings.auth_failure_window_seconds
            ):
                throttle.failure_count = 0
                throttle.window_started_at = current_time
                throttle.locked_until = None

            user = db.query(UserDB).filter(UserDB.email == _normalized_email(email)).first()
            password_matches = _verify_password(password, user.password_hash if user else _DUMMY_PASSWORD_HASH)
            if not user or not password_matches:
                throttle.failure_count += 1
                throttle.updated_at = current_time
                if throttle.failure_count >= settings.auth_max_failed_attempts:
                    throttle.locked_until = current_time + timedelta(seconds=settings.auth_lockout_seconds)
                db.commit()
                if throttle.locked_until:
                    raise AuthenticationRateLimitedError("Too many sign-in attempts. Try again later.")
                raise ValueError("Invalid email or password")
            memberships = db.query(MembershipDB).filter(MembershipDB.user_id == user.id).all()
            if workspace_id:
                membership = next((item for item in memberships if item.workspace_id == workspace_id), None)
            elif len(memberships) == 1:
                membership = memberships[0]
            else:
                membership = None
            if not membership:
                raise ValueError("Select a workspace with X-Workspace-ID")
            db.delete(throttle)
            token = secrets.token_urlsafe(32)
            expires_at = current_time + timedelta(hours=12)
            db.add(UserSessionDB(id=str(uuid.uuid4()), user_id=user.id, token_hash=_token_hash(token), expires_at=expires_at, created_at=_now()))
            db.commit()
            return AuthContext(user.id, user.email, membership.workspace_id, membership.role), token, expires_at

    def sign_out(self, token: str) -> None:
        with get_db() as db:
            session = db.query(UserSessionDB).filter(UserSessionDB.token_hash == _token_hash(token)).first()
            if session:
                session.revoked_at = _now()
            else:
                user = db.query(UserDB).filter(UserDB.api_token_hash == _token_hash(token)).first()
                if user:
                    user.api_token_hash = _token_hash(secrets.token_urlsafe(32))
            db.commit()

    def change_password(self, user_id: str, current_password: Optional[str], new_password: str) -> None:
        with get_db() as db:
            user = db.query(UserDB).filter(UserDB.id == user_id).first()
            if not user or (user.password_hash and not _verify_password(current_password or "", user.password_hash)):
                raise ValueError("Current password is incorrect")
            user.password_hash = _password_hash(new_password)
            user.api_token_hash = _token_hash(secrets.token_urlsafe(32))
            db.query(UserSessionDB).filter(UserSessionDB.user_id == user.id).update(
                {"revoked_at": _now()}, synchronize_session=False)
            db.commit()

    def has_users(self) -> bool:
        with get_db() as db:
            return db.query(UserDB.id).first() is not None

    def add_member(self, context: AuthContext, email: str, display_name: str, role: str, initial_password: Optional[str] = None) -> tuple[AuthContext, str]:
        if context.role not in OWNER_ROLES:
            raise PermissionError("Only workspace owners can add members")
        if role not in VALID_ROLES:
            raise ValueError(f"Role must be one of: {', '.join(sorted(VALID_ROLES))}")
        with get_db() as db:
            user = db.query(UserDB).filter(UserDB.email == _normalized_email(email)).first()
            token = ""
            if user and initial_password:
                raise ValueError("Cannot assign a password to an existing identity; use its own password-change flow.")
            if not user:
                token = secrets.token_urlsafe(32)
                user = UserDB(
                    id=str(uuid.uuid4()),
                    email=_normalized_email(email),
                    display_name=display_name.strip(),
                    api_token_hash=_token_hash(token),
                password_hash=_password_hash(initial_password) if initial_password else None,
                    created_at=datetime.now(timezone.utc),
                )
                db.add(user)
                db.flush()
                db.add(UserSessionDB(id=str(uuid.uuid4()), user_id=user.id, token_hash=_token_hash(token),
                                     expires_at=_now() + timedelta(hours=12), created_at=_now()))
            membership = db.query(MembershipDB).filter(
                MembershipDB.workspace_id == context.workspace_id,
                MembershipDB.user_id == user.id,
            ).first()
            if membership:
                self._assert_owner_remains(db, membership, role)
                membership.role = role
            else:
                membership = MembershipDB(
                    id=str(uuid.uuid4()), workspace_id=context.workspace_id, user_id=user.id,
                    role=role, created_at=datetime.now(timezone.utc)
                )
                db.add(membership)
            db.commit()
            return AuthContext(user.id, user.email, context.workspace_id, role), token

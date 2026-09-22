"""Encrypted workspace-scoped provider connection management."""

import uuid
from datetime import datetime, timezone
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings
from app.database.connection import get_db
from app.database.models import ProviderConnectionDB


def _cipher() -> Fernet:
    key = get_settings().workspace_encryption_key
    if not key:
        raise ValueError("WORKSPACE_ENCRYPTION_KEY must be configured before saving provider connections.")
    try:
        return Fernet(key.encode("utf-8"))
    except (TypeError, ValueError) as error:
        raise ValueError("WORKSPACE_ENCRYPTION_KEY must be a valid Fernet key.") from error


class ProviderConnectionService:
    def create(
        self, workspace_id: str, name: str, provider: str, default_model: str,
        api_key: Optional[str] = None, credential_reference: Optional[str] = None,
        base_url: Optional[str] = None, allow_unauthenticated: bool = False,
    ) -> ProviderConnectionDB:
        if api_key and credential_reference:
            raise ValueError("Provide either api_key or credential_reference, not both.")
        if not api_key and not credential_reference and not allow_unauthenticated:
            raise ValueError("Provide api_key or credential_reference, unless an unauthenticated endpoint is explicitly allowed.")
        encrypted_api_key = _cipher().encrypt(api_key.encode("utf-8")).decode("utf-8") if api_key else None
        with get_db() as db:
            connection = ProviderConnectionDB(
                id=str(uuid.uuid4()), workspace_id=workspace_id, name=name.strip(), provider=provider,
                default_model=default_model, base_url=base_url, encrypted_api_key=encrypted_api_key,
                credential_reference=credential_reference, allow_unauthenticated=allow_unauthenticated,
                created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
            )
            db.add(connection)
            db.commit()
            db.refresh(connection)
            return connection

    def list(self, workspace_id: str) -> list[ProviderConnectionDB]:
        with get_db() as db:
            return db.query(ProviderConnectionDB).filter(
                ProviderConnectionDB.workspace_id == workspace_id
            ).order_by(ProviderConnectionDB.name).all()

    def resolve(self, workspace_id: str, connection_id: str) -> ProviderConnectionDB:
        with get_db() as db:
            connection = db.query(ProviderConnectionDB).filter(
                ProviderConnectionDB.id == connection_id,
                ProviderConnectionDB.workspace_id == workspace_id,
            ).first()
            if not connection:
                raise ValueError("Provider connection was not found in this workspace.")
            if connection.credential_reference and not connection.encrypted_api_key:
                raise ValueError("Credential references require an external secret resolver in this deployment.")
            if connection.encrypted_api_key:
                try:
                    api_key = _cipher().decrypt(connection.encrypted_api_key.encode("utf-8")).decode("utf-8")
                except InvalidToken as error:
                    raise ValueError("Provider connection credentials cannot be decrypted with the current encryption key.") from error
                connection.api_key = api_key  # transient attribute, never serialized or stored
            else:
                connection.api_key = None
            return connection

    def delete(self, workspace_id: str, connection_id: str) -> bool:
        with get_db() as db:
            connection = db.query(ProviderConnectionDB).filter(
                ProviderConnectionDB.id == connection_id,
                ProviderConnectionDB.workspace_id == workspace_id,
            ).first()
            if not connection:
                return False
            db.delete(connection)
            db.commit()
            return True

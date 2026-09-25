"""Billing-ready account service with a provider boundary for future integrations."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional, Protocol

from app.database.connection import get_db
from app.database.models import WorkspaceDB


VALID_BILLING_STATUSES = {"trial", "active", "manual", "past_due", "cancelled"}


class BillingProvider(Protocol):
    """Optional future adapter for Stripe or another billing system."""

    def sync_workspace(self, workspace_id: str) -> None:
        """Synchronize provider state without exposing payment data to core models."""


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class BillingService:
    """Operates manual trials and paid pilots without a payment-provider dependency."""

    def __init__(self, provider: Optional[BillingProvider] = None) -> None:
        self.provider = provider

    @staticmethod
    def _warnings(workspace: WorkspaceDB) -> list[str]:
        if workspace.billing_status != "trial":
            return []
        if workspace.trial_ends_at is None:
            return ["Trial end date is not set."]
        remaining = workspace.trial_ends_at.replace(tzinfo=None) - _now()
        if remaining.total_seconds() < 0:
            return ["Trial has expired. Update billing status before accepting more pilot work."]
        if remaining <= timedelta(days=7):
            days = max(0, remaining.days)
            return [f"Trial ends in {days} day{'s' if days != 1 else ''}."]
        return []

    def account(self, workspace_id: str) -> dict:
        with get_db() as db:
            workspace = db.query(WorkspaceDB).filter(WorkspaceDB.id == workspace_id).first()
            if not workspace:
                raise ValueError("Workspace not found.")
            return {
                "workspace_id": workspace.id,
                "plan": workspace.plan,
                "billing_status": workspace.billing_status,
                "trial_ends_at": workspace.trial_ends_at,
                "invoice_contact_email": workspace.invoice_contact_email,
                "warnings": self._warnings(workspace),
            }

    def update_account(
        self,
        workspace_id: str,
        *,
        plan: Optional[str] = None,
        billing_status: Optional[str] = None,
        trial_ends_at: Optional[datetime] = None,
        invoice_contact_email: Optional[str] = None,
    ) -> dict:
        if plan is not None and not plan.strip():
            raise ValueError("Plan must not be blank.")
        if billing_status is not None and billing_status not in VALID_BILLING_STATUSES:
            raise ValueError("Billing status must be trial, active, manual, past_due, or cancelled.")
        with get_db() as db:
            workspace = db.query(WorkspaceDB).filter(WorkspaceDB.id == workspace_id).first()
            if not workspace:
                raise ValueError("Workspace not found.")
            if plan is not None:
                workspace.plan = plan.strip()
            if billing_status is not None:
                workspace.billing_status = billing_status
            if trial_ends_at is not None:
                workspace.trial_ends_at = trial_ends_at.replace(tzinfo=None)
            if invoice_contact_email is not None:
                email = invoice_contact_email.strip().lower()
                if email and "@" not in email:
                    raise ValueError("Invoice contact must be a valid email address.")
                workspace.invoice_contact_email = email or None
            db.commit()
        if self.provider:
            self.provider.sync_workspace(workspace_id)
        return self.account(workspace_id)

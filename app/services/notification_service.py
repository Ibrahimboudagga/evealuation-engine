"""Safe, workspace-level notification preferences.

Delivery integrations are intentionally outside this service.  This stores only
preferences and recipient addresses; it never stores webhook secrets or report
content.  A future email or Slack adapter can consume this configuration.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from app.database.connection import get_db
from app.database.models import WorkspaceDB


NOTIFICATION_EVENTS = (
    "run_completed",
    "run_failed",
    "run_regressed",
    "report_expiring",
)

DEFAULT_SETTINGS = {
    "enabled": False,
    "recipients": [],
    "events": list(NOTIFICATION_EVENTS),
}


class NotificationService:
    """Persist validated notification preferences without delivery credentials."""

    def get(self, workspace_id: str) -> dict[str, Any]:
        with get_db() as db:
            workspace = db.query(WorkspaceDB).filter(WorkspaceDB.id == workspace_id).first()
            if not workspace:
                raise ValueError("Workspace not found.")
            try:
                stored = json.loads(workspace.notification_settings_json or "{}")
            except json.JSONDecodeError:
                stored = {}
        return {
            "enabled": bool(stored.get("enabled", DEFAULT_SETTINGS["enabled"])),
            "recipients": list(stored.get("recipients", DEFAULT_SETTINGS["recipients"])),
            "events": list(stored.get("events", DEFAULT_SETTINGS["events"])),
            "delivery_status": "configured" if stored.get("enabled") and stored.get("recipients") else "not_configured",
        }

    def update(self, workspace_id: str, values: Mapping[str, Any]) -> dict[str, Any]:
        recipients = values.get("recipients", [])
        events = values.get("events", list(NOTIFICATION_EVENTS))
        if not isinstance(recipients, list) or any(not isinstance(item, str) or "@" not in item or len(item) > 255 for item in recipients):
            raise ValueError("Notification recipients must be valid email addresses.")
        if not isinstance(events, list) or any(item not in NOTIFICATION_EVENTS for item in events):
            raise ValueError("Notification events must be supported event names.")
        enabled = bool(values.get("enabled", False))
        if enabled and not recipients:
            raise ValueError("Add at least one recipient before enabling notifications.")
        cleaned = {
            "enabled": enabled,
            "recipients": sorted(set(item.strip().lower() for item in recipients if item.strip())),
            "events": sorted(set(events)),
        }
        with get_db() as db:
            workspace = db.query(WorkspaceDB).filter(WorkspaceDB.id == workspace_id).first()
            if not workspace:
                raise ValueError("Workspace not found.")
            workspace.notification_settings_json = json.dumps(cleaned, sort_keys=True)
            db.commit()
        return self.get(workspace_id)

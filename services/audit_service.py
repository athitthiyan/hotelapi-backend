import json
from typing import Any

from sqlalchemy.orm import Session

import models


def write_audit_log(
    db: Session,
    actor_user_id: int | None,
    action: str,
    entity_type: str,
    entity_id: str | int,
    metadata: dict[str, Any] | None = None,
) -> models.AuditLog:
    log = models.AuditLog(
        actor_user_id=actor_user_id,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id),
        metadata_json=json.dumps(metadata or {}, sort_keys=True),
    )
    db.add(log)
    db.flush()
    return log

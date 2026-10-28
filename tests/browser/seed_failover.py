"""Put the fixture NVR into a failover state for the journeys: ``python -m tests.browser.seed_failover takeover`` makes it a standby that has taken over."""
from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone

from tests.browser.seed_fixtures import connect  # noqa: F401 - sets up the import path

from app import failover  # noqa: E402


def takeover() -> None:
    now = datetime.now(timezone.utc)
    with connect() as db:
        db.execute("UPDATE failover_settings SET role='standby',peer_url='https://192.168.50.20',allow_private=1")
        db.execute("INSERT INTO failover_episodes(id,epoch,role,silent_since,started_at,state) VALUES(?,1,'standby',?,?,'active')", (str(uuid.uuid4()), (now - timedelta(minutes=3)).isoformat(), (now - timedelta(minutes=2)).isoformat()))
        failover._set_state(db, takeover_active=1, takeover_started_at=(now - timedelta(minutes=2)).isoformat(), epoch=1, last_contact_at=(now - timedelta(minutes=3)).isoformat(),
                            last_sync_at=(now - timedelta(hours=1)).isoformat())
        failover._takeover_incident(db, "standby", True, {"epoch": 1})


if __name__ == "__main__":
    {"takeover": takeover}[sys.argv[1]]()

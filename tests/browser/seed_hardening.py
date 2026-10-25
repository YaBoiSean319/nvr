"""Give a fixture camera a stored ONVIF login and camera address, while a journey runs (the seeded cameras carry no login)."""
from __future__ import annotations

import argparse
import json

from tests.browser.seed_fixtures import _camera_id, connect  # noqa: F401 - sets up the import path

from app.credentials import encrypt  # noqa: E402


def weaken(camera: int, ip: str | None, onvif_user: str | None, onvif_password: str | None, device: str | None) -> dict[str, object]:
    camera_id = _camera_id(camera)
    with connect() as db:
        if ip is not None:
            db.execute("UPDATE cameras SET camera_ip=? WHERE id=?", (ip, camera_id))
        if onvif_user is not None:
            db.execute("UPDATE cameras SET motion_credentials_ciphertext=? WHERE id=?", (encrypt(json.dumps({"username": onvif_user, "password": onvif_password or ""})), camera_id))
        if device is not None:
            db.execute("UPDATE cameras SET device_service_url=? WHERE id=?", (device, camera_id))
    return {"camera_id": camera_id}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--camera", type=int, required=True)
    parser.add_argument("--ip")
    parser.add_argument("--onvif-user")
    parser.add_argument("--onvif-password")
    parser.add_argument("--device")
    options = parser.parse_args()
    print(json.dumps(weaken(options.camera, options.ip, options.onvif_user, options.onvif_password, options.device)))


if __name__ == "__main__":
    main()

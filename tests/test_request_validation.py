from __future__ import annotations

import unittest
import uuid

from pydantic import ValidationError

from app.api.onboarding import CameraBulkUpdateRequest, CameraImportRow
from app.api.policies import CameraGroupCreateRequest, CameraGroupUpdateRequest
from app.api.live_views import LiveWorkspaceRequest, ViewerLayoutRequest


class SharedRequestValidationTests(unittest.TestCase):
    def test_layout_and_workspace_share_grid_autosizing_and_tile_normalization(self):
        camera_ids = [str(uuid.uuid4()) for _ in range(5)]
        layout = ViewerLayoutRequest(camera_ids=camera_ids)
        workspace = LiveWorkspaceRequest(camera_ids=camera_ids)

        self.assertEqual(layout.grid_size, 9)
        self.assertEqual(workspace.grid_size, 9)
        self.assertEqual(layout.camera_ids, camera_ids)
        self.assertEqual(workspace.camera_ids, camera_ids)
        self.assertEqual(layout.tile_camera_ids, workspace.tile_camera_ids)
        self.assertEqual(len(layout.tile_camera_ids), layout.grid_size)

    def test_layout_and_workspace_share_duplicate_and_camera_tile_consistency_rules(self):
        repeated = str(uuid.uuid4())
        for model in (ViewerLayoutRequest, LiveWorkspaceRequest):
            with self.subTest(model=model.__name__, case="duplicate cameras"), self.assertRaisesRegex(ValidationError, "appear only once"):
                model(camera_ids=[repeated, repeated])
            with self.subTest(model=model.__name__, case="selection mismatch"), self.assertRaisesRegex(ValidationError, "selection must match"):
                model(camera_ids=[str(uuid.uuid4())], grid_size=4, tile_camera_ids=[str(uuid.uuid4()), None, None, None])
            with self.subTest(model=model.__name__, case="tile count mismatch"), self.assertRaisesRegex(ValidationError, "Tile positions must match"):
                model(camera_ids=[repeated], grid_size=4, tile_camera_ids=[repeated])

    def test_only_workspaces_allow_an_empty_grid(self):
        workspace = LiveWorkspaceRequest()
        self.assertEqual(workspace.camera_ids, [])
        self.assertEqual(workspace.tile_camera_ids, [None, None, None, None])
        with self.assertRaisesRegex(ValidationError, "must contain at least one camera"):
            ViewerLayoutRequest()

    def test_required_group_names_trim_and_reject_blank_for_create_and_update(self):
        self.assertEqual(CameraGroupCreateRequest(name="  North wing  ").name, "North wing")
        self.assertEqual(CameraGroupUpdateRequest(name="  North wing  ").name, "North wing")
        for model in (CameraGroupCreateRequest, CameraGroupUpdateRequest):
            with self.subTest(model=model.__name__), self.assertRaisesRegex(ValidationError, "Group name is required"):
                model(name="   ")

    def test_optional_group_names_share_blank_to_none_normalization(self):
        camera_id = str(uuid.uuid4())
        bulk = CameraBulkUpdateRequest(camera_ids=[camera_id], group_name="  Operations  ")
        blank_bulk = CameraBulkUpdateRequest(camera_ids=[camera_id], group_name="   ")
        imported = CameraImportRow(name=" Door ", stream_url="rtsp://camera.local/live", group_name="  Operations  ")
        blank_import = CameraImportRow(name="Door", stream_url="rtsp://camera.local/live", group_name="   ")

        self.assertEqual(bulk.group_name, "Operations")
        self.assertIsNone(blank_bulk.group_name)
        self.assertEqual(imported.group_name, "Operations")
        self.assertIsNone(blank_import.group_name)


if __name__ == "__main__":
    unittest.main()

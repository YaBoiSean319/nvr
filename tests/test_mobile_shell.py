"""The native shell's trust boundary (#299), as far as it can be checked without a device: configuration and source guards.

The native code itself has not been built or run here; these tests keep the settings that confine the privileged WebView from being undone by accident.
"""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

MOBILE = Path(__file__).resolve().parent.parent / "mobile"
ANDROID = MOBILE / "android/app/src"


class ShellTrustTests(unittest.TestCase):
    def test_no_wildcard_navigation_and_no_cleartext_in_the_shared_configuration(self):
        server = json.loads((MOBILE / "capacitor.config.json").read_text())["server"]
        self.assertNotIn("allowNavigation", server)
        self.assertNotIn("cleartext", server)

    def test_release_android_forbids_cleartext_and_only_debug_allows_it(self):
        main = (ANDROID / "main/AndroidManifest.xml").read_text()
        self.assertIn('android:usesCleartextTraffic="false"', main)
        self.assertNotIn('usesCleartextTraffic="true"', main)
        self.assertIn('usesCleartextTraffic="true"', (ANDROID / "debug/AndroidManifest.xml").read_text())

    def test_android_registers_the_plugin_and_builds_the_bridge_from_the_enrolled_origin(self):
        activity = (ANDROID / "main/java/app/nvr/mobile/MainActivity.java").read_text()
        self.assertIn("registerPlugin(EnrolledServerPlugin.class)", activity)
        self.assertIn('"allowNavigation"', activity)
        plugin = (ANDROID / "main/java/app/nvr/mobile/EnrolledServerPlugin.java").read_text()
        self.assertIn("shouldOverrideLoad", plugin)
        self.assertIn("fromStartScreen()", plugin)
        self.assertEqual(plugin.count("fromStartScreen()"), 3)  # Declared once, checked by enrol and forget

    def test_ios_uses_the_view_controller_that_registers_the_plugin(self):
        storyboard = (MOBILE / "ios/App/App/Base.lproj/Main.storyboard").read_text()
        self.assertIn('customClass="NvrBridgeViewController"', storyboard)
        delegate = (MOBILE / "ios/App/App/AppDelegate.swift").read_text()
        self.assertIn("registerPluginInstance(EnrolledServerPlugin())", delegate)
        self.assertIn("shouldOverrideLoad", delegate)

    def test_the_start_screen_enrols_and_ignores_a_saved_address_that_is_not_enrolled(self):
        script = (MOBILE / "www/connect.js").read_text()
        self.assertIn("EnrolledServer.enrol", script)
        self.assertRegex(script, r"enrolled !== address")
        self.assertIsNone(re.search(r"allowNavigation", script))


if __name__ == "__main__":
    unittest.main()

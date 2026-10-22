# Workplace NVR

Language for camera administration and monitoring in one workplace installation.

## Language

**Camera inventory**:
The set of cameras already added to the installation, including archived cameras when explicitly included.
_Avoid_: Discovery results, device scan list

**Discovery result**:
A device reported by a camera scan, whether or not it is already linked to the camera inventory.
_Avoid_: Added camera

**Discovery candidate**:
A discovery result not yet added or linked to the camera inventory.
_Avoid_: Added camera, recording camera

**Selected camera**:
The single inventory camera whose settings or operational details the user is inspecting.
_Avoid_: Bulk selection

**Bulk selection**:
The inventory cameras explicitly checked for a shared administrative action, independently of the selected camera.
_Avoid_: Selected camera, active camera

**Policy group**:
The camera group from which a camera inherits shared recording, schedule, retention, storage, or AI policy, subject to supported camera overrides.
_Avoid_: Browsing group

**Browsing group**:
An operational grouping for finding or opening cameras that can overlap other browsing groups.
_Avoid_: Policy group

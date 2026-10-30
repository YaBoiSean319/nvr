#!/usr/bin/env python3
"""Check the licence plate models in the image: every shipped region's files are there, are the pinned ones, and load (and run once on a blank picture)."""
from __future__ import annotations

import sys

import numpy as np

from app import plate_models


def main() -> int:
    failed = False
    for region in plate_models.REGIONS:
        if region.recogniser is None:
            continue  # custom: installed by hand, not part of the image
        if not plate_models.installed(region):
            print(f"{region.id}: models missing or not the pinned files", file=sys.stderr)
            failed = True
            continue
        reader = plate_models.RegionReader(region)
        found = reader.read(np.zeros((720, 1280, 3), dtype=np.uint8))
        print(f"{region.id}: installed, loads, reads {len(found)} plates from a blank picture ({reader.reader_id})")
        failed = failed or bool(found)  # A blank picture holds no plate
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

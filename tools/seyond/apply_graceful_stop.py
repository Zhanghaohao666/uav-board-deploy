#!/usr/bin/env python3
"""Apply UAV graceful-stop patch to an isolated SDK copy (never overwrite a live binary)."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

p = argparse.ArgumentParser(description=__doc__)
p.add_argument("sdk_copy", type=Path, help="isolated copy of the vendor SDK source directory")
a = p.parse_args()
source = a.sdk_copy / "apps/tools/get_pcd/get_pcd.cpp"
patch = Path(__file__).with_name("0001-get-pcd-graceful-frame-boundary-stop.patch")
raw = source.read_bytes()
if b"UAV_GRACEFUL_STOP_VERSION=1" in raw:
    raise SystemExit("Source already has UAV graceful-stop patch; no changes made.")
command = ["patch", "--batch", "--forward", "-p1", "-d", str(a.sdk_copy.resolve()), "-i", str(patch.resolve())]
subprocess.run(command + ["--dry-run"], check=True)
subprocess.run(command, check=True)
print(json.dumps({"source": str(source.resolve()), "before_sha256": hashlib.sha256(raw).hexdigest(), "after_sha256": hashlib.sha256(source.read_bytes()).hexdigest()}, indent=2))

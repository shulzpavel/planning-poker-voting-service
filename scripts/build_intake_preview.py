"""Build loopback preview snapshots from jira-service normalized timeline data.

PYTHONPATH=. python3 scripts/build_intake_preview.py --source INPUT --output-dir OUTPUT
No Jira or other network access; source data and generated reports stay untracked.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile

from app.domain.intake_dashboard import build_intake_snapshot
from app.domain.discovery_dashboard import build_discovery_snapshot


def build_previews(source: Path, output_dir: Path) -> None:
    payload = json.loads(source.read_text())
    now = datetime.fromisoformat(payload["fetched_at"].replace("Z", "+00:00"))
    output_dir.mkdir(parents=True, exist_ok=True)
    for days in (7, 30, 90):
        snapshot = build_intake_snapshot(payload["issues"], now=now, days=days)
        with NamedTemporaryFile(mode="w", dir=output_dir, delete=False, encoding="utf-8") as temp:
            json.dump(snapshot, temp, ensure_ascii=False)
            name = temp.name
        os.replace(name, output_dir / f"intake-{days}.json")
        print(f"{days} days: {snapshot['metrics']['arrivals']} arrivals, {snapshot['metrics']['decisions']} decisions")
        discovery = build_discovery_snapshot(payload["issues"], now=now, days=days)
        with NamedTemporaryFile(mode="w", dir=output_dir, delete=False, encoding="utf-8") as temp:
            json.dump(discovery, temp, ensure_ascii=False)
            discovery_name = temp.name
        os.replace(discovery_name, output_dir / f"discovery-{days}.json")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    build_previews(args.source, args.output_dir)

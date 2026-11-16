#!/usr/bin/env python3
"""Emit deterministic, strict Sage v2 events for local ABI gates."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

SOURCE_SCHEMA = "sage-native-events-v2"
FEATURE_SCHEMA = "sage-prior-native-features-v2"
FILE_PREFIX = "sage-native-v2"


def _feature(index: int, transport: str, scene_index: int, node_index: int) -> list[float]:
    return [
        float(transport == "udp"),
        float(transport == "tcp"),
        math.log1p(443 if transport == "tcp" else 4433),
        float(1 + index % 17),
        float(1 + index % 13),
        float(1 + index % 11),
        float(1 + index % 2),
        float(1 + node_index % 4),
        float(10_000 + node_index),
        float(20_000 + node_index * 17),
        float(scene_index),
    ]


def write_synthetic_events(output_dir: Path, attempts: int = 180) -> Path:
    if attempts < 40:
        raise ValueError("at least 40 attempts are required")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    run_id = "synthetic"
    path = output_dir / f"{FILE_PREFIX}.{run_id}.1.1.jsonl.ready"
    rows: list[dict] = []
    sequence = 0
    base = 1_800_000_000_000_000_000
    tied_decision = base + 72 * 10_000_000_000
    for index in range(attempts):
        transport = "udp" if index % 3 == 0 else "tcp"
        scene_index = 1 + index % 3
        scene = ("default", "media", "bulk")[scene_index - 1]
        node_index = index % 12
        decision = tied_decision if 70 <= index < 76 else base + index * 10_000_000_000
        attempt_id = f"{run_id}:a{index + 1}"
        common = {
            "source_schema": SOURCE_SCHEMA,
            "feature_schema": FEATURE_SCHEMA,
            "run_id": run_id,
            "attempt_id": attempt_id,
            "decision_unix_nano": decision,
            "group_id": f"group-{index % 2}",
            "node_id": f"node-{node_index:02d}",
            "node_id_stable": True,
            "privacy_key_id": f"privacy-{node_index // 6}",
            "transport": transport,
            "scene": scene,
            "features": _feature(index, transport, scene_index, node_index),
        }
        connect_ms = 35 + (index % 29)
        boundary = "first_positive_datagram" if transport == "udp" else ("dial_return" if index % 2 == 0 else "first_positive_write")
        event_specs: list[tuple[str, dict]] = [
            ("decision", {}),
            ("setup", {"setup_status": "returned", "elapsed_nano": 1_000_000}),
            ("connect", {"metric": "connect", "boundary": boundary, "elapsed_nano": connect_ms * 1_000_000, "value": float(connect_ms + 777)}),
        ]
        if transport == "tcp":
            response_ms = 60 + index % 37
            event_specs.extend([
                ("response", {"metric": "response", "elapsed_nano": response_ms * 1_000_000, "value": float(response_ms)}),
                ("throughput", {"metric": "throughput", "elapsed_nano": 2_500_000_000, "bytes": 300_000, "segment_index": 1, "value": float(1_000_000 + (index % 19) * 100_000)}),
                ("throughput", {"metric": "throughput", "elapsed_nano": 2_600_000_000, "bytes": 350_000, "segment_index": 2, "value": float(1_100_000 + (index % 19) * 100_000)}),
            ])
        failed = index % 3 == 0
        event_specs.extend([
            ("outcome", {"metric": "failure", "outcome": "failure" if failed else "success", "value": 1.0 if failed else 0.0}),
            ("attempt_end", {"end_reason": "closed", "total_bytes": 650_000 if transport == "tcp" else 1_200}),
        ])
        for offset, (event_type, extra) in enumerate(event_specs, 1):
            sequence += 1
            observed = decision + offset * 100_000_000
            if index in (47, 94, 141) and event_type in ("connect", "response", "throughput", "outcome"):
                observed = decision + 900_000_000_000
            rows.append({
                **common,
                "event_seq": sequence,
                "event_id": f"{run_id}:{sequence}",
                "event_type": event_type,
                "observed_unix_nano": observed,
                **extra,
            })
    header = {"source_schema": SOURCE_SCHEMA, "feature_schema": FEATURE_SCHEMA, "run_id": run_id, "event_type": "file_header", "observed_unix_nano": base}
    footer = {"source_schema": SOURCE_SCHEMA, "run_id": run_id, "event_type": "file_footer", "observed_unix_nano": base + attempts * 10_000_000_000, "dropped_control": 0, "dropped_segment": 0, "incomplete_attempts": 0, "written_events": len(rows), "first_event_seq": 1, "last_event_seq": sequence}
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for row in (header, *rows, footer):
            stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n")
    return path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--attempts", type=int, default=180)
    args = parser.parse_args(argv)
    print(write_synthetic_events(args.output_dir, args.attempts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

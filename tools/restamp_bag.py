#!/usr/bin/env python3
"""Re-stamp a rosbag2 bag (MCAP or sqlite3) whose recording clock was broken.

Standalone wrapper around ``fairy.utils.bag_repair`` for use outside a ROS
environment. Prefer ``ros2 fairy repair`` when ros-fairy is installed and sourced;
this script is handy for a bare bag directory on any machine with the package
importable.

Unlike a raw re-stamp, this regenerates ``metadata.yaml`` too, so the output is
immediately playable — no ``ros2 bag reindex`` needed.

Usage: restamp_bag.py <in_bag_dir> <out_bag_dir> [--duration SECONDS]
"""
import argparse
import sys
from pathlib import Path

from fairy.utils import bag_repair


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("in_bag", type=Path)
    ap.add_argument("out_bag", type=Path)
    ap.add_argument("--duration", type=float, default=None,
                    help="target playback length (s); default = span of the "
                         "few good stamps, else 60s")
    args = ap.parse_args()
    try:
        summary = bag_repair.restamp_bag(args.in_bag, args.out_bag,
                                         duration_s=args.duration)
    except bag_repair.BagRepairError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"re-stamped {summary['messages']} messages over "
          f"{summary['new_duration_s']:.1f}s -> {summary['dest']}")
    print("metadata.yaml regenerated — play directly with `ros2 bag play`.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env bash
# Snapshot RAM/CPU/disk for the Day 6 checklist. Run on the VPS from the
# project root, after the stack has been up for a few minutes under normal use.
set -euo pipefail

OUTPUT_DIR="${1:-./artifacts/day-06}"
mkdir -p "$OUTPUT_DIR"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
REPORT="$OUTPUT_DIR/resource-usage-$STAMP.txt"

{
  echo "=== docker compose ps ==="
  docker compose ps

  echo
  echo "=== docker stats (single sample, no streaming) ==="
  docker stats --no-stream

  echo
  echo "=== free -h ==="
  free -h

  echo
  echo "=== df -h / ==="
  df -h /
} | tee "$REPORT"

echo "Report: $REPORT"

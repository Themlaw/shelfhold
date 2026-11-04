#!/usr/bin/env bash
# ENF-PERF-002: fail when the compressed image exceeds the limit (default 300 MB = 300 000 000 bytes).
# Usage: check-image-size.sh <image> [max-bytes]. Compressed size = gzip of `docker save`
# (a slight over-estimate of the registry size, which compresses layer by layer).
set -euo pipefail

image="${1:?usage: check-image-size.sh <image> [max-bytes]}"
max="${2:-300000000}"
size="$(docker save "$image" | gzip -6 | wc -c)"
echo "image ${image}: ${size} bytes compressed (limit ${max})"
if [ "$size" -gt "$max" ]; then
  echo "::error::image is larger than ${max} bytes compressed (ENF-PERF-002)"
  exit 1
fi

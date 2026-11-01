#!/usr/bin/env bash
# Keep the two operator entrypoints on the same instance controller.
set -euo pipefail
if (( $# != 0 )); then
    printf '用法：bash %s\n' "$0" >&2
    exit 2
fi
instance_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
exec python3 "$instance_dir/instance.py" stop

#!/usr/bin/env bash
set -euo pipefail
(( $# == 0 )) || exit 2
project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
exec python3 "$project_dir/scripts/native.py"

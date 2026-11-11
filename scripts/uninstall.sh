#!/usr/bin/env bash
set -euo pipefail

data_home="${XDG_DATA_HOME:-$HOME/.local/share}"
bin_home="${XDG_BIN_HOME:-$HOME/.local/bin}"

targets=(
  "$bin_home/resolve-ready"
  "$data_home/applications/io.github.resolve_ready.desktop"
  "$data_home/icons/hicolor/scalable/apps/resolve-ready.svg"
  "$data_home/kio/servicemenus/resolve-ready.desktop"
  "$data_home/kservices5/ServiceMenus/resolve-ready.desktop"
  "$data_home/nemo/actions/resolve-ready.nemo_action"
  "$data_home/nautilus-python/extensions/resolve_ready.py"
)

for target in "${targets[@]}"; do
  rm -f -- "$target"
done
rm -rf -- "$data_home/resolve-ready"
printf 'Resolve Ready uninstalled.\n'

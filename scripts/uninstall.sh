#!/usr/bin/env bash
set -euo pipefail

data_home="${XDG_DATA_HOME:-$HOME/.local/share}"
bin_home="${XDG_BIN_HOME:-$HOME/.local/bin}"

targets=(
  "$bin_home/resolver"
  "$data_home/applications/io.github.redtheory17.resolver.desktop"
  "$data_home/icons/hicolor/scalable/apps/resolver.svg"
  "$data_home/kio/servicemenus/resolver.desktop"
  "$data_home/kservices5/ServiceMenus/resolver.desktop"
  "$data_home/nemo/actions/resolver.nemo_action"
  "$data_home/nautilus-python/extensions/resolver_menu.py"
  # From when the app was called Resolve Ready.
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
rm -rf -- "$data_home/resolver" "$data_home/resolve-ready"
printf 'Resolver uninstalled.\n'

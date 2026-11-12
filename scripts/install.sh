#!/usr/bin/env bash
set -euo pipefail

script_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_dir="$(dirname -- "$script_dir")"
data_home="${XDG_DATA_HOME:-$HOME/.local/share}"
state_home="${XDG_STATE_HOME:-$HOME/.local/state}"
bin_home="${XDG_BIN_HOME:-$HOME/.local/bin}"
app_home="$data_home/resolver"
executable="$bin_home/resolver"

# Remove an installation from when the app was called Resolve Ready, so the
# launcher and the right-click menu do not list it twice.
rm -f -- \
  "$bin_home/resolve-ready" \
  "$data_home/applications/io.github.resolve_ready.desktop" \
  "$data_home/icons/hicolor/scalable/apps/resolve-ready.svg" \
  "$data_home/kio/servicemenus/resolve-ready.desktop" \
  "$data_home/kservices5/ServiceMenus/resolve-ready.desktop" \
  "$data_home/nemo/actions/resolve-ready.nemo_action" \
  "$data_home/nautilus-python/extensions/resolve_ready.py"
rm -rf -- "$data_home/resolve-ready"
if [[ -d "$state_home/resolve-ready" && ! -e "$state_home/resolver" ]]; then
  mv -- "$state_home/resolve-ready" "$state_home/resolver"
fi

install -d "$app_home" "$bin_home"
install -d "$app_home/bin" "$app_home/resolver"
install -m 755 "$project_dir/bin/resolver" "$app_home/bin/resolver"
install -m 644 "$project_dir/resolver/"*.py "$app_home/resolver/"

install -d "$data_home/applications" "$data_home/icons/hicolor/scalable/apps"
install -m 644 \
  "$project_dir/data/io.github.redtheory17.resolver.desktop" \
  "$data_home/applications/io.github.redtheory17.resolver.desktop"
install -m 644 \
  "$project_dir/data/resolver.svg" \
  "$data_home/icons/hicolor/scalable/apps/resolver.svg"

wrapper_tmp="$(mktemp)"
trap 'rm -f "$wrapper_tmp"' EXIT
printf '#!/usr/bin/env bash\nexec python3 %q "$@"\n' \
  "$app_home/bin/resolver" > "$wrapper_tmp"
install -m 755 "$wrapper_tmp" "$executable"

# Dolphin / KDE Plasma
install -d "$data_home/kio/servicemenus"
sed "s|@EXECUTABLE@|$executable|g" \
  "$project_dir/integrations/dolphin/resolver.desktop.in" \
  > "$data_home/kio/servicemenus/resolver.desktop"
chmod 755 "$data_home/kio/servicemenus/resolver.desktop"

# KDE Plasma 5 fallback
install -d "$data_home/kservices5/ServiceMenus"
install -m 755 \
  "$data_home/kio/servicemenus/resolver.desktop" \
  "$data_home/kservices5/ServiceMenus/resolver.desktop"

# Nemo
install -d "$data_home/nemo/actions"
sed "s|@EXECUTABLE@|$executable|g" \
  "$project_dir/integrations/nemo/resolver.nemo_action.in" \
  > "$data_home/nemo/actions/resolver.nemo_action"

# Nautilus (used when the optional nautilus-python package is installed)
install -d "$data_home/nautilus-python/extensions"
escaped_executable="${executable//\\/\\\\}"
escaped_executable="${escaped_executable//\'/\\\'}"
sed "s|@EXECUTABLE_REPR@|'$escaped_executable'|g" \
  "$project_dir/integrations/nautilus/resolver_menu.py.in" \
  > "$data_home/nautilus-python/extensions/resolver_menu.py"

command -v update-desktop-database >/dev/null 2>&1 \
  && update-desktop-database "$data_home/applications" >/dev/null 2>&1 \
  || true

printf 'Resolver installed.\n'
printf 'Open it from your app menu, or right-click a video and choose “Resolving”.\n'
printf 'Restart your file manager once if the new menu item is not visible yet.\n'

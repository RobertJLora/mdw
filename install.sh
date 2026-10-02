#!/bin/sh
# Install mdw into a prefix (default ~/.local):
#   PREFIX/bin/mdw
#   PREFIX/lib/mdw/*.py
#   PREFIX/share/mdw/styles/*.json
#
# Usage: ./install.sh [--prefix DIR] [--uninstall]
set -eu

PREFIX="${PREFIX:-$HOME/.local}"
UNINSTALL=0
while [ $# -gt 0 ]; do
  case "$1" in
    --prefix) PREFIX="$2"; shift 2 ;;
    --prefix=*) PREFIX="${1#--prefix=}"; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help) sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

if [ "$UNINSTALL" = 1 ]; then
  rm -f "$PREFIX/bin/mdw"
  rm -rf "$PREFIX/lib/mdw" "$PREFIX/share/mdw"
  echo "removed mdw from $PREFIX"
  exit 0
fi

SRC=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$PREFIX/bin" "$PREFIX/lib/mdw" "$PREFIX/share/mdw/styles"
cp "$SRC/bin/mdw" "$PREFIX/bin/mdw"
chmod 755 "$PREFIX/bin/mdw"
cp "$SRC"/lib/mdw/*.py "$PREFIX/lib/mdw/"
cp "$SRC"/share/mdw/styles/*.json "$PREFIX/share/mdw/styles/"
echo "installed mdw to $PREFIX/bin/mdw"

# Dependency check: report, never fail.
missing=0
need() {  # $1 = command, $2 = why, $3 = install hint
  if command -v "$1" >/dev/null 2>&1; then
    printf '  ok       %s\n' "$1"
  else
    printf '  missing  %-8s %s (%s)\n' "$1" "$2" "$3"
    missing=1
  fi
}
echo "dependencies:"
need zsh     "required, runs the viewer"             "brew install zsh"
need python3 "required, runs the renderer"           "brew install python"
need glow    "required for styled output"            "brew install glow"
need fswatch "recommended, instant reload (else 0.5s polling)" "brew install fswatch"

case ":$PATH:" in
  *":$PREFIX/bin:"*) ;;
  *) echo "note: $PREFIX/bin is not on your PATH; add it to your shell profile:"
     echo "  export PATH=\"$PREFIX/bin:\$PATH\"" ;;
esac
[ "$missing" = 0 ] || echo "install the missing tools above, then run: mdw README.md"

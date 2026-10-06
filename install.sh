#!/usr/bin/env bash
# Install the youtube-clipper skill into a DSH skills root.
#
# DSH discovers skills from <project>/.dsh/skills, <project>/.agents/skills, ~/.dsh/skills and
# ~/.agents/skills. This script targets ~/.dsh/skills/youtube-clipper by default.
#
#   ./install.sh              symlink (edits in the repo take effect immediately)
#   ./install.sh --copy       plain copy, for a stable snapshot
#   ./install.sh --dest DIR   install somewhere else
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
src="$here/skill"
dest="${HOME}/.dsh/skills"
mode=link

while [[ $# -gt 0 ]]; do
  case "$1" in
    --copy) mode=copy; shift ;;
    --link) mode=link; shift ;;
    --dest) dest="$2"; shift 2 ;;
    -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

target="$dest/youtube-clipper"
mkdir -p "$dest"

if [[ -e "$target" || -L "$target" ]]; then
  rm -rf "${target:?}"
fi

if [[ "$mode" == link ]]; then
  ln -s "$src" "$target"
  echo "linked $target -> $src"
else
  cp -a "$src" "$target"
  echo "copied $src -> $target"
fi

echo "skill file: $target/SKILL.md"
echo "DSH picks up new and changed skills automatically; start a new session if it does not appear."

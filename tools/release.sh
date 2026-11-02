#!/usr/bin/env bash
# Cut a release: bump meat3/__init__.py and the PKGBUILD, commit, tag v<version>, push both.
#   tools/release.sh 0.2.1
# The tag triggers .github/workflows/release.yml (package build + pacman repo + GitHub release).
set -euo pipefail
v="${1:?version, e.g. 0.2.1}"
[[ "$v" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "version must be X.Y.Z"; exit 1; }
cd "$(dirname "$0")/.."
[[ -z "$(git status --porcelain)" ]] || { echo "working tree not clean"; exit 1; }
sed -i "s/^__version__ = \"[^\"]*\"/__version__ = \"$v\"/" meat3/__init__.py
sed -i "s/^pkgver=.*/pkgver=$v/; s/echo \"\${v:-[0-9.]*}\"/echo \"\${v:-$v}\"/" packaging/arch/PKGBUILD
git add meat3/__init__.py packaging/arch/PKGBUILD
git commit -q -m "Release v$v"
git tag -a "v$v" -m "meat3 v$v"
git push origin master "v$v"
echo "tagged v$v; the release workflow builds the package and updates the pacman repository"

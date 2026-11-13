#!/usr/bin/env sh
# Copy the covers that ship in the image into the data folder, without overwriting.
#
#   sync_covers.sh <shipped dir> <data dir>
#
# A cover you replaced or added yourself is never touched. A cover that is new in this
# version of the image is copied in, which is how new genre covers reach an existing
# install: the data folder outlives the container.
set -eu

src="${1:?usage: sync_covers.sh <shipped dir> <data dir>}"
dst="${2:?usage: sync_covers.sh <shipped dir> <data dir>}"

mkdir -p "$dst"
copied=0
for f in "$src"/*; do
    [ -f "$f" ] || continue
    name="$(basename "$f")"
    if [ ! -e "$dst/$name" ]; then
        cp "$f" "$dst/$name"
        copied=$((copied + 1))
    fi
done
echo "Covers: $copied new file(s) copied to $dst"

#!/usr/bin/env sh
# Copy the covers that ship in the image into the data folder.
#
#   sync_covers.sh <shipped dir> <data dir> [superseded list]
#
# A cover that is not in the data folder yet is copied in, which is how new genre covers
# reach an existing install: the data folder outlives the container.
#
# A cover that is there already is left alone, with one exception: when it is byte for byte
# a cover an earlier version shipped (its sha256 is in the superseded list), nobody changed
# it, so it is replaced by the new drawing. A cover you replaced or added yourself is never
# touched.
set -eu

src="${1:?usage: sync_covers.sh <shipped dir> <data dir> [superseded list]}"
dst="${2:?usage: sync_covers.sh <shipped dir> <data dir> [superseded list]}"
superseded="${3:-$(dirname "$0")/cover-superseded.sha256}"

mkdir -p "$dst"
copied=0
updated=0
for f in "$src"/*; do
    [ -f "$f" ] || continue
    name="$(basename "$f")"
    if [ ! -e "$dst/$name" ]; then
        cp "$f" "$dst/$name"
        copied=$((copied + 1))
    elif [ -f "$superseded" ] && ! cmp -s "$f" "$dst/$name"; then
        sum="$(sha256sum "$dst/$name" | cut -d' ' -f1)"
        if grep -qx "$sum" "$superseded"; then
            cp "$f" "$dst/$name"
            updated=$((updated + 1))
        fi
    fi
done
echo "Covers: $copied new, $updated updated file(s) in $dst"

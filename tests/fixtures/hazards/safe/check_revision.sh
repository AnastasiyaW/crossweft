#!/bin/sh
# accept only a full 40-character commit id
REV="$1"
[ "${#REV}" -eq 40 ] || { echo "not a full commit id: $REV" >&2; exit 2; }
case "$REV" in
  *[!0-9a-f]*) echo "not hexadecimal: $REV" >&2; exit 2 ;;
esac

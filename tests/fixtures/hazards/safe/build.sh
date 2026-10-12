#!/bin/sh
./prepare.sh
prepare_rc=$?
[ "$prepare_rc" -eq 0 ] || exit "$prepare_rc"
export CC="${CC:-cc}"
make -j4

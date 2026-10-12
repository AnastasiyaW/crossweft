#!/bin/sh
./prepare.sh
export CC=$?
make -j4

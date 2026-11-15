#!/bin/bash
# SPDX-License-Identifier: MIT
set -euo pipefail

cd "$HOME/archiver"

if [ -f PAUSED ]; then
    echo "Archiver paused."
    exit 0
fi

source "$HOME/archiver/secrets.sh"

if ! "$HOME/archiver/venv/bin/python" \
    "$HOME/archiver/archiver.py" --run
then
    touch "$HOME/archiver/PAUSED"
    echo "Error encountered. Paused for review."
    exit 1
fi

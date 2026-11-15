#!/bin/bash
# SPDX-License-Identifier: MIT
set -euo pipefail

python3 -m venv "$HOME/archiver/venv"

"$HOME/archiver/venv/bin/python" -m pip install \
  'requests>=2.32,<3' \
  'mwparserfromhell>=0.7,<0.8'

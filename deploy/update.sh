#!/bin/bash
# Holt die neueste Version und startet Arriba neu. Aufruf auf dem Server: ~/arriba/deploy/update.sh
set -euo pipefail
cd "$(dirname "$0")/.."
git pull
.venv/bin/python -m pip install --quiet -e .
supervisorctl restart arriba
supervisorctl status arriba

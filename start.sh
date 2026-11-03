#!/bin/sh
cd "$(dirname "$0")" || exit 1
exec python3 service_control.py start --open "$@"

#!/bin/bash
set -e

# Trading bot runs in the background (scheduler — no inbound traffic needed)
python main.py &

# Dashboard web server runs in the foreground so Railway can detect the port binding
exec uvicorn dashboard:app --host 0.0.0.0 --port "${PORT:-8080}"

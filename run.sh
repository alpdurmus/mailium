#!/bin/bash
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
source .venv/bin/activate
export PYTHONPATH="$DIR"
echo "Starting MailForge Email Automation Studio..."
echo "URL: http://127.0.0.1:8000"
echo "API Docs: http://127.0.0.1:8000/docs"
uvicorn main:app --host 127.0.0.1 --port 8000 --reload

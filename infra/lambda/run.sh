#!/bin/sh
# Lambda handler for the Lambda Web Adapter layer: start the ASGI server; the adapter
# forwards each invocation to it over HTTP on localhost.
export PYTHONPATH="/var/task:${PYTHONPATH}"
exec python3 -m uvicorn --factory cotizador_mcp.lambda_app:create_app \
  --host 127.0.0.1 --port "${AWS_LWA_PORT:-8080}" --no-access-log

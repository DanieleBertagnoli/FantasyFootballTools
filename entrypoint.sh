#!/bin/sh
set -eu

# Docker Compose waits for the MariaDB health check before starting this
# process. Keeping startup local to the built image avoids cloning arbitrary
# source revisions or installing dependencies each time the container starts.
# APP_PORT is intentionally only the host-side Compose mapping.  Keeping the
# process on the fixed container port avoids a mismatch when a user chooses a
# different public port in `.env`.
exec flask --app main:app run --host=0.0.0.0 --port 5010

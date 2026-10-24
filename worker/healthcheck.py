"""Container healthcheck: exit 0 if Redis is reachable and this container's RQ worker is
registered.  python -m worker.healthcheck
"""

import socket
import sys

from redis import Redis
from rq import Worker

from worker.settings import WorkerSettings


def main() -> int:
    try:
        redis = Redis.from_url(WorkerSettings().redis_url, socket_timeout=3)
        redis.ping()
        prefix = socket.gethostname() + "."
        return 0 if any(w.name.startswith(prefix) for w in Worker.all(connection=redis)) else 1
    except Exception as exc:
        print(f"unhealthy: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

"""
Gunicorn configuration for ALOFT Backend production deployment.

Uses Uvicorn workers for async FastAPI support with sensible defaults:
- Worker count based on CPU cores (2x cores + 1)
- 60-second worker timeout for long-running content generation
- Graceful shutdown with 30-second timeout
- Request limits to prevent memory leaks
- Production-ready logging
"""

import multiprocessing
import os

# Server socket
#
# Render (and most PaaS hosts) assign a port dynamically via the $PORT
# env var and route traffic to it — a hardcoded port here means Render's
# proxy can't reach the app at all, which shows up as a 502 on every
# request. Fall back to 8000 for local/manual runs where $PORT isn't set.
bind = f"0.0.0.0:{os.getenv('PORT', '8000')}"
backlog = 2048

# Worker processes
# Formula: (2 x CPU cores) + 1 is a good starting point for I/O-bound apps
workers = (2 * multiprocessing.cpu_count()) + 1
worker_class = "uvicorn.workers.UvicornWorker"
worker_connections = 1000
max_requests = 1000  # Restart workers after 1000 requests to prevent memory leaks
max_requests_jitter = 100  # Add randomness to prevent all workers restarting simultaneously

# Timeout settings
timeout = 60  # 60 seconds for content generation endpoints
keepalive = 2
graceful_timeout = 30  # Allow 30 seconds for graceful shutdown

# preload_app=False is required for correctness with async workers.
#
# With preload_app=True, gunicorn runs the lifespan startup (connect_to_mongo,
# connect_to_redis, start the content worker) inside the *parent* process, then
# forks workers.  Async event loops (asyncio, uvloop) are NOT fork-safe: file
# descriptors, socket state, and the event loop itself are shared across forks,
# leading to silent connection corruption under load.
#
# With preload_app=False each uvicorn worker runs its own lifespan — fresh
# connections, fresh event loop, no shared state. Memory overhead is slightly
# higher but correctness is guaranteed.
preload_app = False

# Process naming
proc_name = "aloft-backend"

# Logging
accesslog = "-"  # Log to stdout
errorlog = "-"  # Log to stderr
loglevel = os.getenv("LOG_LEVEL", "info").lower()
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(D)s'

# SSL (if using HTTPS - disabled by default, use load balancer SSL instead)
# keyfile = "/path/to/ssl/key.pem"
# certfile = "/path/to/ssl/cert.pem"

# Server mechanics
daemon = False  # Run in foreground (containerized environment)
pidfile = None
umask = 0o007
user = None  # Set by Dockerfile non-root user
group = None
tmp_upload_dir = None


# Server hooks
def on_starting(server):
    """Called just before the master process is initialized."""
    server.log.info("Gunicorn starting up")


def on_reload(server):
    """Called to recycle workers during a reload via SIGHUP."""
    server.log.info("Gunicorn reloading workers")


def when_ready(server):
    """Called just after the server is ready to serve requests."""
    server.log.info("Gunicorn server is ready. Listening on: %s", server.address)


def pre_fork(server, worker):
    """Called just before a worker is forked."""
    server.log.info("Worker spawned (pid: %s)", worker.pid)


def post_fork(server, worker):
    """Called just after a worker has been forked."""
    server.log.info("Worker spawned (pid: %s)", worker.pid)


def pre_exec(server):
    """Called just before a new master process is forked."""
    server.log.info("Forked child, re-executing.")


def worker_int(worker):
    """Called just after a worker exited on SIGINT or SIGQUIT."""
    worker.log.info("Worker received INT or QUIT signal")


def worker_abort(worker):
    """Called when a worker received the SIGABRT signal."""
    worker.log.info("Worker received SIGABRT signal")

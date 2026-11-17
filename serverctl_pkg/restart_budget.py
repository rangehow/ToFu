"""Composed deadlines for the shared artifact preparation/restart chain.

Standard-library control callers import these fixed durations. Outer clocks
cover queueing, both builders and joined cleanup; no stage renews an inner
deadline and no hardware probe sizes a deadline.
"""

NATIVE_LOCK_WAIT_SECONDS = 600
NATIVE_WORK_SECONDS = 600
NATIVE_PREPARATION_SECONDS = NATIVE_LOCK_WAIT_SECONDS + NATIVE_WORK_SECONDS
NATIVE_CHILD_SECONDS = NATIVE_PREPARATION_SECONDS + 30
# Fixed parser allowance for standalone callers (boot admission, tools); it
# includes interpreter startup on the source volume. Inside a preparation,
# this and every other stage is bounded only by the composed deadline
# (native_engine.stage_seconds).
NATIVE_MANIFEST_PARSE_SECONDS = 30.0
FRONTEND_LOCK_WAIT_SECONDS = 60.0
FRONTEND_BUILD_SECONDS = 600.0
# The manager prepares the storage engine and the frontend in separate children
# so an engine failure can retain the serving storage host instead of refusing
# the application restart; together they keep the original artifact budget.
STORAGE_ENGINE_PREFLIGHT_SECONDS = NATIVE_CHILD_SECONDS + 60
FRONTEND_PREFLIGHT_SECONDS = FRONTEND_LOCK_WAIT_SECONDS + FRONTEND_BUILD_SECONDS + 60
ARTIFACT_PREPARATION_SECONDS = STORAGE_ENGINE_PREFLIGHT_SECONDS + FRONTEND_PREFLIGHT_SECONDS
# First admission may import from a slow installed environment. This entire
# budget runs before the serving worker is drained; expiration preserves it.
NATIVE_SERVICE_PREPARATION_SECONDS = 120.0
# A new engine is started on a disposable authority (cold start, health
# handshake, contract probes) before the serving worker is drained; the same
# first-admission class of work as above.
NATIVE_GENERATION_PROBE_SECONDS = NATIVE_SERVICE_PREPARATION_SECONDS
# A replaced or stopped host drains its native daemon: maintenance stop,
# overlay merge and the closing checkpoint. On the FUSE authority a clean drain
# measured up to 13.4 s + 17.6 s; a kill inside the checkpoint moved 51-102 s of
# WAL replay onto the next open (2026-10-27). The kill stays a wedge backstop.
NATIVE_DAEMON_STOP_SECONDS = 90.0
# The daemon drain, then the host's startup-thread join (5 s) and record write.
NATIVE_SERVICE_STOP_SECONDS = NATIVE_DAEMON_STOP_SECONDS + 10.0
# Maximum configured worker shutdown, manager escalation, storage lease release,
# then command/transport overhead. Readiness observation has its own clock.
LIFECYCLE_REQUEST_SECONDS = (ARTIFACT_PREPARATION_SECONDS + 300 + 5 + 10 + 45
                             + NATIVE_SERVICE_PREPARATION_SECONDS + NATIVE_GENERATION_PROBE_SECONDS
                             + NATIVE_SERVICE_STOP_SECONDS)

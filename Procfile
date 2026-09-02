# Combined: scheduler + dashboard API in one process.
#
# MALLOC_ARENA_MAX=2  - glibc gives every thread its own heap arena by default
#   (up to 8 x cores). With a sampling pool churning multi-megabyte JSON, that
#   fragments into a resident set several times the live heap and never returns
#   it. Resident memory is the bulk of the hosting bill, so cap the arenas.
# PYTHONUNBUFFERED=1  - stdout is a pipe here, so print() is block-buffered and
#   the deploy logs stay empty for long stretches, which makes the scheduler look
#   dead when it isn't.
web: MALLOC_ARENA_MAX=2 PYTHONUNBUFFERED=1 python app.py

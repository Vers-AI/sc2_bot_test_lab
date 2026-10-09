#!/bin/sh
# Extended bot_controller entrypoint for Churchill-era compiled bots.
#
# Those bots hardwire 127.0.0.1:<port>; the proxy identifies players by
# the source port of the bot's OWN socket. connect_redirect.so
# (LD_PRELOAD) rewrites the loopback destination to the proxy, resolving
# the proxy hostname lazily at first connect (no DNS race at startup).
# The exported environment propagates to the spawned bot process.

if [ "${PROXY_FWD_ENABLE:-0}" = "1" ]; then
    PORT="${PROXY_FWD_PORT:-8080}"
    if [ -f /shim/connect_redirect.so ]; then
        export CONNECT_REDIRECT_HOST="${ACBOT_PROXY_HOST:-proxy_controller}"
        export CONNECT_REDIRECT_PORT="${PORT}"
        export LD_PRELOAD="/shim/connect_redirect.so${LD_PRELOAD:+:${LD_PRELOAD}}"
        echo "connect_redirect: armed (loopback:${PORT} -> ${CONNECT_REDIRECT_HOST}, resolved at connect)"
    else
        echo "connect_redirect: .so missing" >&2
    fi
fi
exec ./bot_controller "$@"

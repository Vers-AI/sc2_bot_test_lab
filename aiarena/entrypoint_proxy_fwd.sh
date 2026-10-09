#!/bin/sh
# Extended bot_controller entrypoint for compiled (Churchill-era) bots.
#
# Those bots hardwire localhost:<port>, while the bot_controller's relay
# listener (where bots are supposed to connect, and where identification
# happens) binds the container's eth0 address during start_bot. The
# shim bridges loopback:<port> to the relay on eth0:<port>, answers the
# client's pre-join ping locally, and injects the bot name into the join.
#
# CRITICAL: start the shim IMMEDIATELY. v5 computed $(hostname -i) here,
# delaying the shim past the controller's relay bind -> EADDRINUSE
# (match 958). The shim resolves the target address lazily AFTER it
# has bound loopback.

if [ "${PROXY_FWD_ENABLE:-0}" = "1" ]; then
    PORT="${PROXY_FWD_PORT:-8080}"
    DELAY="${PROXY_FWD_DELAY:-0}"
    if [ "$DELAY" -gt 0 ] 2>/dev/null; then
        ( sleep "$DELAY" && \
          SHIM_LISTEN_PORT="$PORT" SHIM_TARGET_PORT="$PORT" \
          python3 /shim/protocol_shim.py ) &
    else
        SHIM_LISTEN_PORT="$PORT" SHIM_TARGET_PORT="$PORT" \
        python3 /shim/protocol_shim.py &
    fi
fi
exec ./bot_controller "$@"

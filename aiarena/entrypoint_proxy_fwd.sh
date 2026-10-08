#!/bin/sh
# Extended bot_controller entrypoint for compiled bots.
#
# When PROXY_FWD_ENABLE=1, the SC2API protocol shim
# (protocol_shim.py) listens on localhost:${PROXY_FWD_PORT} and
# bridges to the real proxy controller. This serves two needs of
# Churchill-era compiled bots (MicroMachine and similar):
#   1. they hardwire localhost:<port> for the ladder connection
#      (ignoring --LadderServer), and
#   2. they open with a RequestPing and block until it is answered,
#      which the aiarena proxy never does pre-join.
# The shim answers pings locally, injects the player name into the
# JoinGame request, and relays everything else transparently. It also
# works fine for compiled bots that speak the aiarena flow natively.

if [ "${PROXY_FWD_ENABLE:-0}" = "1" ]; then
    PORT="${PROXY_FWD_PORT:-8080}"
    DELAY="${PROXY_FWD_DELAY:-0}"
    if [ "$DELAY" -gt 0 ] 2>/dev/null; then
        ( sleep "$DELAY" && SHIM_LISTEN_PORT="$PORT" python3 /shim/protocol_shim.py ) &
    else
        SHIM_LISTEN_PORT="$PORT" python3 /shim/protocol_shim.py &
    fi
fi
exec ./bot_controller "$@"

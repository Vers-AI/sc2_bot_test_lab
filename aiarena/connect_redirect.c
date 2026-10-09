/* connect_redirect.c — LD_PRELOAD socket fixups for Churchill-era bots.
 *
 * Churchill-era compiled bots (MicroMachine & siblings) do two things
 * the aiarena stack cannot digest:
 *   1. hardwire 127.0.0.1:<port> for the ladder websocket, and
 *   2. bind a vestigial game-link LISTENER on 0.0.0.0:<port> in ladder
 *      mode (proven: EADDRINUSE errno 98, matches 976-977).
 *
 * The aiarena proxy identifies players by the SOURCE PORT of the bot's
 * OWN TCP socket: the bot_controller netstats the bot's PID and reports
 * the port it finds (structurally, the LISTENER's port — proven in
 * match 973); the proxy matches it against the websocket's source port
 * (proven from sc2-ai-match-controller source, v0.6.10 = our v0.8.0
 * images; matches 932-977, 2026-10-08/09).
 *
 * Two hooks close the registration loop while keeping the bot's own
 * socket end to end:
 *
 *   bind()    the vestigial listener's bind on target_port is relocated
 *             to (127.0.0.1, shadow_port): same-role socket, isolated
 *             address, nothing in the aiarena flow ever contacts it.
 *             This frees the port number for the outbound pin.
 *
 *   connect() destinations on loopback:target_port are rewritten to
 *             the proxy (resolved lazily — container DNS is ready by
 *             the time the bot connects). The socket's source is then
 *             pinned to (eth0, shadow_port) — the SAME port number the
 *             relocated listener carries. Whichever of the two sockets
 *             the controller's netstat finds, it reports shadow_port,
 *             and the websocket arrives from shadow_port: registration
 *             matches.
 *
 * Resolver traffic (127.0.0.11:53) passes untouched: port mismatch.
 *
 * Env:
 *   CONNECT_REDIRECT_HOST        proxy hostname (default ACBOT_PROXY_HOST
 *                                env, else "proxy_controller")
 *   CONNECT_REDIRECT_IP          explicit IP, skips resolution if set
 *   CONNECT_REDIRECT_PORT        port to rewrite (default 8080)
 *   CONNECT_REDIRECT_SHADOW_PORT shadow port (default 18080)
 */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <dlfcn.h>
#include <errno.h>
#include <netdb.h>
#include <netinet/in.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

typedef int (*connect_fn)(int, const struct sockaddr *, socklen_t);
typedef int (*bind_fn)(int, const struct sockaddr *, socklen_t);

static connect_fn real_connect;
static bind_fn real_bind;
static in_addr_t target_ip;
static int target_ip_valid;
static in_port_t target_port;
static in_port_t shadow_port;
static int initialized;

static void init_once(void)
{
    if (initialized)
        return;
    initialized = 1;
    real_connect = (connect_fn)dlsym(RTLD_NEXT, "connect");
    real_bind = (bind_fn)dlsym(RTLD_NEXT, "bind");
    const char *ip = getenv("CONNECT_REDIRECT_IP");
    const char *port = getenv("CONNECT_REDIRECT_PORT");
    const char *shadow = getenv("CONNECT_REDIRECT_SHADOW_PORT");
    if (ip && *ip) {
        in_addr_t parsed;
        if (inet_pton(AF_INET, ip, &parsed) == 1) {
            target_ip = parsed;
            target_ip_valid = 1;
        }
    }
    target_port = htons((in_port_t)(port ? atoi(port) : 8080));
    shadow_port = htons((in_port_t)(shadow ? atoi(shadow) : 18080));
}

static in_addr_t resolve_proxy(void)
{
    if (target_ip_valid)
        return target_ip;

    const char *host = getenv("CONNECT_REDIRECT_HOST");
    if (!host || !*host)
        host = getenv("ACBOT_PROXY_HOST");
    if (!host || !*host)
        host = "proxy_controller";

    struct addrinfo hints, *res;
    memset(&hints, 0, sizeof(hints));
    hints.ai_family = AF_INET;
    hints.ai_socktype = SOCK_STREAM;
    if (getaddrinfo(host, NULL, &hints, &res) == 0 && res) {
        struct sockaddr_in *addr = (struct sockaddr_in *)res->ai_addr;
        target_ip = addr->sin_addr.s_addr;
        target_ip_valid = 1;
        freeaddrinfo(res);
        return target_ip;
    }
    return 0;
}

int bind(int sockfd, const struct sockaddr *addr, socklen_t addrlen)
{
    init_once();
    if (real_bind && addr && addr->sa_family == AF_INET) {
        const struct sockaddr_in *a = (const struct sockaddr_in *)addr;
        if (a->sin_port == target_port) {
            /* Vestigial Churchill listener (0.0.0.0:target in ladder
             * mode — EADDRINUSE proven, matches 976-977). Relocate to
             * (loopback, shadow): isolated, unused by the aiarena flow,
             * and it frees the port number so the outbound pin and the
             * listener both carry shadow_port — the number the
             * controller's netstat will report (match 973). */
            struct sockaddr_in copy = *a;
            copy.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
            copy.sin_port = shadow_port;
            fprintf(stderr, "[cRedirect] listener relocated -> 127.0.0.1:%u\n",
                    (unsigned)ntohs(shadow_port));
            return real_bind(sockfd, (struct sockaddr *)&copy, addrlen);
        }
    }
    return real_bind(sockfd, addr, addrlen);
}

int connect(int sockfd, const struct sockaddr *addr, socklen_t addrlen)
{
    init_once();
    if (real_connect && addr && addr->sa_family == AF_INET) {
        const struct sockaddr_in *a = (const struct sockaddr_in *)addr;
        if (a->sin_addr.s_addr == htonl(INADDR_LOOPBACK) &&
            a->sin_port == target_port) {
            in_addr_t proxy = resolve_proxy();
            if (proxy) {
                struct sockaddr_in copy = *a;
                copy.sin_addr.s_addr = proxy;

                /* Pin the source to (eth0, shadow_port) via a UDP probe:
                 * the controller netstats this process and reports the
                 * port it finds — with the listener relocated, both of
                 * the bot's sockets carry shadow_port, so the proxy's
                 * source-port match succeeds whichever one is listed. */
                int probe = socket(AF_INET, SOCK_DGRAM, 0);
                if (probe >= 0) {
                    struct sockaddr_in pa, local;
                    socklen_t llen = (socklen_t)sizeof(local);
                    memset(&pa, 0, sizeof(pa));
                    pa.sin_family = AF_INET;
                    pa.sin_addr.s_addr = proxy;
                    pa.sin_port = target_port;
                    if (real_connect(probe, (struct sockaddr *)&pa,
                                      sizeof(pa)) == 0 &&
                        getsockname(probe, (struct sockaddr *)&local,
                                    &llen) == 0 &&
                        local.sin_family == AF_INET) {
                        struct sockaddr_in src;
                        memset(&src, 0, sizeof(src));
                        src.sin_family = AF_INET;
                        src.sin_addr = local.sin_addr;
                        src.sin_port = shadow_port;
                        int br = real_bind(sockfd, (struct sockaddr *)&src,
                                           sizeof(src));
                        fprintf(stderr,
                                "[cRedirect] pin %s:%u -> %s (errno=%d)\n",
                                inet_ntoa(local.sin_addr),
                                (unsigned)ntohs(shadow_port),
                                br == 0 ? "OK" : "FAIL", errno);
                    } else {
                        fprintf(stderr, "[cRedirect] probe FAILED\n");
                    }
                    close(probe);
                } else {
                    fprintf(stderr, "[cRedirect] probe socket FAILED\n");
                }
                return real_connect(sockfd, (struct sockaddr *)&copy,
                                    addrlen);
            }
        }
    }
    return real_connect(sockfd, addr, addrlen);
}

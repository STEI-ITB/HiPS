"""ojsdn patches for wmediumd (Mininet-WiFi branch), applied idempotently to wmediumd/wmediumd.c.

Every patch is opt-in through an environment variable read once at start-up, so the patched binary
behaves exactly like the original when none is set. topo.py sets them from the run config.

  OJSDN_WMD_RCVBUF=<bytes>     receive buffer of the hwsim netlink socket. libnl's default (32 KiB)
                               overflows on bursts and the kernel drops frames silently
                               (medium_rcvbuf_kb; medium_nl_drops in run_meta.json).
  OJSDN_WMD_PER_CHANNEL=1      contention per channel: a frame waits only for frames queued on the same
                               frequency. Stock wmediumd puts every radio in one medium, so APs on
                               channels 1/6/11 serialise as if they shared one channel (medium_per_channel).
  OJSDN_WMD_MAXQ_US=<us>       bounded queue: a frame that would wait longer than this for the medium is
                               dropped as a failed transmission (not delivered, TX status without ACK).
                               Stock wmediumd queues without limit; once offered airtime exceeds real
                               time the schedule falls behind for good and the emulation stalls with
                               minute-long delays (medium_max_queue_ms).
  OJSDN_WMD_STATS=<path>       once per second: "<frames_queued> <frames_dropped_maxq>" (per run as
                               medium_frames / medium_maxq_drops in run_meta.json).

Usage: python3 wmediumd_ojsdn.py <path/to/wmediumd/wmediumd.c>
"""
from __future__ import annotations

import sys

GLOBALS_MARK = "ojsdn patch: globals"
GLOBALS = """
/* ojsdn patch: globals (see testbed/install/wmediumd_ojsdn.py) */
static int ojsdn_per_channel = -1;      /* OJSDN_WMD_PER_CHANNEL */
static long ojsdn_maxq_us = -1;         /* OJSDN_WMD_MAXQ_US, 0 = unbounded */
static const char *ojsdn_stats_path;    /* OJSDN_WMD_STATS */
static unsigned long long ojsdn_frames, ojsdn_maxq_drops;
static time_t ojsdn_stats_last;
void deliver_frame(struct wmediumd *ctx, struct frame *frame);   /* defined below queue_frame() */

static void ojsdn_init(void)
{
	const char *e;
	if (ojsdn_per_channel >= 0)
		return;
	e = getenv("OJSDN_WMD_PER_CHANNEL");
	ojsdn_per_channel = e && atoi(e) > 0;
	e = getenv("OJSDN_WMD_MAXQ_US");
	ojsdn_maxq_us = e ? atol(e) : 0;
	ojsdn_stats_path = getenv("OJSDN_WMD_STATS");
}

static void ojsdn_write_stats(const struct timespec *now)
{
	FILE *f;
	char tmp[4096];
	if (!ojsdn_stats_path || now->tv_sec == ojsdn_stats_last)
		return;
	ojsdn_stats_last = now->tv_sec;
	snprintf(tmp, sizeof(tmp), "%s.tmp", ojsdn_stats_path);
	f = fopen(tmp, "w");
	if (!f)
		return;
	fprintf(f, "%llu %llu\\n", ojsdn_frames, ojsdn_maxq_drops);
	fclose(f);
	rename(tmp, ojsdn_stats_path);
}
"""

RCVBUF_MARK = "OJSDN_WMD_RCVBUF"
RCVBUF_ANCHOR = """	ret = genl_connect(sock);
	if (ret < 0) {
		w_logf(ctx, LOG_ERR, "Error connecting netlink socket ret=%d\\n", ret);
		return -1;
	}
"""
RCVBUF = """
	/* ojsdn patch: optional receive buffer for the hwsim socket. libnl's default (32 KiB)
	 * overflows on traffic bursts and the kernel drops frames silently; too large a buffer
	 * under sustained overload turns drops into seconds of queueing, so it is opt-in. */
	{
		const char *env = getenv("OJSDN_WMD_RCVBUF");
		int sz = env ? atoi(env) : 0;
		if (sz > 0 && setsockopt(nl_socket_get_fd(sock), SOL_SOCKET, SO_RCVBUFFORCE, &sz, sizeof(sz)) < 0)
			nl_socket_set_buffer_size(sock, sz, 0);
	}
"""

CHAN_MARK = "ojsdn patch: per-channel contention"
CHAN_OLD = """                tail = list_last_entry_or_null(&tmpsta->queues[i].frames,
                                               struct frame, list);
                if (tail && timespec_before(&target, &tail->expires))
                    target = tail->expires;"""
CHAN_NEW = """                tail = list_last_entry_or_null(&tmpsta->queues[i].frames,
                                               struct frame, list);
                /* ojsdn patch: per-channel contention (frames on other channels do not delay) */
                if (tail && ojsdn_per_channel && tail->freq != frame->freq)
                    continue;
                if (tail && timespec_before(&target, &tail->expires))
                    target = tail->expires;"""

MAXQ_MARK = "ojsdn patch: bounded queue"
MAXQ_OLD = """	timespec_add_usec(&target, send_time);

	frame->duration = send_time;
	frame->expires = target;
	list_add_tail(&frame->list, &queue->frames);
	rearm_timer(ctx);"""
MAXQ_NEW = """	/* ojsdn patch: bounded queue. A frame that would wait longer than OJSDN_WMD_MAXQ_US for the
	 * medium is dropped as a failed transmission: not delivered, TX status without ACK. */
	ojsdn_frames++;
	if (ojsdn_maxq_us > 0) {
		long wait_us = (target.tv_sec - now.tv_sec) * 1000000L + (target.tv_nsec - now.tv_nsec) / 1000;
		if (wait_us > ojsdn_maxq_us) {
			ojsdn_maxq_drops++;
			frame->flags &= ~HWSIM_TX_STAT_ACK;
			frame->duration = 0;
			deliver_frame(ctx, frame);   /* reports the TX status and frees the frame */
			return;
		}
	}

	timespec_add_usec(&target, send_time);

	frame->duration = send_time;
	frame->expires = target;
	list_add_tail(&frame->list, &queue->frames);
	rearm_timer(ctx);"""

INIT_OLD = """	/* TODO configure phy parameters */
	int slot_time = 9;"""
INIT_NEW = """	ojsdn_init();
	/* TODO configure phy parameters */
	int slot_time = 9;"""

STATS_OLD = """	w_logf(ctx, LOG_DEBUG, "\\n\\n");

	if (!ctx->intf)
		return;"""
STATS_NEW = """	w_logf(ctx, LOG_DEBUG, "\\n\\n");
	ojsdn_write_stats(&now);   /* ojsdn patch: stats */

	if (!ctx->intf)
		return;"""


def patch(s: str) -> tuple[str, list[str]]:
    done = []

    def rep(old: str, new: str, what: str) -> None:
        nonlocal s
        if s.count(old) != 1:
            sys.exit(f"wmediumd.c: anchor for '{what}' not found (source changed?): patch by hand")
        s = s.replace(old, new)
        done.append(what)

    if "#include <sys/socket.h>" not in s:
        s = s.replace("#include <string.h>\n", "#include <string.h>\n#include <stdlib.h>\n#include <sys/socket.h>\n", 1)
    if RCVBUF_MARK not in s:
        rep(RCVBUF_ANCHOR, RCVBUF_ANCHOR + RCVBUF, "rcvbuf")
    if GLOBALS_MARK not in s:
        anchor = '#include "wserver_messages.h"\n'
        rep(anchor, anchor + GLOBALS, "globals")
    if CHAN_MARK not in s:
        rep(CHAN_OLD, CHAN_NEW, "per-channel")
    if MAXQ_MARK not in s:
        rep(MAXQ_OLD, MAXQ_NEW, "bounded queue")
        rep(INIT_OLD, INIT_NEW, "init")
        rep(STATS_OLD, STATS_NEW, "stats")
    return s, done


def main() -> int:
    p = sys.argv[1]
    s, done = patch(open(p).read())
    if done:
        open(p, "w").write(s)
    print(f"{p}: {'patched: ' + ', '.join(done) if done else 'already patched'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

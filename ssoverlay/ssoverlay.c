/*
 * ssoverlay - draw extra info on the A810Pro (DR2830 1.2.42 CN) speed screensaver.
 *
 * Prototype. Runs as a separate process: reads main_app memory through
 * /proc/<pid>/mem (read-only) to decide whether the speed screensaver page is
 * the active LVGL page, and only then draws into the blank bands of /dev/fb0.
 * In real mode it also appends one CSV line per GPS epoch to /mnt/sd/gpsx/
 * (see "gpsx" below). It never writes to main_app memory.
 *
 * Build: see build.sh. -DHOST builds a test harness that renders into a
 * framebuffer snapshot instead of the device.
 */
#include "font_data.h"
#include "fingerprint.h"

typedef unsigned int u32;
typedef int s32;
typedef unsigned short u16;
typedef unsigned char u8;
typedef unsigned long long u64;

/* ---------------------------------------------------------------- platform */
#ifdef HOST
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#define sys_write(fd, b, n) fwrite(b, 1, n, fd == 1 ? stdout : stderr)
#else
static long sc6(long n, long a, long b, long c, long d, long e, long f)
{
    register long r7 __asm__("r7") = n;
    register long r0 __asm__("r0") = a;
    register long r1 __asm__("r1") = b;
    register long r2 __asm__("r2") = c;
    register long r3 __asm__("r3") = d;
    register long r4 __asm__("r4") = e;
    register long r5 __asm__("r5") = f;
    __asm__ volatile("svc 0" : "+r"(r0) : "r"(r7), "r"(r1), "r"(r2), "r"(r3), "r"(r4), "r"(r5) : "memory");
    return r0;
}
#define SC(n, a, b, c) sc6(n, (long)(a), (long)(b), (long)(c), 0, 0, 0)
#define NR_exit_group 248
#define NR_write 4
#define NR_open 5
#define NR_close 6
#define NR_read 3
#define NR_ioctl 54
#define NR_mmap2 192
#define NR_pread64 180
#define NR_nanosleep 162
#define NR_clock_gettime 263
#define NR_rt_sigaction 174
#define NR_lseek 19
#define NR_rename 38
#define NR_mkdir 39
#define NR_fsync 118
#define NR_stat64 195
#define sys_write(fd, b, n) SC(NR_write, fd, b, n)

void *memset(void *d, int c, unsigned long n)
{
    u8 *p = d;
    while (n--) *p++ = (u8)c;
    return d;
}
void *memcpy(void *d, const void *s, unsigned long n)
{
    u8 *p = d;
    const u8 *q = s;
    while (n--) *p++ = *q++;
    return d;
}
/* Cortex-A9 has no integer divide; provide the EABI helpers. */
static u32 udivmod(u32 n, u32 d, u32 *rem)
{
    u32 q = 0, r = 0;
    for (int i = 31; i >= 0; i--) {
        r = (r << 1) | ((n >> i) & 1);
        if (r >= d) { r -= d; q |= 1u << i; }
    }
    *rem = r;
    return q;
}
u32 __aeabi_uidiv(u32 n, u32 d) { u32 r; return udivmod(n, d, &r); }
u64 __aeabi_uidivmod(u32 n, u32 d) { u32 r, q = udivmod(n, d, &r); return ((u64)r << 32) | q; }
s32 __aeabi_idiv(s32 n, s32 d)
{
    u32 r, q = udivmod(n < 0 ? -(u32)n : (u32)n, d < 0 ? -(u32)d : (u32)d, &r);
    return ((n < 0) ^ (d < 0)) ? -(s32)q : (s32)q;
}
u64 __aeabi_idivmod(s32 n, s32 d)
{
    u32 r, q = udivmod(n < 0 ? -(u32)n : (u32)n, d < 0 ? -(u32)d : (u32)d, &r);
    s32 qs = ((n < 0) ^ (d < 0)) ? -(s32)q : (s32)q;
    s32 rs = n < 0 ? -(s32)r : (s32)r;
    return ((u64)(u32)rs << 32) | (u32)qs;
}
#endif

static unsigned slen(const char *s) { unsigned n = 0; while (s[n]) n++; return n; }
static void log_write(const char *b, long n);
static void puts2(const char *s) { log_write(s, slen(s)); }
static char *put_u(char *p, u32 v, int width)
{
    char t[12];
    int n = 0;
    do { t[n++] = '0' + v % 10; v /= 10; } while (v);
    while (n < width) t[n++] = '0';
    while (n) *p++ = t[--n];
    return p;
}
static const char *sdlog;  /* optional file, opened/appended/closed per line */
static void log_write(const char *b, long n)
{
    sys_write(2, b, n);
#ifndef HOST
    if (!sdlog) return;
    int fd = SC(NR_open, sdlog, 1 | 0100 | 02000 /* WRONLY|CREAT|APPEND */, 0644);
    if (fd < 0) return;
    if (SC(NR_lseek, fd, 0, 2 /* SEEK_END */) < 1000000) SC(NR_write, fd, b, n);
    SC(NR_close, fd, 0, 0);
#endif
}
static void log_kv(const char *k, u32 v)
{
    char b[64], *p = b;
    for (const char *q = k; *q; q++) *p++ = *q;
    p = put_u(p, v, 1);
    *p++ = '\n';
    log_write(b, p - b);
}
static int parse_int(const char *s)
{
    int v = 0;
    while (*s >= '0' && *s <= '9') v = v * 10 + (*s++ - '0');
    return v;
}

/* ---------------------------------------------------- main_app knowledge */
/* DR2830 1.2.42 CN, main_app MD5 40c91047536e78e80572363fe2ee8a4b */
#define A_DISP_DEF     0x2a271b8u  /* lv_disp_t *disp_def */
#define DISP_ACT_SCR   0x44u       /* disp->act_scr */
#define OBJ_CHILD_LL   0x04u       /* lv_ll_t {n_size, head, tail} */
#define OBJ_FLAGS      0x40u       /* bit4 = hidden */
#define A_SS_UNIT      0x1b37308u  /* speed saver "km/h" label */
#define A_SS_SPEEDBOX  0x1b372acu  /* speed saver digit container */
#define A_SS_TIMEBOX   0x1b372a4u  /* clock saver container */
#define A_PARAM56      0x1a2b9d8u  /* screensaver type, 4 = speed */
#define A_UI_SPEED     0x1a27854u  /* 0x1a27730 + 0x124: km/h shown by the saver */
/* GPS manager's latest parsed NMEA batch (memcpy'd, 0x78c bytes, by
 * GPSMNG_MsgReadThread at 0xf05b4). Fields are {value, scale} pairs where
 * scale = 10^decimals, as stored by the number parser at 0xf2134. */
#define A_GPS          0x1b44228u
#define G_VALID   0   /* 1 when RMC status == 'A' */
#define G_HH      1   /* UTC h, m, s (words 1..3) */
#define G_LAT     8   /* ddmm.mmmm signed: value word 8, scale word 9 */
#define G_LON     10  /* dddmm.mmmm signed: words 10, 11 */
#define G_KN      12  /* speed over ground, knots: words 12, 13 */
#define G_CRS     14  /* course over ground, degrees: words 14, 15 */
#define G_WORDS   16
/* Rest of the batch, mapped from the parser (0xf2518) and checked live:
 * RMC 4 = fractional s, 5..7 = day, month, yy; GGA 18..21 = time,
 * 26 = fix quality, 27 = satellites used, 28/29 = HDOP, 30/31 = altitude MSL m,
 * 33/34 = geoid separation; GSA 48 = 1/2/3 (none/2D/3D), 61..66 = PDOP, HDOP,
 * VDOP; GSV blocks of 76 words at 76, 152, 228, 304, 380 (GP, GB, GA, GL, GQ),
 * each {total msgs, msg no, satellites in view, 4 x (prn, elev, azim, snr)} x 4. */
#define G_DAY     5
#define G_FIXQ    26
#define G_SATS    27
#define G_HDOP    28
#define G_ALT     30
#define G_MODE    48
#define G_PDOP    61
#define G_VDOP    65
#define G_GSV     76
#define G_GSV_LEN 76
#define G_NGSV    5
#define GB_WORDS  (0x78c / 4)

static int memfd = -1;

static int rd(u32 addr, u32 *out)
{
#ifdef HOST
    (void)addr; *out = 0; return -1;
#else
    long r = sc6(NR_pread64, memfd, (long)out, 4, 0, (long)addr, 0);
    return r == 4 ? 0 : -1;
#endif
}

static int rdn(u32 addr, u32 *out, int n)
{
#ifdef HOST
    (void)addr; (void)out; (void)n; return -1;
#else
    long r = sc6(NR_pread64, memfd, (long)out, n * 4, 0, (long)addr, 0);
    return r == n * 4 ? 0 : -1;
#endif
}

/* Returns 1 only when the speed screensaver page is the visible LVGL page. */
static int saver_active(void)
{
    u32 disp, act, unit, page, pparent, ll[3], fl, box, bparent, tbox, p56;
    if (rd(A_PARAM56, &p56) || p56 != 4) return 0;
    if (rd(A_DISP_DEF, &disp) || !disp) return 0;
    if (rd(disp + DISP_ACT_SCR, &act) || !act) return 0;
    if (rd(A_SS_UNIT, &unit) || !unit) return 0;
    if (rd(unit, &page) || !page) return 0;               /* unit->parent */
    if (rd(page, &pparent) || pparent != act) return 0;   /* page on active screen */
    if (rd(act + OBJ_CHILD_LL, &ll[0]) || rd(act + OBJ_CHILD_LL + 4, &ll[1]) ||
        rd(act + OBJ_CHILD_LL + 8, &ll[2]))
        return 0;
    if (ll[1] != page || ll[2] != page) return 0;         /* the only child */
    if (rd(page + OBJ_FLAGS, &fl) || (fl & 0x10)) return 0;
    if (rd(unit + OBJ_FLAGS, &fl) || (fl & 0x10)) return 0;
    if (rd(A_SS_SPEEDBOX, &box) || !box) return 0;
    if (rd(box, &bparent) || bparent != page) return 0;
    if (rd(box + OBJ_FLAGS, &fl) || (fl & 0x10)) return 0;
    if (rd(A_SS_TIMEBOX, &tbox) || !tbox) return 0;
    if (rd(tbox + OBJ_FLAGS, &fl) || !(fl & 0x10)) return 0; /* clock box hidden */
    return 1;
}

/* ------------------------------------------------------------- framebuffer */
#define FB_W 480                 /* physical fb: 480 x 640, rotated 90 deg */
#define FB_H 640
#define LW 640                   /* logical (as seen on the panel) */
#define LH 480
static u32 *fb;

/* logical (lx, ly) -> fb index */
#define FBI(lx, ly) ((u32)(FB_H - 1 - (lx)) * FB_W + (u32)(ly))

/* Regions we own, logical coordinates. Native objects to avoid:
 *   REC label x 10-82, y 73-95;
 *   speed digit box x 90-569, y 135-344: it has an opaque black background
 *     and is invalidated every second, so LVGL repaints that whole rectangle;
 *     the DMA2D rotate flush writes a block-aligned superset of it (measured:
 *     at least x 80.., y ..351), so nothing of ours may sit above y 352;
 *   km/h text x 288-350, y 357-380 (label rect spans x 0-639, y 348-386 but
 *     is only repainted when the page opens).
 * Visible part of the 640x480 canvas on the panel: y 60-420 (user-measured). */
struct rect { int x, y, w, h; };
static const struct rect REG_TOP = {100, 60, 540, 46};    /* clock, course, fix */
static const struct rect REG_LEFT = {0, 352, 280, 68};    /* lat / lon */
static const struct rect REG_RIGHT = {400, 352, 240, 68}; /* mph */
static const struct rect REG_RULER = {572, 0, 68, 480};   /* calibration only */
#define MAXREG 4
#define MAXREG_PX (270 * 117)  /* covers every region incl. v3 clean list */
static const struct rect *REGS[MAXREG] = {&REG_TOP, &REG_LEFT, &REG_RIGHT, &REG_RULER};
static int NREG = 3;

static u32 canvas[LW * LH];          /* composed overlay, logical */
static u32 shadow[MAXREG][MAXREG_PX]; /* what we last wrote per region */
static u8 drawn[MAXREG];
static u32 n_clobbered;
static struct { u32 n; int x0, x1, y0, y1; } cb[MAXREG];
static void cb_reset(void) { for (int r = 0; r < MAXREG; r++) { cb[r].n = 0; cb[r].x0 = cb[r].y0 = 9999; cb[r].x1 = cb[r].y1 = -1; } }  /* frames where some of our pixels had been repainted black */

#define BLACK 0xff000000u

/* -------------------------------------------------------------- text */
static const struct glyph *find_glyph(u32 cp)
{
    int lo = 0, hi = NGLYPHS - 1;
    while (lo <= hi) {
        int m = (lo + hi) / 2;
        if (GLYPHS[m].cp == cp) return &GLYPHS[m];
        if (GLYPHS[m].cp < cp) lo = m + 1; else hi = m - 1;
    }
    return 0;
}
static int kern16(u32 a, u32 b)
{
    for (int i = 0; i < NKERNS; i++)
        if (KERNS[i].a == a && KERNS[i].b == b) return KERNS[i].k16;
    return 0;
}
/* minimal UTF-8 decoder */
static const char *utf8(const char *s, u32 *cp)
{
    u8 c = (u8)*s++;
    if (c < 0x80) { *cp = c; return s; }
    int n = c >= 0xf0 ? 3 : c >= 0xe0 ? 2 : 1;
    u32 v = c & (0x3f >> n);
    while (n-- && *s) v = (v << 6) | ((u8)*s++ & 0x3f);
    *cp = v;
    return s;
}
static int text_w16(const char *s)
{
    int x16 = 0;
    u32 prev = 0, cp;
    while (*s) {
        s = utf8(s, &cp);
        if (prev) x16 += kern16(prev, cp);
        const struct glyph *g = find_glyph(cp);
        if (g) x16 += g->adv16;
        prev = cp;
    }
    return x16;
}
static void blend(u32 *px, u32 rgb, u32 a)
{
    /* background is opaque black; keep the max of overlapping glyph coverage */
    u32 r = ((rgb >> 16) & 255) * a / 255, g = ((rgb >> 8) & 255) * a / 255, b = (rgb & 255) * a / 255;
    u32 o = *px;
    if (((o >> 16) & 255) > r) r = (o >> 16) & 255;
    if (((o >> 8) & 255) > g) g = (o >> 8) & 255;
    if ((o & 255) > b) b = o & 255;
    *px = BLACK | (r << 16) | (g << 8) | b;
}
/* draw one line; (x, top) is the LVGL line box origin. Returns end x. */
static int draw_text(int x, int top, const char *s, u32 rgb)
{
    int x16 = x * 16;
    u32 prev = 0, cp;
    while (*s) {
        s = utf8(s, &cp);
        if (prev) x16 += kern16(prev, cp);
        const struct glyph *g = find_glyph(cp);
        if (g) {
            int gx = (x16 + 8) / 16 + g->ox;
            int gy = top + (FONT_LINE_HEIGHT - FONT_BASE_LINE) - g->h - g->oy;
            const u8 *a = ALPHA + g->off;
            for (int yy = 0; yy < g->h; yy++)
                for (int xx = 0; xx < g->w; xx++) {
                    int X = gx + xx, Y = gy + yy;
                    u8 v = a[yy * g->w + xx];
                    if (v && X >= 0 && X < LW && Y >= 0 && Y < LH) blend(&canvas[Y * LW + X], rgb, v);
                }
            x16 += g->adv16;
        }
        prev = cp;
    }
    return (x16 + 15) / 16;
}
enum { AL_LEFT, AL_CENTER, AL_RIGHT };
static void text_at(int x, int top, int align, const char *s, u32 rgb)
{
    int w = (text_w16(s) + 15) / 16;
    if (align == AL_CENTER) x -= w / 2;
    else if (align == AL_RIGHT) x -= w;
    draw_text(x, top, s, rgb);
}

/* --------------------------------------------------------------- data */
struct info {
    int valid;          /* GPS fix */
    s32 lat_e5, lon_e5; /* degrees * 1e5, signed */
    int course;         /* degrees 0..359, -1 unknown */
    int speed_kmh;      /* -1 unknown */
    int alt_ok, alt_m;  /* GGA altitude above mean sea level, whole metres */
    int sats;           /* satellites used, -1 unknown */
    int hh, mm;         /* local time */
};

static u32 now_s(void)
{
#ifdef HOST
    return (u32)time(0);
#else
    u32 ts[2];
    SC(NR_clock_gettime, 0, ts, 0);
    return ts[0];
#endif
}
static int tz_off_s = 8 * 3600;  /* parsed from /etc/localtime POSIX tail */

static void fake_info(struct info *in, u32 t)
{
    int ph = (int)(t % 60);
    in->valid = 1;
    in->lat_e5 = 3344838 + ph * 3;        /* N 33.44838 (fake, downtown Phoenix) */
    in->lon_e5 = -11207404 - ph * 5;      /* W 112.07404 */
    in->course = (int)((t * 6) % 360);    /* rotates once a minute */
    in->speed_kmh = 48;
    in->alt_ok = 1;
    in->alt_m = 331;                      /* Phoenix */
    in->sats = 23;
}

/* ddmm.mmmm {value, scale} -> degrees * 1e5, integer only. Returns -1 if unusable. */
static int nmea_to_e5(s32 v, s32 sc, s32 *out)
{
    if (sc <= 0) return -1;
    while (sc > 100000) { v /= 10; sc /= 10; }          /* keep at most 5 decimals */
    u32 a = v < 0 ? -(u32)v : (u32)v;
    u32 s = (u32)sc;
    u32 deg = a / (100 * s);
    u32 rem = a - deg * 100 * s;                        /* minutes * s, < 60 * s */
    if (deg > 180 || rem >= 60 * s) return -1;
    u32 e5 = deg * 100000 + (rem * (100000 / s) + 30) / 60;
    *out = v < 0 ? -(s32)e5 : (s32)e5;
    return 0;
}

static u32 mono_s(void)
{
#ifdef HOST
    return (u32)time(0);
#else
    u32 ts[2];
    SC(NR_clock_gettime, 1 /* CLOCK_MONOTONIC */, ts, 0);
    return ts[0];
#endif
}

static int last_course = -1;
static u32 last_hms = 0xffffffff, hms_changed_at;

/* Read the whole parsed batch; two identical reads in a row, so a copy
 * caught in the middle of main_app's memcpy is rejected. 0 on success. */
static u32 gtmp[GB_WORDS];
static int read_gps(u32 *a)
{
    for (int tries = 0; tries < 3; tries++) {
        if (rdn(A_GPS, a, GB_WORDS) || rdn(A_GPS, gtmp, GB_WORDS)) return -1;
        int i = 0;
        while (i < GB_WORDS && a[i] == gtmp[i]) i++;
        if (i == GB_WORDS) return 0;
    }
    return -1;
}

static void real_info(struct info *in, const u32 *a, int have)
{
    u32 ui;
    in->valid = 0;
    in->course = -1;
    in->speed_kmh = -1;
    in->lat_e5 = in->lon_e5 = 0;
    in->alt_ok = 0;
    in->sats = -1;
    if (!rd(A_UI_SPEED, &ui) && ui < 1000) in->speed_kmh = (int)ui;
    if (!have) return;
    /* staleness: GPS UTC time must keep advancing */
    u32 hms = a[G_HH] * 3600 + a[G_HH + 1] * 60 + a[G_HH + 2];
    u32 now = mono_s();
    if (hms != last_hms) { last_hms = hms; hms_changed_at = now; }
    if (now - hms_changed_at > 5) return;
    if (a[G_SATS] < 100) in->sats = (int)a[G_SATS];
    if (a[G_VALID] != 1) return;
    if (nmea_to_e5((s32)a[G_LAT], (s32)a[G_LAT + 1], &in->lat_e5) ||
        nmea_to_e5((s32)a[G_LON], (s32)a[G_LON + 1], &in->lon_e5))
        return;
    if (in->lat_e5 > 9000000 || in->lat_e5 < -9000000) return;
    in->valid = 1;
    s32 av = (s32)a[G_ALT], as = (s32)a[G_ALT + 1];
    if (a[G_FIXQ] >= 1 && as > 0 && av > -1000 * as && av < 10000 * as) {
        in->alt_ok = 1;
        in->alt_m = av >= 0 ? (av + as / 2) / as : -((-av + as / 2) / as);
    }
    s32 cv = (s32)a[G_CRS], cs = (s32)a[G_CRS + 1];
    int crs = -1;
    if (cs > 0 && cv >= 0) crs = (int)(((u32)cv + (u32)cs / 2) / (u32)cs) % 360;
    /* course is noise when (nearly) stationary: hold the last good one */
    if (crs >= 0 && (in->speed_kmh < 0 || in->speed_kmh >= 3)) last_course = crs;
    in->course = last_course >= 0 ? last_course : crs;
}

/* ------------------------------------------------------------------ gpsx */
/* One CSV line per GPS epoch (1 Hz), independent of the screensaver.
 * Batches of GX_BATCH lines like main_app's own GPSData writer, appended with
 * open/write/fsync/close so the SD card is never held open (format/unmount).
 * GX_CUR rolls over to GX_OLD at GX_MAX bytes: at most two files. */
#define GX_DIR   "/mnt/sd/gpsx"
#define GX_CUR   GX_DIR "/GPSX.csv"
#define GX_OLD   GX_DIR "/GPSX.old.csv"
#define GX_MAX   (100u * 1024 * 1024)
#define GX_BATCH 5
#define GX_HEADER "unix,utc,status,lat,lon,alt_m,speed_kn,course,ui_kmh,fix_quality,fix_mode," \
                  "sats_used,hdop,pdop,vdop,sats_in_view,temp_c\n"

/* {value, 10^k} -> decimal text with k decimals; nothing when scale is 0 (empty NMEA field) */
static char *put_fixed(char *p, s32 v, s32 sc)
{
    if (sc <= 0) return p;
    int k = 0;
    for (s32 t = sc; t > 1; t /= 10) k++;
    u32 a = v < 0 ? -(u32)v : (u32)v;
    if (v < 0) *p++ = '-';
    p = put_u(p, a / (u32)sc, 1);
    if (k) { *p++ = '.'; p = put_u(p, a % (u32)sc, k); }
    return p;
}

/* ddmm.mmmmm {value, scale} -> degrees * 1e7 (minutes carry 5 decimals, ~2 cm) */
static int nmea_to_e7(s32 v, s32 sc, s32 *out)
{
    if (sc <= 0) return -1;
    while (sc > 10000000) { v /= 10; sc /= 10; }
    u32 a = v < 0 ? -(u32)v : (u32)v;
    u32 s = (u32)sc;
    u32 deg = a / (100 * s);
    u32 rem = a - deg * 100 * s;
    if (deg > 180 || rem >= 60 * s) return -1;
    u32 e7 = deg * 10000000u + (rem * (10000000u / s) + 30) / 60;
    *out = v < 0 ? -(s32)e7 : (s32)e7;
    return 0;
}
static char *put_deg7(char *p, const u32 *g, int w)
{
    s32 e7;
    if (nmea_to_e7((s32)g[w], (s32)g[w + 1], &e7)) return p;
    u32 a = e7 < 0 ? -(u32)e7 : (u32)e7;
    if (e7 < 0) *p++ = '-';
    p = put_u(p, a / 10000000u, 1);
    *p++ = '.';
    return put_u(p, a % 10000000u, 7);
}

/* RMC date + time -> Unix seconds; -1 while the receiver has no date yet */
static int gps_unix(const u32 *g, u32 *out)
{
    u32 d = g[G_DAY], mo = g[G_DAY + 1], yy = g[G_DAY + 2];
    u32 h = g[G_HH], mi = g[G_HH + 1], se = g[G_HH + 2];
    if (d < 1 || d > 31 || mo < 1 || mo > 12 || yy > 99 || h > 23 || mi > 59 || se > 60) return -1;
    int y = 2000 + (int)yy - (mo <= 2);            /* days_from_civil (H. Hinnant) */
    int era = y / 400, yoe = y - era * 400;
    int doy = (153 * ((int)mo + (mo > 2 ? -3 : 9)) + 2) / 5 + (int)d - 1;
    int days = era * 146097 + yoe * 365 + yoe / 4 - yoe / 100 + doy - 719468;
    *out = (u32)days * 86400u + h * 3600 + mi * 60 + se;
    return 0;
}

static int gx_format(char *b, const u32 *g, u32 ut, int kmh, int temp_ok, int temp)
{
    char *p = b;
    p = put_u(p, ut, 1); *p++ = ',';
    p = put_u(p, 2000 + g[G_DAY + 2], 4); *p++ = '-';
    p = put_u(p, g[G_DAY + 1], 2); *p++ = '-';
    p = put_u(p, g[G_DAY], 2); *p++ = ' ';
    p = put_u(p, g[G_HH], 2); *p++ = ':';
    p = put_u(p, g[G_HH + 1], 2); *p++ = ':';
    p = put_u(p, g[G_HH + 2], 2); *p++ = ',';
    *p++ = g[G_VALID] == 1 ? 'A' : 'V'; *p++ = ',';
    p = put_deg7(p, g, G_LAT); *p++ = ',';
    p = put_deg7(p, g, G_LON); *p++ = ',';
    p = put_fixed(p, (s32)g[G_ALT], (s32)g[G_ALT + 1]); *p++ = ',';
    p = put_fixed(p, (s32)g[G_KN], (s32)g[G_KN + 1]); *p++ = ',';
    p = put_fixed(p, (s32)g[G_CRS], (s32)g[G_CRS + 1]); *p++ = ',';
    if (kmh >= 0) p = put_u(p, (u32)kmh, 1);
    *p++ = ',';
    p = put_u(p, g[G_FIXQ] < 10 ? g[G_FIXQ] : 0, 1); *p++ = ',';
    p = put_u(p, g[G_MODE] < 4 ? g[G_MODE] : 0, 1); *p++ = ',';
    p = put_u(p, g[G_SATS] < 100 ? g[G_SATS] : 0, 1); *p++ = ',';
    p = put_fixed(p, (s32)g[G_HDOP], (s32)g[G_HDOP + 1]); *p++ = ',';
    p = put_fixed(p, (s32)g[G_PDOP], (s32)g[G_PDOP + 1]); *p++ = ',';
    p = put_fixed(p, (s32)g[G_VDOP], (s32)g[G_VDOP + 1]); *p++ = ',';
    u32 view = 0;
    for (int k = 0; k < G_NGSV; k++) {
        const u32 *h = g + G_GSV + k * G_GSV_LEN;
        if (h[0] >= 1 && h[0] <= 4 && h[2] < 100) view += h[2];
    }
    p = put_u(p, view, 1); *p++ = ',';
    if (temp_ok) {
        if (temp < 0) { *p++ = '-'; temp = -temp; }
        p = put_u(p, (u32)temp, 1);
    }
    *p++ = '\n';
    return (int)(p - b);
}

#ifndef HOST
static char gx_buf[(GX_BATCH + 1) * 256];
static int gx_len, gx_n;
static u32 gx_last_unix, gx_first_at, gx_temp_at;
static int gx_temp, gx_temp_ok;
static long gx_err = 1;  /* unknown: first result is always logged */

static int sd_mounted(void)
{
    u32 a[26], b[26];  /* struct stat64, st_dev first */
    if (SC(NR_stat64, "/mnt/sd", a, 0) || SC(NR_stat64, "/mnt", b, 0)) return 0;
    return a[0] != b[0] || a[1] != b[1];
}

static void read_temp(void)
{
    char b[16];
    gx_temp_ok = 0;
    int fd = SC(NR_open, "/sys/class/thermal/thermal_zone0/temp", 0, 0);
    if (fd < 0) return;
    long n = SC(NR_read, fd, b, sizeof b - 1);
    SC(NR_close, fd, 0, 0);
    if (n <= 0) return;
    b[n] = 0;
    int neg = b[0] == '-';
    if (!(b[neg] >= '0' && b[neg] <= '9')) return;
    int t = parse_int(b + neg);
    if (t > 1000) t = (t + 500) / 1000;  /* millidegrees on other kernels */
    gx_temp = neg ? -t : t;
    gx_temp_ok = 1;
}

static void gx_flush(void)
{
    long err = -1;
    if (gx_len && sd_mounted()) {
        SC(NR_mkdir, GX_DIR, 0777, 0);
        int fd = SC(NR_open, GX_CUR, 1 | 0100 | 02000 /* WRONLY|CREAT|APPEND */, 0644);
        long size = fd >= 0 ? SC(NR_lseek, fd, 0, 2 /* SEEK_END */) : -1;
        if (fd >= 0 && size >= (long)GX_MAX) {
            SC(NR_close, fd, 0, 0);
            SC(NR_rename, GX_CUR, GX_OLD, 0);  /* replaces the previous old file */
            fd = SC(NR_open, GX_CUR, 1 | 0100 | 01000 | 02000 /* ...|TRUNC */, 0644);
            size = 0;
        }
        if (fd >= 0) {
            err = 0;
            if (size == 0) SC(NR_write, fd, GX_HEADER, slen(GX_HEADER));
            if (SC(NR_write, fd, gx_buf, gx_len) != gx_len) err = -2;
            SC(NR_fsync, fd, 0, 0);
            SC(NR_close, fd, 0, 0);
        } else {
            err = fd;
        }
    }
    if (gx_len && err != gx_err) {  /* log only changes: 0 ok, -1 no SD, <0 errno */
        if (err < 0) log_kv("gpsx error -", (u32)-err); else puts2("gpsx ok\n");
        gx_err = err;
    }
    gx_len = gx_n = 0;
}

static void gx_tick(const u32 *g, int have)
{
    u32 now = mono_s(), t;
    if (have && !gps_unix(g, &t) && t != gx_last_unix) {
        gx_last_unix = t;
        if (!gx_temp_at || now - gx_temp_at >= 5) { read_temp(); gx_temp_at = now; }
        u32 ui;
        int kmh = !rd(A_UI_SPEED, &ui) && ui < 1000 ? (int)ui : -1;
        if (!gx_n) gx_first_at = now;
        gx_len += gx_format(gx_buf + gx_len, g, t, kmh, gx_temp_ok, gx_temp);
        if (++gx_n >= GX_BATCH) gx_flush();
    }
    if (gx_n && now - gx_first_at >= 10) gx_flush();  /* GPS stalled: don't sit on lines */
}
#endif

static const char *compass8(int deg)
{
    static const char *n[8] = {"N", "NE", "E", "SE", "S", "SW", "W", "NW"};
    return n[((deg * 2 + 45) / 90) & 7];
}
static char *put_coord(char *p, s32 v_e5, char pos, char neg)
{
    *p++ = v_e5 < 0 ? neg : pos;
    *p++ = ' ';
    u32 a = v_e5 < 0 ? -(u32)v_e5 : (u32)v_e5;
    p = put_u(p, a / 100000, 1);
    *p++ = '.';
    p = put_u(p, a % 100000, 5);
    return p;
}

#define C_WHITE 0xffffff
#define C_GRAY  0x999999   /* same as native km/h label */
#define C_LIGHT 0xd8d8d8
#define C_GREEN 0x34c759
#define C_DIM   0x5a5a5a

static char *put_s(char *p, const char *q) { while (*q) *p++ = *q++; return p; }
static char *put_big(char *p, u32 v)  /* digits as U+E030.. (scaled native digit images) */
{
    char t[12];
    int n = 0;
    do { t[n++] = (char)(v % 10); v /= 10; } while (v);
    while (n) { *p++ = (char)0xee; *p++ = (char)0x80; *p++ = (char)(0xb0 + t[--n]); }
    return p;
}

#define BASE 414          /* bottom baseline; visible area ends at y 420 */
#define PITCH 27          /* line pitch of the left block (cap height 20) */
#define LTOP(baseline) ((baseline) - (FONT_LINE_HEIGHT - FONT_BASE_LINE))

static int calib;

static void compose_ruler(void)
{
    char b[8];
    for (int y = 0; y < LH; y += 10) {
        int len = y % 40 == 0 ? 22 : 10;
        for (int x = LW - len; x < LW; x++) canvas[y * LW + x] = y % 40 == 0 ? 0xffffff00u : 0xff808080u;
        if (y % 40 == 0) {
            char *p = put_u(b, (u32)y, 1);
            *p = 0;
            text_at(LW - 24, y - 20, AL_RIGHT, b, C_WHITE);
        }
    }
}

static void compose(const struct info *in)
{
    char b[96], *p;
    for (int r = 0; r < NREG; r++) {
        const struct rect *R = REGS[r];
        for (int y = R->y; y < R->y + R->h; y++)
            for (int x = R->x; x < R->x + R->w; x++) canvas[y * LW + x] = BLACK;
    }
    if (calib) compose_ruler();

    /* clock, baseline aligned with native REC (glyph bottom y=95) */
    p = b; p = put_u(p, (u32)in->hh, 2); *p++ = ':'; p = put_u(p, (u32)in->mm, 2); *p = 0;
    text_at(320, 66, AL_CENTER, b, C_WHITE);

    /* top-right: course, then fix dot + satellites used */
    char s[8] = "";
    if (in->sats >= 0) { s[0] = ' '; p = put_u(s + 1, (u32)in->sats, 1); *p = 0; }
    if (in->valid && in->course >= 0) {
        p = put_u(b, (u32)in->course, 1);
        *p++ = (char)0xc2; *p++ = (char)0xb0; *p++ = ' ';
        p = put_s(p, compass8(in->course));
        p = put_s(p, "   ");
    } else {
        p = put_s(b, "GPS ");
    }
    *p = 0;
    u32 cdot = in->valid ? C_GREEN : C_DIM, ctxt = in->valid ? C_LIGHT : C_DIM;
    int x = 626 - (text_w16(b) + text_w16("●") + text_w16(s) + 15) / 16;
    x = draw_text(x, 66, b, ctxt);
    x = draw_text(x, 66, "●", cdot);
    draw_text(x, 66, s, ctxt);

    /* top-left, right of REC: altitude */
    if (in->valid && in->alt_ok) {
        p = b;
        if (in->alt_m < 0) *p++ = '-';
        p = put_u(p, (u32)(in->alt_m < 0 ? -in->alt_m : in->alt_m), 1);
        *p = 0;
        x = draw_text(104, 66, "▲", C_GRAY);   /* glyph advance leaves a 5 px gap */
        x = draw_text(x, 66, b, C_LIGHT);
        draw_text(x, 66, " m", C_GRAY);
    }

    /* bottom-left: latitude / longitude (signed values -> N/S, E/W) */
    const int x0 = 12;
    if (in->valid) {
        p = put_coord(b, in->lat_e5, 'N', 'S'); *p = 0;
        draw_text(x0, LTOP(BASE - PITCH), b, C_LIGHT);
        p = put_coord(b, in->lon_e5, 'E', 'W'); *p = 0;
        draw_text(x0, LTOP(BASE), b, C_LIGHT);
    } else {
        draw_text(x0, LTOP(BASE), "No GPS fix", C_DIM);
    }

    /* bottom-right: mph with scaled native digits, right-aligned */
    if (calib) return;  /* ruler occupies the right edge */
    p = b;
    if (in->speed_kmh >= 0) p = put_big(p, (u32)((in->speed_kmh * 6214 + 5000) / 10000));
    else p = put_s(p, "--");
    *p = 0;
    const int xr = 628;
    int wl = (text_w16(" mph") + 15) / 16;
    text_at(xr - wl, LTOP(BASE), AL_RIGHT, b, C_WHITE);
    draw_text(xr - wl, LTOP(BASE), " mph", C_GRAY);
}

/* Write regions to fb. A pixel may only be replaced if it is still black or
 * still holds what we wrote; anything else means another UI element is there
 * (popup etc.), so the whole frame is skipped. Returns 1 if drawn. */
static int flush(void)
{
    for (int r = 0; r < NREG; r++) {
        const struct rect *R = REGS[r];
        int i = 0;
        for (int y = R->y; y < R->y + R->h; y++)
            for (int x = R->x; x < R->x + R->w; x++, i++) {
                u32 f = fb[FBI(x, y)];
                if (f != BLACK && !(drawn[r] && f == shadow[r][i])) return 0;
            }
    }
    int clob = 0;
    for (int r = 0; r < NREG; r++) {
        const struct rect *R = REGS[r];
        int i = 0;
        if (!drawn[r]) continue;
        for (int y = R->y; y < R->y + R->h; y++)
            for (int x = R->x; x < R->x + R->w; x++, i++)
                if (shadow[r][i] != BLACK && fb[FBI(x, y)] == BLACK) {
                    clob = 1;
                    cb[r].n++;
                    if (x < cb[r].x0) cb[r].x0 = x;
                    if (x > cb[r].x1) cb[r].x1 = x;
                    if (y < cb[r].y0) cb[r].y0 = y;
                    if (y > cb[r].y1) cb[r].y1 = y;
                }
    }
    n_clobbered += clob;
    for (int r = 0; r < NREG; r++) {
        const struct rect *R = REGS[r];
        int i = 0;
        for (int y = R->y; y < R->y + R->h; y++)
            for (int x = R->x; x < R->x + R->w; x++, i++) {
                u32 c = canvas[y * LW + x];
                if (fb[FBI(x, y)] != c) fb[FBI(x, y)] = c;
                shadow[r][i] = c;
            }
        drawn[r] = 1;
    }
    return 1;
}
/* restore our pixels to black (only those still holding our content) */
static void erase(void)
{
    for (int r = 0; r < NREG; r++) {
        if (!drawn[r]) continue;
        const struct rect *R = REGS[r];
        int i = 0;
        for (int y = R->y; y < R->y + R->h; y++)
            for (int x = R->x; x < R->x + R->w; x++, i++)
                if (fb[FBI(x, y)] == shadow[r][i] && shadow[r][i] != BLACK) fb[FBI(x, y)] = BLACK;
        drawn[r] = 0;
    }
}

static void local_hm(u32 t, struct info *in)
{
    u32 l = t + (u32)tz_off_s;
    in->hh = (int)((l / 3600) % 24);
    in->mm = (int)((l / 60) % 60);
}

#ifdef HOST
/* host harness: ssoverlay_host in.raw out.raw [epoch] */
int main(int argc, char **argv)
{
    if (argc < 3) return 2;
    if (!strcmp(argv[1], "gpsline")) {  /* ssoverlay_host gpsline batch.bin: print gpsx line */
        static u32 g[GB_WORDS];
        char b[512];
        FILE *f = fopen(argv[2], "rb");
        if (!f || fread(g, 4, GB_WORDS, f) != GB_WORDS) return 1;
        fclose(f);
        u32 t;
        if (gps_unix(g, &t)) { fprintf(stderr, "no date\n"); return 1; }
        fputs(GX_HEADER, stdout);
        fwrite(b, 1, gx_format(b, g, t, 26, 1, 52), stdout);
        return 0;
    }
    static u32 buf[FB_W * FB_H];
    FILE *f = fopen(argv[1], "rb");
    if (!f || fread(buf, 4, FB_W * FB_H, f) != FB_W * FB_H) return 1;
    fclose(f);
    fb = buf;
    struct info in;
    u32 t = argc > 3 ? (u32)atol(argv[3]) : now_s();
    fake_info(&in, t);
    if (argc > 4 && argv[4][0] == 'n') { in.valid = 0; in.sats = 2; }
    if (argc > 4 && argv[4][0] == 'h') { in.alt_m = 4321; in.course = 315; }  /* widest top bar */
    if (argc > 4 && argv[4][0] == 'l') { in.alt_m = -86; }                    /* below sea level */
    if (argc > 4 && argv[4][0] == 'c') { calib = 1; NREG = 4; }
    local_hm(t, &in);
    compose(&in);
    if (!flush()) { fprintf(stderr, "region not clean\n"); return 1; }
    f = fopen(argv[2], "wb");
    fwrite(buf, 4, FB_W * FB_H, f);
    fclose(f);
    return 0;
}
#else
static volatile int stop;
static void on_sig(int s) { (void)s; stop = 1; }

struct ksigaction { void (*h)(int); unsigned long flags; void (*restorer)(void); u32 mask[2]; };

static void parse_tz(void)
{
    char b[512];
    int fd = SC(NR_open, "/etc/localtime", 0, 0);
    if (fd < 0) return;
    long n = SC(NR_read, fd, b, sizeof b - 1);
    SC(NR_close, fd, 0, 0);
    if (n <= 2) return;
    /* tail is "\nSTD[+-]h\n"; POSIX sign is inverted */
    int e = (int)n - 1;
    while (e > 0 && b[e] == '\n') e--;
    int s = e;
    while (s > 0 && b[s - 1] != '\n') s--;
    for (int i = s; i <= e; i++)
        if (b[i] == '-' || b[i] == '+' || (b[i] >= '0' && b[i] <= '9')) {
            int sign = b[i] == '-' ? 1 : -1;
            if (b[i] == '-' || b[i] == '+') i++;
            int h = parse_int(b + i);
            tz_off_s = sign * h * 3600;
            return;
        }
}

/* One-shot: blacken every opaque pixel in all regions this program has ever
 * used (v1 layout included), only while the speed saver is the active page. */
static int clean(void)
{
    static const struct rect ALL[] = {
        {100, 60, 540, 46}, {0, 386, 640, 94},                       /* v1 */
        {200, 60, 240, 46}, {0, 280, 182, 140}, {470, 330, 170, 90}, /* v2 */
        {550, 0, 90, 480},
        {0, 345, 270, 117}, {400, 345, 240, 117}, {572, 0, 68, 480},  /* v3 */
        {0, 352, 280, 68}, {400, 352, 240, 68},                         /* v4 */
    };
    if (!saver_active()) { puts2("saver not active, nothing done\n"); return 1; }
    for (unsigned r = 0; r < sizeof ALL / sizeof ALL[0]; r++)
        for (int y = ALL[r].y; y < ALL[r].y + ALL[r].h; y++)
            for (int x = ALL[r].x; x < ALL[r].x + ALL[r].w; x++) {
                u32 f = fb[FBI(x, y)];
                if ((f >> 24) == 0xff && f != BLACK) fb[FBI(x, y)] = BLACK;
            }
    puts2("cleaned\n");
    return 0;
}

static void sleep_ms(int ms)
{
    u32 ts[2] = {(u32)ms / 1000, (u32)(ms % 1000) * 1000000u};
    SC(NR_nanosleep, ts, 0, 0);
}

/* usage: ssoverlay PID [real|fake|fakenofix|calib|clean] [interval_ms] [logfile]
 *   real (default): GPS manager data + the saver's own km/h value. */
static int run(int argc, char **argv)
{
    if (argc < 2) { puts2("usage: ssoverlay PID [real|fake|fakenofix|calib|clean] [interval_ms] [logfile]\n"); return 2; }
    int pid = parse_int(argv[1]);
    int fake = argc > 2 && argv[2][0] == 'f';
    int nofix = fake && argv[2][4] == 'n';
    if (argc > 2 && argv[2][0] == 'c' && argv[2][1] == 'a') { calib = 1; NREG = 4; }
    int interval = argc > 3 ? parse_int(argv[3]) : 250;
    if (argc > 4 && argv[4][0] == '/') sdlog = argv[4];
    if (interval < 100) interval = 100;

    char path[32] = "/proc/", *p = path + 6;
    p = put_u(p, (u32)pid, 1);
    for (const char *q = "/mem"; (*p = *q); p++, q++) ;
    memfd = SC(NR_open, path, 0 /* O_RDONLY */, 0);
    if (memfd < 0) { puts2("cannot open main_app mem\n"); return 1; }
    for (int i = 0; i < NFINGERPRINT; i++) {
        u32 w;
        if (rd(FINGERPRINT[i].addr, &w) || w != FINGERPRINT[i].val) {
            log_kv("main_app fingerprint mismatch at ", FINGERPRINT[i].addr);
            return 1;
        }
    }

    int fbfd = SC(NR_open, "/dev/fb0", 2 /* O_RDWR */, 0);
    if (fbfd < 0) { puts2("cannot open fb0\n"); return 1; }
    u32 v[40];
    if (SC(NR_ioctl, fbfd, 0x4600 /* FBIOGET_VSCREENINFO */, v) < 0 ||
        v[0] != FB_W || v[1] != FB_H || v[6] != 32) {
        puts2("unexpected fb geometry\n");
        return 1;
    }
    long m = sc6(NR_mmap2, 0, FB_W * FB_H * 4, 3 /* RW */, 1 /* SHARED */, fbfd, 0);
    if (m < 0 && m > -4096) { puts2("mmap fb0 failed\n"); return 1; }
    fb = (u32 *)m;
    parse_tz();
    log_kv("tz_off_s=", (u32)tz_off_s);

    struct ksigaction sa = {on_sig, 0, 0, {0, 0}};
    if (sc6(NR_rt_sigaction, 15, (long)&sa, 0, 8, 0, 0) ||  /* SIGTERM */
        sc6(NR_rt_sigaction, 2, (long)&sa, 0, 8, 0, 0)) {   /* SIGINT */
        puts2("rt_sigaction failed\n");
        return 1;
    }

    if (argc > 2 && argv[2][0] == 'c' && argv[2][1] == 'l') return clean();

    int was = -1, ticks = 0;
    static u32 gps[GB_WORDS];
    cb_reset();
    while (!stop) {
        int have = 0;
        if (!fake && !calib) {
            have = !read_gps(gps);
            gx_tick(gps, have);
        }
        int act = saver_active();
        if (act) {
            struct info in;
            u32 t = now_s();
            if (fake) {
                fake_info(&in, t);
                if (nofix) in.valid = 0;
            } else {
                real_info(&in, gps, have);
            }
            local_hm(t, &in);
            compose(&in);
            if (!saver_active()) act = 0;      /* re-check right before writing */
            else if (!flush()) act = 2;        /* region occupied: skip */
        }
        if (act != was) { log_kv("state=", (u32)act); was = act; }
        if (++ticks * interval >= 30000) {
            if (n_clobbered) {
                log_kv("clobbered_frames_30s=", n_clobbered);
                for (int r = 0; r < NREG; r++)
                    if (cb[r].n) {
                        log_kv(" region=", (u32)r); log_kv("  px=", cb[r].n);
                        log_kv("  x0=", (u32)cb[r].x0); log_kv("  x1=", (u32)cb[r].x1);
                        log_kv("  y0=", (u32)cb[r].y0); log_kv("  y1=", (u32)cb[r].y1);
                    }
            }
            cb_reset();
            n_clobbered = 0;
            ticks = 0;
        }
        sleep_ms(interval);
    }
    if (saver_active()) erase();
    gx_flush();
    puts2("exit\n");
    return 0;
}

__attribute__((used)) static void cstart(u32 *sp)
{
    int argc = (int)sp[0];
    char **argv = (char **)(sp + 1);
    SC(NR_exit_group, run(argc, argv), 0, 0);
}
__attribute__((naked, noreturn)) void _start(void)
{
    __asm__ volatile("mov r0, sp\n bic sp, sp, #7\n bl cstart\n");
}
#endif

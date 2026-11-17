/* LD_PRELOAD storage fault injector for the agent-storage benchmark.
 *
 * Applies the same injected medium behaviour to every backend under test:
 * a fixed or dynamically changed latency on each durability barrier
 * (fsync, fdatasync, syncfs, sync_file_range with WAIT_AFTER, msync MS_SYNC)
 * and, while armed, ENOSPC from write-family calls. Only descriptors whose
 * resolved path starts with FAULTFS_ROOT are affected, so harness logs and
 * the interpreter itself are untouched.
 *
 * Environment:
 *   FAULTFS_ROOT      absolute path prefix in scope (required to inject)
 *   FAULTFS_FSYNC_US  initial added latency per barrier, microseconds
 *   FAULTFS_CONTROL   optional file re-read at most every 5 ms; content
 *                     "fsync_us=<n> enospc=<0|1>" replaces both settings
 *   FAULTFS_STATS     optional 4 KiB file of shared counters (survives
 *                     SIGKILL because it is a MAP_SHARED mapping)
 *
 * Build: cc -O2 -shared -fPIC -o faultfs.so faultfs.c -ldl
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/types.h>
#include <sys/uio.h>
#include <time.h>
#include <unistd.h>

struct faultfs_stats {
    unsigned long long barriers;          /* in-scope durability barriers */
    unsigned long long injected_ns;       /* latency added to them */
    unsigned long long enospc_returns;    /* write-family calls refused */
    unsigned long long out_of_scope;      /* barriers on other paths */
};

static const char *g_root;
static size_t g_root_len;
static const char *g_control;
static volatile long g_fsync_us;
static volatile int g_enospc;
static volatile long long g_last_check_ns;
static struct faultfs_stats *g_stats;

static long long now_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (long long)ts.tv_sec * 1000000000LL + ts.tv_nsec;
}

static const char *copy_env(const char *name) {
    /* Copy, never alias: PostgreSQL and Redis overwrite their original
     * argv/environ area to set the process title, which silently turned
     * every in-scope barrier into an out-of-scope one. */
    const char *value = getenv(name);
    return value ? strdup(value) : NULL;
}

__attribute__((constructor)) static void faultfs_init(void) {
    g_root = copy_env("FAULTFS_ROOT");
    g_root_len = g_root ? strlen(g_root) : 0;
    const char *us = getenv("FAULTFS_FSYNC_US");
    g_fsync_us = us ? atol(us) : 0;
    g_control = copy_env("FAULTFS_CONTROL");
    const char *stats = getenv("FAULTFS_STATS");
    if (stats && *stats) {
        int (*real_open)(const char *, int, ...) = dlsym(RTLD_NEXT, "open");
        int fd = real_open(stats, O_RDWR | O_CREAT | O_CLOEXEC, 0600);
        if (fd >= 0) {
            if (ftruncate(fd, 4096) == 0) {
                void *map = mmap(NULL, 4096, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
                if (map != MAP_FAILED) g_stats = (struct faultfs_stats *)map;
            }
            close(fd);
        }
    }
}

static void refresh_control(void) {
    if (!g_control) return;
    long long now = now_ns();
    if (now - g_last_check_ns < 5000000LL) return;
    g_last_check_ns = now;
    int (*real_open)(const char *, int, ...) = dlsym(RTLD_NEXT, "open");
    ssize_t (*real_read)(int, void *, size_t) = dlsym(RTLD_NEXT, "read");
    int fd = real_open(g_control, O_RDONLY | O_CLOEXEC);
    if (fd < 0) return;
    char buf[128];
    ssize_t n = real_read(fd, buf, sizeof(buf) - 1);
    close(fd);
    if (n <= 0) return;
    buf[n] = 0;
    long us = 0;
    int enospc = 0;
    const char *p = strstr(buf, "fsync_us=");
    if (p) us = atol(p + 9);
    p = strstr(buf, "enospc=");
    if (p) enospc = atoi(p + 7);
    g_fsync_us = us;
    g_enospc = enospc;
}

static int in_scope(int fd) {
    if (!g_root_len || fd < 0) return 0;
    char link[64], target[4096];
    snprintf(link, sizeof(link), "/proc/self/fd/%d", fd);
    ssize_t n = readlink(link, target, sizeof(target) - 1);
    if (n <= 0) return 0;
    target[n] = 0;
    return strncmp(target, g_root, g_root_len) == 0;
}

static void barrier_delay(int fd) {
    refresh_control();
    if (!in_scope(fd)) {
        if (g_stats) __atomic_fetch_add(&g_stats->out_of_scope, 1, __ATOMIC_RELAXED);
        return;
    }
    long us = g_fsync_us;
    if (g_stats) __atomic_fetch_add(&g_stats->barriers, 1, __ATOMIC_RELAXED);
    if (us <= 0) return;
    struct timespec ts = {us / 1000000, (us % 1000000) * 1000};
    while (nanosleep(&ts, &ts) != 0 && errno == EINTR) {}
    if (g_stats) __atomic_fetch_add(&g_stats->injected_ns, (unsigned long long)us * 1000ULL, __ATOMIC_RELAXED);
}

static int refuse_write(int fd) {
    refresh_control();
    if (!g_enospc || !in_scope(fd)) return 0;
    if (g_stats) __atomic_fetch_add(&g_stats->enospc_returns, 1, __ATOMIC_RELAXED);
    errno = ENOSPC;
    return 1;
}

#define REAL(name) static __typeof__(&name) real_##name; \
    if (!real_##name) real_##name = (__typeof__(&name))dlsym(RTLD_NEXT, #name)

int fsync(int fd) {
    REAL(fsync);
    int rc = real_fsync(fd);
    if (rc == 0) barrier_delay(fd);
    return rc;
}

int fdatasync(int fd) {
    REAL(fdatasync);
    int rc = real_fdatasync(fd);
    if (rc == 0) barrier_delay(fd);
    return rc;
}

int syncfs(int fd) {
    REAL(syncfs);
    int rc = real_syncfs(fd);
    if (rc == 0) barrier_delay(fd);
    return rc;
}

int sync_file_range(int fd, off64_t offset, off64_t nbytes, unsigned int flags) {
    REAL(sync_file_range);
    int rc = real_sync_file_range(fd, offset, nbytes, flags);
    if (rc == 0 && (flags & SYNC_FILE_RANGE_WAIT_AFTER)) barrier_delay(fd);
    return rc;
}

ssize_t write(int fd, const void *buf, size_t count) {
    REAL(write);
    if (refuse_write(fd)) return -1;
    return real_write(fd, buf, count);
}

ssize_t pwrite(int fd, const void *buf, size_t count, off_t offset) {
    REAL(pwrite);
    if (refuse_write(fd)) return -1;
    return real_pwrite(fd, buf, count, offset);
}

ssize_t pwrite64(int fd, const void *buf, size_t count, off64_t offset) {
    REAL(pwrite64);
    if (refuse_write(fd)) return -1;
    return real_pwrite64(fd, buf, count, offset);
}

ssize_t writev(int fd, const struct iovec *iov, int iovcnt) {
    REAL(writev);
    if (refuse_write(fd)) return -1;
    return real_writev(fd, iov, iovcnt);
}

ssize_t pwritev(int fd, const struct iovec *iov, int iovcnt, off_t offset) {
    REAL(pwritev);
    if (refuse_write(fd)) return -1;
    return real_pwritev(fd, iov, iovcnt, offset);
}

ssize_t pwritev64(int fd, const struct iovec *iov, int iovcnt, off64_t offset) {
    REAL(pwritev64);
    if (refuse_write(fd)) return -1;
    return real_pwritev64(fd, iov, iovcnt, offset);
}

int fallocate(int fd, int mode, off_t offset, off_t len) {
    REAL(fallocate);
    if (refuse_write(fd)) return -1;
    return real_fallocate(fd, mode, offset, len);
}

int fallocate64(int fd, int mode, off64_t offset, off64_t len) {
    REAL(fallocate64);
    if (refuse_write(fd)) return -1;
    return real_fallocate64(fd, mode, offset, len);
}

int posix_fallocate(int fd, off_t offset, off_t len) {
    REAL(posix_fallocate);
    if (refuse_write(fd)) return ENOSPC;
    return real_posix_fallocate(fd, offset, len);
}

int posix_fallocate64(int fd, off64_t offset, off64_t len) {
    REAL(posix_fallocate64);
    if (refuse_write(fd)) return ENOSPC;
    return real_posix_fallocate64(fd, offset, len);
}

int msync(void *addr, size_t length, int flags) {
    REAL(msync);
    int rc = real_msync(addr, length, flags);
    /* msync has no descriptor; charge synchronous flushes when a root is set. */
    if (rc == 0 && (flags & MS_SYNC) && g_root_len) {
        refresh_control();
        long us = g_fsync_us;
        if (g_stats) __atomic_fetch_add(&g_stats->barriers, 1, __ATOMIC_RELAXED);
        if (us > 0) {
            struct timespec ts = {us / 1000000, (us % 1000000) * 1000};
            while (nanosleep(&ts, &ts) != 0 && errno == EINTR) {}
            if (g_stats) __atomic_fetch_add(&g_stats->injected_ns, (unsigned long long)us * 1000ULL, __ATOMIC_RELAXED);
        }
    }
    return rc;
}

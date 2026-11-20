/* Black-box probes for tests/server/rust_manager_parity, preloaded into a
 * manager of either implementation (LD_PRELOAD). Each probe is off unless its
 * environment names it; nothing else changes.
 *
 * Quota: every open(O_CREAT) of an absolute path under $TOFU_PARITY_QUOTA_DIR
 * fails with the kernel's own "Disk quota exceeded" while
 * $TOFU_PARITY_QUOTA_TRIGGER exists, as the 2026-11-18 11:15 account quota did.
 * With $TOFU_PARITY_QUOTA_ARM set, the first SIGTERM the process sends while
 * that file exists renames it to the trigger: the volume fills exactly as the
 * manager stops the old worker (its commit point).
 *
 * Nagle: every socket the process accept()s appends "nodelay=<0|1>" to
 * $TOFU_PARITY_NODELAY_LOG when the process closes it, read from the kernel
 * (getsockopt TCP_NODELAY) at that moment.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <netinet/in.h>
#include <signal.h>
#include <netinet/tcp.h>
#include <stdio.h>
#include <sys/socket.h>
#include <errno.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <unistd.h>

static int quota_full(const char *path, int flags) {
    if (!(flags & O_CREAT) || path == NULL || path[0] != '/')
        return 0;
    const char *dir = getenv("TOFU_PARITY_QUOTA_DIR");
    const char *trigger = getenv("TOFU_PARITY_QUOTA_TRIGGER");
    if (dir == NULL || trigger == NULL || dir[0] == '\0')
        return 0;
    size_t length = strlen(dir);
    if (strncmp(path, dir, length) != 0 || (path[length] != '/' && path[length] != '\0'))
        return 0;
    struct stat info;
    return syscall(SYS_stat, trigger, &info) == 0;
}

#define MODE_ARG(flags, mode) \
    mode_t mode = 0; \
    if ((flags) & (O_CREAT | __O_TMPFILE)) { va_list args; va_start(args, flags); mode = va_arg(args, mode_t); va_end(args); }

#define FORWARD_OPEN(name) \
    int name(const char *path, int flags, ...) { \
        MODE_ARG(flags, mode) \
        if (quota_full(path, flags)) { errno = EDQUOT; return -1; } \
        int (*real)(const char *, int, ...) = (int (*)(const char *, int, ...))dlsym(RTLD_NEXT, #name); \
        return real(path, flags, mode); \
    }

#define FORWARD_OPENAT(name) \
    int name(int dirfd, const char *path, int flags, ...) { \
        MODE_ARG(flags, mode) \
        if (quota_full(path, flags)) { errno = EDQUOT; return -1; } \
        int (*real)(int, const char *, int, ...) = (int (*)(int, const char *, int, ...))dlsym(RTLD_NEXT, #name); \
        return real(dirfd, path, flags, mode); \
    }

FORWARD_OPEN(open)
FORWARD_OPEN(open64)
FORWARD_OPENAT(openat)
FORWARD_OPENAT(openat64)

int kill(pid_t pid, int sig) {
    const char *arm = getenv("TOFU_PARITY_QUOTA_ARM");
    const char *trigger = getenv("TOFU_PARITY_QUOTA_TRIGGER");
    if (sig == SIGTERM && pid > 0 && arm != NULL && trigger != NULL)
        syscall(SYS_rename, arm, trigger);
    int (*real)(pid_t, int) = (int (*)(pid_t, int))dlsym(RTLD_NEXT, "kill");
    return real(pid, sig);
}

#define TRACKED_DESCRIPTORS 65536
static unsigned char accepted[TRACKED_DESCRIPTORS];

static void track(int fd) {
    if (fd >= 0 && fd < TRACKED_DESCRIPTORS && getenv("TOFU_PARITY_NODELAY_LOG") != NULL)
        __atomic_store_n(&accepted[fd], 1, __ATOMIC_SEQ_CST);
}

int accept(int fd, struct sockaddr *address, socklen_t *length) {
    int (*real)(int, struct sockaddr *, socklen_t *) =
        (int (*)(int, struct sockaddr *, socklen_t *))dlsym(RTLD_NEXT, "accept");
    int result = real(fd, address, length);
    track(result);
    return result;
}

int accept4(int fd, struct sockaddr *address, socklen_t *length, int flags) {
    int (*real)(int, struct sockaddr *, socklen_t *, int) =
        (int (*)(int, struct sockaddr *, socklen_t *, int))dlsym(RTLD_NEXT, "accept4");
    int result = real(fd, address, length, flags);
    track(result);
    return result;
}

int close(int fd) {
    if (fd >= 0 && fd < TRACKED_DESCRIPTORS && __atomic_exchange_n(&accepted[fd], 0, __ATOMIC_SEQ_CST)) {
        const char *log = getenv("TOFU_PARITY_NODELAY_LOG");
        int value = -1;
        socklen_t size = sizeof(value);
        if (getsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &value, &size) != 0)
            value = -1;
        if (log != NULL) {
            char line[32];
            int length = snprintf(line, sizeof(line), "nodelay=%d\n", value != 0 && value != -1 ? 1 : value);
            int out = (int)syscall(SYS_open, log, O_WRONLY | O_APPEND | O_CREAT | O_CLOEXEC, 0600);
            if (out >= 0) {
                ssize_t ignored = write(out, line, (size_t)length);
                (void)ignored;
                syscall(SYS_close, out);
            }
        }
    }
    int (*real)(int) = (int (*)(int))dlsym(RTLD_NEXT, "close");
    return real(fd);
}

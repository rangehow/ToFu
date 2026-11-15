// LD_PRELOAD fault shim for scripts/load_resilience_harness.py. It acts only
// inside the tofu-db engine process and only while a trigger file exists:
//
//   LR_VOLUME_TRIGGER  every file call on a path under LR_VOLUME_DIR (except
//                      the WAL) blocks in the kernel: it reads one byte from
//                      the FIFO LR_VOLUME_FIFO, so the thread waits in read(2)
//                      on a filesystem path, like a call on a stalled network
//                      volume. The harness releases waiters by writing bytes.
//   LR_SOCKET_TRIGGER  every socket receive waits LR_SOCKET_DELAY_MS first: a
//                      slow engine that still answers every request.
//   LR_LEASE_TRIGGER   the next intercepted call forks one helper that inherits
//                      every descriptor, including the project lease, ignores
//                      catchable termination signals and exits only after
//                      "<trigger>.release" exists; "<trigger>.held" marks it.
//                      It models a dying engine that keeps the lease after
//                      its PID was signalled.
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/uio.h>
#include <time.h>
#include <unistd.h>

extern char *program_invocation_short_name;

static int is_engine(void) {
    return strcmp(program_invocation_short_name, "tofu-db") == 0;
}

static int armed(const char *variable) {
    const char *trigger = getenv(variable);
    struct stat st;
    return trigger != NULL && stat(trigger, &st) == 0;
}

static ssize_t (*real_read)(int, void *, size_t);

static int volume_path(const char *path) {
    const char *dir = getenv("LR_VOLUME_DIR");
    size_t n = dir ? strlen(dir) : 0;
    size_t length = strlen(path);
    if (n == 0 || strncmp(path, dir, n) != 0) return 0;
    return !(length >= 4 && strcmp(path + length - 4, ".wal") == 0);
}

static int volume_fd(int fd) {
    char link[64], path[4096];
    snprintf(link, sizeof link, "/proc/self/fd/%d", fd);
    ssize_t n = readlink(link, path, sizeof path - 1);
    if (n <= 0) return 0;
    path[n] = 0;
    return volume_path(path);
}

static void volume_wait(void) {
    static int (*real_open)(const char *, int, ...);
    if (!real_open) real_open = dlsym(RTLD_NEXT, "open");
    if (!real_read) real_read = dlsym(RTLD_NEXT, "read");
    int fifo = real_open(getenv("LR_VOLUME_FIFO"), O_RDWR | O_CLOEXEC);
    if (fifo < 0) return;
    char byte;
    while (real_read(fifo, &byte, 1) < 0 && errno == EINTR) {}
    close(fifo);
}

static void maybe_volume_fd(int fd) {
    if (fd > 2 && is_engine() && armed("LR_VOLUME_TRIGGER") && volume_fd(fd)) volume_wait();
}

static void maybe_volume_path(const char *path) {
    if (path && is_engine() && armed("LR_VOLUME_TRIGGER") && volume_path(path)) volume_wait();
}

static void maybe_fork_lease_holder(void) {
    static int forked;
    if (!is_engine() || !armed("LR_LEASE_TRIGGER") || __sync_lock_test_and_set(&forked, 1)) return;
    char held[4096], release[4096];
    snprintf(held, sizeof held, "%s.held", getenv("LR_LEASE_TRIGGER"));
    snprintf(release, sizeof release, "%s.release", getenv("LR_LEASE_TRIGGER"));
    if (fork() != 0) return;
    signal(SIGTERM, SIG_IGN);
    signal(SIGINT, SIG_IGN);
    signal(SIGHUP, SIG_IGN);
    signal(SIGPIPE, SIG_IGN);
    int marker = open(held, O_CREAT | O_WRONLY | O_CLOEXEC, 0600);
    if (marker >= 0) close(marker);
    struct stat st;
    struct timespec pause = {0, 100 * 1000 * 1000};
    while (stat(release, &st) != 0) nanosleep(&pause, NULL);
    _exit(0);
}

static void maybe_socket_delay(int fd) {
    struct stat st;
    maybe_fork_lease_holder();
    if (!is_engine() || !armed("LR_SOCKET_TRIGGER") || fstat(fd, &st) != 0 || !S_ISSOCK(st.st_mode)) return;
    long ms = atol(getenv("LR_SOCKET_DELAY_MS") ? getenv("LR_SOCKET_DELAY_MS") : "0");
    struct timespec pause = {ms / 1000, (ms % 1000) * 1000000L};
    nanosleep(&pause, NULL);
}

#define FORWARD(ret, name, params, args, check)                  \
    ret name params {                                             \
        static ret (*real) params;                                \
        if (!real) real = dlsym(RTLD_NEXT, #name);                \
        check;                                                    \
        return real args;                                         \
    }

FORWARD(ssize_t, pread64, (int fd, void *b, size_t c, off_t o), (fd, b, c, o), maybe_volume_fd(fd))
FORWARD(ssize_t, pread, (int fd, void *b, size_t c, off_t o), (fd, b, c, o), maybe_volume_fd(fd))
FORWARD(ssize_t, pwrite64, (int fd, const void *b, size_t c, off_t o), (fd, b, c, o), maybe_volume_fd(fd))
FORWARD(ssize_t, pwrite, (int fd, const void *b, size_t c, off_t o), (fd, b, c, o), maybe_volume_fd(fd))
FORWARD(ssize_t, write, (int fd, const void *b, size_t c), (fd, b, c), maybe_volume_fd(fd))
FORWARD(ssize_t, writev, (int fd, const struct iovec *v, int n), (fd, v, n), maybe_volume_fd(fd))
FORWARD(ssize_t, preadv, (int fd, const struct iovec *v, int n, off_t o), (fd, v, n, o), maybe_volume_fd(fd))
FORWARD(ssize_t, pwritev, (int fd, const struct iovec *v, int n, off_t o), (fd, v, n, o), maybe_volume_fd(fd))
FORWARD(int, fsync, (int fd), (fd), maybe_volume_fd(fd))
FORWARD(int, fdatasync, (int fd), (fd), maybe_volume_fd(fd))
FORWARD(ssize_t, recv, (int fd, void *b, size_t c, int f), (fd, b, c, f), maybe_socket_delay(fd))
FORWARD(ssize_t, recvfrom, (int fd, void *b, size_t c, int f, struct sockaddr *a, socklen_t *l),
        (fd, b, c, f, a, l), maybe_socket_delay(fd))
FORWARD(ssize_t, recvmsg, (int fd, struct msghdr *m, int f), (fd, m, f), maybe_socket_delay(fd))

ssize_t read(int fd, void *buffer, size_t count) {
    if (!real_read) real_read = dlsym(RTLD_NEXT, "read");
    maybe_volume_fd(fd);
    maybe_socket_delay(fd);
    return real_read(fd, buffer, count);
}

static mode_t open_mode(int flags, va_list arguments) {
    return (flags & O_CREAT) ? (mode_t)va_arg(arguments, int) : 0;
}

int open(const char *path, int flags, ...) {
    static int (*real)(const char *, int, ...);
    if (!real) real = dlsym(RTLD_NEXT, "open");
    va_list arguments;
    va_start(arguments, flags);
    mode_t mode = open_mode(flags, arguments);
    va_end(arguments);
    maybe_volume_path(path);
    return real(path, flags, mode);
}

int open64(const char *path, int flags, ...) {
    static int (*real)(const char *, int, ...);
    if (!real) real = dlsym(RTLD_NEXT, "open64");
    va_list arguments;
    va_start(arguments, flags);
    mode_t mode = open_mode(flags, arguments);
    va_end(arguments);
    maybe_volume_path(path);
    return real(path, flags, mode);
}

int openat(int dirfd, const char *path, int flags, ...) {
    static int (*real)(int, const char *, int, ...);
    if (!real) real = dlsym(RTLD_NEXT, "openat");
    va_list arguments;
    va_start(arguments, flags);
    mode_t mode = open_mode(flags, arguments);
    va_end(arguments);
    maybe_volume_path(path);
    return real(dirfd, path, flags, mode);
}

int openat64(int dirfd, const char *path, int flags, ...) {
    static int (*real)(int, const char *, int, ...);
    if (!real) real = dlsym(RTLD_NEXT, "openat64");
    va_list arguments;
    va_start(arguments, flags);
    mode_t mode = open_mode(flags, arguments);
    va_end(arguments);
    maybe_volume_path(path);
    return real(dirfd, path, flags, mode);
}

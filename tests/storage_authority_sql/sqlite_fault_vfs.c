/*
** Test-only fault-injecting SQLite VFS: power loss and failed writes.
**
** Loaded as an extension, it registers "tofu-fault" as the DEFAULT VFS,
** wrapping the previous default, so a VFS shim loaded afterwards (the WAL
** coalescing shim) sits above it. It models a volatile write cache: every
** write reaches the file at once, and its pre-image is kept until the file's
** next xSync. A simulated power loss reverts the writes that did not
** survive and exits the process with status 77.
**
** Environment (read at load):
**   TOFU_FAULT_POWER_LOSS_AT_SYNC=N   lose power at the Nth xSync (any file),
**                                     before it runs
**   TOFU_FAULT_POWER_LOSS_AT_WRITE=N  lose power right after the Nth WAL xWrite
**   TOFU_FAULT_MODE=drop|torn|kill    drop: no unsynced write survives;
**                                     torn: unsynced writes survive in issue
**                                     order up to a cut at a seeded 512-byte
**                                     boundary, tearing the write it lands in;
**                                     kill: every write survives (process crash)
**   TOFU_FAULT_SEED=S                 seed for the torn cut
**   TOFU_FAULT_FAIL_WAL_WRITE=N       the Nth WAL xWrite fails with
**                                     SQLITE_IOERR_WRITE and writes nothing
**   TOFU_FAULT_FAIL_DB_WRITE=N        the same for the Nth database xWrite
** SQL functions on the loading connection: tofu_fault_wal_writes(),
** tofu_fault_db_writes() and tofu_fault_syncs() return the xWrite calls that
** reached WAL files and main database files, and the xSync calls on any file.
*/
#include "sqlite3ext.h"
SQLITE_EXTENSION_INIT1
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define SECTOR 512

typedef struct Pending Pending;
struct Pending {
  sqlite3_int64 offset;
  int amount;
  unsigned char *before; /* pre-image, zero beyond the old end */
  Pending *next;
};

typedef struct FaultFile FaultFile;
struct FaultFile {
  sqlite3_file base;
  sqlite3_file *real;
  int is_wal;
  int is_db;
  sqlite3_int64 synced_size;
  Pending *head;
  Pending *tail;
  FaultFile *next_file;
};

static sqlite3_vfs fault_vfs;
static FaultFile *open_files;
static long long syncs, wal_writes, db_writes;
static long long loss_at_sync, loss_at_write, fail_wal_write, fail_db_write;
static int mode; /* 0 drop, 1 torn, 2 kill */
static unsigned long long seed;

static sqlite3_vfs *below(void) { return (sqlite3_vfs *)fault_vfs.pAppData; }

static void forget(FaultFile *file) {
  Pending *pending = file->head;
  while (pending) {
    Pending *next = pending->next;
    free(pending->before);
    free(pending);
    pending = next;
  }
  file->head = file->tail = 0;
}

static unsigned long long next_random(void) {
  seed = seed * 6364136223846793005ULL + 1442695040888963407ULL;
  return seed >> 17;
}

static void revert(FaultFile *file, sqlite3_int64 keep_bytes) {
  /* Keep the first keep_bytes of the pending stream; undo the rest newest
  ** first so overlapping writes restore the oldest pre-image last. */
  Pending **stack;
  int depth = 0;
  sqlite3_int64 consumed = 0;
  sqlite3_int64 end = file->synced_size;
  Pending *pending;
  for (pending = file->head; pending; pending = pending->next) depth++;
  stack = malloc(sizeof(Pending *) * (depth ? depth : 1));
  depth = 0;
  for (pending = file->head; pending; pending = pending->next) stack[depth++] = pending;
  for (pending = file->head; pending; pending = pending->next) {
    sqlite3_int64 kept = keep_bytes - consumed;
    if (kept > pending->amount) kept = pending->amount;
    if (kept < 0) kept = 0;
    if (kept > 0 && pending->offset + kept > end) end = pending->offset + kept;
    consumed += pending->amount;
  }
  consumed = 0;
  {
    sqlite3_int64 *starts = malloc(sizeof(sqlite3_int64) * (depth ? depth : 1));
    int index;
    for (index = 0; index < depth; index++) {
      starts[index] = consumed;
      consumed += stack[index]->amount;
    }
    for (index = depth - 1; index >= 0; index--) {
      Pending *p = stack[index];
      sqlite3_int64 kept = keep_bytes - starts[index];
      if (kept >= p->amount) continue;
      if (kept < 0) kept = 0;
      file->real->pMethods->xWrite(file->real, p->before + kept, (int)(p->amount - kept),
                                   p->offset + kept);
    }
    free(starts);
  }
  free(stack);
  file->real->pMethods->xTruncate(file->real, end);
}

static void lose_power(void) {
  FaultFile *file;
  sqlite3_int64 total = 0;
  sqlite3_int64 keep = 0;
  for (file = open_files; file; file = file->next_file) {
    Pending *pending;
    for (pending = file->head; pending; pending = pending->next) total += pending->amount;
  }
  if (mode == 2) _exit(77);
  if (mode == 1 && total > 0) {
    keep = (sqlite3_int64)(next_random() % (unsigned long long)(total / SECTOR + 1)) * SECTOR;
  }
  /* The cut applies to the files' pending streams in open order. */
  for (file = open_files; file; file = file->next_file) {
    sqlite3_int64 mine = 0;
    Pending *pending;
    for (pending = file->head; pending; pending = pending->next) mine += pending->amount;
    revert(file, keep < mine ? keep : mine);
    keep = keep > mine ? keep - mine : 0;
  }
  _exit(77);
}

static int fault_close(sqlite3_file *handle) {
  FaultFile *file = (FaultFile *)handle;
  FaultFile **link;
  for (link = &open_files; *link; link = &(*link)->next_file) {
    if (*link == file) { *link = file->next_file; break; }
  }
  forget(file);
  return file->real->pMethods->xClose(file->real);
}

static int fault_read(sqlite3_file *handle, void *data, int amount, sqlite3_int64 offset) {
  FaultFile *file = (FaultFile *)handle;
  return file->real->pMethods->xRead(file->real, data, amount, offset);
}

static int fault_write(sqlite3_file *handle, const void *data, int amount, sqlite3_int64 offset) {
  FaultFile *file = (FaultFile *)handle;
  Pending *pending;
  int rc;
  if (file->is_wal) {
    wal_writes++;
    if (fail_wal_write && wal_writes == fail_wal_write) return SQLITE_IOERR_WRITE;
  }
  if (file->is_db) {
    db_writes++;
    if (fail_db_write && db_writes == fail_db_write) return SQLITE_IOERR_WRITE;
  }
  pending = calloc(1, sizeof(*pending));
  pending->offset = offset;
  pending->amount = amount;
  pending->before = calloc(1, (size_t)amount);
  rc = file->real->pMethods->xRead(file->real, pending->before, amount, offset);
  if (rc != SQLITE_OK && rc != SQLITE_IOERR_SHORT_READ) {
    free(pending->before);
    free(pending);
    return rc;
  }
  if (file->tail) file->tail->next = pending; else file->head = pending;
  file->tail = pending;
  rc = file->real->pMethods->xWrite(file->real, data, amount, offset);
  if (file->is_wal && loss_at_write && wal_writes == loss_at_write) lose_power();
  return rc;
}

static int fault_truncate(sqlite3_file *handle, sqlite3_int64 size) {
  FaultFile *file = (FaultFile *)handle;
  /* A truncation is modelled as durable at once (journaled metadata). */
  if (size < file->synced_size) file->synced_size = size;
  return file->real->pMethods->xTruncate(file->real, size);
}

static int fault_sync(sqlite3_file *handle, int flags) {
  FaultFile *file = (FaultFile *)handle;
  int rc;
  syncs++;
  if (loss_at_sync && syncs == loss_at_sync) lose_power();
  rc = file->real->pMethods->xSync(file->real, flags);
  if (rc == SQLITE_OK) {
    forget(file);
    file->real->pMethods->xFileSize(file->real, &file->synced_size);
  }
  return rc;
}

static int fault_file_size(sqlite3_file *handle, sqlite3_int64 *size) {
  FaultFile *file = (FaultFile *)handle;
  return file->real->pMethods->xFileSize(file->real, size);
}

static int fault_lock(sqlite3_file *handle, int lock) {
  return ((FaultFile *)handle)->real->pMethods->xLock(((FaultFile *)handle)->real, lock);
}

static int fault_unlock(sqlite3_file *handle, int lock) {
  return ((FaultFile *)handle)->real->pMethods->xUnlock(((FaultFile *)handle)->real, lock);
}

static int fault_check_reserved_lock(sqlite3_file *handle, int *out) {
  FaultFile *file = (FaultFile *)handle;
  return file->real->pMethods->xCheckReservedLock(file->real, out);
}

static int fault_file_control(sqlite3_file *handle, int op, void *argument) {
  FaultFile *file = (FaultFile *)handle;
  return file->real->pMethods->xFileControl(file->real, op, argument);
}

static int fault_sector_size(sqlite3_file *handle) {
  FaultFile *file = (FaultFile *)handle;
  return file->real->pMethods->xSectorSize(file->real);
}

static int fault_device_characteristics(sqlite3_file *handle) {
  FaultFile *file = (FaultFile *)handle;
  return file->real->pMethods->xDeviceCharacteristics(file->real);
}

static int fault_shm_map(sqlite3_file *handle, int region, int size, int extend, void volatile **out) {
  FaultFile *file = (FaultFile *)handle;
  return file->real->pMethods->xShmMap(file->real, region, size, extend, out);
}

static int fault_shm_lock(sqlite3_file *handle, int offset, int count, int flags) {
  FaultFile *file = (FaultFile *)handle;
  return file->real->pMethods->xShmLock(file->real, offset, count, flags);
}

static void fault_shm_barrier(sqlite3_file *handle) {
  FaultFile *file = (FaultFile *)handle;
  file->real->pMethods->xShmBarrier(file->real);
}

static int fault_shm_unmap(sqlite3_file *handle, int delete_flag) {
  FaultFile *file = (FaultFile *)handle;
  return file->real->pMethods->xShmUnmap(file->real, delete_flag);
}

static int fault_fetch(sqlite3_file *handle, sqlite3_int64 offset, int amount, void **out) {
  /* Memory-mapped reads would bypass the pre-image model; decline them. */
  (void)handle; (void)offset; (void)amount;
  *out = 0;
  return SQLITE_OK;
}

static int fault_unfetch(sqlite3_file *handle, sqlite3_int64 offset, void *pointer) {
  (void)handle; (void)offset; (void)pointer;
  return SQLITE_OK;
}

static const sqlite3_io_methods fault_io_methods = {
  3, fault_close, fault_read, fault_write, fault_truncate, fault_sync, fault_file_size,
  fault_lock, fault_unlock, fault_check_reserved_lock, fault_file_control,
  fault_sector_size, fault_device_characteristics, fault_shm_map, fault_shm_lock,
  fault_shm_barrier, fault_shm_unmap, fault_fetch, fault_unfetch,
};

static int fault_open(sqlite3_vfs *vfs, const char *name, sqlite3_file *handle, int flags, int *out_flags) {
  FaultFile *file = (FaultFile *)handle;
  int rc;
  (void)vfs;
  if (!(flags & (SQLITE_OPEN_WAL | SQLITE_OPEN_MAIN_DB)) || name == 0) {
    return below()->xOpen(below(), name, handle, flags, out_flags);
  }
  memset(file, 0, sizeof(*file));
  file->real = (sqlite3_file *)&file[1];
  rc = below()->xOpen(below(), name, file->real, flags, out_flags);
  if (rc != SQLITE_OK) {
    if (file->real->pMethods) file->real->pMethods->xClose(file->real);
    file->base.pMethods = 0;
    return rc;
  }
  file->base.pMethods = &fault_io_methods;
  file->is_wal = (flags & SQLITE_OPEN_WAL) != 0;
  file->is_db = (flags & SQLITE_OPEN_MAIN_DB) != 0;
  file->real->pMethods->xFileSize(file->real, &file->synced_size);
  file->next_file = open_files;
  open_files = file;
  return SQLITE_OK;
}

static int fault_delete(sqlite3_vfs *v, const char *n, int s) { (void)v; return below()->xDelete(below(), n, s); }
static int fault_access(sqlite3_vfs *v, const char *n, int f, int *r) { (void)v; return below()->xAccess(below(), n, f, r); }
static int fault_full_pathname(sqlite3_vfs *v, const char *n, int s, char *o) { (void)v; return below()->xFullPathname(below(), n, s, o); }
static void *fault_dl_open(sqlite3_vfs *v, const char *p) { (void)v; return below()->xDlOpen(below(), p); }
static void fault_dl_error(sqlite3_vfs *v, int s, char *m) { (void)v; below()->xDlError(below(), s, m); }
static void (*fault_dl_sym(sqlite3_vfs *v, void *h, const char *s))(void) { (void)v; return below()->xDlSym(below(), h, s); }
static void fault_dl_close(sqlite3_vfs *v, void *h) { (void)v; below()->xDlClose(below(), h); }
static int fault_randomness(sqlite3_vfs *v, int s, char *o) { (void)v; return below()->xRandomness(below(), s, o); }
static int fault_sleep(sqlite3_vfs *v, int us) { (void)v; return below()->xSleep(below(), us); }
static int fault_current_time(sqlite3_vfs *v, double *t) { (void)v; return below()->xCurrentTime(below(), t); }
static int fault_get_last_error(sqlite3_vfs *v, int s, char *m) { (void)v; return below()->xGetLastError(below(), s, m); }
static int fault_current_time_int64(sqlite3_vfs *v, sqlite3_int64 *t) { (void)v; return below()->xCurrentTimeInt64(below(), t); }

static void wal_write_count(sqlite3_context *context, int argc, sqlite3_value **argv) {
  (void)argc; (void)argv;
  sqlite3_result_int64(context, wal_writes);
}

static void db_write_count(sqlite3_context *context, int argc, sqlite3_value **argv) {
  (void)argc; (void)argv;
  sqlite3_result_int64(context, db_writes);
}

static void sync_count(sqlite3_context *context, int argc, sqlite3_value **argv) {
  (void)argc; (void)argv;
  sqlite3_result_int64(context, syncs);
}

static long long env_number(const char *name) {
  const char *value = getenv(name);
  return value ? atoll(value) : 0;
}

int sqlite3_tofufaultvfs_init(sqlite3 *db, char **error, const sqlite3_api_routines *api) {
  const char *chosen;
  sqlite3_vfs *previous;
  (void)error;
  SQLITE_EXTENSION_INIT2(api);
  sqlite3_create_function(db, "tofu_fault_wal_writes", 0, SQLITE_UTF8, 0, wal_write_count, 0, 0);
  sqlite3_create_function(db, "tofu_fault_syncs", 0, SQLITE_UTF8, 0, sync_count, 0, 0);
  sqlite3_create_function(db, "tofu_fault_db_writes", 0, SQLITE_UTF8, 0, db_write_count, 0, 0);
  if (sqlite3_vfs_find("tofu-fault")) return SQLITE_OK_LOAD_PERMANENTLY;
  loss_at_sync = env_number("TOFU_FAULT_POWER_LOSS_AT_SYNC");
  loss_at_write = env_number("TOFU_FAULT_POWER_LOSS_AT_WRITE");
  fail_wal_write = env_number("TOFU_FAULT_FAIL_WAL_WRITE");
  fail_db_write = env_number("TOFU_FAULT_FAIL_DB_WRITE");
  seed = (unsigned long long)env_number("TOFU_FAULT_SEED") * 2654435761ULL + 1;
  chosen = getenv("TOFU_FAULT_MODE");
  mode = chosen && !strcmp(chosen, "torn") ? 1 : chosen && !strcmp(chosen, "kill") ? 2 : 0;
  previous = sqlite3_vfs_find(0);
  fault_vfs.iVersion = 2;
  fault_vfs.szOsFile = (int)sizeof(FaultFile) + previous->szOsFile;
  fault_vfs.mxPathname = previous->mxPathname;
  fault_vfs.zName = "tofu-fault";
  fault_vfs.pAppData = previous;
  fault_vfs.xOpen = fault_open;
  fault_vfs.xDelete = fault_delete;
  fault_vfs.xAccess = fault_access;
  fault_vfs.xFullPathname = fault_full_pathname;
  fault_vfs.xDlOpen = fault_dl_open;
  fault_vfs.xDlError = fault_dl_error;
  fault_vfs.xDlSym = fault_dl_sym;
  fault_vfs.xDlClose = fault_dl_close;
  fault_vfs.xRandomness = fault_randomness;
  fault_vfs.xSleep = fault_sleep;
  fault_vfs.xCurrentTime = fault_current_time;
  fault_vfs.xGetLastError = fault_get_last_error;
  fault_vfs.xCurrentTimeInt64 = fault_current_time_int64;
  return sqlite3_vfs_register(&fault_vfs, 1) == SQLITE_OK ? SQLITE_OK_LOAD_PERMANENTLY : SQLITE_ERROR;
}

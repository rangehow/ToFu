/*
** tofu-wal-coalesce: a SQLite VFS shim that writes each commit's WAL frames
** with one write call.
**
** Responsibility: SQLite writes every WAL frame as two calls (the 24-byte
** frame header, then the page). On a userspace network filesystem each call
** is a round trip (DolphinFS: about 0.19 ms), which dominates a commit. This
** shim keeps the contiguous WAL appends of one transaction in memory and
** hands them to the underlying VFS as one xWrite. Durability is unchanged:
** every buffered byte reaches the underlying file before any xSync, xRead,
** xTruncate, xFileSize, lock or shared-memory call on the WAL or its database,
** and before the transaction's commit frame is reported written. The bytes
** written and the order of writes relative to syncs are those of stock
** SQLite. See docs/STORAGE.md, "The Sidecar coalesces each commit's WAL
** writes".
**
** Entry point: sqlite3_tofuwalcoalesce_init (a loadable extension). It
** registers the VFS "tofu-wal-coalesce" (never the default) wrapping the VFS
** that is the default when it loads, and stays loaded for the process.
** Connections opt in with the URI parameter vfs=tofu-wal-coalesce.
**
** Dependencies: SQLite >= 3.31 (sqlite3_filename_database) through the
** extension API table; the C standard library.
*/
#include "sqlite3ext.h"
SQLITE_EXTENSION_INIT1
#include <limits.h>
#include <string.h>

#if SQLITE_VERSION_NUMBER < 3031000
#error "tofu-wal-coalesce needs SQLite 3.31 or newer (sqlite3_filename_database)"
#endif

#define TOFU_WAL_COALESCE_VFS_NAME "tofu-wal-coalesce"
/* SQLite WAL file format: a frame header is 24 bytes; bytes 4..7 hold the
** database size in pages for a commit frame and zero for any other frame. */
#define WAL_FRAME_HEADER_BYTES 24
/* The unix VFS issues one pwrite of at most 0x1ffff bytes per xWrite and
** fails a larger xWrite with SQLITE_FULL once the remainder masks to zero
** (os_unix.c seekAndWriteFd: "nBuf &= 0x1ffff"). A flush larger than that is
** several calls of this size. */
#define UNIX_VFS_WRITE_LIMIT 0x1ffff

typedef struct CoalesceFile CoalesceFile;
struct CoalesceFile {
  sqlite3_file base;
  sqlite3_file *real;
  int is_wal;
  /* Main database file: the pager's filename pointer, its open WAL, and the
  ** registry link that lets the WAL find it. */
  const char *database_name;
  CoalesceFile *wal;
  CoalesceFile *next_database;
  /* WAL file. */
  CoalesceFile *database;
  unsigned char *buffer;
  sqlite3_int64 buffer_offset;
  int buffer_bytes;
  int buffer_capacity;
  /* End of the bytes this handle wrote since the WAL header was last written
  ** at offset 0; a write below it rewrites earlier bytes and goes straight
  ** through, as stock SQLite issues it. */
  sqlite3_int64 written_end;
  /* Offset of the page that completes a buffered commit frame, or -1. */
  sqlite3_int64 commit_body_offset;
  /* A flush failure raised where SQLite ignores the result (a barrier or an
  ** unlock); the next call on the WAL reports it once. */
  int unreported_rc;
};

static sqlite3_vfs coalesce_vfs;
static sqlite3_mutex *registry_mutex;
static CoalesceFile *registry_head;

static sqlite3_vfs *root_vfs(void) { return (sqlite3_vfs *)coalesce_vfs.pAppData; }

static int wal_flush(CoalesceFile *wal) {
  int rc = SQLITE_OK;
  int done = 0;
  int bytes = wal->buffer_bytes;
  wal->buffer_bytes = 0;
  wal->commit_body_offset = -1;
  while (rc == SQLITE_OK && done < bytes) {
    int amount = bytes - done < UNIX_VFS_WRITE_LIMIT ? bytes - done : UNIX_VFS_WRITE_LIMIT;
    rc = wal->real->pMethods->xWrite(wal->real, wal->buffer + done, amount,
                                     wal->buffer_offset + done);
    done += amount;
  }
  return rc;
}

/* Flush, then report any failure that an earlier flush could not return. */
static int wal_settle(CoalesceFile *wal) {
  int rc = wal_flush(wal);
  if (rc == SQLITE_OK && wal->unreported_rc != SQLITE_OK) {
    rc = wal->unreported_rc;
    wal->unreported_rc = SQLITE_OK;
  }
  return rc;
}

/* Called from database-file methods: their result belongs to the database
** operation, so a WAL flush failure is kept for the WAL's next call. */
static void database_flushes_wal(CoalesceFile *database) {
  CoalesceFile *wal = database->wal;
  int rc;
  if (wal == 0) return;
  rc = wal_flush(wal);
  if (rc != SQLITE_OK && wal->unreported_rc == SQLITE_OK) wal->unreported_rc = rc;
}

static int wal_reserve(CoalesceFile *wal, int needed) {
  int capacity;
  unsigned char *grown;
  if (needed <= wal->buffer_capacity) return 1;
  capacity = wal->buffer_capacity > 0 ? wal->buffer_capacity : needed;
  while (capacity < needed) {
    capacity = capacity > INT_MAX / 2 ? INT_MAX : capacity * 2;
  }
  grown = (unsigned char *)sqlite3_realloc64(wal->buffer, (sqlite3_uint64)capacity);
  if (grown == 0) return 0;
  wal->buffer = grown;
  wal->buffer_capacity = capacity;
  return 1;
}

static unsigned int big_endian_32(const unsigned char *bytes) {
  return ((unsigned int)bytes[0] << 24) | ((unsigned int)bytes[1] << 16)
       | ((unsigned int)bytes[2] << 8) | (unsigned int)bytes[3];
}

static int wal_write(CoalesceFile *wal, const void *data, int amount, sqlite3_int64 offset) {
  sqlite3_int64 end = offset + amount;
  int rc;
  int absorbed = 0;
  if (wal->unreported_rc != SQLITE_OK) {
    rc = wal->unreported_rc;
    wal->unreported_rc = SQLITE_OK;
    return rc;
  }
  if (wal->buffer_bytes > 0) {
    sqlite3_int64 start = wal->buffer_offset;
    sqlite3_int64 stop = start + wal->buffer_bytes;
    if (offset >= start && end <= stop) {
      memcpy(wal->buffer + (offset - start), data, (size_t)amount);
      absorbed = 1;
    } else if (offset == stop && amount <= INT_MAX - wal->buffer_bytes
               && wal_reserve(wal, wal->buffer_bytes + amount)) {
      memcpy(wal->buffer + wal->buffer_bytes, data, (size_t)amount);
      wal->buffer_bytes += amount;
      absorbed = 1;
    } else {
      rc = wal_flush(wal);
      if (rc != SQLITE_OK) return rc;
    }
  }
  if (!absorbed) {
    if (offset == 0) wal->written_end = 0;
    if (offset >= wal->written_end && wal_reserve(wal, amount)) {
      memcpy(wal->buffer, data, (size_t)amount);
      wal->buffer_offset = offset;
      wal->buffer_bytes = amount;
      absorbed = 1;
    }
  }
  if (end > wal->written_end) wal->written_end = end;
  if (!absorbed) {
    wal->commit_body_offset = -1;
    return wal->real->pMethods->xWrite(wal->real, data, amount, offset);
  }
  if (offset == wal->commit_body_offset) {
    /* The commit frame is complete: write the transaction now, so a failure
    ** is the commit's failure exactly as a stock frame write would be. */
    return wal_flush(wal);
  }
  if (amount == WAL_FRAME_HEADER_BYTES
      && big_endian_32((const unsigned char *)data + 4) != 0) {
    wal->commit_body_offset = end;
  } else {
    wal->commit_body_offset = -1;
  }
  return SQLITE_OK;
}

static void registry_add(CoalesceFile *database) {
  sqlite3_mutex_enter(registry_mutex);
  database->next_database = registry_head;
  registry_head = database;
  sqlite3_mutex_leave(registry_mutex);
}

static void registry_remove(CoalesceFile *database) {
  CoalesceFile **link;
  sqlite3_mutex_enter(registry_mutex);
  for (link = &registry_head; *link; link = &(*link)->next_database) {
    if (*link == database) {
      *link = database->next_database;
      break;
    }
  }
  if (database->wal) database->wal->database = 0;
  database->wal = 0;
  sqlite3_mutex_leave(registry_mutex);
}

static void registry_pair(CoalesceFile *wal, const char *database_name) {
  CoalesceFile *database;
  sqlite3_mutex_enter(registry_mutex);
  for (database = registry_head; database; database = database->next_database) {
    if (database->database_name == database_name && database->wal == 0) {
      database->wal = wal;
      wal->database = database;
      break;
    }
  }
  sqlite3_mutex_leave(registry_mutex);
}

static void registry_unpair(CoalesceFile *wal) {
  sqlite3_mutex_enter(registry_mutex);
  if (wal->database) wal->database->wal = 0;
  wal->database = 0;
  sqlite3_mutex_leave(registry_mutex);
}

/* Before the operation on a WAL, its buffer reaches the file; before an
** operation on a database, its WAL's buffer does. */
static int before(CoalesceFile *file) {
  if (file->is_wal) return wal_settle(file);
  database_flushes_wal(file);
  return SQLITE_OK;
}

#define REAL(file) (((CoalesceFile *)(file))->real)
#define METHODS(file) (REAL(file)->pMethods)

static int coalesce_close(sqlite3_file *file) {
  CoalesceFile *self = (CoalesceFile *)file;
  int rc;
  int close_rc;
  if (self->is_wal) {
    rc = wal_settle(self);
    registry_unpair(self);
  } else {
    database_flushes_wal(self);
    registry_remove(self);
    rc = SQLITE_OK;
  }
  close_rc = METHODS(file)->xClose(REAL(file));
  sqlite3_free(self->buffer);
  self->buffer = 0;
  self->buffer_capacity = 0;
  return rc != SQLITE_OK ? rc : close_rc;
}

static int coalesce_read(sqlite3_file *file, void *data, int amount, sqlite3_int64 offset) {
  CoalesceFile *self = (CoalesceFile *)file;
  if (self->is_wal) {
    int rc = wal_settle(self);
    if (rc != SQLITE_OK) return rc;
  }
  return METHODS(file)->xRead(REAL(file), data, amount, offset);
}

static int coalesce_write(sqlite3_file *file, const void *data, int amount, sqlite3_int64 offset) {
  CoalesceFile *self = (CoalesceFile *)file;
  if (self->is_wal) return wal_write(self, data, amount, offset);
  database_flushes_wal(self);
  return METHODS(file)->xWrite(REAL(file), data, amount, offset);
}

static int coalesce_truncate(sqlite3_file *file, sqlite3_int64 size) {
  int rc = before((CoalesceFile *)file);
  if (rc != SQLITE_OK) return rc;
  return METHODS(file)->xTruncate(REAL(file), size);
}

static int coalesce_sync(sqlite3_file *file, int flags) {
  int rc = before((CoalesceFile *)file);
  if (rc != SQLITE_OK) return rc;
  return METHODS(file)->xSync(REAL(file), flags);
}

static int coalesce_file_size(sqlite3_file *file, sqlite3_int64 *size) {
  int rc = before((CoalesceFile *)file);
  if (rc != SQLITE_OK) return rc;
  return METHODS(file)->xFileSize(REAL(file), size);
}

static int coalesce_lock(sqlite3_file *file, int lock) {
  int rc = before((CoalesceFile *)file);
  if (rc != SQLITE_OK) return rc;
  return METHODS(file)->xLock(REAL(file), lock);
}

static int coalesce_unlock(sqlite3_file *file, int lock) {
  int rc = before((CoalesceFile *)file);
  if (rc != SQLITE_OK) return rc;
  return METHODS(file)->xUnlock(REAL(file), lock);
}

static int coalesce_check_reserved_lock(sqlite3_file *file, int *reserved) {
  int rc = before((CoalesceFile *)file);
  if (rc != SQLITE_OK) return rc;
  return METHODS(file)->xCheckReservedLock(REAL(file), reserved);
}

static int coalesce_file_control(sqlite3_file *file, int op, void *argument) {
  CoalesceFile *self = (CoalesceFile *)file;
  if (op == SQLITE_FCNTL_VFSNAME && argument != 0) {
    int rc = METHODS(file)->xFileControl(REAL(file), op, argument);
    if (rc == SQLITE_OK) {
      char *below = *(char **)argument;
      *(char **)argument = sqlite3_mprintf("%s/%z", TOFU_WAL_COALESCE_VFS_NAME, below);
    }
    return rc;
  }
  if (self->is_wal) {
    int rc = wal_settle(self);
    if (rc != SQLITE_OK) return rc;
  } else {
    database_flushes_wal(self);
  }
  return METHODS(file)->xFileControl(REAL(file), op, argument);
}

static int coalesce_sector_size(sqlite3_file *file) {
  return METHODS(file)->xSectorSize(REAL(file));
}

static int coalesce_device_characteristics(sqlite3_file *file) {
  return METHODS(file)->xDeviceCharacteristics(REAL(file));
}

static int coalesce_shm_map(sqlite3_file *file, int region, int size, int extend, void volatile **mapped) {
  int rc;
  if (METHODS(file)->iVersion < 2) return SQLITE_IOERR_SHMMAP;
  rc = before((CoalesceFile *)file);
  if (rc != SQLITE_OK) return rc;
  return METHODS(file)->xShmMap(REAL(file), region, size, extend, mapped);
}

static int coalesce_shm_lock(sqlite3_file *file, int offset, int count, int flags) {
  int rc;
  if (METHODS(file)->iVersion < 2) return SQLITE_IOERR_SHMLOCK;
  rc = before((CoalesceFile *)file);
  if (rc != SQLITE_OK) return rc;
  return METHODS(file)->xShmLock(REAL(file), offset, count, flags);
}

static void coalesce_shm_barrier(sqlite3_file *file) {
  CoalesceFile *self = (CoalesceFile *)file;
  if (METHODS(file)->iVersion < 2) return;
  if (self->is_wal) {
    int rc = wal_flush(self);
    if (rc != SQLITE_OK && self->unreported_rc == SQLITE_OK) self->unreported_rc = rc;
  } else {
    database_flushes_wal(self);
  }
  METHODS(file)->xShmBarrier(REAL(file));
}

static int coalesce_shm_unmap(sqlite3_file *file, int delete_flag) {
  int rc;
  if (METHODS(file)->iVersion < 2) return SQLITE_OK;
  rc = before((CoalesceFile *)file);
  if (rc != SQLITE_OK) return rc;
  return METHODS(file)->xShmUnmap(REAL(file), delete_flag);
}

static int coalesce_fetch(sqlite3_file *file, sqlite3_int64 offset, int amount, void **pointer) {
  int rc;
  *pointer = 0;
  if (METHODS(file)->iVersion < 3) return SQLITE_OK;
  rc = before((CoalesceFile *)file);
  if (rc != SQLITE_OK) return rc;
  return METHODS(file)->xFetch(REAL(file), offset, amount, pointer);
}

static int coalesce_unfetch(sqlite3_file *file, sqlite3_int64 offset, void *pointer) {
  if (METHODS(file)->iVersion < 3) return SQLITE_OK;
  return METHODS(file)->xUnfetch(REAL(file), offset, pointer);
}

static const sqlite3_io_methods coalesce_io_methods = {
  3,
  coalesce_close,
  coalesce_read,
  coalesce_write,
  coalesce_truncate,
  coalesce_sync,
  coalesce_file_size,
  coalesce_lock,
  coalesce_unlock,
  coalesce_check_reserved_lock,
  coalesce_file_control,
  coalesce_sector_size,
  coalesce_device_characteristics,
  coalesce_shm_map,
  coalesce_shm_lock,
  coalesce_shm_barrier,
  coalesce_shm_unmap,
  coalesce_fetch,
  coalesce_unfetch,
};

static int coalesce_open(sqlite3_vfs *vfs, const char *name, sqlite3_file *file,
                         int flags, int *out_flags) {
  CoalesceFile *self = (CoalesceFile *)file;
  sqlite3_vfs *root = root_vfs();
  int wrapped = (flags & SQLITE_OPEN_WAL) || ((flags & SQLITE_OPEN_MAIN_DB) && name != 0);
  int rc;
  (void)vfs;
  if (!wrapped) {
    /* Journals, temporary and transient files: the root VFS owns the whole
    ** object, so they carry no shim at all. */
    return root->xOpen(root, name, file, flags, out_flags);
  }
  memset(self, 0, sizeof(*self));
  self->real = (sqlite3_file *)&self[1];
  self->commit_body_offset = -1;
  rc = root->xOpen(root, name, self->real, flags, out_flags);
  if (rc != SQLITE_OK) {
    if (self->real->pMethods) self->real->pMethods->xClose(self->real);
    self->base.pMethods = 0;
    return rc;
  }
  self->base.pMethods = &coalesce_io_methods;
  if (flags & SQLITE_OPEN_WAL) {
    self->is_wal = 1;
    registry_pair(self, sqlite3_filename_database(name));
  } else {
    self->database_name = name;
    registry_add(self);
  }
  return SQLITE_OK;
}

static int coalesce_delete(sqlite3_vfs *vfs, const char *name, int sync_directory) {
  (void)vfs;
  return root_vfs()->xDelete(root_vfs(), name, sync_directory);
}

static int coalesce_access(sqlite3_vfs *vfs, const char *name, int flags, int *result) {
  (void)vfs;
  return root_vfs()->xAccess(root_vfs(), name, flags, result);
}

static int coalesce_full_pathname(sqlite3_vfs *vfs, const char *name, int size, char *out) {
  (void)vfs;
  return root_vfs()->xFullPathname(root_vfs(), name, size, out);
}

static void *coalesce_dl_open(sqlite3_vfs *vfs, const char *path) {
  (void)vfs;
  return root_vfs()->xDlOpen(root_vfs(), path);
}

static void coalesce_dl_error(sqlite3_vfs *vfs, int size, char *message) {
  (void)vfs;
  root_vfs()->xDlError(root_vfs(), size, message);
}

static void (*coalesce_dl_sym(sqlite3_vfs *vfs, void *handle, const char *symbol))(void) {
  (void)vfs;
  return root_vfs()->xDlSym(root_vfs(), handle, symbol);
}

static void coalesce_dl_close(sqlite3_vfs *vfs, void *handle) {
  (void)vfs;
  root_vfs()->xDlClose(root_vfs(), handle);
}

static int coalesce_randomness(sqlite3_vfs *vfs, int size, char *out) {
  (void)vfs;
  return root_vfs()->xRandomness(root_vfs(), size, out);
}

static int coalesce_sleep(sqlite3_vfs *vfs, int microseconds) {
  (void)vfs;
  return root_vfs()->xSleep(root_vfs(), microseconds);
}

static int coalesce_current_time(sqlite3_vfs *vfs, double *now) {
  (void)vfs;
  return root_vfs()->xCurrentTime(root_vfs(), now);
}

static int coalesce_get_last_error(sqlite3_vfs *vfs, int size, char *message) {
  (void)vfs;
  return root_vfs()->xGetLastError ? root_vfs()->xGetLastError(root_vfs(), size, message) : 0;
}

static int coalesce_current_time_int64(sqlite3_vfs *vfs, sqlite3_int64 *now) {
  (void)vfs;
  return root_vfs()->xCurrentTimeInt64(root_vfs(), now);
}

static int coalesce_set_system_call(sqlite3_vfs *vfs, const char *name, sqlite3_syscall_ptr call) {
  (void)vfs;
  return root_vfs()->xSetSystemCall(root_vfs(), name, call);
}

static sqlite3_syscall_ptr coalesce_get_system_call(sqlite3_vfs *vfs, const char *name) {
  (void)vfs;
  return root_vfs()->xGetSystemCall(root_vfs(), name);
}

static const char *coalesce_next_system_call(sqlite3_vfs *vfs, const char *name) {
  (void)vfs;
  return root_vfs()->xNextSystemCall(root_vfs(), name);
}

#ifdef _WIN32
__declspec(dllexport)
#endif
int sqlite3_tofuwalcoalesce_init(sqlite3 *db, char **error, const sqlite3_api_routines *api) {
  sqlite3_vfs *root;
  (void)db;
  SQLITE_EXTENSION_INIT2(api);
  if (sqlite3_vfs_find(TOFU_WAL_COALESCE_VFS_NAME) != 0) return SQLITE_OK_LOAD_PERMANENTLY;
  if (sqlite3_libversion_number() < 3031000) {
    *error = sqlite3_mprintf("%s needs SQLite 3.31 or newer", TOFU_WAL_COALESCE_VFS_NAME);
    return SQLITE_ERROR;
  }
  root = sqlite3_vfs_find(0);
  if (root == 0 || root->iVersion < 2) {
    *error = sqlite3_mprintf("%s needs a version 2 default VFS", TOFU_WAL_COALESCE_VFS_NAME);
    return SQLITE_ERROR;
  }
  registry_mutex = sqlite3_mutex_alloc(SQLITE_MUTEX_FAST);
  if (registry_mutex == 0 && sqlite3_threadsafe()) return SQLITE_NOMEM;
  coalesce_vfs.iVersion = root->iVersion < 3 ? root->iVersion : 3;
  coalesce_vfs.szOsFile = (int)sizeof(CoalesceFile) + root->szOsFile;
  coalesce_vfs.mxPathname = root->mxPathname;
  coalesce_vfs.zName = TOFU_WAL_COALESCE_VFS_NAME;
  coalesce_vfs.pAppData = root;
  coalesce_vfs.xOpen = coalesce_open;
  coalesce_vfs.xDelete = coalesce_delete;
  coalesce_vfs.xAccess = coalesce_access;
  coalesce_vfs.xFullPathname = coalesce_full_pathname;
  coalesce_vfs.xDlOpen = coalesce_dl_open;
  coalesce_vfs.xDlError = coalesce_dl_error;
  coalesce_vfs.xDlSym = coalesce_dl_sym;
  coalesce_vfs.xDlClose = coalesce_dl_close;
  coalesce_vfs.xRandomness = coalesce_randomness;
  coalesce_vfs.xSleep = coalesce_sleep;
  coalesce_vfs.xCurrentTime = coalesce_current_time;
  coalesce_vfs.xGetLastError = coalesce_get_last_error;
  coalesce_vfs.xCurrentTimeInt64 = coalesce_current_time_int64;
  coalesce_vfs.xSetSystemCall = coalesce_set_system_call;
  coalesce_vfs.xGetSystemCall = coalesce_get_system_call;
  coalesce_vfs.xNextSystemCall = coalesce_next_system_call;
  {
    int rc = sqlite3_vfs_register(&coalesce_vfs, 0);
    if (rc != SQLITE_OK) return rc;
  }
  return SQLITE_OK_LOAD_PERMANENTLY;
}

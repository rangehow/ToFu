/* Scratch-only loader: inject a failure into the real initializer and inspect
** the registry before SQLite can unload this test library. */
#include "sqlite3ext.h"
SQLITE_EXTENSION_INIT3

#ifdef FAIL_VFS_REGISTRATION
static int vfs_registration_attempted;

static int fail_vfs_register(sqlite3_vfs *vfs, int make_default) {
  (void)vfs;
  (void)make_default;
  vfs_registration_attempted = 1;
  return SQLITE_NOMEM;
}

#undef sqlite3_vfs_register
#define sqlite3_vfs_register fail_vfs_register
#else
/* -1 means the injected call was never reached. */
static int premature_publication = -1;

static int fail_auto_extension(void (*entry)(void)) {
  (void)entry;
  premature_publication = sqlite3_vfs_find("tofu-wal-coalesce") != 0;
  return SQLITE_NOMEM;
}

#undef sqlite3_auto_extension
#define sqlite3_auto_extension fail_auto_extension
#endif
#define sqlite3_tofuwalcoalesce_init init_under_test
#include "sqlite_wal_coalesce.c"
#undef sqlite3_tofuwalcoalesce_init

int sqlite3_tofuwalcoalesce_init(sqlite3 *db, char **error, const sqlite3_api_routines *api) {
  sqlite3_vfs *original_default = api->vfs_find(0);
  int rc = init_under_test(db, error, api);
#ifdef FAIL_VFS_REGISTRATION
  /* Check cancellation and clean up a broken test build while still loaded. */
  int callback_left = sqlite3_cancel_auto_extension((void (*)(void))register_functions);
#endif
  if (sqlite3_vfs_find(TOFU_WAL_COALESCE_VFS_NAME) == &coalesce_vfs) {
    /* Report the invariant failure without leaving even a broken test build
    ** registered when the loader releases it. Never open the failed VFS. */
    sqlite3_vfs_unregister(&coalesce_vfs);
    *error = sqlite3_mprintf("failed initialization left a VFS registered");
    return SQLITE_ERROR;
  }
#ifdef FAIL_VFS_REGISTRATION
  if (rc == SQLITE_OK_LOAD_PERMANENTLY && vfs_registration_attempted
      && !callback_left && sqlite3_vfs_find(0) == original_default) {
    return rc;
  }
#else
  if (rc == SQLITE_NOMEM && premature_publication == 0 && registry_mutex == 0
      && sqlite3_vfs_find(0) == original_default) {
    *error = sqlite3_mprintf("injected auto-extension registration failure");
    return rc;
  }
#endif
  *error = sqlite3_mprintf("initialization rollback invariant failed");
  return SQLITE_ERROR;
}

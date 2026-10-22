//! SQLite storage reference for the shared Harness graph experiment.
//! Graph semantics, hashes, CAS and receipts live in harness_graph; this module
//! provides scoped rows and genuine WAL/FULL transactions in the same Rust
//! runtime. It is a disposable benchmark adapter, never a production backend.

use crate::{Backend, Rows, Scope};
use rusqlite::{params, Connection, OptionalExtension, TransactionBehavior};
use std::{io, path::Path, time::Duration};

pub struct SqliteBackend {
    connection: Connection,
}

fn sqlite_error(error: rusqlite::Error) -> io::Error {
    let kind = match &error {
        rusqlite::Error::SqliteFailure(code, _) if matches!(code.code,
            rusqlite::ErrorCode::DatabaseBusy | rusqlite::ErrorCode::DatabaseLocked)
            => io::ErrorKind::WouldBlock,
        _ => io::ErrorKind::Other,
    };
    io::Error::new(kind, error)
}

impl SqliteBackend {
    pub fn open(path: &Path) -> io::Result<Self> {
        let connection = Connection::open(path).map_err(sqlite_error)?;
        connection.busy_timeout(Duration::from_secs(30)).map_err(sqlite_error)?;
        connection.pragma_update(None, "journal_mode", "WAL").map_err(sqlite_error)?;
        connection.pragma_update(None, "synchronous", "FULL").map_err(sqlite_error)?;
        connection.pragma_update(None, "foreign_keys", "ON").map_err(sqlite_error)?;
        connection.execute_batch(
            "CREATE TABLE IF NOT EXISTS harness_graph_rows (
                tenant BLOB NOT NULL CHECK(length(tenant)=8),
                owner BLOB NOT NULL CHECK(length(owner)=8),
                namespace BLOB NOT NULL,
                key BLOB NOT NULL,
                value BLOB NOT NULL,
                PRIMARY KEY(tenant, owner, namespace, key)
            ) WITHOUT ROWID;"
        ).map_err(sqlite_error)?;
        let backend = Self { connection };
        let configuration = backend.diagnostics()?;
        if configuration["journal_mode"] != "wal" || configuration["synchronous"] != 2
            || configuration["foreign_keys"] != 1 {
            return Err(io::Error::other("SQLite reference requires WAL/FULL and foreign keys"));
        }
        Ok(backend)
    }

    /// Explicit comparison input, not a hidden performance-default change.
    pub fn open_with_cache_mib(path: &Path, cache_mib: usize) -> io::Result<Self> {
        let kib = cache_mib.checked_mul(1024).and_then(|value| i64::try_from(value).ok())
            .filter(|value| *value > 0)
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "cache MiB must be positive and bounded"))?;
        let backend = Self::open(path)?;
        backend.connection.pragma_update(None, "cache_size", -kib).map_err(sqlite_error)?;
        Ok(backend)
    }

    pub fn diagnostics(&self) -> io::Result<serde_json::Value> {
        let integer = |name: &str| self.connection.pragma_query_value(None, name,
            |row| row.get::<_, i64>(0)).map_err(sqlite_error);
        let journal: String = self.connection.pragma_query_value(None, "journal_mode",
            |row| row.get(0)).map_err(sqlite_error)?;
        Ok(serde_json::json!({"sqlite_version": rusqlite::version(),
            "journal_mode": journal, "synchronous": integer("synchronous")?,
            "foreign_keys": integer("foreign_keys")?, "page_size": integer("page_size")?,
            "cache_size": integer("cache_size")?, "mmap_size": integer("mmap_size")?,
            "wal_autocheckpoint": integer("wal_autocheckpoint")?,
            "busy_timeout_ms": integer("busy_timeout")?}))
    }
}

struct SqliteRows<'a> {
    connection: &'a Connection,
    tenant: [u8; 8],
    owner: [u8; 8],
    writable: bool,
}

impl Rows for SqliteRows<'_> {
    fn get(&mut self, namespace: &str, key: &str) -> io::Result<Option<Vec<u8>>> {
        self.connection.prepare_cached(
            "SELECT value FROM harness_graph_rows WHERE tenant=?1 AND owner=?2 AND namespace=?3 AND key=?4"
        ).map_err(sqlite_error)?.query_row(params![&self.tenant, &self.owner,
            namespace.as_bytes(), key.as_bytes()], |row| row.get(0))
            .optional().map_err(sqlite_error)
    }

    fn put(&mut self, namespace: &str, key: &str, value: Vec<u8>) -> io::Result<()> {
        if !self.writable {
            return Err(io::Error::new(io::ErrorKind::PermissionDenied, "read transaction cannot write"));
        }
        self.connection.prepare_cached(
            "INSERT INTO harness_graph_rows(tenant,owner,namespace,key,value) VALUES(?1,?2,?3,?4,?5)
             ON CONFLICT(tenant,owner,namespace,key) DO UPDATE SET value=excluded.value"
        ).map_err(sqlite_error)?.execute(params![&self.tenant, &self.owner,
            namespace.as_bytes(), key.as_bytes(), value]).map_err(sqlite_error)?;
        Ok(())
    }
}

impl Backend for SqliteBackend {
    fn transact<T>(&mut self, scope: Scope, write: bool,
        body: impl FnOnce(&mut dyn Rows) -> io::Result<T>) -> io::Result<T> {
        let behavior = if write { TransactionBehavior::Immediate } else { TransactionBehavior::Deferred };
        let transaction = self.connection.transaction_with_behavior(behavior).map_err(sqlite_error)?;
        let result = body(&mut SqliteRows { connection: &transaction,
            tenant: scope.tenant.to_be_bytes(), owner: scope.owner.to_be_bytes(), writable: write });
        match result {
            Ok(value) => {
                transaction.commit().map_err(sqlite_error)?;
                Ok(value)
            }
            Err(error) => {
                transaction.rollback().map_err(sqlite_error)?;
                Err(error)
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    const OWNER: Scope = Scope { tenant: 7, owner: 11 };

    #[test]
    fn sqlite_scoped_rows_preserve_u64_and_survive_reopen() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("reference.sqlite");
        let scope = Scope { tenant: u64::MAX, owner: u64::MAX };
        let mut store = SqliteBackend::open_with_cache_mib(&path, 8).unwrap();
        assert_eq!(store.diagnostics().unwrap()["cache_size"], -8192);
        store.transact(scope, true, |rows| rows.put("nodes", "head", vec![0, 1, 255])).unwrap();
        for foreign in [OWNER, Scope { tenant: u64::MAX, owner: 11 }, Scope { tenant: 7, owner: u64::MAX }] {
            assert_eq!(store.transact(foreign, false, |rows| rows.get("nodes", "head")).unwrap(), None);
        }
        drop(store);
        let mut store = SqliteBackend::open(&path).unwrap();
        assert_eq!(store.transact(scope, false, |rows| rows.get("nodes", "head")).unwrap(), Some(vec![0, 1, 255]));
        assert_eq!(store.diagnostics().unwrap()["synchronous"], 2);
    }

    #[test]
    fn sqlite_failed_body_rolls_back_every_namespace_and_read_cannot_write() {
        let directory = tempfile::tempdir().unwrap();
        let mut store = SqliteBackend::open(&directory.path().join("reference.sqlite")).unwrap();
        let result: io::Result<()> = store.transact(OWNER, true, |rows| {
            rows.put("nodes", "a", b"frame".to_vec())?;
            rows.put("receipts", "r", b"receipt".to_vec())?;
            Err(io::Error::new(io::ErrorKind::InvalidInput, "injected failure before commit"))
        });
        assert_eq!(result.unwrap_err().kind(), io::ErrorKind::InvalidInput);
        store.transact(OWNER, false, |rows| {
            assert_eq!(rows.get("nodes", "a")?, None);
            assert_eq!(rows.get("receipts", "r")?, None);
            Ok(())
        }).unwrap();
        assert_eq!(store.transact(OWNER, false, |rows| rows.put("nodes", "a", vec![]))
            .unwrap_err().kind(), io::ErrorKind::PermissionDenied);
    }

    #[test]
    fn sqlite_reader_keeps_one_snapshot_while_another_writer_commits() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("reference.sqlite");
        let mut first = SqliteBackend::open(&path).unwrap();
        let mut second = SqliteBackend::open(&path).unwrap();
        first.transact(OWNER, true, |rows| rows.put("heads", "branch", b"old".to_vec())).unwrap();
        first.transact(OWNER, false, |rows| {
            assert_eq!(rows.get("heads", "branch")?, Some(b"old".to_vec()));
            second.transact(OWNER, true, |writer| writer.put("heads", "branch", b"new".to_vec()))?;
            assert_eq!(rows.get("heads", "branch")?, Some(b"old".to_vec()));
            Ok(())
        }).unwrap();
        assert_eq!(first.transact(OWNER, false, |rows| rows.get("heads", "branch")).unwrap(), Some(b"new".to_vec()));
    }
}

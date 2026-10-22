//! Existing TofuDB authority adapter; graph rows share its WAL, tree and scope.
use crate::{Backend, Rows, Scope};
use std::{io, path::Path};
use tofu_db::{
    authority::{AuthorityDatabase, AuthorityTransaction},
    entity::EntityKey,
};

pub struct NativeBackend {
    pub database: AuthorityDatabase,
}
impl NativeBackend {
    pub fn diagnostics(&self) -> serde_json::Value {
        serde_json::json!({"owner":"direct AuthorityDatabase", "wal_durability":"Strict",
            "configuration_evidence":"source defaults: wal.rs::WalDurability, block.rs::DEFAULT_BLOCK_CACHE_BYTES, entity.rs::DEFAULT_PAGE_CACHE_BYTES",
            "block_cache_default_bytes":32*1024*1024, "page_cache_default_bytes":16*1024*1024,
            "daemon_resource_probe":false, "daemon_background_maintenance":false,
            "cache_budget_equivalent_to_sqlite":false})
    }
    pub fn open(path: &Path) -> io::Result<Self> {
        // Both Entity/Engine constructors use this default. The prototype never
        // calls an override; fail closed if a future source revision changes it.
        if tofu_db::wal::WalDurability::default() != tofu_db::wal::WalDurability::Strict {
            return Err(io::Error::other(
                "graph experiment requires Strict WAL durability",
            ));
        }
        Ok(Self {
            database: if path.exists() {
                AuthorityDatabase::open(path)?
            } else {
                AuthorityDatabase::initialize(path)?
            },
        })
    }
}
struct NativeRows<'a> {
    database: &'a AuthorityDatabase,
    transaction: &'a mut AuthorityTransaction,
    scope: Scope,
    writable: bool,
    mutated: bool,
}
impl Rows for NativeRows<'_> {
    fn get(&mut self, namespace: &str, key: &str) -> io::Result<Option<Vec<u8>>> {
        self.database.entity_get(
            self.transaction,
            &EntityKey::new(
                self.scope.tenant,
                self.scope.owner,
                namespace,
                key.as_bytes(),
            )?,
        )
    }
    fn put(&mut self, namespace: &str, key: &str, value: Vec<u8>) -> io::Result<()> {
        if !self.writable {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "read-only graph transaction",
            ));
        }
        self.database.entity_put(
            self.transaction,
            EntityKey::new(
                self.scope.tenant,
                self.scope.owner,
                namespace,
                key.as_bytes(),
            )?,
            value,
        )?;
        self.mutated = true;
        Ok(())
    }
}
impl Backend for NativeBackend {
    fn transact<T>(
        &mut self,
        scope: Scope,
        write: bool,
        body: impl FnOnce(&mut dyn Rows) -> io::Result<T>,
    ) -> io::Result<T> {
        let mut transaction = self.database.begin(scope.tenant, scope.owner)?;
        let mut rows = NativeRows {
            database: &self.database,
            transaction: &mut transaction,
            scope,
            writable: write,
            mutated: false,
        };
        let result = body(&mut rows)?;
        let mutated = rows.mutated;
        if mutated {
            self.database.commit(transaction)?;
        }
        Ok(result)
    }
}

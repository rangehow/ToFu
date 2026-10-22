//! One durable decision owner per store. Native uses the existing Engine WAL;
//! SQLite uses one WAL/FULL transaction, including all page bytes and decision.
use crate::tree::{Hash, Pages};
use crate::{Decision, MAX_DECISIONS, MAX_DECISION_BYTES, MAX_PAGE_BYTES};
use fs2::FileExt;
use rusqlite::{params, Connection};
use std::{fs::File, io, path::Path, time::Duration};
use tofu_db::{block::BlockId, engine::Engine, wal::WalDurability};

fn sql(error: rusqlite::Error) -> io::Error {
    io::Error::other(error.to_string())
}
fn invalid(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

pub enum Backend {
    Native(Engine),
    Sqlite {
        connection: Connection,
        _lease: File,
    },
}

impl Backend {
    pub fn native(path: &Path, create: bool) -> io::Result<Self> {
        let engine = if create {
            Engine::initialize(path)?
        } else {
            Engine::open(path)?
        };
        if engine.wal_durability() != WalDurability::Strict {
            return Err(invalid("experiment requires Strict native durability"));
        }
        Ok(Self::Native(engine))
    }

    pub fn sqlite(path: &Path, create: bool) -> io::Result<Self> {
        if create {
            std::fs::create_dir(path)?;
        }
        let lease = File::options()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(path.join("EXPERIMENT_LOCK"))?;
        lease.try_lock_exclusive()?;
        let flags = rusqlite::OpenFlags::SQLITE_OPEN_READ_WRITE
            | if create {
                rusqlite::OpenFlags::SQLITE_OPEN_CREATE
            } else {
                rusqlite::OpenFlags::empty()
            };
        let connection =
            Connection::open_with_flags(path.join("store.sqlite"), flags).map_err(sql)?;
        connection
            .busy_timeout(Duration::from_secs(30))
            .map_err(sql)?;
        connection.execute_batch("PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA cache_size=-32768; PRAGMA mmap_size=0; PRAGMA foreign_keys=ON;").map_err(sql)?;
        if create {
            connection.execute_batch("CREATE TABLE blocks(hash BLOB PRIMARY KEY, payload BLOB NOT NULL) WITHOUT ROWID; CREATE TABLE decisions(sequence INTEGER PRIMARY KEY, payload BLOB NOT NULL);").map_err(sql)?;
        }
        let mode: String = connection
            .query_row("PRAGMA journal_mode", [], |r| r.get(0))
            .map_err(sql)?;
        let sync: i64 = connection
            .query_row("PRAGMA synchronous", [], |r| r.get(0))
            .map_err(sql)?;
        if mode != "wal" || sync != 2 {
            return Err(invalid("SQLite WAL/FULL readback failed"));
        }
        Ok(Self::Sqlite {
            connection,
            _lease: lease,
        })
    }

    pub fn diagnostics(&self) -> io::Result<serde_json::Value> {
        Ok(match self {
            Self::Native(engine) => {
                serde_json::json!({"backend":"native", "wal_durability":format!("{:?}",engine.wal_durability()),"owner":"one existing Engine", "block_cache_default_bytes":32*1024*1024,"entity_database":false,"background_gc":false,"private_pages_durable_before_decision":false})
            }
            Self::Sqlite { connection, .. } => {
                let mut values = serde_json::Map::new();
                values.insert("backend".into(), "sqlite".into());
                values.insert("sqlite_version".into(), rusqlite::version().into());
                values.insert(
                    "journal_mode".into(),
                    connection
                        .query_row("PRAGMA journal_mode", [], |r| r.get::<_, String>(0))
                        .map_err(sql)?
                        .into(),
                );
                for name in [
                    "synchronous",
                    "cache_size",
                    "page_size",
                    "mmap_size",
                    "wal_autocheckpoint",
                ] {
                    values.insert(
                        name.into(),
                        connection
                            .query_row(&format!("PRAGMA {name}"), [], |r| r.get::<_, i64>(0))
                            .map_err(sql)?
                            .into(),
                    );
                }
                values.into()
            }
        })
    }

    pub fn last_sequence(&self) -> io::Result<u64> {
        let value = match self {
            Self::Native(engine) => engine.state().durable_sequence,
            Self::Sqlite { connection, .. } => connection
                .query_row("SELECT coalesce(max(sequence),0) FROM decisions", [], |r| {
                    r.get::<_, u64>(0)
                })
                .map_err(sql)?,
        };
        if value > MAX_DECISIONS {
            return Err(invalid("experiment decision retention bound exceeded"));
        }
        Ok(value)
    }

    pub(crate) fn decision(&self, sequence: u64) -> io::Result<Decision> {
        let bytes = match self {
            Self::Native(engine) => {
                engine
                    .transaction_at(sequence)?
                    .ok_or_else(|| invalid("missing native decision"))?
                    .envelope
                    .inline_payload
            }
            Self::Sqlite { connection, .. } => connection
                .query_row(
                    "SELECT payload FROM decisions WHERE sequence=?1",
                    [sequence],
                    |r| r.get::<_, Vec<u8>>(0),
                )
                .map_err(sql)?,
        };
        if bytes.len() > MAX_DECISION_BYTES {
            return Err(invalid("oversized decision"));
        }
        serde_json::from_slice(&bytes).map_err(Into::into)
    }

    pub fn read_page(&self, hash: Hash) -> io::Result<Vec<u8>> {
        let bytes = match self {
            Self::Native(engine) => engine.read_block(BlockId(hash))?,
            Self::Sqlite { connection, .. } => connection
                .query_row(
                    "SELECT payload FROM blocks WHERE hash=?1",
                    [hash.as_slice()],
                    |r| r.get::<_, Vec<u8>>(0),
                )
                .map_err(sql)?,
        };
        if bytes.len() > MAX_PAGE_BYTES || BlockId::for_payload(&bytes).0 != hash {
            return Err(invalid("durable page identity invalid"));
        }
        Ok(bytes)
    }

    pub fn persist(
        &mut self,
        decision: &[u8],
        pages: &Pages,
        sequence: u64,
        mut after_sql_write: impl FnMut(usize) -> io::Result<()>,
    ) -> io::Result<()> {
        if decision.len() > MAX_DECISION_BYTES {
            return Err(invalid("oversized decision"));
        }
        match self {
            Self::Native(engine) => {
                let payloads: Vec<&[u8]> = pages.values().map(Vec::as_slice).collect();
                let result = engine.commit_transaction(decision, &payloads)?;
                if result.sequence != sequence
                    || result
                        .block_ids
                        .iter()
                        .map(|id| id.0)
                        .ne(pages.keys().copied())
                {
                    return Err(invalid("native commit identity differs"));
                }
                Ok(())
            }
            Self::Sqlite { connection, .. } => {
                let transaction = connection
                    .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)
                    .map_err(sql)?;
                for (index, (hash, payload)) in pages.iter().enumerate() {
                    let inserted = transaction
                        .prepare_cached("INSERT OR IGNORE INTO blocks(hash,payload) VALUES(?1,?2)")
                        .map_err(sql)?
                        .execute(params![hash.as_slice(), payload])
                        .map_err(sql)?;
                    if inserted == 0 {
                        let existing: Vec<u8> = transaction
                            .prepare_cached("SELECT payload FROM blocks WHERE hash=?1")
                            .map_err(sql)?
                            .query_row([hash.as_slice()], |row| row.get(0))
                            .map_err(sql)?;
                        if existing != *payload {
                            return Err(invalid("SQLite content-addressed collision"));
                        }
                    }
                    after_sql_write(index + 1)?;
                }
                transaction
                    .execute(
                        "INSERT INTO decisions(sequence,payload) VALUES(?1,?2)",
                        params![sequence, decision],
                    )
                    .map_err(sql)?;
                after_sql_write(pages.len() + 1)?;
                transaction.commit().map_err(sql)
            }
        }
    }
}

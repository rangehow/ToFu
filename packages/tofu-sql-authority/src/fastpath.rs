//! Opt-in SQLite write-front selection, lineage reconciliation and durable WAL shadow.
//! The backend owns the writer lock and background lifecycle; this owner alone
//! checkpoints an admitted front and publishes immutable snapshot generations.

mod backup;
#[cfg(test)]
mod fault;
mod lifecycle;
mod selection;
mod storage;
#[cfg(test)]
mod tests;

pub use selection::{select, Selection};

use std::fs::{self, File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::{Instant, SystemTime, UNIX_EPOCH};

use crate::error::{OpResult, StorageError};
use rusqlite::Connection;
use serde_json::{json, Value};
use storage::{appended, deadline, io, load, sync_dir, write_json};

pub const SHADOW_DIRNAME: &str = "fastpath-shadow";
const HEADER: u64 = 32;

fn wall() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).expect("wall clock").as_secs_f64()
}

/// Apply to every connection before any write or close can checkpoint the front.
pub fn configure(connection: &Connection) -> OpResult<()> {
    connection.pragma_update(None, "wal_autocheckpoint", 0)?;
    let mut current = 0;
    let code = unsafe {
        rusqlite::ffi::sqlite3_db_config(
            connection.handle(),
            rusqlite::ffi::SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE,
            1i32,
            &mut current,
        )
    };
    if code != rusqlite::ffi::SQLITE_OK {
        return Err(rusqlite::Error::SqliteFailure(rusqlite::ffi::Error::new(code), None).into());
    }
    Ok(())
}

struct State {
    generation: u64,
    shipped: u64,
    snapshot: PathBuf,
    wal: PathBuf,
    recovery_point: f64,
    ships: u64,
    bytes_shipped: u64,
    snapshots: u64,
    failures: u64,
    last_ship: Option<f64>,
    ready: bool,
    manifest_dirty: bool,
}

pub struct Fastpath {
    local_db: PathBuf,
    shadow: PathBuf,
    authority_uuid: String,
    state: Mutex<State>,
    lifecycle: lifecycle::Lifecycle,
    rebase_trigger: u64,
    rebase_budget: u64,
}

impl Fastpath {
    pub fn new(local_db: PathBuf, data_dir: &Path, authority_uuid: String) -> OpResult<Self> {
        let shadow = data_dir.join(SHADOW_DIRNAME);
        fs::create_dir_all(&shadow).map_err(io)?;
        let manifest = load(&shadow.join("manifest.json"))?;
        if let Some(value) = &manifest {
            if value["authority_uuid"].as_str() != Some(authority_uuid.as_str()) {
                return Err(StorageError::new(
                    "database_integrity",
                    "fastpath authority uuid mismatch; refusing to guess",
                )
                .into());
            }
        }
        write_json(
            &local_db.parent().expect("front directory").join("tofu-fastpath.json"),
            &json!({"authority_uuid":authority_uuid,"shadow_dir":shadow}),
        )?;
        let value = manifest.unwrap_or(Value::Null);
        let snapshot = storage::manifest_path(&shadow, &value, "snapshot_file", "snapshot.sqlite3")?;
        let wal = storage::manifest_path(&shadow, &value, "wal_file", "shadow.wal")?;
        let local_wal = appended(&local_db, "-wal");
        let frame = wal_frame_size(&local_wal)?;
        let matching = headers_match(&local_wal, &wal)?;
        let shipped = if matching {
            File::open(&wal).map_err(io)?.sync_all().map_err(io)?;
            floor_frame(storage::size(&wal)?.min(storage::size(&local_wal)?), frame.unwrap_or(0))
        } else {
            0
        };
        let ready = snapshot.is_file() && matching;
        let mut maximum = 512 * 1024 * 1024;
        for path in [local_db.parent().expect("front directory"), shadow.as_path()] {
            if let Ok(free) = storage::free_bytes(path) {
                maximum = maximum.min((free / 50).max(64 * 1024 * 1024));
            }
        }
        let pressure = (storage::size(&local_db)? / 4).max(64 * 1024 * 1024).min(maximum);
        Ok(Self {
            local_db,
            shadow,
            authority_uuid,
            lifecycle: lifecycle::Lifecycle::new(),
            rebase_trigger: pressure - pressure / 16,
            rebase_budget: pressure,
            state: Mutex::new(State {
                generation: value["generation"].as_u64().unwrap_or(0),
                shipped,
                snapshot,
                wal,
                recovery_point: value["recovery_point_at"].as_f64().unwrap_or(0.0),
                ships: 0,
                bytes_shipped: 0,
                snapshots: 0,
                failures: 0,
                last_ship: None,
                ready,
                manifest_dirty: ready && value["wal_shipped_bytes"].as_u64() != Some(shipped),
            }),
        })
    }

    /// Call before readiness while holding the writer. A matching surviving WAL
    /// is forward-shipped instead of checkpointed, preserving its exact lineage.
    pub fn initialize(&self, writer: &Connection, at: Instant) -> OpResult<()> {
        configure(writer)?;
        let mut state = self.state.lock().expect("fastpath state");
        if state.ready {
            self.ship_locked(&mut state, at)
        } else {
            self.snapshot_locked(&mut state, writer, at)
        }
    }

    /// No writer lock is needed: only this owner is permitted to checkpoint.
    pub fn ship(&self) -> OpResult<()> {
        let mut state = self.state.lock().expect("fastpath state");
        let result = self.ship_locked(&mut state, Instant::now() + std::time::Duration::from_secs(30));
        if result.is_err() {
            state.failures += 1;
        }
        result
    }

    fn ship_locked(&self, state: &mut State, at: Instant) -> OpResult<()> {
        if !state.ready {
            return Err(StorageError::new("database_unavailable", "fastpath snapshot initialization required").into());
        }
        let source = appended(&self.local_db, "-wal");
        let Some(frame) = wal_frame_size(&source)? else {
            if state.manifest_dirty {
                self.publish(state)?;
            }
            return Ok(());
        };
        let target = floor_frame(storage::size(&source)?, frame);
        if target < state.shipped || (state.shipped > 0 && !headers_match(&source, &state.wal)?) {
            return Err(StorageError::new(
                "database_integrity",
                "fastpath WAL generation changed outside its checkpoint owner",
            )
            .into());
        }
        if target > state.shipped {
            copy_prefix(&source, &state.wal, state.shipped, target, at)?;
            state.bytes_shipped += target - state.shipped;
            state.shipped = target;
            state.ships += 1;
            state.last_ship = Some(wall());
            state.manifest_dirty = true;
        }
        if state.manifest_dirty {
            self.publish(state)?;
        }
        Ok(())
    }

    /// Caller holds the physical writer throughout the immutable image capture.
    pub fn snapshot(&self, writer: &Connection, at: Instant) -> OpResult<()> {
        let mut state = self.state.lock().expect("fastpath state");
        self.snapshot_locked(&mut state, writer, at)
    }

    fn snapshot_locked(&self, state: &mut State, writer: &Connection, at: Instant) -> OpResult<()> {
        deadline(at)?;
        configure(writer)?;
        if state.ready {
            self.ship_locked(state, at)?;
        }
        storage::reclaim_generations(&self.shadow, &state.snapshot, &state.wal)?;
        let (busy, log, checkpointed): (i64, i64, i64) =
            writer.query_row("PRAGMA wal_checkpoint(TRUNCATE)", [], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))?;
        if busy != 0 || log != checkpointed {
            return Err(StorageError::retryable("database_busy", "Fastpath checkpoint is busy", 100).into());
        }
        state.ready = false;
        let recovery_point = wall();
        let next = state.generation + 1;
        // Unique files keep the prior manifest's complete pair intact even if
        // the process exits between copying the image and publishing its WAL.
        let token = uuid::Uuid::new_v4().simple().to_string();
        let snapshot = self.shadow.join(format!("snapshot-{next}-{token}.sqlite3"));
        let wal = self.shadow.join(format!("shadow-{next}-{token}.wal"));
        storage::require_capacity(&self.shadow, storage::size(&self.local_db)?)?;
        storage::copy_durable(&self.local_db, &snapshot, at)?;
        File::create(&wal).map_err(io)?.sync_all().map_err(io)?;
        sync_dir(&self.shadow)?;
        state.snapshot = snapshot;
        state.wal = wal;
        state.generation = next;
        state.recovery_point = recovery_point;
        state.shipped = 0;
        state.ready = true;
        state.manifest_dirty = true;
        self.publish(state)?;
        storage::reclaim_generations(&self.shadow, &state.snapshot, &state.wal)?;
        state.snapshots += 1;
        Ok(())
    }

    fn publish(&self, state: &mut State) -> OpResult<()> {
        write_json(
            &self.shadow.join("manifest.json"),
            &json!({
                "format":"tofu.fastpath-shadow.v1","authority_uuid":self.authority_uuid,
                "generation":state.generation,"wal_shipped_bytes":state.shipped,
                "snapshot_bytes":storage::size(&state.snapshot)?,"updated_at":wall(),
                "recovery_point_at":state.recovery_point,
                "snapshot_file":state.snapshot.file_name().expect("snapshot name").to_string_lossy(),
                "wal_file":state.wal.file_name().expect("WAL name").to_string_lossy()
            }),
        )?;
        storage::link_replace(&state.snapshot, &self.shadow.join("snapshot.sqlite3"))?;
        storage::link_replace(&state.wal, &self.shadow.join("shadow.wal"))?;
        sync_dir(&self.shadow)?;
        state.manifest_dirty = false;
        Ok(())
    }

    pub fn status(&self) -> Value {
        let state = self.state.lock().expect("fastpath state");
        let local = storage::size(&appended(&self.local_db, "-wal"));
        let bytes = local.as_ref().copied().unwrap_or(0);
        json!({"generation":state.generation,"wal_shipped_bytes":state.shipped,
            "ships":state.ships,"bytes_shipped":state.bytes_shipped,"snapshots":state.snapshots,
            "ship_failures":state.failures,"local_wal_bytes":bytes,
            "ship_lag_bytes":bytes.saturating_sub(state.shipped),
            "wal_size_observation_trustworthy":local.is_ok(),
            "wal_rebase_trigger_bytes":self.rebase_trigger,
            "wal_rebase_budget_bytes":self.rebase_budget,
            "wal_write_pressure_bytes":self.rebase_budget,
            "wal_write_headroom_bytes":self.rebase_budget.saturating_sub(bytes),
            "write_pressure_active":local.is_err() || bytes >= self.rebase_budget,
            "rebase_active":!state.ready,
            "last_ship_age_s":state.last_ship.map(|v| wall()-v)})
    }
}

fn wal_frame_size(path: &Path) -> OpResult<Option<u64>> {
    let Some(header) = storage::header(path)? else { return Ok(None) };
    let page = u32::from_be_bytes(header[8..12].try_into().expect("page size"));
    if !(512..=65536).contains(&page) || !page.is_power_of_two() {
        return Err(StorageError::new("database_integrity", "Invalid fastpath WAL page size").into());
    }
    Ok(Some(24 + u64::from(page)))
}

fn floor_frame(bytes: u64, frame: u64) -> u64 {
    if bytes < HEADER || frame == 0 {
        0
    } else {
        HEADER + (bytes - HEADER) / frame * frame
    }
}

fn headers_match(a: &Path, b: &Path) -> OpResult<bool> {
    Ok(match (storage::header(a)?, storage::header(b)?) {
        (Some(a), Some(b)) => a[16..24] == b[16..24],
        _ => false,
    })
}

fn copy_prefix(source: &Path, destination: &Path, start: u64, end: u64, at: Instant) -> OpResult<()> {
    let mut input = File::open(source).map_err(io)?;
    input.seek(SeekFrom::Start(start)).map_err(io)?;
    let mut output = OpenOptions::new().create(true).truncate(false).write(true).open(destination).map_err(io)?;
    output.seek(SeekFrom::Start(start)).map_err(io)?;
    let mut remaining = end - start;
    let mut buffer = vec![0; 4 * 1024 * 1024];
    while remaining > 0 {
        deadline(at)?;
        let requested = remaining.min(buffer.len() as u64) as usize;
        input.read_exact(&mut buffer[..requested]).map_err(io)?;
        output.write_all(&buffer[..requested]).map_err(io)?;
        remaining -= requested as u64;
    }
    output.set_len(end).map_err(io)?;
    #[cfg(test)]
    fault::check("wal_prefix_sync")?;
    output.sync_all().map_err(io)
}

//! Explicit opt-in admission and fail-closed front/shadow reconciliation.
use super::{storage, SHADOW_DIRNAME};
use crate::error::{OpResult, StorageError};
use rusqlite::Connection;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};
use storage::{appended, io, load, sync_dir, write_json};

pub struct Selection {
    pub path: PathBuf,
    pub active: bool,
    pub reason: String,
    pub mode: String,
    pub benchmark: Value,
}

fn env(name: &str) -> String {
    std::env::var(name).unwrap_or_default().trim().to_owned()
}

pub fn select(data_dir: &Path, classic: &Path) -> OpResult<Selection> {
    let mode = match env("TOFU_STORAGE_FASTPATH").to_lowercase().as_str() {
        "" | "off" => "off",
        "auto" => "auto",
        "required" => "required",
        _ => {
            return Err(StorageError::new(
                "database_invalid_payload",
                "TOFU_STORAGE_FASTPATH must be off, auto, or required",
            )
            .into())
        }
    }
    .to_owned();
    let shadow = data_dir.join(SHADOW_DIRNAME);
    let mut selection = Selection {
        path: classic.to_owned(),
        active: false,
        reason: "disabled by TOFU_STORAGE_FASTPATH=off".into(),
        mode: mode.clone(),
        benchmark: json!({}),
    };
    if mode == "off" {
        require_classic(&shadow)?;
        return Ok(selection);
    }
    let minimum = match env("TOFU_STORAGE_FASTPATH_MIN_SPEEDUP").as_str() {
        "" => 3.0,
        raw => raw
            .parse::<f64>()
            .map_err(|_| StorageError::new("database_invalid_payload", "invalid TOFU_STORAGE_FASTPATH_MIN_SPEEDUP"))?,
    };
    if !(0.0..=1000.0).contains(&minimum) {
        return Err(
            StorageError::new("database_invalid_payload", "TOFU_STORAGE_FASTPATH_MIN_SPEEDUP out of bounds").into()
        );
    }
    let explicit = env("TOFU_STORAGE_FASTPATH_DIR");
    let mut candidates = Vec::new();
    if !explicit.is_empty() {
        candidates.push((PathBuf::from(&explicit), false));
    } else {
        let canonical = fs::canonicalize(data_dir).map_err(io)?;
        let key = hex::encode(Sha256::digest(canonical.to_string_lossy().as_bytes()))[..12].to_owned();
        let state = env("XDG_STATE_HOME");
        let home = env("HOME");
        if !state.is_empty() {
            candidates.push((PathBuf::from(state).join("tofu-fastpath").join(&key), false))
        } else if !home.is_empty() && home != "/" {
            candidates.push((PathBuf::from(home).join(".local/state/tofu-fastpath").join(&key), false))
        }
        candidates
            .push((std::env::temp_dir().join(format!("tofu-fastpath-{}-{key}", unsafe { libc::getuid() })), true));
    }
    let mut data_benchmark = None;
    for (candidate, temporary) in candidates {
        if temporary && !candidate.join("tofu.db").is_file() {
            continue;
        }
        if fs::create_dir_all(&candidate).is_err() {
            continue;
        }
        if explicit.is_empty() {
            let same = fs::metadata(&candidate)
                .and_then(|a| fs::metadata(data_dir).map(|b| a.dev() == b.dev()))
                .unwrap_or(true);
            if same {
                continue;
            }
        }
        let probe = (|| -> OpResult<Value> {
            probe_wal(&candidate)?;
            let source = storage::size(classic)? + storage::size(&appended(classic, "-wal"))?;
            storage::require_capacity(&candidate, if candidate.join("tofu.db").exists() { 0 } else { source })?;
            let data = match data_benchmark {
                Some(v) => v,
                None => {
                    let v = benchmark(data_dir)?;
                    data_benchmark = Some(v);
                    v
                }
            };
            let local = benchmark(&candidate)?;
            let speedup = data / local;
            Ok(json!({"data_dir_median_fsync_ms":(data*1000.0).round()/1000.0,
                "candidate_median_fsync_ms":(local*1000.0).round()/1000.0,"speedup":(speedup*100.0).round()/100.0,"admitted":speedup>=minimum}))
        })();
        let Ok(mut bench) = probe else { continue };
        let admitted = bench["admitted"].as_bool().unwrap_or(false);
        bench.as_object_mut().expect("benchmark object").remove("admitted");
        selection.benchmark = bench;
        if !admitted {
            continue;
        }
        selection.path = reconcile(&candidate, &shadow, classic)?;
        selection.active = true;
        selection.reason = "activated".into();
        return Ok(selection);
    }
    if mode == "required" {
        return Err(StorageError::new("database_unavailable","TOFU_STORAGE_FASTPATH=required but no candidate passed every probe; refusing to boot a slow authority the operator explicitly rejected").into());
    }
    require_classic(&shadow)?;
    selection.reason = "no candidate passed every probe".into();
    Ok(selection)
}

fn require_classic(shadow: &Path) -> OpResult<()> {
    if let Some(value) = load(&shadow.join("manifest.json"))? {
        return Err(StorageError::new("database_unavailable",format!("fastpath durable shadow exists (generation={}); data/tofu.db may be stale and cannot be opened as authority. Stop Tofu and run `python3 scripts/storagectl.py retire-fastpath --confirm`, then set TOFU_STORAGE_FASTPATH=off.",value["generation"])).into());
    }
    Ok(())
}

fn probe_wal(directory: &Path) -> OpResult<()> {
    let path = directory.join(format!(".fastpath-probe-{}.sqlite3", uuid::Uuid::new_v4().simple()));
    let result = (|| -> OpResult<()> {
        {
            let c = Connection::open(&path)?;
            c.execute_batch("PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; CREATE TABLE probe(value TEXT NOT NULL); BEGIN IMMEDIATE; INSERT INTO probe VALUES ('committed'); COMMIT;")?;
        }
        let c = Connection::open(&path)?;
        let row: String = c.query_row("SELECT value FROM probe", [], |r| r.get(0))?;
        if row != "committed" {
            return Err(StorageError::new("database_integrity", "candidate WAL recovery probe failed").into());
        }
        Ok(())
    })();
    for suffix in ["", "-wal", "-shm"] {
        let _ = fs::remove_file(appended(&path, suffix));
    }
    result
}
fn benchmark(directory: &Path) -> OpResult<f64> {
    let path = directory.join(format!(".fastpath-benchmark-{}", uuid::Uuid::new_v4().simple()));
    let result = (|| -> OpResult<f64> {
        let mut file = OpenOptions::new().create_new(true).write(true).open(&path).map_err(io)?;
        let mut bytes = [0u8; 4096];
        File::open("/dev/urandom").map_err(io)?.read_exact(&mut bytes).map_err(io)?;
        let mut samples = Vec::new();
        for _ in 0..16 {
            let at = Instant::now();
            file.seek(SeekFrom::Start(0)).map_err(io)?;
            file.write_all(&bytes).map_err(io)?;
            file.sync_all().map_err(io)?;
            samples.push(at.elapsed().as_secs_f64() * 1000.0);
        }
        samples.sort_by(f64::total_cmp);
        Ok((samples[7] + samples[8]) / 2.0)
    })();
    let _ = fs::remove_file(path);
    result
}

pub(super) fn reconcile(local: &Path, shadow: &Path, classic: &Path) -> OpResult<PathBuf> {
    let path = local.join("tofu.db");
    let mut local_manifest = load(&local.join("tofu-fastpath.json"))?;
    let shadow_manifest = load(&shadow.join("manifest.json"))?;
    if path.exists() && local_manifest.as_ref().and_then(|v| v["shadow_dir"].as_str()) != shadow.to_str() {
        let quarantine = appended(&path, &format!(".foreign-{}", uuid::Uuid::new_v4().simple()));
        for suffix in ["", "-wal", "-shm"] {
            let from = appended(&path, suffix);
            if from.try_exists().map_err(io)? {
                fs::rename(from, appended(&quarantine, suffix)).map_err(io)?;
            }
        }
        sync_dir(local)?;
        local_manifest = None;
    }
    if let (Some(a), Some(b)) = (&local_manifest, &shadow_manifest) {
        if a["authority_uuid"] != b["authority_uuid"] {
            return Err(
                StorageError::new("database_integrity", "fastpath authority uuid mismatch; refusing to guess").into()
            );
        }
    }
    if path.exists() {
        return Ok(path);
    }
    if let Some(manifest) = shadow_manifest {
        let snapshot = storage::manifest_path(shadow, &manifest, "snapshot_file", "snapshot.sqlite3")?;
        if !snapshot.is_file() {
            return Err(StorageError::new("database_integrity", "fastpath shadow snapshot is missing").into());
        }
        let wal = storage::manifest_path(shadow, &manifest, "wal_file", "shadow.wal")?;
        install(&path, &snapshot, &wal)?;
        // Keep the verified restore connection alive until its replay has
        // checkpointed; the next initialization re-bases this new WAL lineage.
        let c = Connection::open(&path)?;
        let uuid: String =
            c.query_row("SELECT meta_value FROM storage_meta WHERE meta_key = 'authority_uuid'", [], |r| r.get(0))?;
        if manifest["authority_uuid"].as_str() != Some(uuid.as_str()) {
            return Err(
                StorageError::new("database_integrity", "restored shadow failed the authority uuid check").into()
            );
        }
        drop(c);
        write_json(&local.join("tofu-fastpath.json"), &json!({"authority_uuid":uuid,"shadow_dir":shadow}))?;
    } else if classic.is_file() {
        install(&path, classic, &appended(classic, "-wal"))?;
        write_json(
            &local.join("tofu-fastpath.json"),
            &json!({"authority_uuid":"","shadow_dir":shadow,"seeded_from":"classic"}),
        )?;
    }
    Ok(path)
}
fn install(destination: &Path, source: &Path, wal: &Path) -> OpResult<()> {
    let directory = destination.parent().expect("front parent");
    storage::require_capacity(directory, storage::size(source)? + storage::size(wal)?)?;
    let temporary = appended(destination, ".restore-tmp");
    let temporary_wal = appended(&temporary, "-wal");
    let at = Instant::now() + Duration::from_secs(3600);
    storage::copy_durable(source, &temporary, at)?;
    let has_wal = storage::size(wal)? > 0;
    if has_wal {
        storage::copy_durable(wal, &temporary_wal, at)?;
    }
    for suffix in ["-wal", "-shm"] {
        let stale = appended(destination, suffix);
        if stale.try_exists().map_err(io)? {
            fs::remove_file(stale).map_err(io)?;
        }
    }
    if has_wal {
        fs::rename(temporary_wal, appended(destination, "-wal")).map_err(io)?;
    }
    fs::rename(temporary, destination).map_err(io)?;
    sync_dir(directory)?;
    File::open(destination).map_err(io)?.sync_all().map_err(io)
}

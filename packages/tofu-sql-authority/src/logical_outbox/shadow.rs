//! Checksummed Python-compatible shadow segments, exclusive writer ownership and crash-tail recovery.
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::os::fd::AsRawFd;
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};

use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use super::{canonical, now_ms, LogicalOutboxPolicy};
use crate::error::{Failure, OpResult, StorageError};

const MAGIC: &[u8] = b"TOFU-LSHADOW\0\x01\n";
fn corrupt(message: &str) -> Failure {
    StorageError::new("database_integrity", message).into()
}
fn io(error: std::io::Error) -> Failure {
    StorageError::new("database_unavailable", format!("Logical shadow I/O: {error}")).into()
}
fn frame(payload: &[u8]) -> OpResult<Vec<u8>> {
    let length: u32 = payload.len().try_into().map_err(|_| corrupt("logical frame length is invalid"))?;
    let mut bytes = length.to_be_bytes().to_vec();
    bytes.extend_from_slice(payload);
    bytes.extend_from_slice(&Sha256::digest(payload));
    Ok(bytes)
}

fn read_frame(file: &mut File, repair: bool, maximum: Option<u64>) -> OpResult<Option<Vec<u8>>> {
    let offset = file.stream_position().map_err(io)?;
    let end = file.metadata().map_err(io)?.len();
    if offset == end {
        return Ok(None);
    }
    let mut length = [0; 4];
    if file.read_exact(&mut length).is_err() {
        if repair {
            file.set_len(offset).map_err(io)?;
            file.sync_all().map_err(io)?;
            return Ok(None);
        }
        return Err(corrupt("truncated logical frame"));
    }
    let size = u32::from_be_bytes(length) as u64;
    if size == 0 || maximum.is_some_and(|maximum| size > maximum) {
        return Err(corrupt("logical frame length is invalid"));
    }
    let remaining = end - offset - 4;
    if size + 32 > remaining {
        if !repair {
            return Err(corrupt("truncated logical frame"));
        }
        // A complete checksummed body with a damaged length is corruption, not a crash tail.
        if remaining >= 32 {
            let mut hash = Sha256::new();
            let mut left = remaining - 32;
            let mut buffer = [0u8; 65536];
            while left > 0 {
                let count = left.min(buffer.len() as u64) as usize;
                file.read_exact(&mut buffer[..count]).map_err(io)?;
                hash.update(&buffer[..count]);
                left -= count as u64;
            }
            let mut digest = [0; 32];
            file.read_exact(&mut digest).map_err(io)?;
            if hash.finalize().as_slice() == digest {
                return Err(corrupt("complete logical frame length differs"));
            }
        }
        file.set_len(offset).map_err(io)?;
        file.sync_all().map_err(io)?;
        file.seek(SeekFrom::Start(offset)).map_err(io)?;
        return Ok(None);
    }
    let mut payload = vec![0; size as usize];
    file.read_exact(&mut payload).map_err(io)?;
    let mut digest = [0; 32];
    file.read_exact(&mut digest).map_err(io)?;
    if Sha256::digest(&payload).as_slice() != digest {
        return Err(corrupt("logical frame checksum mismatch"));
    }
    Ok(Some(payload))
}

pub struct Shadow {
    root: PathBuf,
    stream: String,
    _lock: File,
    active: File,
    active_name: String,
    active_start: u64,
    pub next_sequence: u64,
    sealed: u64,
    bytes_used: u64,
    repaired: u64,
    policy: LogicalOutboxPolicy,
}

fn validate_record(record: &Value) -> OpResult<()> {
    for (field, maximum) in [("event_id", 256), ("operation", 256), ("tenant_id", 128)] {
        if !record[field].as_str().is_some_and(|value| !value.is_empty() && value.chars().count() <= maximum) {
            return Err(corrupt("logical record identity is invalid"));
        }
    }
    if !record["owner_user_id"].as_i64().is_some_and(|value| value >= 0)
        || !record["committed_at_ms"].as_i64().is_some_and(|value| value >= 0)
        || !record["payload"].is_object()
        || (!record["command_id"].is_null()
            && !record["command_id"].as_str().is_some_and(|value| !value.is_empty() && value.chars().count() <= 256))
        || !record["request_digest"].as_str().is_some_and(|value| {
            value.len() == 64 && value.bytes().all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        })
    {
        return Err(corrupt("logical record metadata is invalid"));
    }
    Ok(())
}

fn private(path: &Path, directory: bool, group: bool) -> OpResult<()> {
    let metadata = fs::symlink_metadata(path).map_err(io)?;
    if metadata.file_type().is_symlink() || (directory && !metadata.is_dir()) || (!directory && !metadata.is_file()) {
        return Err(corrupt("logical shadow path type is invalid"));
    }
    let mode = metadata.permissions().mode() & 0o777;
    let required = if directory {
        if group {
            0o770
        } else {
            0o700
        }
    } else if group {
        0o660
    } else {
        0o600
    };
    if mode & 0o007 != 0 || mode & required != required || (!group && mode & 0o077 != 0) {
        return Err(StorageError::new("database_forbidden", "logical shadow violates its declared access mode").into());
    }
    Ok(())
}

impl Shadow {
    fn sync_directory(&self) -> OpResult<()> {
        File::open(&self.root).and_then(|file| file.sync_all()).map_err(io)
    }
    fn create(root: &Path, stream: &str, start: u64, group: bool) -> OpResult<(File, String, u64)> {
        let name = format!("segment-{start:020}.open");
        let mut file = OpenOptions::new()
            .write(true)
            .read(true)
            .create_new(true)
            .mode(if group { 0o660 } else { 0o600 })
            .open(root.join(&name))
            .map_err(io)?;
        file.set_permissions(fs::Permissions::from_mode(if group { 0o660 } else { 0o600 })).map_err(io)?;
        let mut header = MAGIC.to_vec();
        header.extend(frame(&canonical(&json!({"created_at_ms":now_ms(),"format":"tofu.logical-shadow.v1","start_sequence":start,"stream_id":stream})))?);
        file.write_all(&header).and_then(|()| file.sync_all()).map_err(io)?;
        File::open(root).and_then(|file| file.sync_all()).map_err(io)?;
        Ok((file, name, header.len() as u64))
    }

    fn scan(
        path: &Path,
        stream: &str,
        start: u64,
        repair: bool,
        find: Option<u64>,
    ) -> OpResult<(u64, u64, u64, Option<Vec<u8>>)> {
        let mut file = OpenOptions::new().read(true).write(repair).open(path).map_err(io)?;
        let before = file.metadata().map_err(io)?.len();
        let mut magic = vec![0; MAGIC.len()];
        file.read_exact(&mut magic).map_err(io)?;
        if magic != MAGIC {
            return Err(corrupt("logical segment magic mismatch"));
        }
        let header_bytes =
            read_frame(&mut file, false, Some(16384))?.ok_or_else(|| corrupt("logical segment header is missing"))?;
        if header_bytes.len() > 16384 {
            return Err(corrupt("logical segment header is too large"));
        }
        let header: Value =
            serde_json::from_slice(&header_bytes).map_err(|_| corrupt("logical header is invalid JSON"))?;
        if canonical(&header) != header_bytes
            || header["format"] != "tofu.logical-shadow.v1"
            || header["stream_id"] != stream
            || header["start_sequence"] != start
        {
            return Err(corrupt("logical segment header identity mismatch"));
        }
        let mut expected = start;
        let mut found = None;
        while let Some(bytes) = read_frame(&mut file, repair, None)? {
            let record: Value =
                serde_json::from_slice(&bytes).map_err(|_| corrupt("logical record is invalid JSON"))?;
            if canonical(&record) != bytes
                || record["format"] != "tofu.logical-commit.v1"
                || record["stream_id"] != stream
                || record["sequence"] != expected
            {
                return Err(corrupt("logical record lineage mismatch"));
            }
            validate_record(&record)?;
            if find == Some(expected) {
                found = Some(bytes);
            }
            expected += 1;
        }
        let after = file.metadata().map_err(io)?.len();
        Ok((expected, after, before - after, found))
    }

    pub fn open(policy: &LogicalOutboxPolicy, stream: &str) -> OpResult<Self> {
        if stream.is_empty()
            || stream.len() > 128
            || !stream.as_bytes()[0].is_ascii_alphanumeric()
            || !stream.bytes().all(|byte| byte.is_ascii_alphanumeric() || b"._:-".contains(&byte))
        {
            return Err(corrupt("stream_id must be 1-128 safe identifier characters"));
        }
        let root = policy.sink_root.as_ref().ok_or_else(|| corrupt("logical sink root is missing"))?.clone();
        let group = policy.access_mode == "group";
        if !root.exists() {
            fs::create_dir_all(&root).map_err(io)?;
            fs::set_permissions(&root, fs::Permissions::from_mode(if group { 0o2770 } else { 0o700 })).map_err(io)?;
        }
        private(&root, true, group)?;
        let lock_path = root.join(".writer.lock");
        let lock = match OpenOptions::new()
            .read(true)
            .write(true)
            .create_new(true)
            .mode(if group { 0o660 } else { 0o600 })
            .open(&lock_path)
        {
            Ok(file) => {
                file.set_permissions(fs::Permissions::from_mode(if group { 0o660 } else { 0o600 })).map_err(io)?;
                file
            }
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
                OpenOptions::new().read(true).write(true).custom_flags(libc::O_NOFOLLOW).open(&lock_path).map_err(io)?
            }
            Err(error) => return Err(io(error)),
        };
        private(&lock_path, false, group)?;
        if unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
            return Err(StorageError::new("database_busy", "another publisher owns the sink writer lock").into());
        }
        let mut inventory = Vec::new();
        let re = regex::Regex::new(r"^segment-([0-9]{20})(?:-([0-9]{20}))?\.(open|sealed)$").expect("segment pattern");
        for child in fs::read_dir(&root).map_err(io)? {
            let path = child.map_err(io)?.path();
            let name = path.file_name().unwrap_or_default().to_string_lossy().to_string();
            if !name.starts_with("segment-") {
                continue;
            }
            let captures = re.captures(&name).ok_or_else(|| corrupt("unrecognized logical segment name"))?;
            let start: u64 = captures[1].parse().map_err(|_| corrupt("invalid segment sequence"))?;
            let end: Option<u64> = captures
                .get(2)
                .map(|v| v.as_str().parse())
                .transpose()
                .map_err(|_| corrupt("invalid segment sequence"))?;
            let open = &captures[3] == "open";
            if open == end.is_some() {
                return Err(corrupt("invalid segment name"));
            }
            private(&path, false, group)?;
            inventory.push((start, end, path));
        }
        inventory.sort_by_key(|item| item.0);
        let mut expected = 1;
        let mut bytes_used = 0;
        let mut repaired = 0;
        let mut sealed = 0;
        let mut opened = None;
        for (start, end, path) in inventory {
            if start != expected || opened.is_some() {
                return Err(corrupt("logical segments are not contiguous"));
            }
            let (next, size, tail, _) = Self::scan(&path, stream, start, end.is_none(), None)?;
            if let Some(end) = end {
                if next <= start || next - 1 != end {
                    return Err(corrupt("sealed segment name differs from records"));
                }
                sealed += 1;
            } else {
                opened = Some((path, start));
            }
            expected = next;
            bytes_used += size;
            repaired += tail;
        }
        let (active, active_name, active_start) = if let Some((path, start)) = opened {
            let mut file = OpenOptions::new().read(true).write(true).open(&path).map_err(io)?;
            file.seek(SeekFrom::End(0)).map_err(io)?;
            (file, path.file_name().expect("segment name").to_string_lossy().into_owned(), start)
        } else {
            let (file, name, size) = Self::create(&root, stream, expected, group)?;
            bytes_used += size;
            (file, name, expected)
        };
        Ok(Self {
            root,
            stream: stream.into(),
            _lock: lock,
            active,
            active_name,
            active_start,
            next_sequence: expected,
            sealed,
            bytes_used,
            repaired,
            policy: policy.clone(),
        })
    }

    pub fn append(&mut self, record: &Value) -> OpResult<bool> {
        let sequence = record["sequence"].as_u64().ok_or_else(|| corrupt("logical record sequence is invalid"))?;
        validate_record(record)?;
        if sequence == 0 || record["format"] != "tofu.logical-commit.v1" || record["stream_id"] != self.stream {
            return Err(corrupt("logical append lineage mismatch"));
        }
        let bytes = canonical(record);
        if sequence < self.next_sequence {
            for child in fs::read_dir(&self.root).map_err(io)? {
                let path = child.map_err(io)?.path();
                let name = path.file_name().unwrap_or_default().to_string_lossy();
                if !name.starts_with("segment-") {
                    continue;
                }
                let start =
                    name.get(8..28).and_then(|s| s.parse().ok()).ok_or_else(|| corrupt("invalid segment name"))?;
                if start > sequence {
                    continue;
                }
                let (_, _, _, found) = Self::scan(&path, &self.stream, start, false, Some(sequence))?;
                if let Some(found) = found {
                    if found != bytes {
                        return Err(corrupt("logical sequence was reused for a different event"));
                    }
                    return Ok(true);
                }
            }
            return Err(corrupt("logical retry references missing committed sequence"));
        }
        if sequence != self.next_sequence {
            return Err(corrupt("logical append would create a sequence gap"));
        }
        let encoded = frame(&bytes)?;
        if self.active.metadata().map_err(io)?.len() + encoded.len() as u64 > self.policy.max_segment_bytes
            && self.next_sequence > self.active_start
        {
            self.active.sync_all().map_err(io)?;
            let sealed =
                self.root.join(format!("segment-{:020}-{:020}.sealed", self.active_start, self.next_sequence - 1));
            if sealed.exists() {
                return Err(corrupt("logical sealed segment already exists"));
            }
            fs::rename(self.root.join(&self.active_name), sealed).map_err(io)?;
            self.sync_directory()?;
            let (file, name, size) =
                Self::create(&self.root, &self.stream, self.next_sequence, self.policy.access_mode == "group")?;
            self.active = file;
            self.active_name = name;
            self.active_start = self.next_sequence;
            self.sealed += 1;
            self.bytes_used += size;
        }
        self.active.write_all(&encoded).and_then(|()| self.active.sync_all()).map_err(io)?;
        self.bytes_used += encoded.len() as u64;
        self.next_sequence += 1;
        Ok(false)
    }

    pub fn status(&self) -> Value {
        json!({"format":"tofu.logical-shadow.v1","authoritative":false,"stream_id":self.stream,"next_sequence":self.next_sequence,"records":self.next_sequence-1,"sealed_segments":self.sealed,"active_segment":self.active_name,"bytes_used":self.bytes_used,"max_segment_bytes":self.policy.max_segment_bytes,"max_record_bytes":self.policy.max_record_bytes,"max_total_bytes":self.policy.max_shadow_bytes,"repaired_tail_bytes":self.repaired,"fsync_each_append":true,"access_mode":self.policy.access_mode,"closed":false,"poisoned":false})
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn policy(path: &Path) -> LogicalOutboxPolicy {
        LogicalOutboxPolicy {
            mode: "required".into(),
            capture_enabled: true,
            publisher_enabled: true,
            sink_root: Some(path.into()),
            reason: "test".into(),
            max_pending_bytes: 1,
            max_record_bytes: 1,
            max_segment_bytes: 400,
            max_shadow_bytes: 1,
            publish_batch_size: 1,
            access_mode: "owner".into(),
        }
    }
    fn record(sequence: u64) -> Value {
        json!({"format":"tofu.logical-commit.v1","stream_id":"stream","sequence":sequence,"event_id":format!("event-{sequence}"),"operation":"record.put","tenant_id":"system","owner_user_id":0,"committed_at_ms":0,"request_digest":"a".repeat(64),"command_id":null,"payload":{}})
    }
    #[test]
    fn durable_duplicate_rotation_and_reopen() {
        let temp = tempfile::tempdir().unwrap();
        let policy = policy(&temp.path().join("sink"));
        let mut sink = Shadow::open(&policy, "stream").unwrap();
        assert!(!sink.append(&record(1)).unwrap());
        assert!(sink.append(&record(1)).unwrap());
        let mut different = record(1);
        different["event_id"] = json!("different");
        assert!(sink.append(&different).is_err());
        assert!(sink.append(&record(3)).is_err());
        assert!(!sink.append(&record(2)).unwrap());
        drop(sink);
        let reopened = Shadow::open(&policy, "stream").unwrap();
        assert_eq!(reopened.next_sequence, 3);
    }
    #[test]
    fn partial_tail_is_repaired_but_checksum_corruption_refuses() {
        let temp = tempfile::tempdir().unwrap();
        let policy = policy(&temp.path().join("sink"));
        let mut sink = Shadow::open(&policy, "stream").unwrap();
        sink.append(&record(1)).unwrap();
        let path = sink.root.join(&sink.active_name);
        drop(sink);
        OpenOptions::new().append(true).open(&path).unwrap().write_all(&[0, 0]).unwrap();
        let sink = Shadow::open(&policy, "stream").unwrap();
        assert_eq!(sink.repaired, 2);
        drop(sink);
        let mut file = OpenOptions::new().read(true).write(true).open(&path).unwrap();
        file.seek(SeekFrom::End(-1)).unwrap();
        let mut byte = [0];
        file.read_exact(&mut byte).unwrap();
        file.seek(SeekFrom::End(-1)).unwrap();
        file.write_all(&[byte[0] ^ 1]).unwrap();
        drop(file);
        assert!(Shadow::open(&policy, "stream").is_err());
    }
    #[test]
    fn ownership_and_lineage_are_exclusive() {
        let temp = tempfile::tempdir().unwrap();
        let policy = policy(&temp.path().join("sink"));
        let sink = Shadow::open(&policy, "stream").unwrap();
        assert!(Shadow::open(&policy, "stream").is_err());
        drop(sink);
        assert!(Shadow::open(&policy, "different").is_err());
    }
}

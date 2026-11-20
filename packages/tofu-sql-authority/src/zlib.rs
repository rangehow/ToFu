//! zlib-compatible compression byte-identical to CPython's `zlib.compress`.
//!
//! Receipts above 64 KiB and large task-result/task-event fields are stored
//! zlib-compressed at level 1. Decoding is backend-neutral, but durable
//! bytes must equal the Python authority's, so compression calls zlib's own
//! `compress2` (CPython's `zlib.compress(data, level)` uses the same defaults:
//! deflate window 15, memLevel 8, default strategy). The pure-Rust
//! `miniz_oxide` encoder produces different, equally valid streams.

use std::os::raw::{c_int, c_uchar, c_ulong};

#[link(name = "z")]
extern "C" {
    fn compress2(
        dest: *mut c_uchar,
        dest_len: *mut c_ulong,
        source: *const c_uchar,
        source_len: c_ulong,
        level: c_int,
    ) -> c_int;
    fn compressBound(source_len: c_ulong) -> c_ulong;
}

const Z_OK: c_int = 0;

/// `zlib.compress(data, level)`.
pub fn compress(data: &[u8], level: i32) -> Vec<u8> {
    unsafe {
        let mut capacity = compressBound(data.len() as c_ulong);
        let mut output = vec![0u8; capacity as usize];
        let status = compress2(output.as_mut_ptr(), &mut capacity, data.as_ptr(), data.len() as c_ulong, level);
        assert_eq!(status, Z_OK, "zlib compress2 cannot fail with a compressBound buffer");
        output.truncate(capacity as usize);
        output
    }
}

#[cfg(test)]
mod tests {
    use std::io::Read;

    #[test]
    fn round_trips_through_an_independent_decoder() {
        let data: Vec<u8> = (0..200_000u32).map(|i| (i % 251) as u8).collect();
        let compressed = super::compress(&data, 1);
        let mut decoded = Vec::new();
        flate2::read::ZlibDecoder::new(&compressed[..]).read_to_end(&mut decoded).unwrap();
        assert_eq!(decoded, data);
    }

    /// The digest of CPython 3.12's `zlib.compress(document, 1)` (zlib 1.3.x)
    /// for the same JSON document the Python oracle would store.
    #[test]
    fn level_one_bytes_equal_cpython_zlib() {
        use sha2::{Digest, Sha256};
        let rows: Vec<String> = (0..4000)
            .map(|i| {
                format!(
                    "{{\"k\":{i},\"text\":\"{}\"}}",
                    format!("line {} of a semi-repetitive document ", i % 97).repeat(3)
                )
            })
            .collect();
        let document = format!("[{}]", rows.join(","));
        assert_eq!(document.len(), 537_631);
        let digest = hex::encode(Sha256::digest(super::compress(document.as_bytes(), 1)));
        assert_eq!(digest, "ff665cbca4d93868c7de5fdc6821ccfc05ef6c547fc748a8d02f99bd47efe35c");
    }
}

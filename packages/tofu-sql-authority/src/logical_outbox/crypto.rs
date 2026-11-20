//! Fernet-compatible AES-128-CBC/HMAC-SHA256 bound payload envelopes.
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::Path;

use aes::cipher::{BlockEncrypt, KeyInit};
use aes::Aes128;
use base64::{engine::general_purpose::URL_SAFE, Engine};
use hmac::{Hmac, Mac};
use serde_json::json;
use sha2::{Digest, Sha256};

use super::{canonical, now_ms};
use crate::error::{Failure, OpResult, StorageError};

fn failure() -> Failure {
    StorageError::new("database_integrity", "Logical payload encryption failed").into()
}

fn random<const N: usize>() -> OpResult<[u8; N]> {
    let mut bytes = [0; N];
    File::open("/dev/urandom").and_then(|mut file| file.read_exact(&mut bytes)).map_err(|_| failure())?;
    Ok(bytes)
}

pub struct Cipher {
    key: [u8; 32],
    pub key_id: String,
}

impl Cipher {
    pub fn load(data_dir: &Path) -> OpResult<Self> {
        let configured = std::env::var("TOFU_SECRET_ENCRYPTION_KEY").unwrap_or_default();
        let encoded = if configured.trim().is_empty() {
            let path = data_dir.join("config/.secret_encryption.key");
            fs::create_dir_all(path.parent().expect("key parent")).map_err(|_| failure())?;
            match OpenOptions::new().write(true).create_new(true).mode(0o600).open(&path) {
                Ok(mut file) => {
                    let key = URL_SAFE.encode(random::<32>()?);
                    file.write_all(key.as_bytes()).and_then(|()| file.sync_all()).map_err(|_| failure())?;
                    key
                }
                Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
                    fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).map_err(|_| failure())?;
                    fs::read_to_string(path).map_err(|_| failure())?.trim().to_owned()
                }
                Err(_) => return Err(failure()),
            }
        } else {
            configured.trim().to_owned()
        };
        let decoded = URL_SAFE.decode(&encoded).map_err(|_| {
            StorageError::new("database_integrity", "TOFU_SECRET_ENCRYPTION_KEY is not a valid Fernet key")
        })?;
        let key: [u8; 32] = decoded.try_into().map_err(|_| failure())?;
        Ok(Self { key, key_id: hex::encode(Sha256::digest(encoded.as_bytes()))[..16].to_owned() })
    }

    pub fn seal(&self, value: &str, tenant: &str, owner: i64, event: &str) -> OpResult<String> {
        let clear = canonical(
            &json!({"version":1,"purpose":"logical-commit-payload","tenant_id":tenant.trim(),"owner_user_id":owner,"record_id":event,"value":value}),
        );
        Ok(self.encrypt(&clear, random::<16>()?, (now_ms() / 1000) as u64))
    }

    fn encrypt(&self, clear: &[u8], iv: [u8; 16], timestamp: u64) -> String {
        let cipher = Aes128::new_from_slice(&self.key[16..]).expect("AES key length");
        let padding = 16 - clear.len() % 16;
        let mut encrypted = clear.to_vec();
        encrypted.resize(clear.len() + padding, padding as u8);
        let mut previous = iv;
        for block in encrypted.as_chunks_mut::<16>().0 {
            for (byte, previous) in block.iter_mut().zip(previous) {
                *byte ^= previous;
            }
            cipher.encrypt_block((&mut *block).into());
            previous.copy_from_slice(block);
        }
        let mut token = vec![0x80];
        token.extend_from_slice(&timestamp.to_be_bytes());
        token.extend_from_slice(&iv);
        token.extend_from_slice(&encrypted);
        let mut mac = <Hmac<Sha256> as Mac>::new_from_slice(&self.key[..16]).expect("HMAC key length");
        mac.update(&token);
        token.extend_from_slice(&mac.finalize().into_bytes());
        URL_SAFE.encode(token)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn fernet_structure_and_binding_are_authenticated() {
        let cipher = Cipher { key: [7; 32], key_id: "test".into() };
        let token = cipher.seal("secret", "tenant", 2, "event").unwrap();
        let bytes = URL_SAFE.decode(token).unwrap();
        assert_eq!(bytes[0], 0x80);
        assert_eq!((bytes.len() - 57) % 16, 0);
        assert!(!bytes.windows(6).any(|window| window == b"secret"));
        let mut mac = <Hmac<Sha256> as Mac>::new_from_slice(&cipher.key[..16]).unwrap();
        mac.update(&bytes[..bytes.len() - 32]);
        mac.verify_slice(&bytes[bytes.len() - 32..]).unwrap();
    }
}

#[cfg(test)]
mod interoperability_tests {
    use super::*;
    #[test]
    fn ciphertext_matches_python_cryptography_fernet_vector() {
        let cipher = Cipher { key: [7; 32], key_id: "test".into() };
        assert_eq!(cipher.encrypt(b"independent Fernet vector",std::array::from_fn(|index|index as u8),1234567890),"gAAAAABJlgLSAAECAwQFBgcICQoLDA0OD526SsWGqj20h0tCB3JLg6Mev89i2pWTUKS21mJISHtFnD5bhzp5GSspXyYtJoaLVtaLhIfykzoO0L2EL_POLcE=");
    }
}

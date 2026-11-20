//! Production bindings of engine ports to the storage.v2 client
//! (`packages/tofu-storage-client`, Phase 3a).
//!
//! Responsibility: settle tool output through the SAME known-credential
//! redactor the storage client applies at write time
//! (`CredentialRedactor::current(owner)`, including the owner's
//! model-routing keys through `ModelRoutingKnownSecrets`), so settlement and
//! storage never disagree about what is a credential. No second redactor
//! exists in the engine.
//!
//! `StorageV2DurableStore` uses that same client for every durable operation.
//! The host supplies the existing Turn projection owner and scheduled-Turn
//! dispatcher; this adapter does not invent a second projection or queue policy.

mod binding;
pub use binding::{
    CheckpointArchive, PreparedTurnEvent, SteerRequeue, StorageIdentity, StorageProjection,
    StorageV2DurableStore,
};

use std::sync::Arc;

use serde_json::Value;
use tofu_storage_client::CredentialRedactor;

use super::redaction::SettlementRedactor;

/// `SettlementRedactor` over the storage client's credential redactor.
pub struct StorageCredentialRedactor(pub Arc<CredentialRedactor>);

impl SettlementRedactor for StorageCredentialRedactor {
    fn redact_value(&self, owner_user_id: u64, v: &Value) -> Value {
        self.0.current(Some(owner_user_id)).redact_value(v)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use tofu_storage_client::{CredentialFiles, OwnerCredentialSource};

    struct OwnerKeys;

    impl OwnerCredentialSource for OwnerKeys {
        fn owner_credential_keys(&self, owner: u64) -> Result<Vec<(String, String)>, String> {
            Ok(if owner == 7 {
                vec![("route-a".into(), "owner-route-key-0123456789".into())]
            } else {
                Vec::new()
            })
        }
    }

    /// Recent fix "credential redaction at write time and at tool
    /// settlement": settlement uses the storage client's redactor, so an
    /// owner's model-routing key is redacted for that owner only.
    #[test]
    fn settlement_redacts_the_owners_model_routing_keys_with_the_storage_redactor() {
        let config =
            std::env::temp_dir().join(format!("tofu-engine-redaction-{}", std::process::id()));
        std::fs::create_dir_all(&config).unwrap();
        let redactor = StorageCredentialRedactor(Arc::new(CredentialRedactor::new(
            CredentialFiles::in_config_dir(&config),
            Arc::new(OwnerKeys),
        )));
        let output = json!({"text": "key=owner-route-key-0123456789"});
        let mine = redactor.redact_value(7, &output);
        let other = redactor.redact_value(8, &output);
        std::fs::remove_dir_all(&config).ok();
        assert!(!mine.to_string().contains("owner-route-key-0123456789"));
        assert!(other.to_string().contains("owner-route-key-0123456789"));
    }
}

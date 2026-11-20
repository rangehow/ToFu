//! Serde types generated from Tofu's machine-readable contracts.
//!
//! Every `generated_*` module is produced by an in-repo generator from the
//! contract named in its header; edit the contract and regenerate, never the
//! module. `wire` holds the hand-written serde helpers those modules share.

pub mod generated_api_v1;
pub mod generated_events_v1;
pub mod wire;

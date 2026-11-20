//! Serde helpers referenced by generated contract modules.

use serde::{Deserialize, Deserializer};

/// Deserializes a present field as `Some`, including an explicit JSON `null`
/// (paired with `#[serde(default)]`, absence stays `None`). Optional nullable
/// wire fields keep "absent" and "null" as different facts.
pub fn present<'de, D, T>(deserializer: D) -> Result<Option<T>, D::Error>
where
    D: Deserializer<'de>,
    T: Deserialize<'de>,
{
    T::deserialize(deserializer).map(Some)
}

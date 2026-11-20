//! The bounded build transfer the Home's `BundleReceiver` accepts
//! (`lib/paper/builds/bundle.py::iter_bundle`): one `paper-manifest` frame
//! (`PaperBuildBundleV1` as compact Python JSON), then
//! `paper-artifact/<name>` frames of base64 chunks in receipt order.

use super::compiler::{artifact_metadata, CompiledBuild};
use super::contract::{limit, validate};
use super::pyjson::dumps;
use super::{check_cancelled, Cancel, Fault, Outcome};
use crate::commands::project::base64_encode;
use serde_json::{json, Value};
use std::io::Read;

pub(crate) fn iter_bundle(
    build: &CompiledBuild,
    location: Option<Value>,
    cancel: Cancel,
    send: &dyn Fn(&str, String) -> Outcome<()>,
) -> Outcome<()> {
    let manifest = json!({
        "schema_version": 1,
        "receipt": Value::Object(build.receipt.clone()),
        "location": location.unwrap_or(Value::Null),
    });
    validate(&manifest, "PaperBuildBundleV1")?;
    let text = dumps(&manifest);
    if text.len() as u64 > limit("transfer_frame_bytes") {
        return Err(Fault::build(
            "transfer_limit",
            "Paper build manifest exceeds its transfer limit",
        ));
    }
    send("paper-manifest", text)?;
    let chunk_bytes = limit("transfer_chunk_bytes");
    for artifact in build.receipt["artifacts"].as_array().into_iter().flatten() {
        let name = artifact["name"].as_str().unwrap_or("");
        let path = build.directory.join(name);
        if &artifact_metadata(&path)? != artifact {
            return Err(Fault::build(
                "artifact_changed",
                "Paper artifact changed before transfer",
            ));
        }
        let mut file = std::fs::File::open(&path)?;
        loop {
            let mut chunk = Vec::new();
            (&mut file).take(chunk_bytes).read_to_end(&mut chunk)?;
            if chunk.is_empty() {
                break;
            }
            check_cancelled(cancel)?;
            send(&format!("paper-artifact/{name}"), base64_encode(&chunk))?;
        }
    }
    Ok(())
}

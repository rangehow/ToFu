//! Launcher pieces that need no Python: the bridge reads a tool's input schema
//! under either SDK spelling (`_tool_input_schema`, the 2026-08-06 outage pin
//! of tests/mcp_protocol/test_mcp_sdk_pin_bounded.py), and the isolated
//! vendored launch argv with its exact-pin cutoff exemptions.

use serde_json::json;
use std::sync::Arc;
use tofu_mcp::bridge::tool_input_schema;
use tofu_mcp::callback::NoBinding;
use tofu_mcp::vendor::{vendored_launch_argv, LauncherContext, SourceLauncher};

#[test]
fn tool_input_schema_accepts_both_sdk_spellings() {
    let payload = json!({"type": "object", "properties": {"q": {"type": "string"}}});
    assert_eq!(
        tool_input_schema(&json!({"name": "search", "input_schema": payload})),
        payload
    );
    assert_eq!(
        tool_input_schema(&json!({"name": "search", "inputSchema": payload})),
        payload
    );
    assert_eq!(
        tool_input_schema(&json!({"name": "search"})),
        json!({"type": "object", "properties": {}})
    );
}

#[test]
fn vendored_launch_is_isolated_and_exempts_only_exact_pins() {
    let scratch = tempfile::tempdir().unwrap();
    let repo = scratch.path().join("repo");
    let source = repo.join("tools").join("pin-mcp");
    std::fs::create_dir_all(&source).unwrap();
    std::fs::write(
        source.join("pyproject.toml"),
        "[project]\nname = \"pin-mcp\"\ndependencies = [\"Exact_Pkg[extra]==1.2.3\", \"floating>=1\"]\n",
    )
    .unwrap();
    let context = LauncherContext {
        process_env: Default::default(),
        runtime_base: String::new(),
        repo_root: repo.to_string_lossy().into_owned(),
        source_root: String::new(),
        interpreter: String::new(),
        base_prefix: String::new(),
        python_minor: "3.12".into(),
        launchers: vec![(
            "pin-mcp".into(),
            SourceLauncher {
                sources: vec!["tools/pin-mcp".into()],
                extra_sources: Vec::new(),
            },
        )],
        extra_sources: Default::default(),
        binding: Arc::new(NoBinding),
    };
    let source = source.to_string_lossy().into_owned();
    assert_eq!(
        vendored_launch_argv(&context, "pin-mcp").unwrap().unwrap(),
        vec![
            "uv",
            "run",
            "--no-project",
            "--exclude-newer-package",
            "exact-pkg=false",
            "--with-editable",
            source.as_str(),
            "pin-mcp"
        ]
    );
    assert_eq!(
        vendored_launch_argv(&context, "/usr/bin/pin-mcp").unwrap(),
        None
    );
    assert_eq!(vendored_launch_argv(&context, "unknown-mcp").unwrap(), None);
}

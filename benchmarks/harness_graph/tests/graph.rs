//! Executable graph semantics on the actual native authority, not a map fake.
use serde_json::{json, Value};
use tofu_db_harness_graph::{execute, native::NativeBackend, Outcome, Request};

fn call(db: &mut NativeBackend, v: Value) -> std::io::Result<Outcome> {
    execute(db, &serde_json::from_value::<Request>(v).unwrap())
}
fn scope() -> Value {
    json!({"tenant":7,"owner":11})
}
fn create(db: &mut NativeBackend) -> Value {
    call(
        db,
        json!({"op":"create","scope":scope(),"branch":"main","command_id":"create"}),
    )
    .unwrap()
    .result["token"]
        .clone()
}
fn append(db: &mut NativeBackend, token: &Value, delta: &str, id: &str) -> Value {
    call(db,json!({"op":"append","scope":scope(),"branch":token["branch"],"expected":token["head"],"delta":delta,"command_id":id})).unwrap().result["token"].clone()
}
fn context(
    db: &mut NativeBackend,
    branch: &str,
    after: Value,
    verify: bool,
) -> std::io::Result<Outcome> {
    call(
        db,
        json!({"op":"context","scope":scope(),"branch":branch,"after":after,"verify_prefix":verify}),
    )
}

#[test]
fn fork_shares_history_and_delta_reads_never_visit_confirmed_prefix() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = NativeBackend::open(&dir.path().join("db")).unwrap();
    let mut head = create(&mut db);
    for i in 0..100 {
        head = append(&mut db, &head, &format!("base-{i}"), &format!("b-{i}"));
    }
    let fork = call(
        &mut db,
        json!({"op":"fork","scope":scope(),"branch":"branch","command_id":"fork","source":head}),
    )
    .unwrap();
    assert_eq!(fork.work.node_reads, 0);
    assert_eq!(fork.work.header_reads, 0);
    assert_eq!(fork.work.writes, 3);
    let anchor = fork.result["token"].clone();
    let mut branch = anchor.clone();
    for i in 0..8 {
        branch = append(&mut db, &branch, &format!("new-{i}"), &format!("n-{i}"));
    }
    let delta = context(&mut db, "branch", anchor.clone(), false).unwrap();
    let scan = context(&mut db, "branch", anchor, true).unwrap();
    assert_eq!(delta.result, scan.result);
    assert_eq!(delta.work.node_reads, 8);
    assert_eq!(delta.work.header_reads, 0);
    assert_eq!(scan.work.node_reads, 8);
    assert_eq!(scan.work.header_reads, 108);
    assert_eq!(
        delta.result["fragments"],
        json!((0..8).map(|i| format!("new-{i}")).collect::<Vec<_>>())
    );
    assert_eq!(
        context(&mut db, "main", head, false).unwrap().result["fragments"],
        json!([])
    );
    drop(db);
    let mut db = NativeBackend::open(&dir.path().join("db")).unwrap();
    assert_eq!(
        context(&mut db, "branch", Value::Null, false)
            .unwrap()
            .result["fragments"]
            .as_array()
            .unwrap()
            .len(),
        108
    );
}

#[test]
fn committed_tokens_owner_branch_and_idempotent_commands_are_fenced() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = NativeBackend::open(&dir.path().join("db")).unwrap();
    let original = create(&mut db);
    let request = json!({"op":"append","scope":scope(),"branch":"main","expected":original["head"],"delta":"x","command_id":"append"});
    let first = call(&mut db, request.clone()).unwrap();
    let again = call(&mut db, request.clone()).unwrap();
    assert_eq!(first.result, again.result);
    assert_eq!(again.work.writes, 0);
    let mut changed = request;
    changed["delta"] = json!("changed");
    assert!(call(&mut db, changed)
        .unwrap_err()
        .to_string()
        .contains("command_conflict"));
    let mut foreign = original.clone();
    foreign["scope"]["owner"] = json!(12);
    assert!(context(&mut db, "main", foreign, false).is_err());
    let mut forged = original.clone();
    forged["head"]["node"] = first.result["token"]["head"]["node"].clone();
    assert!(context(&mut db, "main", forged, false).is_err());
    let mut wrong = original.clone();
    wrong["branch"] = json!("different");
    assert!(context(&mut db, "main", wrong, false).is_err());
    let mut version = original;
    version["projection_version"] = json!(2);
    assert!(context(&mut db, "main", version, false).is_err());
}

#[test]
fn edits_delete_compaction_and_aba_adoption_invalidate_prior_context() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = NativeBackend::open(&dir.path().join("db")).unwrap();
    let base = create(&mut db);
    let mut head = append(&mut db, &base, "old", "a");
    let saved = call(
        &mut db,
        json!({"op":"fork","scope":scope(),"branch":"saved","command_id":"save","source":head}),
    )
    .unwrap()
    .result["token"]
        .clone();
    for reason in ["edit", "delete", "compaction"] {
        let old = head.clone();
        head=call(&mut db,json!({"op":"reset","scope":scope(),"branch":"main","command_id":reason,"expected":head["head"],"reason":reason,"delta":"replacement"})).unwrap().result["token"].clone();
        assert!(context(&mut db, "main", old, false)
            .unwrap_err()
            .to_string()
            .contains("invalidated"));
        assert_eq!(
            context(&mut db, "main", Value::Null, false).unwrap().result["fragments"],
            json!(["replacement"])
        );
    }
    let before = head.clone();
    head=call(&mut db,json!({"op":"adopt","scope":scope(),"branch":"main","command_id":"adopt","expected":head["head"],"source":saved})).unwrap().result["token"].clone();
    assert!(context(&mut db, "main", before, false).is_err());
    assert_eq!(
        context(&mut db, "main", Value::Null, false).unwrap().result["fragments"],
        json!(["old"])
    );
    assert!(call(&mut db,json!({"op":"append","scope":scope(),"branch":"main","command_id":"stale","expected":base["head"],"delta":"bad"})).is_err());
    assert_eq!(head["head"]["epoch"], json!(4));
}

#[test]
fn acknowledged_frame_survives_sigkill_without_destructor_cleanup() {
    use std::io::{BufRead, BufReader, Write};
    use std::process::{Command, Stdio};
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("db");
    let mut child = Command::new(env!("CARGO_BIN_EXE_tofudb-harness-graph"))
        .arg("native")
        .arg(&path)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    let mut input = child.stdin.take().unwrap();
    let mut output = BufReader::new(child.stdout.take().unwrap());
    let mut send = |v: Value| {
        writeln!(input, "{v}").unwrap();
        input.flush().unwrap();
        let mut line = String::new();
        output.read_line(&mut line).unwrap();
        serde_json::from_str::<Value>(&line).unwrap()
    };
    let created =
        send(json!({"op":"create","scope":scope(),"branch":"main","command_id":"create"}));
    let receipt = send(
        json!({"op":"append","scope":scope(),"branch":"main","command_id":"acked","expected":created["value"]["result"]["token"]["head"],"delta":"durable ✓"}),
    );
    assert_eq!(receipt["ok"], true);
    child.kill().unwrap();
    child.wait().unwrap();
    let mut db = NativeBackend::open(&path).unwrap();
    let restored = context(&mut db, "main", Value::Null, false).unwrap();
    assert_eq!(
        restored.result["token"],
        receipt["value"]["result"]["token"]
    );
    assert_eq!(restored.result["fragments"], json!(["durable ✓"]));
}

#[test]
fn response_limits_count_serialized_utf8_and_fragment_count() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = NativeBackend::open(&dir.path().join("db")).unwrap();
    let base = create(&mut db);
    let one = append(&mut db, &base, "escaped \" \n ✓", "one");
    append(&mut db, &one, "two", "two");
    let full = context(&mut db, "main", Value::Null, false).unwrap();
    let exact = serde_json::to_vec(&full.result).unwrap().len();
    let req = |bytes, fragments| json!({"op":"context","scope":scope(),"branch":"main","after":null,"max_bytes":bytes,"max_fragments":fragments});
    assert_eq!(call(&mut db, req(exact, 2)).unwrap().result, full.result);
    assert!(call(&mut db, req(exact - 1, 2))
        .unwrap_err()
        .to_string()
        .contains("bound"));
    assert!(call(&mut db, req(exact, 1))
        .unwrap_err()
        .to_string()
        .contains("bound"));
    assert!(call(&mut db, req(exact, 0)).is_err());
    assert_eq!(
        context(&mut db, "main", Value::Null, false).unwrap().result,
        full.result
    );
}

#[test]
fn failure_after_each_row_write_rolls_back_graph_and_receipt_together() {
    use std::{cell::RefCell, io, rc::Rc};
    use tofu_db_harness_graph::{Backend, Rows, Scope};
    type Prior = Rc<RefCell<Vec<(String, String, Option<Vec<u8>>)>>>;
    struct FailRows<'a> {
        inner: &'a mut dyn Rows,
        remaining: usize,
        prior: Prior,
    }
    impl Rows for FailRows<'_> {
        fn get(&mut self, n: &str, k: &str) -> io::Result<Option<Vec<u8>>> {
            self.inner.get(n, k)
        }
        fn put(&mut self, n: &str, k: &str, v: Vec<u8>) -> io::Result<()> {
            self.prior
                .borrow_mut()
                .push((n.into(), k.into(), self.inner.get(n, k)?));
            self.inner.put(n, k, v)?;
            self.remaining -= 1;
            if self.remaining == 0 {
                Err(io::Error::other("injected after row write"))
            } else {
                Ok(())
            }
        }
    }
    struct Fault<'a> {
        db: &'a mut NativeBackend,
        after: usize,
        prior: Prior,
    }
    impl Backend for Fault<'_> {
        fn transact<T>(
            &mut self,
            s: Scope,
            w: bool,
            f: impl FnOnce(&mut dyn Rows) -> io::Result<T>,
        ) -> io::Result<T> {
            self.db.transact(s, w, |rows| {
                f(&mut FailRows {
                    inner: rows,
                    remaining: self.after,
                    prior: self.prior.clone(),
                })
            })
        }
    }
    let dir = tempfile::tempdir().unwrap();
    let mut db = NativeBackend::open(&dir.path().join("db")).unwrap();
    let base = create(&mut db);
    for fail_after in 1..=5 {
        let request:Request=serde_json::from_value(json!({"op":"append","scope":scope(),"branch":"main","expected":base["head"],"delta":"failed delta","command_id":"failed"})).unwrap();
        let prior = Rc::new(RefCell::new(Vec::new()));
        let mut fault = Fault {
            db: &mut db,
            after: fail_after,
            prior: prior.clone(),
        };
        assert!(execute(&mut fault, &request)
            .unwrap_err()
            .to_string()
            .contains("injected"));
        db.transact(
            Scope {
                tenant: 7,
                owner: 11,
            },
            false,
            |rows| {
                for (n, k, v) in prior.borrow().iter() {
                    assert_eq!(&rows.get(n, k)?, v);
                }
                Ok(())
            },
        )
        .unwrap();
        assert_eq!(
            context(&mut db, "main", Value::Null, false).unwrap().result["token"],
            base
        );
    }
    let retried = append(&mut db, &base, "failed delta", "failed");
    assert_eq!(retried["head"]["generation"], 1);
    let loser = json!({"op":"append","scope":scope(),"branch":"main","expected":base["head"],"delta":"loser","command_id":"loser"});
    assert!(call(&mut db, loser)
        .unwrap_err()
        .to_string()
        .contains("head_conflict"));
}

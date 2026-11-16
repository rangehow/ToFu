// Included into `server::tests`: a commit watch answers the daemon wire like any
// storage request (docs/STORAGE.md, worker transport control); only a watch the
// caller must correct is a `worker_control_refused` refusal.

fn daemon_watch(database: &Mutex<AuthorityDatabase>, control: &Arc<crate::worker_control::WorkerControl>,
                watch: &Request, admitted_at_ms: u64) -> Response {
    let stores = StorageV2Stores::new(database, None).with_worker_control(control.clone(), scope());
    let session = authenticator().authenticate(&hello()).unwrap();
    let RequestDispatch::Responded(response) = dispatch_one_request(
        &stores, &session, watch, false, admitted_at_ms, &mut || admitted_at_ms, &mut || true).unwrap()
        else { panic!("live connection lost"); };
    response.into_response()
}

fn watch_answer(response: &Response) -> (u16, Option<&str>, bool) {
    (response.status, response.error_code.as_deref(), response.retryable)
}

/// The listener's stop closes the commit control before it joins the
/// connection workers. A wait in progress and every later watch then answer
/// what `map_storage_error` answers for a component closed for a stop or
/// restart: the retryable `database_unavailable`, which the restarted
/// authority's next watch outlives, never a refusal of the caller's request.
#[test]
fn a_closed_commit_control_answers_daemon_watches_retryable_unavailable() {
    use crate::worker_control::{CONVERSATION_WATCH_OPERATION, WATCH_OPERATION};
    let directory = tempfile::tempdir().unwrap();
    let database = Mutex::new(AuthorityDatabase::initialize(directory.path()).unwrap());
    let control = database.lock().unwrap().worker_control.clone();
    let first = daemon_watch(&database, &control, &request(WATCH_OPERATION, 11, json!({"cursor": null})), 1_000);
    assert_eq!(first.status, 0, "{:?}", first.error_message);
    let cursor = serde_json::from_slice::<Value>(&first.payload).unwrap()["cursor"].clone();
    let pending = request(WATCH_OPERATION, 11, json!({"cursor": cursor}));
    let woken = std::thread::scope(|threads| {
        let waiting = threads.spawn(|| daemon_watch(&database, &control, &pending, 1_000));
        control.close();
        waiting.join().unwrap()
    });
    assert_eq!(watch_answer(&woken), (1, Some("database_unavailable"), true), "{:?}", woken.error_message);
    for operation in [WATCH_OPERATION, CONVERSATION_WATCH_OPERATION] {
        let later = daemon_watch(&database, &control, &request(operation, 11, json!({"cursor": null})), 1_000);
        assert_eq!(watch_answer(&later), (1, Some("database_unavailable"), true),
                   "{operation}: {:?}", later.error_message);
    }
}

/// A watch admitted at or past its deadline answers what every storage
/// request past its deadline answers (`map_storage_error`, semantic
/// admission, checkpoint control): the non-retryable `deadline_elapsed`.
/// Replaying the identical request cannot succeed; the watcher's next watch
/// carries a new deadline.
#[test]
fn an_elapsed_watch_deadline_answers_the_daemon_wire_deadline_elapsed() {
    use crate::worker_control::{CONVERSATION_WATCH_OPERATION, WATCH_OPERATION};
    let directory = tempfile::tempdir().unwrap();
    let database = Mutex::new(AuthorityDatabase::initialize(directory.path()).unwrap());
    let control = database.lock().unwrap().worker_control.clone();
    for operation in [WATCH_OPERATION, CONVERSATION_WATCH_OPERATION] {
        let watch = request(operation, 11, json!({"cursor": null}));
        for admitted_at_ms in [watch.deadline_unix_ms, watch.deadline_unix_ms + 1] {
            let response = daemon_watch(&database, &control, &watch, admitted_at_ms);
            assert_eq!(watch_answer(&response), (1, Some("deadline_elapsed"), false),
                       "{operation} at {admitted_at_ms}: {:?}", response.error_message);
        }
    }
}

/// Only a watch the caller must correct is refused: its binding (schema,
/// receipt, owner and tenant, the transport's permission check) or its
/// payload. Repeating it cannot succeed, so the refusal stays non-retryable.
#[test]
fn a_watch_the_caller_must_correct_stays_a_non_retryable_refusal_on_the_daemon_wire() {
    use crate::worker_control::{CONVERSATION_WATCH_OPERATION, WATCH_OPERATION};
    let directory = tempfile::tempdir().unwrap();
    let database = Mutex::new(AuthorityDatabase::initialize(directory.path()).unwrap());
    let control = database.lock().unwrap().worker_control.clone();
    type Refuse = fn(&mut Request);
    let refusals: [(&str, Refuse); 9] = [
        ("foreign owner", |watch| watch.owner_id = 12),
        ("foreign tenant", |watch| watch.tenant_id = Some(8)),
        ("other schema", |watch| watch.schema_id += 1),
        ("command receipt", |watch| watch.command_id = Some("watch-receipt".into())),
        ("missing cursor", |watch| watch.payload = b"{}".to_vec()),
        ("extra field", |watch| watch.payload = br#"{"cursor":null,"since":1}"#.to_vec()),
        ("malformed payload", |watch| watch.payload = br#"{"cursor":"#.to_vec()),
        ("cursor of another type", |watch| watch.payload = br#"{"cursor":7}"#.to_vec()),
        ("cursor of another channel", |watch| watch.payload = br#"{"cursor":{"epoch":"e","other":0}}"#.to_vec()),
    ];
    for operation in [WATCH_OPERATION, CONVERSATION_WATCH_OPERATION] {
        for (case, refuse) in refusals {
            let mut watch = request(operation, 11, json!({"cursor": null}));
            refuse(&mut watch);
            let response = daemon_watch(&database, &control, &watch, 1_000);
            assert_eq!(watch_answer(&response), (1, Some("worker_control_refused"), false),
                       "{operation}, {case}: {:?}", response.error_message);
        }
    }
}

mod common;
use serde_json::{Value, json};
use std::{
    io::{BufRead, BufReader, Write},
    process::{Child, Command, Stdio},
    sync::mpsc,
    time::Duration,
};
use uuid::Uuid;

struct Peer {
    child: Child,
    replies: mpsc::Receiver<String>,
}
impl Peer {
    fn open(store: &std::path::Path, crash: bool) -> Self {
        Self::open_fault(store, crash.then_some("--fixture-crash-after-effect"))
    }
    fn open_fault(store: &std::path::Path, fault: Option<&str>) -> Self {
        let mut command = Command::new(env!("CARGO_BIN_EXE_synth-code-runtime"));
        command
            .arg("stdio")
            .arg(store)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit());
        if let Some(flag) = fault {
            command.arg(flag);
        }
        let mut child = command.spawn().unwrap();
        let stdout = child.stdout.take().unwrap();
        let (tx, replies) = mpsc::channel();
        std::thread::spawn(move || {
            for line in BufReader::new(stdout).lines() {
                if tx.send(line.unwrap()).is_err() {
                    break;
                }
            }
        });
        Self { child, replies }
    }
    fn write(&mut self, method: &str, params: Value) {
        let message = json!({"jsonrpc":"2.0","id":1,"method":method,"params":params});
        let stdin = self.child.stdin.as_mut().unwrap();
        writeln!(stdin, "{message}").unwrap();
        stdin.flush().unwrap();
    }
    fn call(&mut self, method: &str, params: Value) -> Value {
        self.write(method, params);
        serde_json::from_str(
            &self
                .replies
                .recv_timeout(Duration::from_secs(5))
                .unwrap_or_else(|error| {
                    panic!(
                        "bounded RPC reply for {method}: {error}; child status {:?}",
                        self.child.try_wait()
                    )
                }),
        )
        .unwrap()
    }
    fn initialize(&mut self) {
        assert!(self.call("initialize", json!({}))["result"].is_object());
    }
}
impl Drop for Peer {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

#[test]
fn process_exit_after_effect_recovers_unknown_without_rerunning() {
    let store = common::tempdir().unwrap();
    let workspace = common::tempdir().unwrap();
    let mut peer = Peer::open(store.path(), true);
    peer.initialize();
    let start = peer.call(
        "thread/start",
        json!({"mutationId":Uuid::new_v4(),"workspace":workspace.path()}),
    );
    let session = start["result"]["result"]["id"].clone();
    let admitted = peer.call(
        "fixture/admit",
        json!({"mutationId":Uuid::new_v4(),"threadId":session,"text":"crash witness"}),
    );
    let op = admitted["result"]["result"]["id"].clone();
    let approval = json!({"mutationId":Uuid::new_v4(),"operationId":op,"requestDigest":admitted["result"]["result"]["request_digest"],"allow":true});
    peer.write("synth/approval/respond", approval.clone());
    let reply = peer.replies.recv_timeout(Duration::from_secs(5));
    assert!(
        reply.is_err(),
        "fault boundary unexpectedly replied: {reply:?}"
    );
    assert_eq!(peer.child.wait().unwrap().code(), Some(86));
    drop(peer);
    let path = workspace
        .path()
        .join(format!("fixture-{}.txt", op.as_str().unwrap()));
    let before = std::fs::metadata(&path).unwrap();
    assert_eq!(std::fs::read_to_string(&path).unwrap(), "crash witness");
    let mut peer = Peer::open(store.path(), false);
    peer.initialize();
    assert_eq!(
        peer.call("thread/read", json!({"threadId":session}))["result"]["operations"][0]["status"],
        "unknown"
    );
    assert_eq!(
        peer.call("synth/approval/respond", approval)["result"]["status"],
        "unknown"
    );
    assert_eq!(
        std::fs::metadata(&path).unwrap().modified().unwrap(),
        before.modified().unwrap()
    );
    assert_eq!(std::fs::read_dir(workspace.path()).unwrap().count(), 1);
}

#[test]
fn protocol_requires_initialization_and_rejects_changed_mutation() {
    let store = common::tempdir().unwrap();
    let workspace = common::tempdir().unwrap();
    let other = common::tempdir().unwrap();
    let mut peer = Peer::open(store.path(), false);
    assert_eq!(
        peer.call("synth/status", json!({}))["error"]["message"],
        "not_initialized"
    );
    peer.initialize();
    let id = Uuid::new_v4();
    let params = json!({"mutationId":id,"workspace":workspace.path()});
    let first = peer.call("thread/start", params.clone());
    let replay = peer.call("thread/start", params);
    assert_eq!(first["result"]["result"], replay["result"]["result"]);
    assert_eq!(replay["result"]["replayed"], true);
    assert_eq!(
        peer.call(
            "thread/start",
            json!({"mutationId":id,"workspace":other.path()})
        )["error"]["message"],
        "mutation_conflict"
    );
}

#[test]
fn abrupt_process_exit_rolls_back_uncommitted_sqlite_transaction() {
    let store = common::tempdir().unwrap();
    {
        let _owner = synth_code_runtime::store::SqliteStore::open(store.path()).unwrap();
    }
    let status = Command::new(std::env::current_exe().unwrap())
        .args(["--exact", "uncommitted_child", "--ignored"])
        .env("SYNTH_CODE_TEST_STORE", store.path())
        .status()
        .unwrap();
    assert_eq!(status.code(), Some(87));
    let db = rusqlite::Connection::open(store.path().join("journal.sqlite")).unwrap();
    assert_eq!(
        db.query_row(
            "SELECT count(*) FROM streams WHERE id='uncommitted'",
            [],
            |r| r.get::<_, i64>(0)
        )
        .unwrap(),
        0
    );
}

#[test]
#[ignore = "child process for abrupt uncommitted transaction test"]
fn uncommitted_child() {
    let path = std::path::PathBuf::from(
        std::env::var_os("SYNTH_CODE_TEST_STORE").expect("child test path required"),
    );
    let db = rusqlite::Connection::open(path.join("journal.sqlite")).unwrap();
    db.execute_batch("BEGIN IMMEDIATE; INSERT INTO streams VALUES('uncommitted',0);")
        .unwrap();
    std::process::exit(87);
}

#[test]
fn crash_after_real_edit_keeps_replacement_unknown_and_never_repeats() {
    use std::os::unix::fs::MetadataExt;
    let store = common::tempdir().unwrap();
    let workspace = common::tempdir().unwrap();
    let target = workspace.path().join("answer.py");
    std::fs::write(&target, "before\n").unwrap();
    let mut peer = Peer::open(store.path(), true);
    peer.initialize();
    let started = peer.call(
        "thread/start",
        json!({"mutationId":Uuid::new_v4(),"workspace":workspace.path()}),
    );
    let thread = started["result"]["result"]["id"].clone();
    let read = peer.call(
        "workspace/read",
        json!({"threadId":thread,"path":"answer.py"}),
    );
    let mutation = Uuid::new_v4();
    let proposal = json!({"mutationId":mutation,"threadId":thread,"path":"answer.py","expectedSha256":read["result"]["version"]["sha256"],"content":"after\n"});
    let admitted = peer.call("workspace/proposeEdit", proposal.clone());
    let op = admitted["result"]["result"]["id"].clone();
    let approval = json!({"mutationId":Uuid::new_v4(),"operationId":op,"requestDigest":admitted["result"]["result"]["request_digest"],"allow":true});
    peer.write("synth/approval/respond", approval.clone());
    let reply = peer.replies.recv_timeout(Duration::from_secs(5));
    assert!(
        reply.is_err(),
        "fault boundary unexpectedly replied: {reply:?}"
    );
    assert_eq!(peer.child.wait().unwrap().code(), Some(86));
    drop(peer);
    assert_eq!(std::fs::read_to_string(&target).unwrap(), "after\n");
    let inode = target.metadata().unwrap().ino();
    let mut peer = Peer::open(store.path(), false);
    peer.initialize();
    assert_eq!(
        peer.call("synth/operation/read", json!({"operationId":op}))["result"]["operation"]["status"],
        "unknown"
    );
    assert_eq!(
        peer.call("workspace/proposeEdit", proposal)["result"]["replayed"],
        true
    );
    peer.call("synth/approval/respond", approval);
    assert_eq!(target.metadata().unwrap().ino(), inode);
    assert_eq!(std::fs::read_to_string(&target).unwrap(), "after\n");
    assert!(peer.call("workspace/proposeEdit",json!({"mutationId":Uuid::new_v4(),"threadId":thread,"path":"other","expectedSha256":null,"content":"no"}))["error"].is_object());
}

#[test]
fn escaped_unicode_response_is_bounded_and_connection_remains_usable() {
    let store = common::tempdir().unwrap();
    let workspace = common::tempdir().unwrap();
    std::fs::write(workspace.path().join("control.txt"), vec![1u8; 12 * 1024]).unwrap();
    let mut peer = Peer::open(store.path(), false);
    peer.initialize();
    let start = peer.call(
        "thread/start",
        json!({"mutationId":Uuid::new_v4(),"workspace":workspace.path()}),
    );
    let thread = start["result"]["result"]["id"].clone();
    assert_eq!(
        peer.call(
            "workspace/read",
            json!({"threadId":thread,"path":"control.txt"})
        )["error"]["message"],
        "response_too_large"
    );
    assert!(peer.call("synth/status", json!({}))["result"].is_object());
}

#[test]
fn oversized_or_structured_request_ids_are_not_echoed() {
    let store = common::tempdir().unwrap();
    let mut peer = Peer::open(store.path(), false);
    let input = json!({"jsonrpc":"2.0","id":{"oversized":"x".repeat(60_000)},"method":"initialize","params":{}});
    let stdin = peer.child.stdin.as_mut().unwrap();
    writeln!(stdin, "{input}").unwrap();
    stdin.flush().unwrap();
    let line = peer.replies.recv_timeout(Duration::from_secs(5)).unwrap();
    assert!(line.len() < 512);
    let reply: Value = serde_json::from_str(&line).unwrap();
    assert!(reply["id"].is_null());
    assert_eq!(reply["error"]["message"], "invalid_request");
    peer.initialize();
}

#[test]
fn session_admission_retry_uses_receipt_when_workspace_disappears() {
    let store = common::tempdir().unwrap();
    let parent = common::tempdir().unwrap();
    let workspace = parent.path().join("project");
    std::fs::create_dir(&workspace).unwrap();
    let mut peer = Peer::open(store.path(), false);
    peer.initialize();
    let params = json!({"mutationId":Uuid::new_v4(),"workspace":workspace});
    let initial = peer.call("thread/start", params.clone());
    std::fs::rename(&workspace, parent.path().join("moved")).unwrap();
    let replay = peer.call("thread/start", params);
    assert_eq!(replay["result"]["replayed"], true);
    assert_eq!(initial["result"]["result"], replay["result"]["result"]);
}

#[test]
fn approved_but_undispatched_edit_resumes_explicitly_once() {
    let store = common::tempdir().unwrap();
    let workspace = common::tempdir().unwrap();
    std::fs::write(workspace.path().join("file"), "before").unwrap();
    let mut peer = Peer::open_fault(store.path(), Some("--fixture-crash-before-dispatch"));
    peer.initialize();
    let session = peer.call(
        "thread/start",
        json!({"mutationId":Uuid::new_v4(),"workspace":workspace.path()}),
    )["result"]["result"]["id"]
        .clone();
    let file = peer.call("workspace/read", json!({"threadId":session,"path":"file"}));
    let op=peer.call("workspace/proposeEdit",json!({"mutationId":Uuid::new_v4(),"threadId":session,"path":"file","expectedSha256":file["result"]["version"]["sha256"],"content":"after"}))["result"]["result"].clone();
    peer.write("synth/approval/respond",json!({"mutationId":Uuid::new_v4(),"operationId":op["id"],"requestDigest":op["request_digest"],"allow":true}));
    assert_eq!(peer.child.wait().unwrap().code(), Some(85));
    drop(peer);
    assert_eq!(
        std::fs::read_to_string(workspace.path().join("file")).unwrap(),
        "before"
    );
    let mut peer = Peer::open(store.path(), false);
    peer.initialize();
    assert_eq!(
        peer.call("synth/operation/read", json!({"operationId":op["id"]}))["result"]["operation"]["status"],
        "ready"
    );
    assert!(
        peer.call(
            "synth/operation/run",
            json!({"operationId":op["id"],"requestDigest":"wrong"})
        )["error"]
            .is_object()
    );
    let params = json!({"operationId":op["id"],"requestDigest":op["request_digest"]});
    assert_eq!(
        peer.call("synth/operation/run", params.clone())["result"]["operations"][0]["status"],
        "succeeded"
    );
    let before = workspace
        .path()
        .join("file")
        .metadata()
        .unwrap()
        .modified()
        .unwrap();
    peer.call("synth/operation/run", params);
    assert_eq!(
        workspace
            .path()
            .join("file")
            .metadata()
            .unwrap()
            .modified()
            .unwrap(),
        before
    );
    assert_eq!(
        std::fs::read_to_string(workspace.path().join("file")).unwrap(),
        "after"
    );
}

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
        let mut command = Command::new(env!("CARGO_BIN_EXE_synth-code-runtime"));
        command
            .arg("stdio")
            .arg(store)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit());
        if crash {
            command.arg("--fixture-crash-after-effect");
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
                .expect("bounded RPC reply"),
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
    let store = tempfile::tempdir().unwrap();
    let workspace = tempfile::tempdir().unwrap();
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
    assert!(peer.replies.recv_timeout(Duration::from_secs(5)).is_err());
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
    let store = tempfile::tempdir().unwrap();
    let workspace = tempfile::tempdir().unwrap();
    let other = tempfile::tempdir().unwrap();
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
    let store = tempfile::tempdir().unwrap();
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

//! One process owner; atomic expected-sequence appends through Temper's EventStore.
use fs2::FileExt;
use rusqlite::{Connection, OptionalExtension, Transaction, TransactionBehavior, params};
use sha2::{Digest, Sha256};
use std::{
    fs::{File, OpenOptions},
    os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt, PermissionsExt},
    path::Path,
    sync::Mutex,
};
use temper_runtime::persistence::*;

const MAX_EVENT_BYTES: usize = 64 * 1024;
const MAX_EVENTS: u64 = 100_000;

pub struct SqliteStore {
    connection: Mutex<Connection>,
    _owner: File,
}

fn storage(error: impl std::fmt::Display) -> PersistenceError {
    PersistenceError::Storage(error.to_string())
}

pub fn digest(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

impl SqliteStore {
    pub fn open(directory: &Path) -> anyhow::Result<Self> {
        if !directory.exists() {
            std::fs::DirBuilder::new()
                .recursive(true)
                .mode(0o700)
                .create(directory)?;
        }
        let metadata = directory.symlink_metadata()?;
        anyhow::ensure!(
            metadata.is_dir()
                && !metadata.file_type().is_symlink()
                && metadata.mode() & 0o077 == 0
                && metadata.uid() == unsafe { libc::geteuid() },
            "store directory must be private and owned"
        );
        let owner = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
            .open(directory.join("owner.lock"))?;
        let metadata = owner.metadata()?;
        anyhow::ensure!(
            metadata.is_file()
                && metadata.nlink() == 1
                && metadata.mode() & 0o077 == 0
                && metadata.uid() == unsafe { libc::geteuid() },
            "invalid store owner lock"
        );
        owner
            .try_lock_exclusive()
            .map_err(|_| anyhow::anyhow!("store already owned"))?;
        let database = directory.join("journal.sqlite");
        if let Ok(metadata) = database.symlink_metadata() {
            anyhow::ensure!(
                metadata.is_file()
                    && !metadata.file_type().is_symlink()
                    && metadata.nlink() == 1
                    && metadata.mode() & 0o077 == 0,
                "invalid journal file"
            );
        }
        let connection = Connection::open_with_flags(
            &database,
            rusqlite::OpenFlags::SQLITE_OPEN_READ_WRITE
                | rusqlite::OpenFlags::SQLITE_OPEN_CREATE
                | rusqlite::OpenFlags::SQLITE_OPEN_NO_MUTEX
                | rusqlite::OpenFlags::SQLITE_OPEN_NOFOLLOW,
        )?;
        std::fs::set_permissions(database, std::fs::Permissions::from_mode(0o600))?;
        connection.busy_timeout(std::time::Duration::from_secs(2))?;
        let version: i64 = connection.pragma_query_value(None, "user_version", |row| row.get(0))?;
        anyhow::ensure!(
            version == 0 || version == 1,
            "unsupported store schema {version}"
        );
        if version == 0 {
            let count: i64 = connection.query_row(
                "SELECT count(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'", [], |r| r.get(0))?;
            anyhow::ensure!(
                count == 0,
                "unversioned nonempty store refuses initialization"
            );
        }
        connection.execute_batch(
            "PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA foreign_keys=ON;
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS streams(id TEXT PRIMARY KEY, head INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS events(stream TEXT NOT NULL REFERENCES streams(id),
                sequence INTEGER NOT NULL, body TEXT NOT NULL, digest TEXT NOT NULL,
                PRIMARY KEY(stream,sequence));
            PRAGMA user_version=1; COMMIT;",
        )?;
        Ok(Self {
            connection: Mutex::new(connection),
            _owner: owner,
        })
    }

    fn append_in(
        tx: &Transaction<'_>,
        id: &str,
        expected: u64,
        events: &[PersistenceEnvelope],
    ) -> Result<u64, PersistenceError> {
        if id.is_empty() || id.len() > 256 {
            return Err(storage("invalid persistence id"));
        }
        let head: u64 = tx
            .query_row("SELECT head FROM streams WHERE id=?", [id], |r| r.get(0))
            .optional()
            .map_err(storage)?
            .unwrap_or(0);
        if head != expected {
            return Err(PersistenceError::ConcurrencyViolation {
                expected,
                actual: head,
            });
        }
        let end = head
            .checked_add(events.len() as u64)
            .ok_or_else(|| storage("sequence overflow"))?;
        if end > MAX_EVENTS {
            return Err(storage("journal capacity reached"));
        }
        tx.execute("INSERT OR IGNORE INTO streams VALUES(?,0)", [id])
            .map_err(storage)?;
        for (index, event) in events.iter().enumerate() {
            if event.sequence_nr != head + index as u64 + 1 {
                return Err(storage("noncontiguous event sequence"));
            }
            let body = serde_json::to_string(event).map_err(storage)?;
            if body.len() > MAX_EVENT_BYTES {
                return Err(storage("event exceeds size limit"));
            }
            tx.execute(
                "INSERT INTO events VALUES(?,?,?,?)",
                params![id, event.sequence_nr, body, digest(body.as_bytes())],
            )
            .map_err(storage)?;
        }
        tx.execute("UPDATE streams SET head=? WHERE id=?", params![end, id])
            .map_err(storage)?;
        Ok(end)
    }
}

impl EventStore for SqliteStore {
    async fn append(
        &self,
        id: &str,
        expected: u64,
        events: &[PersistenceEnvelope],
    ) -> Result<u64, PersistenceError> {
        let mut connection = self.connection.lock().map_err(storage)?;
        let tx = connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(storage)?;
        let end = Self::append_in(&tx, id, expected, events)?;
        tx.commit().map_err(storage)?;
        Ok(end)
    }

    async fn append_batch(
        &self,
        appends: &[PersistenceAppend],
    ) -> Result<Vec<PersistenceAppendResult>, PersistenceError> {
        let mut connection = self.connection.lock().map_err(storage)?;
        let tx = connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(storage)?;
        let mut result = Vec::new();
        for append in appends {
            let sequence_nr = Self::append_in(
                &tx,
                &append.persistence_id,
                append.expected_sequence,
                &append.events,
            )?;
            result.push(PersistenceAppendResult {
                persistence_id: append.persistence_id.clone(),
                sequence_nr,
            });
        }
        tx.commit().map_err(storage)?;
        Ok(result)
    }

    async fn read_events(
        &self,
        id: &str,
        from: u64,
    ) -> Result<Vec<PersistenceEnvelope>, PersistenceError> {
        let connection = self.connection.lock().map_err(storage)?;
        let head: u64 = connection
            .query_row("SELECT head FROM streams WHERE id=?", [id], |r| r.get(0))
            .optional()
            .map_err(storage)?
            .unwrap_or(0);
        let mut statement = connection
            .prepare("SELECT sequence,body,digest FROM events WHERE stream=? ORDER BY sequence")
            .map_err(storage)?;
        let mut rows = statement.query([id]).map_err(storage)?;
        let mut sequence = 0;
        let mut result = Vec::new();
        while let Some(row) = rows.next().map_err(storage)? {
            let position: u64 = row.get(0).map_err(storage)?;
            let body: String = row.get(1).map_err(storage)?;
            let hash: String = row.get(2).map_err(storage)?;
            sequence += 1;
            if position != sequence
                || sequence > MAX_EVENTS
                || body.len() > MAX_EVENT_BYTES
                || digest(body.as_bytes()) != hash
            {
                return Err(storage("journal integrity failure"));
            }
            let event: PersistenceEnvelope = serde_json::from_str(&body).map_err(storage)?;
            if event.sequence_nr != sequence {
                return Err(storage("envelope sequence mismatch"));
            }
            if position > from {
                result.push(event);
            }
        }
        if sequence != head {
            return Err(storage("journal head mismatch"));
        }
        Ok(result)
    }

    // This embedded adapter has no entity-query or snapshot plane. Refuse these
    // methods explicitly rather than pretending a successful empty result.
    async fn save_snapshot(&self, _: &str, _: u64, _: &[u8]) -> Result<(), PersistenceError> {
        Err(storage("snapshots unsupported"))
    }
    async fn load_snapshot(&self, _: &str) -> Result<Option<(u64, Vec<u8>)>, PersistenceError> {
        Err(storage("snapshots unsupported"))
    }
    async fn list_entity_ids(&self, _: &str) -> Result<Vec<(String, String)>, PersistenceError> {
        Err(storage("entity queries unsupported"))
    }
    async fn list_entity_ids_by_type(
        &self,
        _: &str,
        _: &str,
    ) -> Result<Vec<String>, PersistenceError> {
        Err(storage("entity queries unsupported"))
    }
    async fn append_with_index_rows(
        &self,
        _: &str,
        _: u64,
        _: &[PersistenceEnvelope],
        _: &[EntityKeyRow],
        _: &[EntityVectorRow],
        _: bool,
    ) -> Result<u64, PersistenceError> {
        Err(storage("indexes unsupported"))
    }
    async fn backfill_entity_vectors(
        &self,
        _: &str,
        _: &str,
        _: &str,
        _: &[EntityVectorRow],
    ) -> Result<(), PersistenceError> {
        Err(storage("indexes unsupported"))
    }
    async fn vector_candidates(
        &self,
        _: &str,
        _: &str,
        _: &str,
        _: &str,
        _: usize,
    ) -> Result<Vec<EntityVectorCandidate>, PersistenceError> {
        Err(storage("indexes unsupported"))
    }
    async fn mark_vector_index_backfilled(
        &self,
        _: &str,
        _: &str,
        _: &str,
    ) -> Result<(), PersistenceError> {
        Err(storage("indexes unsupported"))
    }
    async fn vector_index_backfilled_types(
        &self,
        _: &str,
    ) -> Result<Vec<(String, String)>, PersistenceError> {
        Err(storage("indexes unsupported"))
    }
    async fn vectored_entity_ids_for_type(
        &self,
        _: &str,
        _: &str,
    ) -> Result<Vec<String>, PersistenceError> {
        Err(storage("indexes unsupported"))
    }
    async fn backfill_entity_keys(
        &self,
        _: &str,
        _: &str,
        _: &str,
        _: &[EntityKeyRow],
    ) -> Result<(), PersistenceError> {
        Err(storage("indexes unsupported"))
    }
    async fn lookup_by_key(
        &self,
        _: &str,
        _: &str,
        _: &str,
        _: &str,
    ) -> Result<Option<String>, PersistenceError> {
        Err(storage("indexes unsupported"))
    }
    async fn mark_key_index_backfilled(
        &self,
        _: &str,
        _: &str,
        _: &str,
    ) -> Result<(), PersistenceError> {
        Err(storage("indexes unsupported"))
    }
    async fn key_index_backfilled_types(
        &self,
        _: &str,
    ) -> Result<Vec<(String, String)>, PersistenceError> {
        Err(storage("indexes unsupported"))
    }
    async fn keyed_entity_ids_for_type(
        &self,
        _: &str,
        _: &str,
    ) -> Result<Vec<String>, PersistenceError> {
        Err(storage("indexes unsupported"))
    }
}

//! Immutable content-addressed payloads, independent of the event journal.
use crate::store::digest;
use anyhow::{Context, ensure};
use std::{
    fs::{self, OpenOptions},
    io::{Read, Write},
    os::unix::fs::{DirBuilderExt, OpenOptionsExt},
    path::{Path, PathBuf},
};

#[derive(Clone)]
pub struct Artifacts {
    root: PathBuf,
    gate: std::sync::Arc<std::sync::Mutex<()>>,
}
impl Artifacts {
    pub fn open(store: &Path) -> anyhow::Result<Self> {
        let root = store.join("artifacts");
        if !root.exists() {
            fs::DirBuilder::new().mode(0o700).create(&root)?;
        }
        ensure!(
            root.symlink_metadata()?.is_dir() && !root.symlink_metadata()?.file_type().is_symlink(),
            "invalid_artifact_directory"
        );
        ensure!(
            std::os::unix::fs::PermissionsExt::mode(&root.metadata()?.permissions()) & 0o077 == 0,
            "artifact directory must be private"
        );
        Ok(Self {
            root,
            gate: Default::default(),
        })
    }
    pub fn put(&self, bytes: &[u8]) -> anyhow::Result<String> {
        let _guard = self
            .gate
            .lock()
            .map_err(|_| anyhow::anyhow!("artifact lock poisoned"))?;
        ensure!(bytes.len() <= 8 * 1024 * 1024, "artifact_too_large");
        let hash = digest(bytes);
        let target = self.root.join(&hash);
        if target.exists() {
            ensure!(self.get(&hash)? == bytes, "artifact_conflict");
            return Ok(hash);
        }
        let mut size = 0u64;
        let mut count = 0usize;
        for item in fs::read_dir(&self.root)? {
            let metadata = item?.metadata()?;
            size = size.saturating_add(metadata.len());
            count += 1;
        }
        ensure!(
            count < 16384 && size.saturating_add(bytes.len() as u64) <= 512 * 1024 * 1024,
            "artifact_storage_capacity_reached"
        );
        let stage = self.root.join(format!(".stage-{}", uuid::Uuid::new_v4()));
        let result = (|| -> anyhow::Result<()> {
            let mut file = OpenOptions::new()
                .create_new(true)
                .write(true)
                .mode(0o600)
                .open(&stage)?;
            file.write_all(bytes)?;
            file.sync_all()?;
            match fs::hard_link(&stage, &target) {
                Ok(()) => {}
                Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
                    ensure!(self.get(&hash)? == bytes, "artifact_conflict")
                }
                Err(error) => return Err(error.into()),
            }
            fs::remove_file(&stage)?;
            fs::File::open(&self.root)?.sync_all()?;
            Ok(())
        })();
        if stage.exists() {
            let _ = fs::remove_file(&stage);
        }
        result?;
        Ok(hash)
    }
    pub fn put_json(&self, value: &impl serde::Serialize) -> anyhow::Result<String> {
        self.put(&serde_json::to_vec(value)?)
    }
    pub fn get(&self, hash: &str) -> anyhow::Result<Vec<u8>> {
        ensure!(
            hash.len() == 64
                && hash
                    .bytes()
                    .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase()),
            "invalid_artifact_hash"
        );
        let path = self.root.join(hash);
        let mut file = OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
            .open(path)
            .context("artifact_missing")?;
        let metadata = file.metadata()?;
        ensure!(
            metadata.is_file()
                && !metadata.file_type().is_symlink()
                && metadata.len() <= 8 * 1024 * 1024,
            "invalid_artifact_file"
        );
        let mut bytes = Vec::new();
        (&mut file)
            .take(8 * 1024 * 1024 + 1)
            .read_to_end(&mut bytes)?;
        ensure!(bytes.len() <= 8 * 1024 * 1024, "artifact_too_large");
        ensure!(digest(&bytes) == hash, "artifact_integrity_failure");
        Ok(bytes)
    }
    pub fn json(&self, hash: &str) -> anyhow::Result<serde_json::Value> {
        Ok(serde_json::from_slice(&self.get(hash)?)?)
    }
}

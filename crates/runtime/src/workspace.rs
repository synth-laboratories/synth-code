//! Bounded descriptor-relative workspace access. This is not an OS sandbox.
use crate::{domain::Session, store::digest};
use anyhow::{Context, bail, ensure};
use serde::{Deserialize, Serialize};
use std::{
    ffi::{CStr, CString},
    fs::{File, OpenOptions},
    io::{Read, Write},
    os::{
        fd::{AsRawFd, FromRawFd, RawFd},
        unix::fs::{MetadataExt, OpenOptionsExt},
    },
    path::Path,
};
use uuid::Uuid;

pub const MAX_READ: usize = 12 * 1024;
pub const MAX_EDIT: usize = 8192;
const MAX_ENTRIES: usize = 1024;
const MAX_SCANNED_BYTES: usize = 1024 * 1024;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FileVersion {
    pub sha256: String,
    pub device: u64,
    pub inode: u64,
    pub length: u64,
    pub modified_seconds: i64,
    pub modified_nanos: i64,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReadResult {
    pub path: String,
    pub text: String,
    pub version: FileVersion,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EditRequest {
    pub path: String,
    pub expected: Option<FileVersion>,
    pub before: Option<String>,
    pub content: String,
    pub parent_device: u64,
    pub parent_inode: u64,
}
impl EditRequest {
    pub fn validate(&self) -> anyhow::Result<()> {
        ensure!(
            serde_json::to_vec(self)?.len() <= 24 * 1024,
            "encoded_edit_too_large"
        );
        components(&self.path)?;
        ensure!(
            self.content.len() <= MAX_EDIT && !self.content.contains('\0'),
            "replacement_too_large_or_binary"
        );
        ensure!(
            self.before.as_ref().is_none_or(|s| s.len() <= MAX_EDIT),
            "preimage_too_large"
        );
        ensure!(
            self.expected.is_some() == self.before.is_some(),
            "invalid_preimage"
        );
        if let (Some(version), Some(before)) = (&self.expected, &self.before) {
            ensure!(
                digest(before.as_bytes()) == version.sha256,
                "preimage_hash_mismatch"
            );
        }
        Ok(())
    }
    pub fn fingerprint(&self) -> String {
        digest(&serde_json::to_vec(self).expect("serializable edit request"))
    }
}
#[derive(Debug, Serialize)]
pub struct SearchMatch {
    pub path: String,
    pub line: usize,
    pub text: String,
}
#[derive(Debug, Serialize, Default)]
pub struct SearchResult {
    pub matches: Vec<SearchMatch>,
    pub entries_examined: usize,
    pub bytes_examined: usize,
    pub skipped: usize,
    pub truncated: bool,
}

fn protected(name: &str) -> bool {
    let s = name.to_ascii_lowercase();
    s.starts_with(".env")
        || s.starts_with(".synth-")
        || matches!(
            s.as_str(),
            ".git"
                | ".hg"
                | ".svn"
                | ".ssh"
                | ".aws"
                | ".config"
                | ".pressure"
                | ".state"
                | ".cache"
                | ".venv"
                | "venv"
                | "node_modules"
                | "target"
                | "__pycache__"
                | ".netrc"
                | ".npmrc"
                | ".pypirc"
                | "credentials.json"
                | "id_rsa"
                | "id_ed25519"
        )
        || [".pem", ".key", ".p12", ".pfx"]
            .iter()
            .any(|suffix| s.ends_with(suffix))
}
fn components(path: &str) -> anyhow::Result<Vec<&str>> {
    ensure!(
        !path.is_empty()
            && path.len() <= 1024
            && !path.contains(['\\', '\0'])
            && !path.starts_with('/'),
        "invalid_relative_path"
    );
    let parts = path.split('/').collect::<Vec<_>>();
    ensure!(
        parts.len() <= 16
            && parts
                .iter()
                .all(|p| !p.is_empty() && *p != "." && *p != ".." && !protected(p)),
        "protected_or_invalid_path"
    );
    Ok(parts)
}
fn open_at(directory: RawFd, name: &str, flags: i32) -> anyhow::Result<File> {
    let name = CString::new(name)?;
    // SAFETY: the directory descriptor remains live, name is NUL-terminated,
    // and a successful call transfers one newly owned descriptor to File.
    let fd = unsafe {
        libc::openat(
            directory,
            name.as_ptr(),
            flags | libc::O_NOFOLLOW | libc::O_CLOEXEC | libc::O_NONBLOCK,
            0o600,
        )
    };
    if fd < 0 {
        return Err(std::io::Error::last_os_error().into());
    }
    Ok(unsafe { File::from_raw_fd(fd) })
}

pub struct Workspace {
    root: File,
}
impl Workspace {
    pub fn open(session: &Session) -> anyhow::Result<Self> {
        let root = OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC)
            .open(Path::new(&session.workspace))?;
        let metadata = root.metadata()?;
        ensure!(
            metadata.dev() == session.device && metadata.ino() == session.inode,
            "workspace_identity_changed"
        );
        Ok(Self { root })
    }
    fn parent(&self, path: &str) -> anyhow::Result<(File, String)> {
        let parts = components(path)?;
        let mut directory = self.root.try_clone()?;
        for name in &parts[..parts.len() - 1] {
            directory = open_at(
                directory.as_raw_fd(),
                name,
                libc::O_RDONLY | libc::O_DIRECTORY,
            )
            .context("parent_directory_refused")?;
        }
        Ok((directory, parts.last().unwrap().to_string()))
    }
    fn read_at(
        directory: &File,
        name: &str,
        path: &str,
        limit: usize,
    ) -> anyhow::Result<ReadResult> {
        let mut file = open_at(directory.as_raw_fd(), name, libc::O_RDONLY)?;
        let before = file.metadata()?;
        ensure!(
            before.is_file() && before.nlink() == 1,
            "nonregular_or_hardlinked_file"
        );
        ensure!(before.len() <= limit as u64, "file_too_large");
        let mut bytes = Vec::new();
        Read::by_ref(&mut file)
            .take(limit as u64 + 1)
            .read_to_end(&mut bytes)?;
        ensure!(
            bytes.len() <= limit && !bytes.contains(&0),
            "file_too_large_or_binary"
        );
        // Reopen the entry to detect replacement while reading; also verify the
        // original file did not change its mtime/length during the bounded read.
        let current = open_at(directory.as_raw_fd(), name, libc::O_RDONLY)?;
        let after = current.metadata()?;
        let original_after = file.metadata()?;
        ensure!(
            before.dev() == after.dev()
                && before.ino() == after.ino()
                && before.len() == after.len()
                && before.mtime() == after.mtime()
                && before.mtime_nsec() == after.mtime_nsec(),
            "file_changed_during_read"
        );
        ensure!(
            before.len() == original_after.len()
                && before.mtime() == original_after.mtime()
                && before.mtime_nsec() == original_after.mtime_nsec()
                && bytes.len() as u64 == after.len(),
            "file_changed_during_read"
        );
        let version = FileVersion {
            sha256: digest(&bytes),
            device: after.dev(),
            inode: after.ino(),
            length: after.len(),
            modified_seconds: after.mtime(),
            modified_nanos: after.mtime_nsec(),
        };
        Ok(ReadResult {
            path: path.into(),
            text: String::from_utf8(bytes).context("non_utf8_file")?,
            version,
        })
    }
    pub fn read(&self, path: &str) -> anyhow::Result<ReadResult> {
        let (parent, name) = self.parent(path)?;
        Self::read_at(&parent, &name, path, MAX_READ)
    }
    pub fn prepare_edit(
        &self,
        path: &str,
        expected_sha256: Option<&str>,
        content: String,
    ) -> anyhow::Result<EditRequest> {
        ensure!(
            content.len() <= MAX_EDIT && !content.contains('\0'),
            "replacement_too_large_or_binary"
        );
        let (parent, name) = self.parent(path)?;
        let identity = parent.metadata()?;
        let (expected, before) = if let Some(hash) = expected_sha256 {
            let current = Self::read_at(&parent, &name, path, MAX_EDIT)?;
            ensure!(current.version.sha256 == hash, "file_precondition_failed");
            (Some(current.version), Some(current.text))
        } else {
            match open_at(parent.as_raw_fd(), &name, libc::O_RDONLY) {
                Err(error)
                    if error
                        .downcast_ref::<std::io::Error>()
                        .is_some_and(|e| e.kind() == std::io::ErrorKind::NotFound) => {}
                _ => bail!("target_already_exists_or_refused"),
            }
            (None, None)
        };
        let request = EditRequest {
            path: path.into(),
            expected,
            before,
            content,
            parent_device: identity.dev(),
            parent_inode: identity.ino(),
        };
        request.validate()?;
        Ok(request)
    }
    pub fn search(&self, query: &str) -> anyhow::Result<SearchResult> {
        ensure!(
            !query.is_empty() && query.len() <= 256 && !query.contains('\0'),
            "invalid_search_query"
        );
        let mut result = SearchResult::default();
        Self::walk(&self.root, "", query, 0, &mut result)?;
        Ok(result)
    }
    fn walk(
        directory: &File,
        prefix: &str,
        query: &str,
        depth: usize,
        result: &mut SearchResult,
    ) -> anyhow::Result<()> {
        if depth >= 16 {
            result.truncated = true;
            return Ok(());
        }
        let entries = names(directory, MAX_ENTRIES - result.entries_examined)?;
        if entries.1 {
            result.truncated = true;
        }
        for name in entries.0 {
            if result.entries_examined >= MAX_ENTRIES
                || result.matches.len() >= 32
                || result.bytes_examined >= MAX_SCANNED_BYTES
            {
                result.truncated = true;
                break;
            }
            result.entries_examined += 1;
            if protected(&name) {
                result.skipped += 1;
                continue;
            }
            let path = if prefix.is_empty() {
                name.clone()
            } else {
                format!("{prefix}/{name}")
            };
            if let Ok(child) = open_at(
                directory.as_raw_fd(),
                &name,
                libc::O_RDONLY | libc::O_DIRECTORY,
            ) {
                Self::walk(&child, &path, query, depth + 1, result)?;
                continue;
            }
            match Self::read_at(
                directory,
                &name,
                &path,
                MAX_READ.min(MAX_SCANNED_BYTES - result.bytes_examined),
            ) {
                Ok(file) => {
                    result.bytes_examined += file.text.len();
                    for (line, text) in file.text.lines().enumerate() {
                        if text.contains(query) {
                            result.matches.push(SearchMatch {
                                path: path.clone(),
                                line: line + 1,
                                text: text.chars().take(160).collect(),
                            });
                            if result.matches.len() >= 32 {
                                result.truncated = true;
                                break;
                            }
                        }
                    }
                }
                Err(_) => result.skipped += 1,
            }
        }
        Ok(())
    }
}

fn names(directory: &File, limit: usize) -> anyhow::Result<(Vec<String>, bool)> {
    // Reopen "." for an independent directory offset; dup shares the original
    // open-file description and would make subsequent searches skip entries.
    let stream_file = open_at(
        directory.as_raw_fd(),
        ".",
        libc::O_RDONLY | libc::O_DIRECTORY,
    )?;
    let fd = std::os::fd::IntoRawFd::into_raw_fd(stream_file);
    let raw = unsafe { libc::fdopendir(fd) };
    if raw.is_null() {
        unsafe { libc::close(fd) };
        return Err(std::io::Error::last_os_error().into());
    }
    struct Directory(*mut libc::DIR);
    impl Drop for Directory {
        fn drop(&mut self) {
            unsafe { libc::closedir(self.0) };
        }
    }
    let owner = Directory(raw);
    let mut result = Vec::new();
    let mut truncated = false;
    let mut seen = 0;
    loop {
        // readdir uses NULL for both EOF and errors; reset/check thread-local errno.
        #[cfg(target_os = "macos")]
        let errno = unsafe { libc::__error() };
        #[cfg(target_os = "linux")]
        let errno = unsafe { libc::__errno_location() };
        unsafe { *errno = 0 };
        let entry = unsafe { libc::readdir(owner.0) };
        if entry.is_null() {
            let code = unsafe { *errno };
            if code != 0 {
                return Err(std::io::Error::from_raw_os_error(code).into());
            }
            break;
        }
        let name = unsafe { CStr::from_ptr((*entry).d_name.as_ptr()) };
        if name.to_bytes() == b"." || name.to_bytes() == b".." {
            continue;
        }
        if seen >= limit {
            truncated = true;
            break;
        }
        seen += 1;
        if let Ok(name) = name.to_str() {
            result.push(name.to_string());
        }
    }
    result.sort();
    Ok((result, truncated))
}

#[derive(Debug, Serialize)]
pub struct EditOutcome {
    pub path: String,
    pub before_sha256: Option<String>,
    pub after_sha256: String,
    pub bytes: usize,
}
#[derive(Debug)]
pub struct EditFailure {
    pub uncertain: bool,
    pub error: String,
}
impl Workspace {
    /// Stage+sync, recheck the approved preimage, then atomically install and sync.
    /// POSIX rename is not a compare-and-swap against uncooperative external writers.
    pub fn apply_edit(
        &self,
        id: Uuid,
        request: &EditRequest,
        crash_after_replace: bool,
    ) -> Result<EditOutcome, EditFailure> {
        let mut installed = false;
        let result = (|| -> anyhow::Result<EditOutcome> {
            request.validate()?;
            let (parent, name) = self.parent(&request.path)?;
            let identity = parent.metadata()?;
            ensure!(
                identity.dev() == request.parent_device && identity.ino() == request.parent_inode,
                "parent_identity_changed"
            );
            let mode = if let Some(expected) = &request.expected {
                ensure!(
                    Self::read_at(&parent, &name, &request.path, MAX_EDIT)?.version == *expected,
                    "file_precondition_failed"
                );
                open_at(parent.as_raw_fd(), &name, libc::O_RDONLY)?
                    .metadata()?
                    .mode()
                    & 0o777
            } else {
                0o600
            };
            let temporary = format!(".synth-code-{id}.stage");
            let mut file = open_at(
                parent.as_raw_fd(),
                &temporary,
                libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL,
            )?;
            struct Staged {
                parent: File,
                name: CString,
            }
            impl Drop for Staged {
                fn drop(&mut self) {
                    unsafe { libc::unlinkat(self.parent.as_raw_fd(), self.name.as_ptr(), 0) };
                }
            }
            let _staged = Staged {
                parent: parent.try_clone()?,
                name: CString::new(temporary.clone())?,
            };
            file.write_all(request.content.as_bytes())?;
            use std::os::unix::fs::PermissionsExt;
            file.set_permissions(std::fs::Permissions::from_mode(mode))?;
            file.sync_all()?;
            let source = CString::new(temporary)?;
            let target = CString::new(name.clone())?;
            let code = if let Some(expected) = &request.expected {
                ensure!(
                    Self::read_at(&parent, &name, &request.path, MAX_EDIT)?.version == *expected,
                    "file_precondition_failed"
                );
                unsafe {
                    libc::renameat(
                        parent.as_raw_fd(),
                        source.as_ptr(),
                        parent.as_raw_fd(),
                        target.as_ptr(),
                    )
                }
            } else {
                // linkat creates a new destination atomically and refuses EEXIST.
                unsafe {
                    libc::linkat(
                        parent.as_raw_fd(),
                        source.as_ptr(),
                        parent.as_raw_fd(),
                        target.as_ptr(),
                        0,
                    )
                }
            };
            if code != 0 {
                return Err(std::io::Error::last_os_error().into());
            }
            installed = true;
            if crash_after_replace {
                std::process::exit(86);
            }
            drop(_staged);
            parent.sync_all()?;
            Ok(EditOutcome {
                path: request.path.clone(),
                before_sha256: request.expected.as_ref().map(|v| v.sha256.clone()),
                after_sha256: digest(request.content.as_bytes()),
                bytes: request.content.len(),
            })
        })();
        result.map_err(|e| EditFailure {
            uncertain: installed,
            error: e.to_string(),
        })
    }
}

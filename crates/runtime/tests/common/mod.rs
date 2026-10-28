use std::os::unix::fs::PermissionsExt;
pub fn tempdir() -> std::io::Result<tempfile::TempDir> {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../artifacts/tests");
    std::fs::create_dir_all(&root)?;
    let directory = tempfile::tempdir_in(root.canonicalize()?)?;
    std::fs::set_permissions(directory.path(), std::fs::Permissions::from_mode(0o700))?;
    Ok(directory)
}

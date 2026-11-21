//! `deploy` (sdk/NODE.md §5): a service's package, a zip of its files, unpacked into a new
//! folder of the managed services root.
//!
//! TODO(review): isolation — what a package may hold: plain files and folders only, every
//! name relative and inside the new folder (no `..`, no root, no drive, no `:`), no links;
//! at most 20,000 entries and 2 GB unpacked, counted as written (not as the zip says). Any
//! entry that breaks a rule refuses the whole package, and the half-made folder is removed.

use std::io::{Read, Write};
use std::path::{Path, PathBuf};

const MOST_ENTRIES: usize = 20_000;
const MOST_BYTES: u64 = 2 << 30;

/// The parts of an entry's name, or why it is refused.
fn parts(name: &str) -> Result<Vec<String>, String> {
    let shown = crate::util::cut(name, 120);
    if name.starts_with('/') || name.starts_with('\\') || name.contains('\0') {
        return Err(format!("{shown:?}: an absolute name"));
    }
    let mut out = Vec::new();
    for p in name.split(['/', '\\']) {
        if p.is_empty() || p == "." {
            continue;
        }
        if p == ".." {
            return Err(format!("{shown:?}: a name leaving the folder"));
        }
        if p.contains(':') || p.chars().any(char::is_control) || p.ends_with(' ') || p.ends_with('.') {
            return Err(format!("{shown:?}: a name this machine would not take as a plain file"));
        }
        out.push(p.to_string());
    }
    if out.is_empty() || out.len() > 32 {
        return Err(format!("{shown:?}: no name, or folders more than 32 deep"));
    }
    Ok(out)
}

/// Unpacks `zip` into `into` (which must not exist yet). Returns files and bytes written.
pub fn unpack(zip: &Path, into: &Path) -> Result<(usize, u64), String> {
    std::fs::create_dir(into).map_err(|e| format!("cannot make {}: {e}", into.display()))?;
    let done = unpack_into(zip, into);
    if done.is_err() {
        let _ = std::fs::remove_dir_all(into);
    }
    done
}

fn unpack_into(zip: &Path, into: &Path) -> Result<(usize, u64), String> {
    let file = std::fs::File::open(zip).map_err(|e| e.to_string())?;
    let mut archive = zip::ZipArchive::new(file).map_err(|e| format!("the package is not a zip: {e}"))?;
    if archive.len() > MOST_ENTRIES {
        return Err(format!("the package has {} entries, more than {MOST_ENTRIES}", archive.len()));
    }
    let (mut files, mut bytes) = (0usize, 0u64);
    for i in 0..archive.len() {
        let mut entry = archive.by_index(i).map_err(|e| format!("entry {i}: {e}"))?;
        let name = entry.name().to_string();
        if entry.is_symlink() {
            return Err(format!("{:?}: a link", crate::util::cut(&name, 120)));
        }
        let mode = entry.unix_mode().unwrap_or(0);
        let kind = mode & 0o170000;
        if kind != 0 && kind != 0o100000 && kind != 0o040000 {
            return Err(format!("{:?}: not a plain file or folder", crate::util::cut(&name, 120)));
        }
        let mut path: PathBuf = into.to_path_buf();
        path.extend(parts(&name)?);
        if entry.is_dir() {
            make_dir(&path)?;
            continue;
        }
        if let Some(parent) = path.parent() {
            make_dir(parent)?;
        }
        let mut out = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&path)
            .map_err(|e| format!("cannot write {}: {e}", path.display()))?;
        let room = MOST_BYTES - bytes;
        let n = std::io::copy(&mut entry.by_ref().take(room + 1), &mut out)
            .map_err(|e| format!("{:?}: {e}", crate::util::cut(&name, 120)))?;
        if n > room {
            return Err(format!("the package unpacks to more than {} GB", MOST_BYTES >> 30));
        }
        out.flush().map_err(|e| e.to_string())?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let run = if mode & 0o111 != 0 { 0o755 } else { 0o644 };
            std::fs::set_permissions(&path, std::fs::Permissions::from_mode(run)).map_err(|e| e.to_string())?;
        }
        bytes += n;
        files += 1;
    }
    #[cfg(unix)]
    open_folders(into)?;
    Ok((files, bytes))
}

fn make_dir(path: &Path) -> Result<(), String> {
    std::fs::create_dir_all(path).map_err(|e| format!("cannot make {}: {e}", path.display()))
}

/// Every folder of the package 0755, whatever the umask: the job account reads them.
#[cfg(unix)]
fn open_folders(dir: &Path) -> Result<(), String> {
    use std::os::unix::fs::PermissionsExt;
    std::fs::set_permissions(dir, std::fs::Permissions::from_mode(0o755)).map_err(|e| e.to_string())?;
    for entry in std::fs::read_dir(dir).map_err(|e| e.to_string())?.flatten() {
        if entry.file_type().map(|t| t.is_dir()).unwrap_or(false) {
            open_folders(&entry.path())?;
        }
    }
    Ok(())
}

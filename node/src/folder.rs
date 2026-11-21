//! Node 2.1 (spec v3 §8.3, sdk/NODE.md §6): the folders the person at this machine allowed
//! (`amsyos-node allow <name> <path>`, only here, never from afar). A job's input
//! `folder:<name>/<path>` is read in place: the node, as its own account (the person's, who
//! can read the folder), copies what the path names into the job's own folder, where the job
//! account reads it and cannot change it. The Gateway learns the names, never the paths.
//!
//! TODO(review): isolation — why a copy, and not a read-only bind or a link to the folder:
//! - a bind mount needs root or CAP_SYS_ADMIN on Linux (the node never runs as root and gives
//!   the job account no namespace of its own), and Windows has no bind for one process;
//! - a symbolic link or a junction in the job's folder would have the job account open the
//!   person's folder itself: that folder (and everything under it) would need an ACL or a
//!   mode for amsyos-job, changed on the person's own files; the job could follow any link
//!   that lies inside it out of it; and what the job reads could change while it runs;
//! - a copy made here needs no privilege and changes no permission of the person's files.
//!   Each file is opened without following a link and used only if it really lies where the
//!   walk from the allowed folder says (`open_checked`), so a link, a junction or a folder
//!   swapped on the way never leads out; links inside a folder are left out (and said). The
//!   job sees a snapshot under its own folder, read-only for it (`sys::job_read_only`), and
//!   nothing above it. The cost is the disk and the time of the copy, bounded below.

use std::fs::{File, OpenOptions};
use std::io::{self, Read};
use std::path::{Path, PathBuf};

use tokio::sync::watch;

use crate::{sys, util};

/// How many folders a person may allow at one machine.
pub const MOST_FOLDERS: usize = 32;
/// What one input may copy: files, bytes, folders deep.
const MOST_FILES: u64 = 20_000;
const MOST_BYTES: u64 = 8 << 30;
const MOST_DEPTH: usize = 32;
const MOST_PARTS: usize = 32;
const PATH_MAX: usize = 1000;
const LEFT_OUT_SHOWN: usize = 20;
/// What `copy` answers when the job was cancelled while it copied.
pub const CANCELLED: &str = "cancelled";

/// `^[a-z][a-z0-9_-]{0,39}$`: a folder's name, the only thing the Gateway learns of it.
pub fn is_folder_name(s: &str) -> bool {
    let mut chars = s.chars();
    matches!(chars.next(), Some(c) if c.is_ascii_lowercase())
        && s.len() <= 40
        && chars.all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_' || c == '-')
}

/// The parts of a path under an allowed folder, checked as the kernel checks them
/// (kernel/manifest.py `folder_ref`): '/' between parts (a trailing one names the folder);
/// no `..`, `.` or empty part; no `\ : * ? " < > |` or control character; no space at either
/// end, no dot at the end; on Windows no device name (CON, NUL, COM1 …).
pub fn parts(rel: &str) -> Result<Vec<&str>, String> {
    if rel.len() > PATH_MAX {
        return Err(format!("a path of at most {PATH_MAX} characters"));
    }
    let mut parts: Vec<&str> = if rel.is_empty() { Vec::new() } else { rel.split('/').collect() };
    if parts.last() == Some(&"") {
        parts.pop();
    }
    if parts.len() > MOST_PARTS {
        return Err(format!("a path of at most {MOST_PARTS} parts"));
    }
    for p in &parts {
        if p.is_empty() || *p == "." || *p == ".." {
            return Err("a path under the folder: no '..', '.' or empty part".into());
        }
        let bad = p.chars().any(|c| c.is_control() || matches!(c, '\\' | '/' | ':' | '*' | '?' | '"' | '<' | '>' | '|'));
        if bad || p.trim() != *p || p.ends_with('.') {
            return Err(format!(
                "the part {:?}: no \\ / : * ? \" < > | or control character, no space at either end, no dot at the end",
                util::cut(p, 40)
            ));
        }
        if cfg!(windows) && device_name(p) {
            return Err(format!("the part {:?} is a device's name on Windows", util::cut(p, 40)));
        }
    }
    Ok(parts)
}

fn device_name(part: &str) -> bool {
    let stem = part.split('.').next().unwrap_or("").to_ascii_uppercase();
    matches!(stem.as_str(), "CON" | "PRN" | "AUX" | "NUL" | "CONIN$" | "CONOUT$")
        || ((stem.starts_with("COM") || stem.starts_with("LPT"))
            && stem.len() == 4
            && stem.as_bytes()[3].is_ascii_digit())
}

/// A path as compared with others: the real one where it exists, without Windows' `\\?\`.
pub fn plain(p: &Path) -> PathBuf {
    let p = std::fs::canonicalize(p).unwrap_or_else(|_| p.to_path_buf());
    #[cfg(windows)]
    {
        let s = p.to_string_lossy().into_owned();
        if let Some(rest) = s.strip_prefix(r"\\?\") {
            if !rest.starts_with("UNC\\") {
                return PathBuf::from(rest);
            }
        }
    }
    p
}

/// What an input names, found without following a link.
pub struct Source {
    pub real: PathBuf,
    pub dir: bool,
    pub ext: Option<String>,
}

impl Source {
    /// Where the job finds it, as `job.json` says: `in/<input>/` for a folder,
    /// `in/<input>.<ext>` for a file.
    pub fn file_for(&self, input: &str) -> String {
        match &self.ext {
            Some(e) if !self.dir => format!("in/{input}.{e}"),
            _ => format!("in/{input}"),
        }
    }
}

/// `<root>/<rel>`, where `root` is the folder as `allow` found it (its real path). The
/// folder must still be there, no part of the path may be a link, and what it names must be
/// a plain file or a folder that really lies there.
/// TODO(review): isolation — `..` is refused (`parts`); a link at any step is never followed.
pub fn resolve(root: &Path, rel: &str) -> Result<Source, String> {
    let parts = parts(rel)?;
    let real_root = std::fs::canonicalize(root)
        .map_err(|e| format!("the folder is not there any more ({e}); the person at the machine allows it again"))?;
    if !sys::same_path(&real_root, root) {
        return Err("the folder is no longer where it was allowed (moved, or a link stands there now); the person at the machine allows it again".into());
    }
    let mut path = real_root;
    for (i, part) in parts.iter().enumerate() {
        path.push(part);
        let shown = parts[..=i].join("/");
        let meta = std::fs::symlink_metadata(&path).map_err(|_| format!("{shown}: no such file or folder there"))?;
        if meta.file_type().is_symlink() {
            return Err(format!("{shown}: a link, which is never followed"));
        }
        if i + 1 < parts.len() && !meta.is_dir() {
            return Err(format!("{shown}: not a folder"));
        }
    }
    let meta = std::fs::symlink_metadata(&path).map_err(|e| format!("{rel}: {e}"))?;
    if meta.is_dir() {
        let real = std::fs::canonicalize(&path).map_err(|e| format!("{rel}: {e}"))?;
        if !sys::same_path(&real, &path) {
            return Err(format!("{rel}: a link on the way, which is never followed"));
        }
        return Ok(Source { real: path, dir: true, ext: None });
    }
    if !meta.is_file() {
        return Err(format!("{rel}: neither a file nor a folder"));
    }
    open_checked(&path).map_err(|e| format!("{rel}: {e}"))?;
    let ext = parts
        .last()
        .and_then(|p| Path::new(p).extension())
        .map(|e| e.to_string_lossy().to_lowercase())
        .filter(|e| util::is_ext(e));
    Ok(Source { real: path, dir: false, ext })
}

/// Opens a file to read only if it is a plain file that really lies at `path` (no link at
/// any step: its real path is the one walked to).
fn open_checked(path: &Path) -> Result<File, String> {
    let f = sys::open_source(path).map_err(|e| format!("cannot open it: {e}"))?;
    if !f.metadata().is_ok_and(|m| m.is_file()) {
        return Err("not a plain file".into());
    }
    let real = sys::final_path(&f).map_err(|e| format!("cannot tell where it lies: {e}"))?;
    if !sys::same_path(&real, path) {
        return Err("a link on the way, which is never followed".into());
    }
    Ok(f)
}

/// What a copy took, and what it left out (links, devices), said back to the kernel.
#[derive(Default)]
pub struct Copied {
    pub files: u64,
    pub bytes: u64,
    pub left_out: Vec<String>,
    more_left_out: u64,
}

impl Copied {
    fn leave(&mut self, what: String) {
        if self.left_out.len() < LEFT_OUT_SHOWN {
            self.left_out.push(what);
        } else {
            self.more_left_out += 1;
        }
    }

    /// One line for the job's `said`.
    pub fn note(&self, input: &str) -> Option<String> {
        if self.left_out.is_empty() {
            return None;
        }
        let more = if self.more_left_out > 0 { format!(" and {} more", self.more_left_out) } else { String::new() };
        Some(format!("folder input {input}: left out {}{more}", self.left_out.join(", ")))
    }
}

/// Copies what `src` names to `to` (inside the job's folder), read-only for the job account.
pub fn copy(src: &Source, to: &Path, cancel: &watch::Receiver<bool>) -> Result<Copied, String> {
    let mut c = Copied::default();
    if src.dir {
        copy_dir(&src.real, to, "", 0, &mut c, cancel)?;
    } else {
        copy_file(&src.real, to, &mut c)?;
    }
    sys::job_read_only(to, src.dir, true);
    Ok(c)
}

fn copy_file(from: &Path, to: &Path, c: &mut Copied) -> Result<(), String> {
    c.files += 1;
    if c.files > MOST_FILES {
        return Err(format!("more than {MOST_FILES} files: name a smaller folder"));
    }
    let f = open_checked(from)?;
    let left = MOST_BYTES.saturating_sub(c.bytes);
    // What is said back names no path of this machine: the caller puts the one under the folder.
    let mut out = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(to)
        .map_err(|e| format!("cannot write its copy in the job's folder: {e}"))?;
    let n = io::copy(&mut (&f).take(left + 1), &mut out).map_err(|e| format!("cannot copy it: {e}"))?;
    c.bytes += n;
    if n > left {
        return Err(format!("more than {} GB: name a smaller folder", MOST_BYTES >> 30));
    }
    sys::job_read_only(to, false, false);
    Ok(())
}

fn copy_dir(from: &Path, to: &Path, shown: &str, depth: usize, c: &mut Copied, cancel: &watch::Receiver<bool>) -> Result<(), String> {
    if depth > MOST_DEPTH {
        return Err(format!("folders more than {MOST_DEPTH} deep: name a smaller folder"));
    }
    let at = if shown.is_empty() { "the folder" } else { shown };
    std::fs::create_dir(to).map_err(|e| format!("{at}: cannot make its copy in the job's folder: {e}"))?;
    // Nothing swapped for a link since the walk came here.
    let real = std::fs::canonicalize(from).map_err(|e| format!("{at}: {e}"))?;
    if !sys::same_path(&real, from) {
        return Err(format!("{at}: a link on the way, which is never followed"));
    }
    let mut entries: Vec<std::fs::DirEntry> = std::fs::read_dir(from)
        .map_err(|e| format!("{at}: cannot list it: {e}"))?
        .filter_map(Result::ok)
        .collect();
    entries.sort_by_key(|e| e.file_name());
    for e in entries {
        if *cancel.borrow() {
            return Err(CANCELLED.into());
        }
        let name = e.file_name();
        let here = format!("{shown}{}", name.to_string_lossy());
        let Ok(kind) = e.file_type() else {
            c.leave(format!("{here} (unreadable)"));
            continue;
        };
        let (src, dst) = (from.join(&name), to.join(&name));
        if kind.is_symlink() {
            c.leave(format!("{here} (a link)"));
        } else if kind.is_dir() {
            copy_dir(&src, &dst, &format!("{here}/"), depth + 1, c, cancel)?;
        } else if kind.is_file() {
            copy_file(&src, &dst, c).map_err(|e| format!("{here}: {e}"))?;
        } else {
            c.leave(format!("{here} (neither a file nor a folder)"));
        }
    }
    sys::job_read_only(to, true, false);
    Ok(())
}

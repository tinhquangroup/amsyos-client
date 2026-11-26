//! Node 2.1.5: the builds `amsyos deploy` makes on this machine, what each deployment holds of
//! the disk, and freeing what no deployment uses (sdk/NODE.md §4 "Builds").
//!
//! `amsyos deploy` (the CLI's nodekit/build.py, run by `node.exec` as this account) builds
//! under `AMSYOS_BUILD_HOME` (by default `~/amsyos-build`): `images/<image>@<version>/`, whose
//! `_node/where.env` names its environment `envs/<key>`, and each deployment's command (and
//! serve) is `sh _node/run.sh <image folder> '<command>'`. Node 2.1.9, Windows: nodekit/build.ps1
//! builds with uv under `AMSYOS_BUILD_HOME`, by default `%ProgramData%\AMSYOS\Node\builds\<org>`
//! (`home`), with the same `images\` and `envs\` (a virtual environment holds `.ready`), and a
//! deployment's line is `call _node\run.cmd "<image folder>" && <command>`, split as cmd does.
//! After an undeploy, after a deploy
//! and at the node's start the node removes the image folders that no deployment's command or
//! serve names (nor a job running now), then the environments that no remaining image folder
//! names. It never removes:
//! - one younger than an hour (by its last change: a build not deployed yet);
//! - anything while a `node.exec` runs (the caller holds the lock: a build in progress);
//! - `hf/`, `fetched/`, `mamba/` (the package cache) or `tools/`, nor on Windows `python\` and
//!   `cache\` (the Pythons and the cache of uv);
//! - a folder not as build.py makes it (an image holds `_node/where.env`, an environment
//!   `conda-meta/` or `.ready`), or a link (only real folders; std's `remove_dir_all` removes
//!   a link inside without following it);
//! - anything in a build home another account may write (TODO(review): isolation — the home,
//!   `images/` and `envs/` must be this account's, written by no other: never the job account).

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use serde_json::{json, Map, Value};

use crate::util;

/// A build younger than this is left alone: it may not be deployed yet.
pub const MIN_AGE: Duration = Duration::from_secs(3600);
/// At most this many entries are looked at in one walk (a size is then a floor).
const MOST_ENTRIES: usize = 300_000;
const RUN_SH: &str = "_node/run.sh";
/// Node 2.1.9: the wrapper of a deployment `amsyos deploy` built on a Windows node.
const RUN_CMD: &str = "_node/run.cmd";
const GB: f64 = (1u64 << 30) as f64;

/// TODO(review): isolation — the build home, as `amsyos deploy` makes it with this account's
/// environment: `AMSYOS_BUILD_HOME`, else `~/amsyos-build` on Linux and, node 2.1.9,
/// `%ProgramData%\AMSYOS\Node\builds\<org>` on Windows: the organisation of this node's
/// config.json, one home per organisation and never shared between two (build.ps1 makes it for
/// the organisation that called node.exec, which can only be this node's own). None when it is
/// not known (a Windows node not joined).
pub fn home() -> Option<PathBuf> {
    if let Some(h) = std::env::var_os("AMSYOS_BUILD_HOME").filter(|h| !h.is_empty()) {
        return Some(PathBuf::from(h));
    }
    #[cfg(windows)]
    {
        let org = crate::config::Config::load(&crate::config::node_dir()).org;
        plain_org(&org).then(|| crate::config::program_data().join("builds").join(org))
    }
    #[cfg(unix)]
    {
        std::env::var_os("HOME").filter(|h| !h.is_empty()).map(|h| PathBuf::from(h).join("amsyos-build"))
    }
}

/// An organisation's id as a folder's name: letters, digits, `_` and `-` only.
#[cfg_attr(unix, allow(dead_code))]
fn plain_org(org: &str) -> bool {
    !org.is_empty() && org.len() <= 64 && org.chars().all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-')
}

pub fn gb(bytes: u64) -> f64 {
    (bytes as f64 / GB * 100.0).round() / 100.0
}

/// The words of a shell command line as `sh` splits them (quotes and backslashes; nothing is
/// expanded): enough for the lines `amsyos deploy` writes (Python's `shlex.quote`).
pub fn words(line: &str) -> Vec<String> {
    let (mut out, mut cur, mut has) = (Vec::new(), String::new(), false);
    let mut chars = line.chars().peekable();
    while let Some(c) = chars.next() {
        match c {
            '\'' => {
                has = true;
                for c in chars.by_ref() {
                    if c == '\'' {
                        break;
                    }
                    cur.push(c);
                }
            }
            '"' => {
                has = true;
                while let Some(c) = chars.next() {
                    match c {
                        '"' => break,
                        '\\' if matches!(chars.peek(), Some('"' | '\\' | '$' | '`')) => cur.extend(chars.next()),
                        c => cur.push(c),
                    }
                }
            }
            '\\' => {
                has = true;
                cur.extend(chars.next());
            }
            c if c.is_whitespace() => {
                if has {
                    out.push(std::mem::take(&mut cur));
                    has = false;
                }
            }
            c => {
                has = true;
                cur.push(c);
            }
        }
    }
    if has {
        out.push(cur);
    }
    out
}

/// Node 2.1.9: the words of a command line as cmd splits them (Windows): double quotes group
/// and are dropped, `^` outside them takes the next character as it is; nothing is expanded.
pub fn cmd_words(line: &str) -> Vec<String> {
    let (mut out, mut cur, mut has, mut quoted) = (Vec::new(), String::new(), false, false);
    let mut chars = line.chars();
    while let Some(c) = chars.next() {
        match c {
            '"' => {
                has = true;
                quoted = !quoted;
            }
            '^' if !quoted => {
                has = true;
                cur.extend(chars.next());
            }
            c if c.is_whitespace() && !quoted => {
                if has {
                    out.push(std::mem::take(&mut cur));
                    has = false;
                }
            }
            c => {
                has = true;
                cur.push(c);
            }
        }
    }
    if has {
        out.push(cur);
    }
    out
}

/// A word that names the run wrapper of a build: `…_node/run.sh`, or `…_node\run.cmd`.
fn is_wrapper(word: &str) -> bool {
    let word = word.replace('\\', "/");
    [RUN_SH, RUN_CMD].iter().any(|w| word == *w || word.ends_with(&format!("/{w}")))
}

/// The image folders command lines name: the word after `…_node/run.sh` (Windows:
/// `…_node\run.cmd`), and any word (or part of one between `; & | ( ) < >`) that is a path
/// inside `<home>/images/`. A line is split as the shell that runs it does: `sh`, cmd on
/// Windows.
pub fn named_images(lines: &[String], home: Option<&Path>) -> BTreeSet<PathBuf> {
    named_images_as(lines, home, cfg!(windows))
}

fn named_images_as(lines: &[String], home: Option<&Path>, cmd: bool) -> BTreeSet<PathBuf> {
    let images = home.map(|h| h.join("images"));
    let mut found = BTreeSet::new();
    for line in lines {
        let w = if cmd { cmd_words(line) } else { words(line) };
        for (i, word) in w.iter().enumerate() {
            if is_wrapper(word) {
                if let Some(next) = w.get(i + 1).filter(|n| n.starts_with('/') || Path::new(n.as_str()).is_absolute()) {
                    found.insert(PathBuf::from(next.trim_end_matches(['/', '\\'])));
                }
            }
            let Some(images) = &images else { continue };
            for part in word.split([';', '&', '|', '(', ')', '<', '>']) {
                if let Ok(rest) = Path::new(part).strip_prefix(images) {
                    if let Some(first) = rest.components().next() {
                        found.insert(images.join(first.as_os_str()));
                    }
                }
            }
        }
    }
    found
}

/// The image a deployment runs from, as its command or serve names it.
pub fn image_of(lines: &[String]) -> Option<PathBuf> {
    named_images(lines, None).into_iter().next()
}

fn is_dir(p: &Path) -> bool {
    std::fs::symlink_metadata(p).is_ok_and(|m| m.file_type().is_dir())
}

fn is_file(p: &Path) -> bool {
    std::fs::symlink_metadata(p).is_ok_and(|m| m.file_type().is_file())
}

/// An image folder as build.py makes it.
pub fn is_image(p: &Path) -> bool {
    is_dir(p) && is_dir(&p.join("_node")) && is_file(&p.join("_node").join("where.env"))
}

fn is_env(p: &Path) -> bool {
    is_dir(p) && (is_dir(&p.join("conda-meta")) || is_file(&p.join(".ready")))
}

/// The name of the environment an image folder's `_node/where.env` names (`ENV_DIR=`).
fn env_of(image: &Path) -> Option<String> {
    let path = image.join("_node").join("where.env");
    if !is_file(&path) || std::fs::metadata(&path).ok()?.len() > 64 << 10 {
        return None;
    }
    let text = std::fs::read_to_string(path).ok()?;
    let value = text.lines().find_map(|l| l.strip_prefix("ENV_DIR="))?;
    // Windows (build.ps1): the path as it is, as cmd's `for /f` in run.cmd reads it.
    let dir = if cfg!(windows) { PathBuf::from(value.trim().trim_matches('"')) } else { PathBuf::from(words(value).into_iter().next()?) };
    dir.file_name().map(|n| n.to_string_lossy().into_owned())
}

/// The children of `dir` that are real folders and pass `keep`, by name.
fn folders(dir: &Path, keep: fn(&Path) -> bool) -> Vec<(String, PathBuf)> {
    let Ok(rd) = std::fs::read_dir(dir) else { return Vec::new() };
    let mut found: Vec<(String, PathBuf)> = rd
        .flatten()
        .take(10_000)
        .map(|e| (e.file_name().to_string_lossy().into_owned(), e.path()))
        .filter(|(_, p)| keep(p))
        .collect();
    found.sort();
    found
}

/// How long ago a folder last changed: its newest of modified and changed (a rename, a chmod
/// at the end of a build).
fn age(p: &Path) -> Duration {
    let Ok(meta) = std::fs::symlink_metadata(p) else { return Duration::ZERO };
    #[allow(unused_mut)]
    let mut newest = meta.modified().unwrap_or(UNIX_EPOCH);
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        newest = newest.max(UNIX_EPOCH + Duration::from_secs(meta.ctime().max(0) as u64));
    }
    SystemTime::now().duration_since(newest).unwrap_or(Duration::ZERO)
}

/// What removing `path` frees: the bytes on disk of the files under it that have no other
/// link (an environment's files hard-linked from the package cache free nothing), never
/// following a link; at most MOST_ENTRIES entries looked at.
pub fn freed_by(path: &Path) -> u64 {
    let (mut total, mut seen, mut stack) = (0u64, 0usize, vec![path.to_path_buf()]);
    while let Some(p) = stack.pop() {
        let Ok(meta) = std::fs::symlink_metadata(&p) else { continue };
        seen += 1;
        if seen > MOST_ENTRIES {
            break;
        }
        if meta.file_type().is_dir() {
            if let Ok(rd) = std::fs::read_dir(&p) {
                stack.extend(rd.flatten().map(|e| e.path()));
            }
        } else if meta.file_type().is_file() {
            #[cfg(unix)]
            {
                use std::os::unix::fs::MetadataExt;
                if meta.nlink() <= 1 {
                    total += meta.blocks() * 512;
                }
            }
            #[cfg(not(unix))]
            {
                total += meta.len();
            }
        }
    }
    total
}

/// TODO(review): isolation — a folder this account owns and no other account may write: other
/// accounts never, its group only when that is this account's own primary group and not the
/// job account's. Never a link.
#[cfg(unix)]
fn closed_to_others(dir: &Path) -> Result<(), String> {
    use std::os::unix::fs::MetadataExt;
    let meta = std::fs::symlink_metadata(dir).map_err(|e| format!("{}: {e}", dir.display()))?;
    if !meta.file_type().is_dir() {
        return Err(format!("{} is not a folder", dir.display()));
    }
    if meta.uid() != unsafe { libc::geteuid() } {
        return Err(format!("{} is not this account's", dir.display()));
    }
    let own_group = meta.gid() == unsafe { libc::getegid() } && Some(meta.gid()) != crate::sys::group_id(crate::sys::JOB_USER);
    if meta.mode() & 0o002 != 0 || (meta.mode() & 0o020 != 0 && !own_group) {
        return Err(format!("another account may write in {}", dir.display()));
    }
    Ok(())
}

/// TODO(review): isolation — node 2.1.9, Windows: a real folder (no junction or link) whose
/// owner, and every ACL entry that lets an account write in it or in what inherits from it,
/// is SYSTEM, Administrators or this account: never the job account, nor anyone else.
/// build.ps1 gives a build home exactly SYSTEM, Administrators and the node's user full and
/// `amsyos-job` read and run. A NULL DACL (anyone may do anything) or an entry of a kind not
/// read here refuses it.
#[cfg(windows)]
fn closed_to_others(dir: &Path) -> Result<(), String> {
    use std::ffi::c_void;
    use std::os::windows::ffi::OsStrExt;
    use std::os::windows::fs::MetadataExt;
    use std::ptr::null_mut;
    use windows_sys::Win32::Foundation::{CloseHandle, HANDLE};
    use windows_sys::Win32::Security::{
        EqualSid, GetAce, GetFileSecurityW, GetSecurityDescriptorDacl, GetSecurityDescriptorOwner, GetTokenInformation,
        IsWellKnownSid, TokenUser, WinBuiltinAdministratorsSid, WinLocalSystemSid, ACCESS_ALLOWED_ACE, ACE_HEADER, ACL,
        DACL_SECURITY_INFORMATION, OWNER_SECURITY_INFORMATION, PSID, TOKEN_QUERY, TOKEN_USER,
    };
    use windows_sys::Win32::System::Threading::{GetCurrentProcess, OpenProcessToken};
    const REPARSE_POINT: u32 = 0x400;
    const ALLOWED: u8 = 0; // ACCESS_ALLOWED_ACE_TYPE
    const DENIED: u8 = 1; // ACCESS_DENIED_ACE_TYPE
    // Write data or add a file, append or add a folder, write extended attributes, delete a
    // child, write attributes, delete, change the ACL or the owner, generic all, generic write.
    const WRITES: u32 = 0x2 | 0x4 | 0x10 | 0x40 | 0x100 | 0x1_0000 | 0x4_0000 | 0x8_0000 | 0x1000_0000 | 0x4000_0000;
    let shown = dir.display();
    let meta = std::fs::symlink_metadata(dir).map_err(|e| format!("{shown}: {e}"))?;
    if !meta.file_type().is_dir() || meta.file_attributes() & REPARSE_POINT != 0 {
        return Err(format!("{shown} is not a folder"));
    }
    // This account's SID (its token's user); `me` holds it while it is compared.
    let mut me = vec![0u64; 64];
    let mut token: HANDLE = null_mut();
    if unsafe { OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut token) } == 0 {
        return Err("cannot read this account's token".into());
    }
    let mut len = 0u32;
    let got = unsafe { GetTokenInformation(token, TokenUser, me.as_mut_ptr() as *mut c_void, 512, &mut len) };
    unsafe { CloseHandle(token) };
    if got == 0 {
        return Err("cannot read this account's SID".into());
    }
    let my_sid = unsafe { (*(me.as_ptr() as *const TOKEN_USER)).User.Sid };
    let trusted = |sid: PSID| unsafe {
        IsWellKnownSid(sid, WinLocalSystemSid) != 0 || IsWellKnownSid(sid, WinBuiltinAdministratorsSid) != 0 || EqualSid(sid, my_sid) != 0
    };
    let path: Vec<u16> = dir.as_os_str().encode_wide().chain(Some(0)).collect();
    let asked = DACL_SECURITY_INFORMATION | OWNER_SECURITY_INFORMATION;
    let mut need = 0u32;
    unsafe { GetFileSecurityW(path.as_ptr(), asked, null_mut(), 0, &mut need) };
    if need == 0 || need > 1 << 20 {
        return Err(format!("cannot read the ACL of {shown}"));
    }
    let mut sd = vec![0u64; (need as usize).div_ceil(8)];
    if unsafe { GetFileSecurityW(path.as_ptr(), asked, sd.as_mut_ptr() as *mut c_void, need, &mut need) } == 0 {
        return Err(format!("cannot read the ACL of {shown}"));
    }
    let (mut owner, mut defaulted): (PSID, i32) = (null_mut(), 0);
    let owned = unsafe { GetSecurityDescriptorOwner(sd.as_mut_ptr() as *mut c_void, &mut owner, &mut defaulted) };
    if owned == 0 || owner.is_null() || !trusted(owner) {
        return Err(format!("{shown} is owned by another account"));
    }
    let (mut present, mut dacl): (i32, *mut ACL) = (0, null_mut());
    if unsafe { GetSecurityDescriptorDacl(sd.as_mut_ptr() as *mut c_void, &mut present, &mut dacl, &mut defaulted) } == 0 {
        return Err(format!("cannot read the ACL of {shown}"));
    }
    if present == 0 || dacl.is_null() {
        return Err(format!("any account may write in {shown} (it has no ACL)"));
    }
    for i in 0..u32::from(unsafe { (*dacl).AceCount }) {
        let mut ace: *mut c_void = null_mut();
        if unsafe { GetAce(dacl, i, &mut ace) } == 0 || ace.is_null() {
            return Err(format!("cannot read the ACL of {shown}"));
        }
        match unsafe { (*(ace as *const ACE_HEADER)).AceType } {
            DENIED => continue,
            ALLOWED => {
                let allowed = ace as *const ACCESS_ALLOWED_ACE;
                let mask = unsafe { (*allowed).Mask };
                let sid = unsafe { std::ptr::addr_of!((*allowed).SidStart) } as PSID;
                if mask & WRITES != 0 && !trusted(sid) {
                    return Err(format!("another account may write in {shown}"));
                }
            }
            _ => return Err(format!("{shown} has an ACL entry this node does not read")),
        }
    }
    Ok(())
}

/// The image folders of the home and the environments: those named (kept), the rest.
struct Seen {
    images: Vec<(String, PathBuf, bool)>, // name, path, named
    envs: Vec<(String, PathBuf, bool)>,
}

/// What the home holds, and what of it the lines name: an image a line names, an environment
/// that such an image (or one named outside the home) names.
fn look(home: &Path, lines: &[String]) -> Seen {
    let named = named_images(lines, Some(home));
    let in_home = home.join("images");
    let named_names: BTreeSet<String> = named
        .iter()
        .filter(|p| p.parent().and_then(Path::file_name).is_some_and(|n| n == "images"))
        .filter_map(|p| p.file_name().map(|n| n.to_string_lossy().into_owned()))
        .collect();
    let images: Vec<(String, PathBuf, bool)> = folders(&in_home, is_image)
        .into_iter()
        .map(|(n, p)| {
            let kept = named_names.contains(&n);
            (n, p, kept)
        })
        .collect();
    let mut env_names: BTreeSet<String> = images.iter().filter(|(_, _, kept)| *kept).filter_map(|(_, p, _)| env_of(p)).collect();
    env_names.extend(named.iter().filter(|p| p.parent() != Some(in_home.as_path())).filter_map(|p| env_of(p)));
    let envs = folders(&home.join("envs"), is_env)
        .into_iter()
        .map(|(n, p)| {
            let kept = env_names.contains(&n);
            (n, p, kept)
        })
        .collect();
    Seen { images, envs }
}

#[derive(Default, Debug)]
pub struct Cleaned {
    pub images: Vec<String>,
    pub envs: Vec<String>,
    pub freed: u64,
    pub failed: Vec<String>,
}

impl Cleaned {
    pub fn is_empty(&self) -> bool {
        self.images.is_empty() && self.envs.is_empty() && self.failed.is_empty()
    }
}

/// Removes the image folders of `home` that no line names and the environments no remaining
/// image names, each older than `min_age`. The caller holds the lock no `node.exec` runs
/// beside. Blocking.
pub fn clean(home: &Path, lines: &[String], min_age: Duration) -> Result<Cleaned, String> {
    let mut done = Cleaned::default();
    if !is_dir(home) {
        return Ok(done);
    }
    closed_to_others(home)?;
    for sub in ["images", "envs"] {
        if std::fs::symlink_metadata(home.join(sub)).is_ok() {
            closed_to_others(&home.join(sub))?;
        }
    }
    let remove = |name: &str, path: &Path, done: &mut Cleaned| -> bool {
        let n = freed_by(path);
        match std::fs::remove_dir_all(path) {
            Ok(()) => {
                done.freed += n;
                true
            }
            Err(e) => {
                done.failed.push(format!("{name}: {e}"));
                false
            }
        }
    };
    let seen = look(home, lines);
    let mut left = Vec::new();
    for (name, path, named) in seen.images {
        if !named && age(&path) >= min_age && remove(&name, &path, &mut done) {
            done.images.push(name);
        } else {
            left.push(path);
        }
    }
    // The environments named by an image left in the home, or named outside it.
    let mut env_names: BTreeSet<String> = left.iter().filter_map(|p| env_of(p)).collect();
    let in_home = home.join("images");
    for p in named_images(lines, Some(home)).iter().filter(|p| p.parent() != Some(in_home.as_path())) {
        env_names.extend(env_of(p));
    }
    for (name, path, _) in seen.envs {
        if !env_names.contains(&name) && age(&path) >= min_age && remove(&name, &path, &mut done) {
            done.envs.push(name);
        }
    }
    Ok(done)
}

/// One deployment: a service a deploy put here (its folder, its command and serve, when).
pub struct Deployment {
    pub id: String,
    pub dir: PathBuf,
    pub lines: Vec<String>,
    pub since: Option<String>,
}

fn mtime_utc(p: &Path) -> String {
    std::fs::symlink_metadata(p).and_then(|m| m.modified()).map(util::utc_of).unwrap_or_default()
}

/// What the node says of its disk (sdk/NODE.md §4, the `disk` message): for each deployment
/// what removing it frees (its folder, and the image it runs from: an image several share is
/// counted for each, as `image` says), and `builds_gb`, the image folders and environments no
/// deployment uses (what the next cleaning removes once they are an hour old). `in_use`: the
/// lines of the jobs running now. Blocking: it walks the folders.
pub fn measure(home: Option<&Path>, deployments: &[Deployment], in_use: &[String]) -> Value {
    let mut sizes: HashMap<PathBuf, u64> = HashMap::new();
    let mut size = |p: &Path| *sizes.entry(p.to_path_buf()).or_insert_with(|| freed_by(p));
    let images_dir = home.map(|h| h.join("images"));
    let mut deployed = BTreeMap::new();
    for d in deployments.iter().take(200) {
        let image = image_of(&d.lines).filter(|p| images_dir.as_deref().is_some_and(|i| p.parent() == Some(i)) && is_image(p));
        let mut bytes = if is_dir(&d.dir) { size(&d.dir) } else { 0 };
        bytes += image.as_deref().map(&mut size).unwrap_or(0);
        let shown = image.as_deref().and_then(Path::file_name).map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
        let since = d.since.clone().unwrap_or_else(|| mtime_utc(&d.dir));
        deployed.insert(d.id.clone(), json!({"gb": gb(bytes), "image": util::cut(&shown, 80), "since": since}));
    }
    let mut builds = 0u64;
    if let Some(home) = home {
        let lines: Vec<String> = deployments.iter().flat_map(|d| d.lines.iter().cloned()).chain(in_use.iter().cloned()).collect();
        let seen = look(home, &lines);
        for (_, p, named) in seen.images.iter().chain(seen.envs.iter()) {
            if !named {
                builds += size(p);
            }
        }
    }
    let mut out = Map::new();
    out.insert("deployed".into(), Value::Object(deployed.into_iter().collect()));
    out.insert("builds_gb".into(), gb(builds).into());
    Value::Object(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scratch(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("amsyos-builds-{name}-{}", util::hex(&util::random_bytes::<4>())));
        std::fs::create_dir_all(&dir).unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o755)).unwrap();
        }
        dir
    }

    fn image(home: &Path, name: &str, env: &str) -> PathBuf {
        let dir = home.join("images").join(name);
        std::fs::create_dir_all(dir.join("_node")).unwrap();
        let env_dir = home.join("envs").join(env);
        std::fs::write(dir.join("_node").join("where.env"), format!("ENV_DIR={}\nHF_DIR=x\n", env_dir.display())).unwrap();
        std::fs::write(dir.join("main.py"), vec![b'x'; 10_000]).unwrap();
        dir
    }

    fn env(home: &Path, key: &str) -> PathBuf {
        let dir = home.join("envs").join(key);
        std::fs::create_dir_all(dir.join("conda-meta")).unwrap();
        std::fs::write(dir.join(".ready"), "{}").unwrap();
        std::fs::write(dir.join("lib.so"), vec![b'y'; 50_000]).unwrap();
        dir
    }

    #[test]
    fn words_as_sh_splits_them() {
        assert_eq!(words("sh _node/run.sh /a/images/x@1 'python main.py'"), ["sh", "_node/run.sh", "/a/images/x@1", "python main.py"]);
        assert_eq!(words(r#"sh _node/run.sh '/a b/images/x@1' "it's" 'a'"'"'b'"#), ["sh", "_node/run.sh", "/a b/images/x@1", "it's", "a'b"]);
    }

    #[test]
    fn words_as_cmd_splits_them_and_the_image_is_found() {
        let line = r#"call _node\run.cmd "C:\ProgramData\AMSYOS\Node\builds\t_x\images\thumb@1.0.0" && python main.py --n "a b""#;
        assert_eq!(
            cmd_words(line),
            ["call", r"_node\run.cmd", r"C:\ProgramData\AMSYOS\Node\builds\t_x\images\thumb@1.0.0", "&&", "python", "main.py", "--n", "a b"]
        );
        assert_eq!(cmd_words(r#"a^&b "x y"z"#), ["a&b", "x yz"]);
        // The word after the wrapper when it is an absolute path (one Linux reads as such here).
        let found = named_images_as(&[r#"call _node\run.cmd "/b/images/thumb@1.0.0" && python main.py"#.to_string()], None, true);
        assert_eq!(found.into_iter().collect::<Vec<_>>(), [PathBuf::from("/b/images/thumb@1.0.0")]);
        assert!(named_images_as(&[r"call _node\run.cmd relative\x && y".to_string()], None, true).is_empty());
        assert!(is_wrapper("/srv/x/_node/run.sh") && is_wrapper(r"D:\s\_node\run.cmd") && !is_wrapper("run.cmd"));
    }

    #[test]
    fn removes_what_no_deployment_uses_and_keeps_the_young() {
        let home = scratch("clean");
        let used = image(&home, "thumb@1.0.0", "e1");
        let old = image(&home, "thumb@0.9.0", "e2");
        env(&home, "e1");
        env(&home, "e2");
        env(&home, "e3");
        std::fs::create_dir_all(home.join("hf").join("models")).unwrap();
        let line = format!("sh _node/run.sh {} 'python main.py'", used.display());
        // Everything is young: nothing goes.
        let none = clean(&home, &[line.clone()], MIN_AGE).unwrap();
        assert!(none.is_empty(), "{none:?}");
        // As if an hour old: the image no deployment names and the environments only it (or
        // none) named go; the used one, its environment and hf/ stay.
        let done = clean(&home, &[line.clone()], Duration::ZERO).unwrap();
        assert_eq!(done.images, ["thumb@0.9.0"]);
        assert_eq!(done.envs, ["e2", "e3"]);
        assert!(done.freed > 0);
        assert!(used.is_dir() && !old.exists());
        assert!(home.join("envs/e1").is_dir() && !home.join("envs/e2").exists() && !home.join("envs/e3").exists());
        assert!(home.join("hf/models").is_dir());
        // A young image (a build not deployed yet) keeps its environment too.
        let fresh = image(&home, "new@1", "e4");
        env(&home, "e4");
        let measured = measure(Some(&home), &[Deployment { id: "t_x.a.b".into(), dir: home.join("hf"), lines: vec![line.clone()], since: None }], &[]);
        assert_eq!(measured["deployed"]["t_x.a.b"]["image"], "thumb@1.0.0");
        assert!(measured["builds_gb"].as_f64().is_some());
        let left = clean(&home, &[line], MIN_AGE).unwrap();
        assert!(left.is_empty(), "{left:?}");
        assert!(fresh.is_dir() && home.join("envs/e4").is_dir());
        let _ = std::fs::remove_dir_all(&home);
    }

    #[cfg(unix)]
    #[test]
    fn leaves_a_home_others_may_write() {
        use std::os::unix::fs::PermissionsExt;
        let home = scratch("open");
        std::fs::create_dir_all(home.join("images")).unwrap();
        std::fs::set_permissions(home.join("images"), std::fs::Permissions::from_mode(0o777)).unwrap();
        assert!(clean(&home, &[], Duration::ZERO).is_err());
        let _ = std::fs::remove_dir_all(&home);
    }
}

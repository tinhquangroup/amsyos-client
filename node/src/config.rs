//! Where the node keeps things (sdk/NODE.md §4) and what it keeps: `config.json` (written by
//! `join`, and by `setup --jobs`), `services.json` (what the person switched on), `node.key`.
//! Node 2.1.9: a profile (`--profile <name>`) is a node of its own on the same computer, for
//! another organisation: its own node folder, key, jobs and managed services folders.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::util;

pub const KEY_FILE: &str = "node.key";
pub const CONFIG_FILE: &str = "config.json";
pub const SERVICES_FILE: &str = "services.json";
#[cfg(windows)]
pub const PASSWORD_FILE: &str = "job-account.dpapi";

/// Node 2.1.9: the environment variable that names the profile when `--profile` does not.
pub const PROFILE_ENV: &str = "AMSYOS_NODE_PROFILE";

/// Node 2.1.9: the profile this process works on (`--profile <name>`, or AMSYOS_NODE_PROFILE),
/// set once at the start; None: the default node, at exactly the paths of before.
static PROFILE: std::sync::OnceLock<Option<String>> = std::sync::OnceLock::new();

/// A profile's name: `[a-z0-9_-]`, at most 32, starting with a letter or a digit (it is in
/// folder names and a scheduled task's name).
pub fn is_profile_name(s: &str) -> bool {
    (1..=32).contains(&s.len())
        && s.bytes().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_' || b == b'-')
        && s.bytes().next().is_some_and(|b| b.is_ascii_alphanumeric())
}

/// Called once, before anything reads a folder.
pub fn set_profile(profile: Option<String>) {
    let _ = PROFILE.set(profile);
}

pub fn profile() -> Option<&'static str> {
    PROFILE.get().and_then(|p| p.as_deref())
}

/// `amsyos-node`, or `amsyos-node --profile <name>`: the command a hint tells to run.
pub fn command() -> String {
    match profile() {
        Some(p) => format!("amsyos-node --profile {p}"),
        None => "amsyos-node".to_string(),
    }
}

fn suffixed(base: &str, profile: Option<&str>) -> String {
    match profile {
        Some(p) => format!("{base}-{p}"),
        None => base.to_string(),
    }
}

/// The name of a node folder: `Node` (Windows) or `amsyos-node`, `-<profile>` after it.
fn node_dir_name(profile: Option<&str>) -> String {
    suffixed(if cfg!(windows) { "Node" } else { "amsyos-node" }, profile)
}

/// Where the node folders lie: `%LOCALAPPDATA%\AMSYOS` (Windows) or `~/.local/share`.
pub fn node_base() -> PathBuf {
    #[cfg(windows)]
    {
        std::env::var_os("LOCALAPPDATA")
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from(r"C:\Users\Default\AppData\Local"))
            .join("AMSYOS")
    }
    #[cfg(unix)]
    {
        let home = std::env::var_os("HOME").map(PathBuf::from).unwrap_or_else(|| PathBuf::from("/"));
        node_base_in(&home)
    }
}

#[cfg(unix)]
pub fn node_base_in(home: &Path) -> PathBuf {
    home.join(".local").join("share")
}

/// The node's own folder: `%LOCALAPPDATA%\AMSYOS\Node` or `~/.local/share/amsyos-node`, and a
/// profile's `…\Node-<name>`, `~/.local/share/amsyos-node-<name>`. Nothing of it is ever given
/// to a job.
pub fn node_dir() -> PathBuf {
    node_base().join(node_dir_name(profile()))
}

/// A profile's node folder (None: the default's), whatever profile this process works on.
pub fn node_dir_of(profile: Option<&str>) -> PathBuf {
    node_base().join(node_dir_name(profile))
}

#[cfg(unix)]
pub fn node_dir_in(home: &Path) -> PathBuf {
    node_base_in(home).join(node_dir_name(profile()))
}

/// The node folders under `base` (`node_base()`): the default's first (None), then each
/// profile's by name; only folders that are there (a link is not one).
pub fn node_dirs_in(base: &Path) -> Vec<(Option<String>, PathBuf)> {
    let default = node_dir_name(None);
    let prefix = format!("{default}-");
    let mut out: Vec<(Option<String>, PathBuf)> = Vec::new();
    let Ok(rd) = std::fs::read_dir(base) else { return out };
    for e in rd.flatten().take(10_000) {
        if !e.file_type().is_ok_and(|t| t.is_dir()) {
            continue;
        }
        let name = e.file_name().to_string_lossy().into_owned();
        if name == default {
            out.push((None, e.path()));
        } else if let Some(p) = name.strip_prefix(&prefix).filter(|p| is_profile_name(p)) {
            out.push((Some(p.to_string()), e.path()));
        }
    }
    out.sort_by(|a, b| a.0.cmp(&b.0));
    out
}

/// The node folders of this account (`profiles`, `setup`).
pub fn node_dirs() -> Vec<(Option<String>, PathBuf)> {
    node_dirs_in(&node_base())
}

/// A profile as a person reads it: its name, or `(default)`.
pub fn profile_label(profile: Option<&str>) -> String {
    profile.map(String::from).unwrap_or_else(|| "(default)".into())
}

/// Windows: the scheduled task that runs a profile's node: "AMSYOS Node", "AMSYOS Node <name>".
pub fn task_name(profile: Option<&str>) -> String {
    match profile {
        Some(p) => format!("AMSYOS Node {p}"),
        None => "AMSYOS Node".to_string(),
    }
}

/// Windows: the one line (cmd or PowerShell) that makes the scheduled task of a profile's node,
/// with the settings of "AMSYOS Node": at the logon of the node's user, as that user with the
/// highest rights it has (so `run --allow-elevated`), no time limit (a task's default is 72 h),
/// one instance; also on batteries, and started again a minute after it fails. No `$` and no
/// inner `"`, so that cmd and PowerShell both pass it as it is; `'` doubled for PowerShell.
#[cfg(any(windows, test))]
pub fn task_line(profile: Option<&str>, exe: &Path, user: &str) -> String {
    let q = |s: &str| s.replace('\'', "''");
    let args = match profile {
        Some(p) => format!("--profile {p} run --allow-elevated"),
        None => "run --allow-elevated".to_string(),
    };
    format!(
        "powershell -NoProfile -Command \"Register-ScheduledTask -Force -TaskName '{task}' \
         -Action (New-ScheduledTaskAction -Execute '{exe}' -Argument '{args}') \
         -Trigger (New-ScheduledTaskTrigger -AtLogOn -User '{user}') \
         -Principal (New-ScheduledTaskPrincipal -UserId '{user}' -LogonType Interactive -RunLevel Highest) \
         -Settings (New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries \
         -DontStopIfGoingOnBatteries -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1))\"",
        task = q(&task_name(profile)),
        exe = q(&exe.display().to_string()),
        user = q(user),
    )
}

/// TODO(review): isolation — what each other profile under `base` holds, for a check that a
/// folder of this profile (its jobs, its managed services, a service's folder, a folder
/// allowed) neither holds nor lies in it: its node folder (its key), its jobs folder and its
/// managed services folder, as its config.json names them.
pub fn others_places(base: &Path, me: Option<&str>) -> Vec<(PathBuf, String)> {
    let mut out = Vec::new();
    for (p, dir) in node_dirs_in(base) {
        if p.as_deref() == me {
            continue;
        }
        let cfg = Config::load(&dir);
        let who = format!("profile {}", profile_label(p.as_deref()));
        out.push((cfg.jobs_root_for(p.as_deref()), format!("the jobs folder of {who}")));
        out.push((cfg.services_root_for(p.as_deref()), format!("the managed services folder of {who}")));
        out.push((dir, format!("the node folder of {who}")));
    }
    out
}

/// TODO(review): isolation — `setup`: each profile's jobs and managed services folders are its
/// own. Why one of `wanted` may not be used: it is, holds or lies in a place of another profile.
pub fn clash(base: &Path, me: Option<&str>, wanted: &[(&Path, &str)]) -> Option<String> {
    let theirs = others_places(base, me);
    for (w, what) in wanted {
        if let Some((p, whose)) = theirs.iter().find(|(p, _)| crate::sys::within(p, w) || crate::sys::within(w, p)) {
            return Some(format!(
                "{what} {} is, holds or lies in {whose} ({}): give each profile folders of its own (--jobs, --services)",
                w.display(),
                p.display()
            ));
        }
    }
    None
}

/// `%ProgramData%\AMSYOS\Node` on Windows: the jobs (by default) and `bin`.
#[cfg(windows)]
pub fn program_data() -> PathBuf {
    std::env::var_os("ProgramData")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(r"C:\ProgramData"))
        .join("AMSYOS")
        .join("Node")
}

/// `%ProgramFiles%\AMSYOS\Node`: where `setup` installs the program, in a folder only
/// SYSTEM, Administrators and the node's user may write to.
#[cfg(windows)]
pub fn install_dir() -> PathBuf {
    std::env::var_os("ProgramFiles")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(r"C:\Program Files"))
        .join("AMSYOS")
        .join("Node")
}

#[cfg(windows)]
pub fn installed_exe() -> PathBuf {
    install_dir().join("amsyos-node.exe")
}

/// Where the jobs and the managed services folders lie by default: `%ProgramData%\AMSYOS\Node`
/// or `/var/lib/amsyos-node`.
fn roots_base() -> PathBuf {
    #[cfg(windows)]
    {
        program_data()
    }
    #[cfg(unix)]
    {
        PathBuf::from("/var/lib/amsyos-node")
    }
}

/// `…\jobs`, and a profile's `…\jobs-<name>`.
pub fn default_jobs_root() -> PathBuf {
    default_jobs_root_of(profile())
}

pub fn default_jobs_root_of(profile: Option<&str>) -> PathBuf {
    roots_base().join(suffixed("jobs", profile))
}

/// Where the copy of this program named `amsyos-job` lies, on every job's PATH.
pub fn bin_dir() -> PathBuf {
    #[cfg(windows)]
    {
        program_data().join("bin")
    }
    #[cfg(unix)]
    {
        PathBuf::from("/usr/local/lib/amsyos-node")
    }
}

/// Where `deploy` unpacks the services the organisation's managers put on the machine: the
/// job account may read and run them there, never write. A profile's: `…\services-<name>`.
pub fn default_services_root() -> PathBuf {
    default_services_root_of(profile())
}

pub fn default_services_root_of(profile: Option<&str>) -> PathBuf {
    roots_base().join(suffixed("services", profile))
}

/// A node's access level (sdk/NODE.md §5). Ordered: services < managed < full.
#[derive(Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Debug)]
pub enum Access {
    Services,
    Managed,
    Full,
}

impl Access {
    pub fn parse(s: &str) -> Option<Access> {
        match s {
            "services" => Some(Access::Services),
            "managed" => Some(Access::Managed),
            "full" => Some(Access::Full),
            _ => None,
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Access::Services => "services",
            Access::Managed => "managed",
            Access::Full => "full",
        }
    }
}

pub fn job_bin() -> PathBuf {
    bin_dir().join(if cfg!(windows) { "amsyos-job.exe" } else { "amsyos-job" })
}

/// Node 2.1.2: the folder on every job's PYTHONPATH that holds the Python job module
/// (`from amsyos import job, serve`, sdk/amsyos), inside `bin`: the job account may read it,
/// never write. `setup` writes it, and `run` when it can (pymod.rs).
pub fn python_dir() -> PathBuf {
    bin_dir().join("python")
}

#[derive(Serialize, Deserialize, Default, Clone, Debug)]
pub struct Config {
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub gateway: String,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub node: String,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub org: String,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub kind: String,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub ws: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub name: Option<String>,
    /// How many jobs at once; by default 1 on a workstation, half the CPUs on a server.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub slots: Option<u32>,
    /// The jobs folder chosen at `setup --jobs`; the default otherwise.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub jobs: Option<PathBuf>,
    /// The access level, raised only at the machine (`amsyos-node access`); `services` when
    /// absent or not understood.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub access: Option<String>,
    /// The managed services folder chosen at `setup --services`; the default otherwise.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub services_root: Option<PathBuf>,
    /// At `full` only: the services switched on here run as the node's own account unless
    /// switched on with `--as-job`, and an admin's `deploy` is `as_node` unless it says
    /// otherwise (sdk/NODE.md §5). Set only at the machine, by an administrator (`amsyos-node
    /// access full --as-node-default`, `access as-node-default on|off`); turned off whenever
    /// the level goes below `full`, at the machine or from afar.
    #[serde(default, skip_serializing_if = "is_false")]
    pub as_node_default: bool,
    /// Node 2.1 (spec v3 §8.3): the folders the person at the machine allowed jobs to read in
    /// place, by name (`amsyos-node allow <name> <path>`, never from afar). The Gateway is told
    /// the names only; the paths (real, as `allow` found them) stay here.
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub folders: BTreeMap<String, PathBuf>,
    /// Node 2.1.9: whether the node updates itself (update.rs, sdk/NODE.md §7). Set only at the
    /// machine, by an administrator (`amsyos-node auto-update on|off`); `join` turns it on when
    /// unset, so a node joined before 2.1.9 (absent) stays off.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub auto_update: Option<bool>,
}

impl Config {
    pub fn load(dir: &Path) -> Config {
        std::fs::read(dir.join(CONFIG_FILE))
            .ok()
            .and_then(|b| serde_json::from_slice(&b).ok())
            .unwrap_or_default()
    }

    pub fn save(&self, dir: &Path) -> Result<(), String> {
        let body = serde_json::to_vec_pretty(self).map_err(|e| e.to_string())?;
        util::write_file(&dir.join(CONFIG_FILE), &body, false)
            .map_err(|e| format!("cannot write {}: {e}", dir.join(CONFIG_FILE).display()))
    }

    pub fn joined(&self) -> bool {
        !self.node.is_empty() && !self.gateway.is_empty()
    }

    pub fn access(&self) -> Access {
        self.access.as_deref().and_then(Access::parse).unwrap_or(Access::Services)
    }

    /// The default `as_node` in force: switched on, and the level `full`.
    pub fn as_node_by_default(&self) -> bool {
        self.as_node_default && self.access() == Access::Full
    }

    /// The names of the allowed folders that are valid, as a hello or beat says them.
    pub fn folder_names(&self) -> Vec<String> {
        self.folders.keys().filter(|n| crate::folder::is_folder_name(n)).take(crate::folder::MOST_FOLDERS).cloned().collect()
    }

    pub fn services_root(&self) -> PathBuf {
        self.services_root_for(profile())
    }

    pub fn jobs_root(&self) -> PathBuf {
        self.jobs_root_for(profile())
    }

    /// The config.json of another profile's node: its folders, by its own defaults.
    pub fn services_root_for(&self, profile: Option<&str>) -> PathBuf {
        self.services_root.clone().unwrap_or_else(|| default_services_root_of(profile))
    }

    pub fn jobs_root_for(&self, profile: Option<&str>) -> PathBuf {
        self.jobs.clone().unwrap_or_else(|| default_jobs_root_of(profile))
    }

    pub fn is_server(&self) -> bool {
        self.kind == "server"
    }

    pub fn slots(&self, cpus: u32) -> u32 {
        match self.slots {
            Some(n) if n >= 1 => n.min(64),
            _ if self.is_server() => (cpus / 2).max(1),
            _ => 1,
        }
    }
}

fn yes() -> bool {
    true
}

fn is_false(b: &bool) -> bool {
    !*b
}

/// One service at this machine: switched on by its person (`enable`), or put there by a
/// manager (`deploy`, `managed`).
#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct Service {
    pub dir: PathBuf,
    /// Started once a job; empty for a service that has only `serve`.
    #[serde(default)]
    pub command: String,
    /// Node 2.1.2: a server kept warm (sdk/CONTRACT.md §3), started once and handed the jobs
    /// one after the other on its stdin (serve.rs). With `command` beside it, a job its
    /// server could not take runs as `command`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub serve: Option<String>,
    #[serde(default)]
    pub gpu: bool,
    /// Only a service that is on is offered (`switch` turns it off and on).
    #[serde(default = "yes")]
    pub on: bool,
    #[serde(default, skip_serializing_if = "is_false")]
    pub managed: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub by: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub sha256: Option<String>,
    /// Runs as the node's own account, for a direct job at `full` only (a deploy's `as_node`).
    #[serde(default, skip_serializing_if = "is_false")]
    pub as_node: bool,
    /// Switched on here with `--as-job`: runs as amsyos-job even where the node's default is
    /// `as_node`.
    #[serde(default, skip_serializing_if = "is_false")]
    pub as_job: bool,
    /// Node 2.1.5: when a deploy put it here (the `disk` message's `since`).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub since: Option<String>,
}

impl Service {
    pub fn new(dir: PathBuf, command: String, gpu: bool) -> Service {
        Service { dir, command, serve: None, gpu, on: true, managed: false, by: None, sha256: None, as_node: false, as_job: false, since: None }
    }

    /// Its command and server, as lines (what names a build's image, builds.rs).
    pub fn lines(&self) -> Vec<String> {
        std::iter::once(self.command.clone()).chain(self.serve.clone()).filter(|l| !l.trim().is_empty()).collect()
    }

    /// The server's command, when the service has one.
    pub fn served(&self) -> Option<&str> {
        self.serve.as_deref().map(str::trim).filter(|s| !s.is_empty())
    }

    /// How it runs, as a person reads it: its command, its server, or both.
    pub fn shown(&self) -> String {
        match (self.command.trim(), self.served()) {
            ("", Some(s)) => format!("serve: {s}"),
            (c, Some(s)) => format!("serve: {s}; command: {c}"),
            (c, None) => c.to_string(),
        }
    }

    /// TODO(review): isolation — whether this service's job may run as the node's own account:
    /// deployed `as_node` (the kernel lets only an admin ask it), or switched on here without
    /// `--as-job` while the node's default `as_node` is on (`node_default`: on, at `full`). A
    /// deployed service follows only what its deploy said. The caller still runs it so only
    /// for a direct job at `full` (net.rs, `take_job`).
    pub fn runs_as_node(&self, node_default: bool) -> bool {
        self.as_node || (node_default && !self.managed && !self.as_job)
    }

    /// Why it runs as the node's own account, for the node's log and `services`.
    pub fn as_node_why(&self) -> &'static str {
        if self.as_node { "as_node" } else { "as_node: the node's default" }
    }
}

static SERVICES_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

#[derive(Serialize, Deserialize, Default, Debug)]
pub struct Services {
    #[serde(default)]
    pub services: BTreeMap<String, Service>,
}

impl Services {
    /// Read again at every job and beat, so `enable` takes effect while `run` runs.
    pub fn load(dir: &Path) -> Services {
        let mut s: Services = std::fs::read(dir.join(SERVICES_FILE))
            .ok()
            .and_then(|b| serde_json::from_slice(&b).ok())
            .unwrap_or_default();
        s.services.retain(|id, _| util::is_service_id(id));
        s
    }

    pub fn save(&self, dir: &Path) -> Result<(), String> {
        let body = serde_json::to_vec_pretty(self).map_err(|e| e.to_string())?;
        util::write_file(&dir.join(SERVICES_FILE), &body, false)
            .map_err(|e| format!("cannot write {}: {e}", dir.join(SERVICES_FILE).display()))
    }

    /// The services that are on: what the node offers.
    pub fn ids(&self) -> Vec<String> {
        self.services.iter().filter(|(_, s)| s.on).map(|(id, _)| id.clone()).collect()
    }

    /// Read, change and write `services.json`, one change at a time in this process.
    pub fn update<T>(dir: &Path, change: impl FnOnce(&mut Services) -> Result<T, String>) -> Result<T, String> {
        let _held = SERVICES_LOCK.lock().map_err(|_| "services.json is held".to_string())?;
        let mut s = Services::load(dir);
        let out = change(&mut s)?;
        s.save(dir)?;
        Ok(out)
    }
}

/// Makes the node's folder, readable by the node's user only.
pub fn ensure_node_dir(dir: &Path) -> Result<(), String> {
    std::fs::create_dir_all(dir).map_err(|e| format!("cannot make {}: {e}", dir.display()))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(dir, std::fs::Permissions::from_mode(0o700))
            .map_err(|e| format!("cannot set the mode of {}: {e}", dir.display()))?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn profile_names_and_folders() {
        for ok in ["t_x", "a", "x-1", "0abc", &"a".repeat(32)] {
            assert!(is_profile_name(ok), "{ok}");
        }
        for bad in ["", "-x", "_x", "T_media", "a b", "a/b", "a.b", &"a".repeat(33)] {
            assert!(!is_profile_name(bad), "{bad}");
        }
        assert_eq!(default_jobs_root_of(None), roots_base().join("jobs"));
        assert_eq!(default_jobs_root_of(Some("t_x")), roots_base().join("jobs-t_x"));
        assert_eq!(default_services_root_of(Some("t_x")), roots_base().join("services-t_x"));
        assert_eq!(node_dir_of(None), node_base().join(if cfg!(windows) { "Node" } else { "amsyos-node" }));
    }

    #[test]
    fn profiles_found_and_kept_apart() {
        let base = std::env::temp_dir().join(format!("amsyos-profiles-{}", util::hex(&util::random_bytes::<4>())));
        let name = |p: Option<&str>| node_dir_name(p);
        for d in [name(None), name(Some("t_x")), name(Some("zz")), format!("{}-Bad", name(None)), "other".into()] {
            std::fs::create_dir_all(base.join(d)).unwrap();
        }
        std::fs::write(base.join(format!("{}-file", name(None))), b"x").unwrap();
        let found: Vec<Option<String>> = node_dirs_in(&base).into_iter().map(|(p, _)| p).collect();
        assert_eq!(found, vec![None, Some("t_x".into()), Some("zz".into())]);

        // t_x names folders of its own; zz and the default use their defaults.
        let acme = Config { jobs: Some(base.join("acme-jobs")), services_root: Some(base.join("acme-svc")), ..Default::default() };
        acme.save(&base.join(name(Some("t_x")))).unwrap();
        assert!(clash(&base, Some("new"), &[(&base.join("own-jobs"), "--jobs")]).is_none());
        let why = clash(&base, Some("new"), &[(&base.join("acme-jobs"), "--jobs")]).unwrap_or_default();
        assert!(why.contains("profile t_x"), "{why}");
        assert!(clash(&base, Some("new"), &[(&base.join("acme-svc").join("x"), "--services")]).is_some());
        assert!(clash(&base, Some("new"), &[(&default_jobs_root_of(Some("zz")), "--jobs")]).is_some());
        assert!(clash(&base, Some("new"), &[(&default_jobs_root_of(None), "--jobs")]).is_some());
        assert!(clash(&base, Some("t_x"), &[(&base.join("acme-jobs"), "--jobs")]).is_none(), "its own");
        assert!(clash(&base, None, &[(&base, "a folder")]).is_some(), "holds the node folders");
        std::fs::remove_dir_all(&base).unwrap();
    }

    #[test]
    fn the_task_line() {
        let exe = Path::new(r"C:\Program Files\AMSYOS\Node\amsyos-node.exe");
        let line = task_line(Some("t_x"), exe, r"HOST\O'Neil");
        assert!(line.contains("-TaskName 'AMSYOS Node t_x'"), "{line}");
        assert!(
            line.contains(r"-Execute 'C:\Program Files\AMSYOS\Node\amsyos-node.exe' -Argument '--profile t_x run --allow-elevated'"),
            "{line}"
        );
        assert!(line.contains(r"-UserId 'HOST\O''Neil'"), "{line}");
        assert!(!line.contains('$'));
        assert_eq!(line.matches('"').count(), 2, "{line}");
        assert!(task_line(None, exe, "u").contains("-TaskName 'AMSYOS Node' "));
    }
}

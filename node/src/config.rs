//! Where the node keeps things (sdk/NODE.md §4) and what it keeps: `config.json` (written by
//! `join`, and by `setup --jobs`), `services.json` (what the person switched on), `node.key`.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::util;

pub const KEY_FILE: &str = "node.key";
pub const CONFIG_FILE: &str = "config.json";
pub const SERVICES_FILE: &str = "services.json";
#[cfg(windows)]
pub const PASSWORD_FILE: &str = "job-account.dpapi";

/// The node's own folder: `%LOCALAPPDATA%\AMSYOS\Node` or `~/.local/share/amsyos-node`.
/// Nothing of it is ever given to a job.
pub fn node_dir() -> PathBuf {
    #[cfg(windows)]
    {
        let base = std::env::var_os("LOCALAPPDATA")
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from(r"C:\Users\Default\AppData\Local"));
        base.join("AMSYOS").join("Node")
    }
    #[cfg(unix)]
    {
        let home = std::env::var_os("HOME").map(PathBuf::from).unwrap_or_else(|| PathBuf::from("/"));
        node_dir_in(&home)
    }
}

#[cfg(unix)]
pub fn node_dir_in(home: &Path) -> PathBuf {
    home.join(".local").join("share").join("amsyos-node")
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

pub fn default_jobs_root() -> PathBuf {
    #[cfg(windows)]
    {
        program_data().join("jobs")
    }
    #[cfg(unix)]
    {
        PathBuf::from("/var/lib/amsyos-node/jobs")
    }
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
/// job account may read and run them there, never write.
pub fn default_services_root() -> PathBuf {
    #[cfg(windows)]
    {
        program_data().join("services")
    }
    #[cfg(unix)]
    {
        PathBuf::from("/var/lib/amsyos-node/services")
    }
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
        self.services_root.clone().unwrap_or_else(default_services_root)
    }

    pub fn jobs_root(&self) -> PathBuf {
        self.jobs.clone().unwrap_or_else(default_jobs_root)
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

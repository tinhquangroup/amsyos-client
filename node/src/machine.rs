//! What the machine has (sdk/NODE.md §1 `Machine`): CPUs, memory, free disk of the jobs
//! drive, NVIDIA GPUs, and the software the node knows how to find. Every probe of a program
//! is bounded by a short timeout.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use serde::Serialize;

use crate::{sys, util};

#[derive(Serialize, Clone, Debug)]
pub struct Gpu {
    pub name: String,
    pub vram_gb: f64,
}

#[derive(Serialize, Clone, Debug)]
pub struct Machine {
    pub os: String,
    pub arch: String,
    pub hostname: String,
    pub version: String,
    pub cpus: u32,
    pub ram_gb: f64,
    pub disk_free_gb: f64,
    pub gpus: Vec<Gpu>,
    pub software: BTreeMap<String, String>,
    pub elevated: bool,
}

/// A program found on the machine, with where it is (shown by `services`, never sent).
#[derive(Clone, Debug)]
pub struct Found {
    pub name: &'static str,
    pub version: String,
    pub path: PathBuf,
}

fn round1(x: f64) -> f64 {
    (x * 10.0).round() / 10.0
}

pub fn cpus() -> u32 {
    std::thread::available_parallelism().map(|n| n.get() as u32).unwrap_or(1)
}

/// The software the node knows how to find.
const NAMES: &[&str] = &["blender", "ffmpeg", "ollama", "python", "powershell", "pwsh", "node", "dotnet"];

/// What the machine has, and every program found (more than one place for a name, the
/// first one first). `software` names the first place of each; `keep_reachable` narrows it
/// to what the job account can run.
pub fn probe(jobs_root: &Path) -> (Machine, Vec<Found>) {
    let gpus = std::thread::spawn(probe_gpus);
    let probes: Vec<_> = NAMES.iter().map(|&n| std::thread::spawn(move || find(n))).collect();
    let found: Vec<Found> = probes.into_iter().filter_map(|h| h.join().ok()).flatten().collect();
    let mut software = BTreeMap::new();
    for f in &found {
        software.entry(f.name.to_string()).or_insert_with(|| f.version.clone());
    }
    let machine = Machine {
        os: if cfg!(windows) { "windows" } else { "linux" }.into(),
        arch: std::env::consts::ARCH.into(),
        hostname: util::cut(&sys::hostname(), 60),
        version: env!("CARGO_PKG_VERSION").into(),
        cpus: cpus(),
        ram_gb: round1(sys::ram_bytes() as f64 / (1u64 << 30) as f64),
        disk_free_gb: disk_free_gb(jobs_root),
        gpus: gpus.join().unwrap_or_default(),
        software,
        elevated: sys::is_elevated(),
    };
    (machine, found)
}

/// TODO(review): isolation — the Machine's `software` (sent at join and hello) names only
/// what the job account itself can reach and run (asked as it, `guard::Ask::See`): for each
/// name, the first place it can. Err: it could not be asked (no job account yet), and
/// `software` is left as found.
pub fn keep_reachable(machine: &mut Machine, found: &[Found], node_dir: &Path, jobs_root: &Path) -> Result<Vec<bool>, String> {
    if found.is_empty() {
        return Ok(Vec::new());
    }
    sys::preflight()?;
    let paths: Vec<PathBuf> = found.iter().map(|f| f.path.clone()).collect();
    let seen: Vec<bool> = crate::guard::ask(&paths, crate::guard::Ask::See, node_dir, jobs_root)?
        .into_iter()
        .map(|a| a == Some(true))
        .collect();
    machine.software.clear();
    for (f, ok) in found.iter().zip(&seen) {
        if *ok {
            machine.software.entry(f.name.to_string()).or_insert_with(|| f.version.clone());
        }
    }
    Ok(seen)
}

/// `probe`, then `keep_reachable`; the note says what the check did, for the log.
pub fn probe_checked(jobs_root: &Path, node_dir: &Path) -> (Machine, Vec<Found>, String) {
    let (mut machine, found) = probe(jobs_root);
    let note = match keep_reachable(&mut machine, &found, node_dir, jobs_root) {
        Ok(seen) => {
            let left: Vec<String> = found
                .iter()
                .zip(&seen)
                .filter(|(_, ok)| !**ok)
                .map(|(f, _)| format!("{} {}", f.name, f.path.display()))
                .collect();
            if left.is_empty() {
                format!("software the job account can run: {}", list(&machine))
            } else {
                format!("software the job account can run: {}; not reachable by it: {}", list(&machine), left.join(", "))
            }
        }
        Err(e) => format!("software sent as found, not checked as the job account ({}): {}", util::cut(&e, 200), list(&machine)),
    };
    (machine, found, note)
}

fn list(machine: &Machine) -> String {
    if machine.software.is_empty() {
        return "none".into();
    }
    machine.software.iter().map(|(k, v)| format!("{k} {v}")).collect::<Vec<_>>().join(", ")
}

pub fn disk_free_gb(jobs_root: &Path) -> f64 {
    // The nearest folder that exists: the jobs folder may not be made yet.
    let mut at = jobs_root.to_path_buf();
    while !at.exists() {
        if !at.pop() {
            break;
        }
    }
    round1(sys::disk_free_bytes(&at) as f64 / (1u64 << 30) as f64)
}

fn probe_gpus() -> Vec<Gpu> {
    #[cfg_attr(not(windows), allow(unused_mut))]
    let mut smi = util::which("nvidia-smi");
    #[cfg(windows)]
    if smi.is_none() {
        let p = PathBuf::from(r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe");
        smi = p.is_file().then_some(p);
    }
    let Some(smi) = smi else { return Vec::new() };
    let args = ["--query-gpu=name,memory.total", "--format=csv,noheader,nounits"];
    let Some((0, out, _)) = util::run_bounded(&smi, &args, 8) else { return Vec::new() };
    out.lines()
        .filter_map(|line| {
            let (name, mib) = line.rsplit_once(',')?;
            let mib: f64 = mib.trim().parse().ok()?;
            Some(Gpu { name: util::cut(name.trim(), 80), vram_gb: round1(mib / 1024.0) })
        })
        .take(8)
        .collect()
}

/// Where a program may be, in the order tried: the PATH first, then the usual install folders.
fn candidates(name: &str) -> Vec<PathBuf> {
    let mut out = Vec::new();
    let on_path: Vec<&str> = match name {
        "python" => vec!["python3", "python"],
        "powershell" if cfg!(not(windows)) => vec![], // Windows PowerShell is Windows' only
        other => vec![other],
    };
    for p in on_path {
        if let Some(found) = util::which(p) {
            // The Microsoft Store's stand-in for python opens the Store: never a real one.
            if !found.to_string_lossy().contains("WindowsApps") {
                out.push(found);
            }
        }
    }
    #[cfg(windows)]
    {
        let env = |k: &str| std::env::var_os(k).map(PathBuf::from);
        let pf = env("ProgramFiles").unwrap_or_else(|| PathBuf::from(r"C:\Program Files"));
        let local = env("LOCALAPPDATA").unwrap_or_default();
        // Newest first: folder names like "Blender 4.2" or "Python312" sort by version.
        let newest = |dir: PathBuf, prefix: &str, exe: &str| -> Vec<PathBuf> {
            let mut dirs: Vec<PathBuf> = std::fs::read_dir(&dir)
                .map(|rd| {
                    rd.filter_map(|e| e.ok())
                        .filter(|e| e.file_name().to_string_lossy().to_lowercase().starts_with(prefix))
                        .map(|e| e.path().join(exe))
                        .filter(|p| p.is_file())
                        .collect()
                })
                .unwrap_or_default();
            dirs.sort_by_key(|p| std::cmp::Reverse(version_key(&p.to_string_lossy())));
            dirs
        };
        match name {
            "blender" => {
                out.extend(newest(pf.join("Blender Foundation"), "blender", "blender.exe"));
                out.push(pf.join(r"Steam\steamapps\common\Blender\blender.exe"));
            }
            "ffmpeg" => {
                out.push(local.join(r"Microsoft\WinGet\Links\ffmpeg.exe"));
                out.push(pf.join(r"ffmpeg\bin\ffmpeg.exe"));
                out.push(PathBuf::from(r"C:\ffmpeg\bin\ffmpeg.exe"));
            }
            "ollama" => out.push(local.join(r"Programs\Ollama\ollama.exe")),
            "powershell" => {
                let root = env("SystemRoot").unwrap_or_else(|| PathBuf::from(r"C:\Windows"));
                out.insert(0, root.join(r"System32\WindowsPowerShell\v1.0\powershell.exe"));
            }
            "pwsh" => out.extend(newest(pf.join("PowerShell"), "", "pwsh.exe")),
            "node" => out.push(pf.join(r"nodejs\node.exe")),
            "dotnet" => out.push(pf.join(r"dotnet\dotnet.exe")),
            _ => {
                out.extend(newest(local.join(r"Programs\Python"), "python3", "python.exe"));
                out.extend(newest(pf.clone(), "python3", "python.exe"));
                out.extend(newest(PathBuf::from(r"C:\"), "python3", "python.exe"));
            }
        }
    }
    #[cfg(unix)]
    match name {
        "blender" => {
            out.push(PathBuf::from("/snap/bin/blender"));
            out.push(PathBuf::from("/opt/blender/blender"));
        }
        "ollama" => out.push(PathBuf::from("/usr/local/bin/ollama")),
        "pwsh" => out.push(PathBuf::from("/opt/microsoft/powershell/7/pwsh")),
        "dotnet" => {
            out.push(PathBuf::from("/usr/share/dotnet/dotnet"));
            out.push(PathBuf::from("/usr/lib/dotnet/dotnet"));
        }
        _ => {}
    }
    out.retain(|p| p.is_file());
    let mut seen = std::collections::HashSet::new();
    out.retain(|p| seen.insert(p.to_string_lossy().to_lowercase()));
    out
}

fn version_key(s: &str) -> Vec<u32> {
    s.split(|c: char| !c.is_ascii_digit()).filter_map(|p| p.parse().ok()).collect()
}

/// Every place a program is found and answers with a version (at most 3), each probe
/// bounded to 8 s.
fn find(name: &'static str) -> Vec<Found> {
    let args: &[&str] = match name {
        "ffmpeg" => &["-version"],
        "powershell" => &["-NoProfile", "-NonInteractive", "-Command", "$PSVersionTable.PSVersion.ToString()"],
        "dotnet" => &["--list-runtimes"],
        _ => &["--version"],
    };
    let mut out = Vec::new();
    for path in candidates(name).into_iter().take(4) {
        if out.len() == 3 {
            break;
        }
        let Some((_, o, e)) = util::run_bounded(&path, args, 8) else { continue };
        let text = format!("{o}\n{e}");
        let version = if name == "dotnet" { newest_runtime(&text) } else { parse_version(&text) };
        if let Some(version) = version {
            out.push(Found { name, version, path });
        }
    }
    out
}

/// `dotnet --list-runtimes`: the newest `Microsoft.NETCore.App <version> [<folder>]`.
fn newest_runtime(text: &str) -> Option<String> {
    text.lines()
        .filter_map(|l| l.trim().strip_prefix("Microsoft.NETCore.App "))
        .filter_map(|rest| rest.split_whitespace().next())
        .max_by_key(|v| version_key(v))
        .map(|v| util::cut(v, 20))
}

/// The first word that starts with a digit and holds a dot ("Blender 4.2.1", "ffmpeg version
/// 7.0.1-full_build", "ollama version is 0.3.12", "Python 3.12.4"), cut to its digits and dots.
fn parse_version(text: &str) -> Option<String> {
    for word in text.split_whitespace() {
        let w = word.trim_start_matches('v');
        if w.starts_with(|c: char| c.is_ascii_digit()) && w.contains('.') {
            let v: String = w.chars().take_while(|c| c.is_ascii_digit() || *c == '.').collect();
            return Some(util::cut(v.trim_end_matches('.'), 20));
        }
    }
    let mut words = text.split_whitespace();
    while let Some(w) = words.next() {
        if w == "version" {
            return words.next().map(|v| util::cut(v, 20));
        }
    }
    None
}

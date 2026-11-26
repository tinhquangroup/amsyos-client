//! One job, contract 1 (sdk/CONTRACT.md §3): its folder, its run as the job account, what it
//! left. Shared by `run` (jobs from the Gateway) and `try` (a job the person starts).
//!
//! Everything the job wrote is untrusted: a file is opened without following links at its
//! last step and used only if it really is where it should be (`open_safe`).

use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::fs::File;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant, SystemTime};

use serde_json::{json, Value};
use tokio::sync::watch;

use crate::config::Service;
use crate::{sys, util};

const LOG_MAX: usize = 1 << 20; // what the node keeps of a job's stdout and stderr
const SAID_MAX: usize = 2000;

/// What `sys::spawn` needs to start a job.
pub struct Launch {
    pub job_dir: PathBuf,
    pub service_dir: PathBuf,
    pub command: String,
    pub cpu: f64,
    pub ram_gb: f64,
    #[cfg_attr(windows, allow(dead_code))]
    pub max_runtime_s: u64,
    pub server: bool,
    #[cfg_attr(unix, allow(dead_code))]
    pub node_dir: PathBuf,
    /// TODO(review): isolation — true only for `node.exec` and an `as_node` service's direct
    /// job at level `full` (net.rs decides): the node's own account, no logon, no memory cap.
    pub as_node: bool,
    /// Node 2.1.2: a server kept warm (serve.rs): where `amsyos-job next` keeps the job now.
    /// `job_dir` is then the server's own folder; its stdin is a pipe the node writes jobs to.
    pub serve_state: Option<PathBuf>,
    /// Node 2.1.8: what the job is told it may use of the machine.
    pub given: Given,
}

/// Node 2.1.8: what every job gets so that Python reads and writes UTF-8 whatever the
/// machine's code page (a customer, 2026-11-26: Python services on a Windows node failed with
/// "UnicodeEncodeError: 'charmap' codec can't encode character" printing Vietnamese text). A
/// service's own command may set them otherwise.
pub const UTF8_VARS: [(&str, &str); 2] = [("PYTHONUTF8", "1"), ("PYTHONIOENCODING", "utf-8")];

/// The variables that tell a job its cores (sdk/CONTRACT.md §3), as a rented node's.
pub const THREAD_VARS: [&str; 6] = [
    "AMSYOS_CPUS",
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "NUMEXPR_MAX_THREADS",
];

/// The variable that tells a job the GPU memory it may count on (sdk/CONTRACT.md §3).
pub const GPU_VAR: &str = "AMSYOS_GPU_MEM_GB";

/// Node 2.1.8: what a job may use of the machine (sdk/CONTRACT.md §3), told in its
/// environment and in job.json `limits`, as on a rented node.
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct Given {
    /// Its cores; 0: not told (the node's own checks as the job account, guard.rs).
    pub cpus: u32,
    /// The GPU memory it may count on, in GB, where known.
    pub gpu_mem_gb: Option<f64>,
}

impl Given {
    /// The cores: the fewest of what the job asked for (`limits.cpu`, rounded up) and what
    /// the node allows jobs (`machine::cpus`: the CPUs it may run on and its cgroup's quota).
    /// A job run as the node's own account (`node.exec`, an `as_node` service at `full`) is
    /// held to none, and is told the node's. The GPU memory, for a job that may use a GPU on
    /// a machine with one: what its service declares (`limits.vram_gb`), at most the first
    /// GPU's, else the whole first GPU's; none for a token amount (under 0.1 GB).
    pub fn of(limits: &Limits, gpu_service: bool, as_node: bool) -> Given {
        let node = crate::machine::cpus().max(1);
        let cpus = if as_node { node } else { (limits.cpu.ceil().max(1.0) as u32).min(node) };
        let gpu_mem_gb = if limits.gpu_job() || gpu_service {
            match (limits.vram_gb, crate::machine::first_gpu_gb()) {
                (_, None) => None,
                (Some(v), _) if v < 0.1 => None,
                (Some(v), Some(whole)) => Some((v.min(whole) * 10.0).round() / 10.0),
                (None, Some(whole)) => Some(whole),
            }
        } else {
            None
        };
        Given { cpus, gpu_mem_gb }
    }

    /// job.json's `limits`: {"cpus", "gpu_mem_gb"} (the latter where known).
    pub fn limits(&self) -> Value {
        let mut l = serde_json::Map::new();
        l.insert("cpus".into(), self.cpus.into());
        if let Some(gb) = self.gpu_mem_gb {
            l.insert("gpu_mem_gb".into(), gb.into());
        }
        Value::Object(l)
    }
}

impl Launch {
    /// Node 2.1.8: the variables the job gets beyond the contract's folders, in the order a
    /// Windows command line too long for all of them drops them from the end (sys_windows.rs):
    /// Python's UTF-8, then its cores, then its GPU memory. A job run as the node's own account
    /// keeps what the node's environment sets already (never AMSYOS_*, which are the node's).
    /// On Linux a job as `amsyos-job` gets the same from `amsyos-job launch` (job.rs).
    pub fn told(&self) -> Vec<(&'static str, String)> {
        let mut out: Vec<(&'static str, String)> = UTF8_VARS.iter().map(|(k, v)| (*k, v.to_string())).collect();
        if self.given.cpus > 0 {
            out.extend(THREAD_VARS.iter().map(|k| (*k, self.given.cpus.to_string())));
        }
        if let Some(gb) = self.given.gpu_mem_gb {
            out.push((GPU_VAR, format!("{gb}")));
        }
        if self.as_node {
            out.retain(|(k, _)| k.starts_with("AMSYOS_") || std::env::var_os(k).is_none_or(|v| v.is_empty()));
        }
        out
    }
}

/// A job's `limits` (sdk/NODE.md §2), bounded here whatever the kernel sent.
#[derive(Clone, Debug)]
pub struct Limits {
    pub cpu: f64,
    pub ram_gb: f64,
    pub gpu: String,
    /// Node 2.1.8: the GPU memory the service declares (its manifest's `resources.vram_gb`).
    pub vram_gb: Option<f64>,
    pub max_runtime_s: u64,
    pub max_output_mb: u64,
}

impl Default for Limits {
    fn default() -> Self {
        Limits { cpu: 1.0, ram_gb: 1.0, gpu: "none".into(), vram_gb: None, max_runtime_s: 3600, max_output_mb: 200 }
    }
}

impl Limits {
    pub fn parse(v: Option<&Value>, problems: &mut Vec<String>) -> Limits {
        let mut l = Limits::default();
        let Some(v) = v else { return l };
        let Some(o) = v.as_object() else {
            problems.push("limits: an object".into());
            return l;
        };
        let num = |k: &str| o.get(k).filter(|x| !x.is_null()).map(|x| x.as_f64());
        match num("cpu") {
            Some(Some(x)) if x > 0.0 && x <= 1024.0 => l.cpu = x,
            None => {}
            _ => problems.push("limits.cpu: a number from 0 to 1024".into()),
        }
        match num("ram_gb") {
            Some(Some(x)) if x > 0.0 && x <= 4096.0 => l.ram_gb = x,
            None => {}
            _ => problems.push("limits.ram_gb: a number from 0 to 4096".into()),
        }
        match o.get("gpu").and_then(Value::as_str) {
            Some(g @ ("none" | "optional" | "required")) => l.gpu = g.into(),
            None if o.get("gpu").is_none_or(Value::is_null) => {}
            _ => problems.push("limits.gpu: none, optional or required".into()),
        }
        // Only what the job is told (Given): one out of bounds is left out, never a refusal.
        if let Some(Some(x)) = num("vram_gb") {
            l.vram_gb = (x.is_finite() && (0.0..=4096.0).contains(&x)).then_some(x);
        }
        match num("max_runtime_s") {
            Some(Some(x)) if (1.0..=7.0 * 86400.0).contains(&x) => l.max_runtime_s = x as u64,
            None => {}
            _ => problems.push("limits.max_runtime_s: 1 to 604800".into()),
        }
        match num("max_output_mb") {
            Some(Some(x)) if (1.0..=100_000.0).contains(&x) => l.max_output_mb = x as u64,
            None => {}
            _ => problems.push("limits.max_output_mb: 1 to 100000".into()),
        }
        l
    }

    pub fn gpu_job(&self) -> bool {
        self.gpu != "none"
    }
}

/// A job's folder as the node made it (`dir`), and where it really is (`real`, resolved
/// before the job ran), against which every file the job left is checked.
pub struct JobDir {
    pub dir: PathBuf,
    pub real: PathBuf,
    /// What `job.json` says, as `prepare` wrote it (`tell` adds `limits`).
    told: Value,
}

/// Makes the job's folder and writes `job.json`: `service` says which service the job is for
/// ({"id", "version"}, contract 1 §3); `version` beside it, as an older node wrote it.
pub fn prepare(
    root: &Path,
    id: &str,
    service: &str,
    version: &str,
    params: &Value,
    inputs: &BTreeMap<String, String>,
) -> Result<JobDir, String> {
    let real = sys::prepare_job_dir(root, id)?;
    let told = json!({
        "job": id, "service": {"id": service, "version": version}, "version": version,
        "params": params, "inputs": inputs,
    });
    let jd = JobDir { dir: root.join(id), real, told };
    let path = jd.dir.join("job.json");
    std::fs::write(&path, jd.told.to_string()).map_err(|e| format!("cannot write {}: {e}", path.display()))?;
    sys::job_may_read(&path);
    Ok(jd)
}

/// Node 2.1.8: `job.json` again with what the job may use (`limits`, sdk/CONTRACT.md §3),
/// just before the job starts. Written beside it as a new file, then renamed over it:
/// whatever lies at `job.json` is replaced, never written through. Without it the job runs
/// as before.
pub fn tell(jd: &JobDir, given: &Given) {
    if given.cpus == 0 {
        return;
    }
    let mut told = jd.told.clone();
    if let Some(o) = told.as_object_mut() {
        o.insert("limits".into(), given.limits());
    }
    let (fresh, path) = (jd.dir.join(".job.json.new"), jd.dir.join("job.json"));
    let _ = std::fs::remove_file(&fresh);
    let written = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&fresh)
        .and_then(|mut f| std::io::Write::write_all(&mut f, told.to_string().as_bytes()))
        .and_then(|_| std::fs::rename(&fresh, &path));
    match written {
        Ok(()) => sys::job_may_read(&path),
        Err(e) => {
            let _ = std::fs::remove_file(&fresh);
            crate::warn!("job.json in {} was not told the job's limits: {e}", jd.dir.display());
        }
    }
}

/// Where an input `in/<file>` goes.
pub fn input_path(jd: &JobDir, file: &str) -> PathBuf {
    let mut p = jd.dir.clone();
    p.extend(file.split('/'));
    p
}

/// Opens `<job>/<parts…>` only if it is a plain file that really lies there.
/// TODO(review): isolation — what keeps a job from making the node read (and upload) a file
/// of the node's own through a link, a junction or a swapped folder.
fn open_safe(jd: &JobDir, parts: &[&str]) -> Option<File> {
    let mut path = jd.dir.clone();
    let mut want = jd.real.clone();
    for p in parts {
        path.push(p);
        want.push(p);
    }
    let f = sys::open_nofollow(&path).ok()?;
    if !sys::is_plain(&f) {
        return None;
    }
    let real = sys::final_path(&f).ok()?;
    sys::same_path(&real, &want).then_some(f)
}

fn read_small(jd: &JobDir, parts: &[&str], most: u64) -> Option<String> {
    let f = open_safe(jd, parts)?;
    let mut s = String::new();
    f.take(most).read_to_string(&mut s).ok()?;
    Some(s)
}

/// The `progress` file, 0 to 100.
pub fn read_progress(jd: &JobDir) -> Option<u8> {
    let x: f64 = read_small(jd, &["progress"], 32)?.trim().parse().ok()?;
    x.is_finite().then(|| x.clamp(0.0, 100.0).round() as u8)
}

/// `error.json`: its error (INVALID_PARAMS or SERVICE_ERROR) and hint, bounded.
pub fn read_error(jd: &JobDir) -> Option<(String, String)> {
    let text = read_small(jd, &["error.json"], 16 * 1024)?;
    let v: Value = serde_json::from_str(&text).unwrap_or(Value::Null);
    let error = match v.get("error").and_then(Value::as_str) {
        Some("INVALID_PARAMS") => "INVALID_PARAMS",
        _ => "SERVICE_ERROR",
    };
    let hint = v.get("hint").and_then(Value::as_str).map(|h| util::cut(h, 500)).unwrap_or_else(|| {
        "the service wrote error.json without a hint".into()
    });
    Some((error.into(), hint))
}

/// One output file the job left in `out/`, opened.
pub struct OutFile {
    pub name: String,
    pub file_name: String,
    pub file: File,
    pub size: u64,
}

/// The files of `out/` that are outputs (`declared`; any name when None), each name once.
/// The rest is said in the notes.
pub fn collect_outputs(jd: &JobDir, declared: Option<&[String]>) -> (Vec<OutFile>, Vec<String>) {
    let mut names: Vec<String> = std::fs::read_dir(jd.dir.join("out"))
        .map(|rd| rd.filter_map(|e| e.ok()).take(1000).map(|e| e.file_name().to_string_lossy().into_owned()).collect())
        .unwrap_or_default();
    names.sort();
    let (mut outs, mut notes, mut seen) = (Vec::new(), Vec::new(), BTreeSet::new());
    for name in names {
        let shown = util::cut(&name, 80);
        let Some(stem) = util::file_stem_ok(&name) else {
            notes.push(format!("out/{shown} is not <output>.<ext>: left out"));
            continue;
        };
        if declared.is_some_and(|d| !d.iter().any(|o| o == stem)) {
            notes.push(format!("out/{shown} is no output of the service: left out"));
            continue;
        }
        if seen.contains(stem) {
            notes.push(format!("out/{shown}: the output {stem} has a file already: left out"));
            continue;
        }
        let Some(file) = open_safe(jd, &["out", &name]) else {
            notes.push(format!("out/{shown} is not a plain file of the job's folder: left out"));
            continue;
        };
        let size = file.metadata().map(|m| m.len()).unwrap_or(0);
        seen.insert(stem.to_string());
        outs.push(OutFile { name: stem.to_string(), file_name: name.clone(), file, size });
    }
    (outs, notes)
}

pub struct Tail {
    buf: VecDeque<u8>,
    cap: usize,
}

impl Tail {
    pub fn new(cap: usize) -> Tail {
        Tail { buf: VecDeque::new(), cap }
    }
    pub fn push(&mut self, data: &[u8]) {
        let data = &data[data.len().saturating_sub(self.cap)..];
        let over = (self.buf.len() + data.len()).saturating_sub(self.cap);
        self.buf.drain(..over.min(self.buf.len()));
        self.buf.extend(data);
    }
    /// As text: UTF-8, and on Windows a line that is no UTF-8 read in the console's code
    /// page (node 2.1.8, `sys::decode_output`).
    pub fn text(&self) -> String {
        let (a, b) = self.buf.as_slices();
        sys::decode_output(&[a, b].concat())
    }
}

/// What a job's processes wrote: all of it (the node's log of the job), and the ends of
/// stdout and stderr (what the job `said`).
pub struct Logs {
    pub all: Tail,
    pub out: Tail,
    pub err: Tail,
}

impl Logs {
    pub fn new() -> Logs {
        Logs { all: Tail::new(LOG_MAX), out: Tail::new(8192), err: Tail::new(8192) }
    }
}

/// How a job's process ended.
pub struct Ended {
    pub pid: u32,
    pub exit: i64,
    pub timed_out: bool,
    pub cancelled: bool,
    pub stdout: String,
    pub stderr: String,
    pub log: String,
    pub ran: Duration,
    /// Node 2.1.2: a served job whose server ended before it said the job was done: what
    /// happened, in words ("the server ended while it did the job"); the job failed.
    pub broke: Option<String>,
    /// Node 2.1.4: killed by the kernel's OOM killer, as the node found it (`mem::oom_note`).
    pub oom: Option<String>,
}

impl Ended {
    /// The end of stderr (of stdout when stderr is empty), at most 2000 characters; after the
    /// node's note when the job ran out of memory (2.1.4).
    pub fn said(&self) -> String {
        let text = if self.stderr.trim().is_empty() { &self.stdout } else { &self.stderr };
        match &self.oom {
            Some(oom) => {
                let room = SAID_MAX.saturating_sub(oom.chars().count() + 1);
                format!("{oom}\n{}", util::tail(text.trim_end(), room)).trim_end().to_string()
            }
            None => util::tail(text.trim_end(), SAID_MAX),
        }
    }

    /// Ok when it succeeded; else (error, hint).
    pub fn judge(&self, jd: &JobDir, max_runtime_s: u64) -> Result<(), (String, String)> {
        if self.timed_out {
            return Err((
                "TIMEOUT".into(),
                format!("the job ran longer than its max_runtime_s ({max_runtime_s} s) and was stopped"),
            ));
        }
        if let Some(e) = read_error(jd) {
            return Err(e);
        }
        if let Some(oom) = &self.oom {
            return Err(("SERVICE_ERROR".into(), oom.clone()));
        }
        if let Some(broke) = &self.broke {
            return Err(("SERVICE_ERROR".into(), self.why(broke)));
        }
        if self.exit != 0 {
            return Err(("SERVICE_ERROR".into(), self.exited()));
        }
        Ok(())
    }

    /// "the service exited with code N", with the line of its stderr (of stdout when that
    /// is empty) that says why: the last one naming an error, else the last one. The hint
    /// is what `amsyos.result` shows; `said` has the longer end.
    pub fn exited(&self) -> String {
        self.why(&format!("the service exited with code {}", self.exit))
    }

    /// `lead`, and the line that says why.
    fn why(&self, lead: &str) -> String {
        let said = self.said();
        let lines: Vec<&str> = said.lines().map(str::trim).filter(|l| !l.is_empty()).collect();
        let why = |l: &&&str| {
            let l = l.to_lowercase();
            !l.starts_with("at ")
                && ["error", "exception", "denied", "not found", "cannot", "can't", "fail", "no such"]
                    .iter()
                    .any(|w| l.contains(w))
        };
        let Some(line) = lines.iter().rev().find(why).or(lines.last()) else {
            return format!("{lead} and printed nothing");
        };
        format!("{lead}: {} (said: the end of its stderr)", util::cut(line, 300))
    }
}

/// Runs the switched-on service's command for the job, as the job account, to its end, its
/// timeout or a cancel. `progress` is called when the progress file changed (every 2 s at
/// most).
pub async fn execute(
    jd: &JobDir,
    svc: &Service,
    limits: &Limits,
    server: bool,
    node_dir: &Path,
    as_node: bool,
    mut cancel: watch::Receiver<bool>,
    mut progress: impl FnMut(u8),
) -> Result<Ended, String> {
    let given = Given::of(limits, svc.gpu, as_node);
    tell(jd, &given);
    let launch = Launch {
        job_dir: jd.dir.clone(),
        service_dir: svc.dir.clone(),
        command: svc.command.clone(),
        cpu: limits.cpu,
        ram_gb: limits.ram_gb,
        max_runtime_s: limits.max_runtime_s,
        server,
        node_dir: node_dir.to_path_buf(),
        as_node,
        serve_state: None,
        given,
    };
    let started = Instant::now();
    let spawned = tokio::task::spawn_blocking(move || sys::spawn(&launch)).await.map_err(|e| e.to_string())??;
    let logs = Arc::new(Mutex::new(Logs::new()));
    let readers: Vec<_> = [(spawned.stdout, false), (spawned.stderr, true)]
        .into_iter()
        .map(|(mut pipe, is_err)| {
            let logs = logs.clone();
            std::thread::spawn(move || {
                let mut buf = [0u8; 8192];
                while let Ok(n) = pipe.read(&mut buf) {
                    if n == 0 {
                        break;
                    }
                    if let Ok(mut l) = logs.lock() {
                        l.all.push(&buf[..n]);
                        if is_err { l.err.push(&buf[..n]) } else { l.out.push(&buf[..n]) }
                    }
                }
            })
        })
        .collect();

    let proc = spawned.proc.clone();
    let mut wait = tokio::task::spawn_blocking(move || proc.wait());
    let deadline = tokio::time::Instant::now() + Duration::from_secs(limits.max_runtime_s);
    let mut tick = tokio::time::interval(Duration::from_secs(2));
    let (mut timed_out, mut cancelled, mut last) = (false, false, None);
    let exit = loop {
        tokio::select! {
            code = &mut wait => break code.unwrap_or(-1),
            _ = tokio::time::sleep_until(deadline), if !timed_out && !cancelled => {
                timed_out = true;
                spawned.proc.kill();
            }
            changed = cancel.changed(), if !timed_out && !cancelled => {
                if changed.is_err() || *cancel.borrow() {
                    cancelled = true;
                    spawned.proc.kill();
                }
            }
            _ = tick.tick() => {
                let now = read_progress(jd);
                if now.is_some() && now != last {
                    last = now;
                    progress(now.unwrap_or(0));
                }
            }
        }
    };
    let ran = started.elapsed();
    // The pipes close when the job's processes are gone; do not wait for ever on a stray one.
    let joined = tokio::task::spawn_blocking(move || {
        for r in readers {
            let _ = r.join();
        }
    });
    let _ = tokio::time::timeout(Duration::from_secs(5), joined).await;
    let l = logs.lock().map_err(|_| "the job's log".to_string())?;
    Ok(Ended {
        pid: spawned.pid,
        exit,
        timed_out,
        cancelled,
        stdout: l.out.text(),
        stderr: l.err.text(),
        log: l.all.text(),
        ran,
        broke: None,
        oom: None,
    })
}

/// The job's log, kept in the node's own folder (`logs/<job>.log`); how long: `sweep_logs`.
pub fn keep_log(node_dir: &Path, id: &str, ended: &Ended) {
    let dir = node_dir.join("logs");
    if std::fs::create_dir_all(&dir).is_err() {
        return;
    }
    let head = format!(
        "job {id}: process {}, exit {}{}{}, ran {:.1} s\n",
        ended.pid,
        ended.exit,
        if ended.timed_out { ", timed out" } else { "" },
        if ended.cancelled { ", cancelled" } else { "" },
        ended.ran.as_secs_f64()
    );
    let _ = std::fs::write(dir.join(format!("{id}.log")), format!("{head}{}", ended.log));
    sweep_logs(node_dir);
}

/// What the node keeps of its jobs' logs (2.1.7; the last 50 jobs before): the last 50 jobs'
/// always, and any other of the last 7 days while all of them stay within 500 MB and 10,000
/// files; the oldest go first. Swept after each job and when the node starts. Only plain
/// `.log` files of `logs/`, never a link.
pub fn sweep_logs(node_dir: &Path) {
    const ALWAYS: usize = 50;
    const DAYS: u64 = 7;
    const BUDGET: u64 = 500 << 20;
    const MOST: usize = 10_000;
    let Ok(rd) = std::fs::read_dir(node_dir.join("logs")) else { return };
    let mut all: Vec<(SystemTime, u64, PathBuf)> = rd
        .filter_map(|e| e.ok())
        .filter(|e| e.file_type().is_ok_and(|t| t.is_file()) && e.file_name().to_string_lossy().ends_with(".log"))
        .filter_map(|e| {
            let m = e.metadata().ok()?;
            Some((m.modified().ok()?, m.len(), e.path()))
        })
        .collect();
    all.sort_by(|a, b| b.0.cmp(&a.0)); // newest first
    let now = SystemTime::now();
    let (mut total, mut full) = (0u64, false);
    for (i, (when, size, path)) in all.iter().enumerate() {
        total += size;
        full |= total > BUDGET || i >= MOST;
        let old = now.duration_since(*when).is_ok_and(|age| age > Duration::from_secs(DAYS * 86_400));
        if i >= ALWAYS && (old || full) {
            let _ = std::fs::remove_file(path);
        }
    }
}

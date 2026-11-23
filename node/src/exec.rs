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
use std::time::{Duration, Instant};

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
}

/// A job's `limits` (sdk/NODE.md §2), bounded here whatever the kernel sent.
#[derive(Clone, Debug)]
pub struct Limits {
    pub cpu: f64,
    pub ram_gb: f64,
    pub gpu: String,
    pub max_runtime_s: u64,
    pub max_output_mb: u64,
}

impl Default for Limits {
    fn default() -> Self {
        Limits { cpu: 1.0, ram_gb: 1.0, gpu: "none".into(), max_runtime_s: 3600, max_output_mb: 200 }
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
    let jd = JobDir { dir: root.join(id), real };
    let told = json!({
        "job": id, "service": {"id": service, "version": version}, "version": version,
        "params": params, "inputs": inputs,
    });
    let path = jd.dir.join("job.json");
    std::fs::write(&path, told.to_string()).map_err(|e| format!("cannot write {}: {e}", path.display()))?;
    sys::job_may_read(&path);
    Ok(jd)
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

struct Tail {
    buf: VecDeque<u8>,
    cap: usize,
}

impl Tail {
    fn new(cap: usize) -> Tail {
        Tail { buf: VecDeque::new(), cap }
    }
    fn push(&mut self, data: &[u8]) {
        let data = &data[data.len().saturating_sub(self.cap)..];
        let over = (self.buf.len() + data.len()).saturating_sub(self.cap);
        self.buf.drain(..over.min(self.buf.len()));
        self.buf.extend(data);
    }
    fn text(&self) -> String {
        let (a, b) = self.buf.as_slices();
        String::from_utf8_lossy(&[a, b].concat()).into_owned()
    }
}

struct Logs {
    all: Tail,
    out: Tail,
    err: Tail,
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
}

impl Ended {
    /// The end of stderr (of stdout when stderr is empty), at most 2000 characters.
    pub fn said(&self) -> String {
        let text = if self.stderr.trim().is_empty() { &self.stdout } else { &self.stderr };
        util::tail(text.trim_end(), SAID_MAX)
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
        if self.exit != 0 {
            return Err(("SERVICE_ERROR".into(), self.exited()));
        }
        Ok(())
    }

    /// "the service exited with code N", with the line of its stderr (of stdout when that
    /// is empty) that says why: the last one naming an error, else the last one. The hint
    /// is what `amsyos.result` shows; `said` has the longer end.
    pub fn exited(&self) -> String {
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
            return format!("the service exited with code {} and printed nothing", self.exit);
        };
        format!(
            "the service exited with code {}: {} (said: the end of its stderr)",
            self.exit,
            util::cut(line, 300)
        )
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
    };
    let started = Instant::now();
    let spawned = tokio::task::spawn_blocking(move || sys::spawn(&launch)).await.map_err(|e| e.to_string())??;
    let logs = Arc::new(Mutex::new(Logs { all: Tail::new(LOG_MAX), out: Tail::new(8192), err: Tail::new(8192) }));
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
    })
}

/// The job's log, kept in the node's own folder (`logs/<job>.log`), the last 50 jobs.
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
    let mut all: Vec<_> = std::fs::read_dir(&dir)
        .map(|rd| rd.filter_map(|e| e.ok()).filter_map(|e| Some((e.metadata().ok()?.modified().ok()?, e.path()))).collect())
        .unwrap_or_default();
    if all.len() > 50 {
        all.sort();
        for (_, p) in &all[..all.len() - 50] {
            let _ = std::fs::remove_file(p);
        }
    }
}

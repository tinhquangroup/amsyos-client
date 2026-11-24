//! Node 2.1.2: servers kept warm (sdk/CONTRACT.md §3 "A server kept warm"; sdk/NODE.md §4).
//!
//! A service switched on or deployed with `serve` has a program that loads what it needs once
//! and then does the jobs one after the other. The node starts it for a job, hands it each job
//! as one line on its stdin and reads one line on its stdout:
//!
//!     node -> server:  {"job": "<job id>", "folder": "<the job's folder>"}
//!     server -> node:  {"done": "<job id>"}
//!
//! each at most 4,096 bytes. Between the two the job is done with the usual folder contract in
//! `folder`; anything else the server writes is the job's log. `from amsyos import serve`
//! (Python), or a loop on `amsyos-job next` … `done` (any language), speaks it.
//!
//! - **Who shares a process** (TODO(review): isolation): one process serves one service
//!   version, one account (amsyos-job or the node's own) and one key the kernel sends
//!   (`serve_key`: a digest of the workspace and who pays, the organisation itself or an
//!   order's buyer), with the same folder, server command and limits. A job without a key
//!   (an older kernel) is served by a process of its own that ends with it.
//! - **Limits**: a process is held to the limits of a cold job (its memory, its CPU share);
//!   the node holds each job to its `max_runtime_s`. At most `slots` processes (2 at least),
//!   their declared memory within the machine's; the idle one unused longest is stopped first,
//!   and with no room a job is served by a process of its own.
//! - **Its end**: idle `IDLE_S` (the rented node's default, 10 minutes), after `MAX_JOBS` jobs
//!   or `MAX_AGE_S`, when its service is switched off or deployed again, or with the node. A job
//!   that fails (`error.json`, an exception) ends the job, not the process. A job cancelled or
//!   out of time stops its process. A process that ends before it took its job (it crashed while
//!   loading, or while it waited) is no failure of the job: the job runs on a fresh process, or
//!   as the service's `command` when it has one. One that ends while it does the job fails the
//!   job with the end of what it wrote: the node never runs a job twice by itself.

use std::fs::File;
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use serde_json::Value;
use sha2::{Digest, Sha256};
use tokio::sync::{mpsc, watch};

use crate::config::{Service, Services};
use crate::exec::{self, Ended, JobDir, Launch, Limits, Logs};
use crate::job::LINE_MAX;
use crate::{info, sys, util, warn};

pub const IDLE_S: u64 = 600;
pub const MAX_JOBS: u32 = 100;
pub const MAX_AGE_S: u64 = 3600;
const STOP_WAIT_S: u64 = 5;
const FOLDER_PREFIX: &str = "serve_";

enum Event {
    Done(String),
    Eof,
    Exited(i64),
}

/// What the reader threads share with the job now.
struct Heard {
    job: Option<String>,
    logs: Logs,
    handed: bool,
}

/// One warm process.
pub struct Warm {
    key: String,
    service: String,
    print: String, // its folder and server command: what services.json must still say
    folder: PathBuf,
    pid: u32,
    proc: Arc<sys::Proc>,
    stdin: Option<File>,
    events: mpsc::UnboundedReceiver<Event>,
    heard: Arc<Mutex<Heard>>,
    jobs: u32,
    since: Instant,
    last: Instant,
    ram_gb: f64,
    exited: Option<i64>,
}

enum Outcome {
    Done,
    TimedOut,
    Cancelled,
    /// It ended after it took the job (its exit code).
    Died(i64),
    /// It ended before it took the job.
    NotTaken(i64),
}

/// The warm processes of this node.
pub struct Pool {
    idle: Vec<Warm>,
    busy: usize,
    busy_ram: f64,
    cap: usize,
    ram_gb: f64,
}

pub type Shared = Arc<Mutex<Pool>>;

impl Pool {
    pub fn new(cap: usize, ram_gb: f64) -> Shared {
        Arc::new(Mutex::new(Pool { idle: Vec::new(), busy: 0, busy_ram: 0.0, cap, ram_gb }))
    }

    fn take(&mut self, key: &str) -> Option<Warm> {
        let i = self.idle.iter().position(|w| w.key == key)?;
        let w = self.idle.swap_remove(i);
        self.busy += 1;
        self.busy_ram += w.ram_gb;
        Some(w)
    }

    /// Room for a new process of `ram_gb`: the idle ones that go for it (unused longest
    /// first; the caller stops them), and whether it fits (else it serves its job alone).
    fn room(&mut self, ram_gb: f64) -> (Vec<Warm>, bool) {
        let mut gone = Vec::new();
        loop {
            let used = self.busy_ram + self.idle.iter().map(|w| w.ram_gb).sum::<f64>();
            if self.idle.len() + self.busy < self.cap && used + ram_gb <= self.ram_gb {
                self.busy += 1;
                self.busy_ram += ram_gb;
                return (gone, true);
            }
            let Some(i) = (0..self.idle.len()).min_by_key(|&i| self.idle[i].last) else { return (gone, false) };
            gone.push(self.idle.swap_remove(i));
        }
    }

    /// A process that did a job comes back (Some) or ends (None, its slot freed).
    fn give_back(&mut self, warm: Option<Warm>, ram_gb: f64) {
        self.busy = self.busy.saturating_sub(1);
        self.busy_ram = (self.busy_ram - ram_gb).max(0.0);
        if let Some(w) = warm {
            self.idle.push(w);
        }
    }

    /// The idle processes to stop now: idle too long, ended, too old, or whose service is no
    /// longer on with the same folder and server command.
    fn sweep(&mut self, services: &Services) -> Vec<Warm> {
        let mut gone = Vec::new();
        let mut i = 0;
        while i < self.idle.len() {
            let w = &mut self.idle[i];
            let current = services.services.get(&w.service).filter(|s| s.on).map(fingerprint);
            if w.last.elapsed() >= Duration::from_secs(IDLE_S) || w.spent() || !w.alive() || current.as_deref() != Some(w.print.as_str()) {
                gone.push(self.idle.swap_remove(i));
            } else {
                i += 1;
            }
        }
        gone
    }
}

fn fingerprint(svc: &Service) -> String {
    format!("{}\n{}", svc.dir.display(), svc.served().unwrap_or(""))
}

/// Who may share a process: the service and version, the account, the kernel's key (the
/// workspace and who pays), the service's folder, server command and limits. None (served
/// alone) without the kernel's key. TODO(review): isolation.
pub fn key(service: &str, version: &str, as_node: bool, kernel_key: Option<&str>, svc: &Service, limits: &Limits) -> Option<String> {
    let kernel_key = kernel_key?;
    let said = format!(
        "{service}\n{version}\n{}\n{kernel_key}\n{}\n{}\n{}",
        if as_node { "node" } else { sys::JOB_USER },
        fingerprint(svc),
        limits.cpu,
        limits.ram_gb
    );
    Some(util::hex(&Sha256::digest(said.as_bytes())))
}

/// Whether the kernel's key looks like one: a digest, hex.
pub fn is_kernel_key(s: &str) -> bool {
    (16..=128).contains(&s.len()) && s.chars().all(|c| c.is_ascii_hexdigit())
}

impl Warm {
    /// Starts the server, in a folder of its own under the jobs folder (its HOME, its tmp,
    /// where `amsyos-job next` keeps the job now). Blocking.
    #[allow(clippy::too_many_arguments)]
    fn start(key: String, svc: &Service, limits: &Limits, server: bool, node_dir: &Path, jobs_root: &Path, as_node: bool) -> Result<Warm, String> {
        let command = svc.served().ok_or("the service has no server")?.to_string();
        let name = format!("{FOLDER_PREFIX}{}", util::hex(&util::random_bytes::<6>()));
        sys::prepare_job_dir(jobs_root, &name)?;
        let folder = jobs_root.join(&name);
        let launch = Launch {
            job_dir: folder.clone(),
            service_dir: svc.dir.clone(),
            command,
            cpu: limits.cpu,
            ram_gb: limits.ram_gb,
            max_runtime_s: limits.max_runtime_s,
            server,
            node_dir: node_dir.to_path_buf(),
            as_node,
            serve_state: Some(folder.join("serve.json")),
        };
        let spawned = match sys::spawn(&launch) {
            Ok(s) => s,
            Err(e) => {
                sys::remove_job_dir(&folder);
                return Err(e);
            }
        };
        let Some(stdin) = spawned.stdin else {
            spawned.proc.kill();
            return Err("the server was started without its stdin".into());
        };
        let (tx, events) = mpsc::unbounded_channel();
        let heard = Arc::new(Mutex::new(Heard { job: None, logs: Logs::new(), handed: false }));
        {
            let (mut pipe, heard, tx) = (spawned.stdout, heard.clone(), tx.clone());
            std::thread::spawn(move || read_out(&mut pipe, &heard, &tx));
        }
        {
            let (mut pipe, heard) = (spawned.stderr, heard.clone());
            std::thread::spawn(move || {
                let mut buf = [0u8; 8192];
                while let Ok(n) = pipe.read(&mut buf) {
                    if n == 0 {
                        break;
                    }
                    if let Ok(mut h) = heard.lock() {
                        h.logs.all.push(&buf[..n]);
                        h.logs.err.push(&buf[..n]);
                    }
                }
            });
        }
        {
            let proc = spawned.proc.clone();
            std::thread::spawn(move || {
                let _ = tx.send(Event::Exited(proc.wait()));
            });
        }
        let now = Instant::now();
        Ok(Warm {
            key,
            service: String::new(),
            print: fingerprint(svc),
            folder,
            pid: spawned.pid,
            proc: spawned.proc,
            stdin: Some(stdin),
            events,
            heard,
            jobs: 0,
            since: now,
            last: now,
            ram_gb: limits.ram_gb,
            exited: None,
        })
    }

    /// What came since it was last asked; false once it ended (or closed its stdout).
    fn alive(&mut self) -> bool {
        while let Ok(e) = self.events.try_recv() {
            match e {
                Event::Exited(code) => self.exited = Some(code),
                Event::Eof => self.exited = Some(self.exited.unwrap_or(-1)),
                Event::Done(_) => {}
            }
        }
        self.exited.is_none()
    }

    fn spent(&self) -> bool {
        self.jobs >= MAX_JOBS || self.since.elapsed() >= Duration::from_secs(MAX_AGE_S)
    }

    /// Hands it the job and waits for its end, its time or a cancel.
    async fn handle(&mut self, jd: &JobDir, id: &str, deadline: tokio::time::Instant, cancel: &mut watch::Receiver<bool>, progress: &mut impl FnMut(u8)) -> Outcome {
        if !self.alive() {
            return Outcome::NotTaken(self.exited.unwrap_or(-1));
        }
        if let Ok(mut h) = self.heard.lock() {
            // What an earlier job of this process wrote says nothing of this one; what it
            // wrote while loading stays, for its first job.
            if h.handed {
                h.logs = Logs::new();
            }
            h.handed = true;
            h.job = Some(id.to_string());
        }
        let line = format!("{}\n", serde_json::json!({"job": id, "folder": jd.dir.display().to_string()}));
        if line.len() > LINE_MAX {
            self.proc.kill();
            return Outcome::NotTaken(-1);
        }
        let written = match self.stdin.as_mut() {
            Some(s) => s.write_all(line.as_bytes()).and_then(|_| s.flush()).is_ok(),
            None => false,
        };
        if !written {
            self.proc.kill();
            return Outcome::NotTaken(self.exited.unwrap_or(-1));
        }
        let mut tick = tokio::time::interval(Duration::from_secs(2));
        let mut last_progress = None;
        let mut closed_at: Option<tokio::time::Instant> = None;
        loop {
            let eof_wait = closed_at.map(|t| t + Duration::from_secs(STOP_WAIT_S));
            tokio::select! {
                e = self.events.recv() => match e {
                    Some(Event::Done(done)) if done == id => {
                        if let Ok(mut h) = self.heard.lock() {
                            h.job = None;
                        }
                        self.jobs += 1;
                        self.last = Instant::now();
                        return Outcome::Done;
                    }
                    Some(Event::Done(_)) => {}
                    Some(Event::Eof) => closed_at = Some(tokio::time::Instant::now()),
                    Some(Event::Exited(code)) => {
                        self.exited = Some(code);
                        let unread = self.stdin.as_ref().and_then(sys::unread).unwrap_or(0);
                        return if unread > 0 { Outcome::NotTaken(code) } else { Outcome::Died(code) };
                    }
                    None => return Outcome::Died(-1),
                },
                _ = tokio::time::sleep_until(eof_wait.unwrap_or(deadline)), if eof_wait.is_some() => {
                    // It closed its stdout and lives on: it can never say a job is done.
                    self.proc.kill();
                    return Outcome::Died(-1);
                }
                _ = tokio::time::sleep_until(deadline) => {
                    self.proc.kill();
                    return Outcome::TimedOut;
                }
                changed = cancel.changed() => {
                    if changed.is_err() || *cancel.borrow() {
                        self.proc.kill();
                        return Outcome::Cancelled;
                    }
                }
                _ = tick.tick() => {
                    let now = exec::read_progress(jd);
                    if now.is_some() && now != last_progress {
                        last_progress = now;
                        progress(now.unwrap_or(0));
                    }
                }
            }
        }
    }

    /// How the job ended, as a cold job's end is told.
    fn ended(&self, exit: i64, timed_out: bool, cancelled: bool, ran: Duration, broke: Option<String>) -> Ended {
        let h = self.heard.lock();
        let (stdout, stderr, log) = match &h {
            Ok(h) => (h.logs.out.text(), h.logs.err.text(), h.logs.all.text()),
            Err(_) => Default::default(),
        };
        Ended { pid: self.pid, exit, timed_out, cancelled, stdout, stderr, log, ran, broke }
    }

    /// Asks it to end (its stdin closed), kills it after STOP_WAIT_S, removes its folder.
    async fn stop(mut self) {
        self.stdin = None;
        let ended = |w: &mut Warm| {
            let _ = w.alive();
            w.exited.is_some()
        };
        let until = tokio::time::Instant::now() + Duration::from_secs(STOP_WAIT_S);
        while !ended(&mut self) && tokio::time::Instant::now() < until {
            tokio::time::sleep(Duration::from_millis(200)).await;
        }
        if !ended(&mut self) {
            self.proc.kill();
            let until = tokio::time::Instant::now() + Duration::from_secs(STOP_WAIT_S);
            while !ended(&mut self) && tokio::time::Instant::now() < until {
                tokio::time::sleep(Duration::from_millis(200)).await;
            }
        }
        let folder = self.folder.clone();
        let _ = tokio::task::spawn_blocking(move || sys::remove_job_dir(&folder)).await;
    }

    fn shown(&self) -> String {
        let key = if self.key.is_empty() { "alone" } else { &self.key[..12.min(self.key.len())] };
        format!("{} ({key}, process {}, {} job(s))", self.service, self.pid, self.jobs)
    }
}

/// The server's stdout: lines, each at most LINE_MAX bytes; the one that ends the job now
/// (exactly `{"done": "<job>"}`) is an event, anything else the job's log.
fn read_out(pipe: &mut Box<dyn Read + Send>, heard: &Arc<Mutex<Heard>>, tx: &mpsc::UnboundedSender<Event>) {
    let (mut buf, mut pending, mut overlong) = ([0u8; 8192], Vec::<u8>::new(), false);
    let output = |data: &[u8]| {
        if let Ok(mut h) = heard.lock() {
            h.logs.all.push(data);
            h.logs.out.push(data);
        }
    };
    loop {
        let n = match pipe.read(&mut buf) {
            Ok(0) | Err(_) => break,
            Ok(n) => n,
        };
        pending.extend_from_slice(&buf[..n]);
        while let Some(end) = pending.iter().position(|b| *b == b'\n') {
            let line: Vec<u8> = pending.drain(..=end).collect();
            let body = &line[..line.len() - 1];
            let now = heard.lock().ok().and_then(|h| h.job.clone());
            match now.filter(|job| !overlong && is_done(body, job)) {
                Some(job) => {
                    let _ = tx.send(Event::Done(job));
                }
                None => output(&line),
            }
            overlong = false;
        }
        if pending.len() > LINE_MAX {
            output(&pending);
            pending.clear();
            overlong = true;
        }
    }
    if !pending.is_empty() {
        output(&pending);
    }
    let _ = tx.send(Event::Eof);
}

fn is_done(line: &[u8], job: &str) -> bool {
    if line.len() > LINE_MAX || !line.starts_with(b"{") {
        return false;
    }
    match serde_json::from_slice::<Value>(line) {
        Ok(Value::Object(m)) => m.len() == 1 && m.get("done").and_then(Value::as_str) == Some(job),
        _ => false,
    }
}

/// Runs the job through a server of the service: a warm one of `key`, or one started for it.
/// Gives back how it ended and how it was served ("warm", "cold", "alone" or "command").
#[allow(clippy::too_many_arguments)]
pub async fn execute(
    pool: &Shared,
    key: Option<String>,
    jd: &JobDir,
    id: &str,
    svc: &Service,
    service: &str,
    limits: &Limits,
    server: bool,
    node_dir: &Path,
    jobs_root: &Path,
    as_node: bool,
    mut cancel: watch::Receiver<bool>,
    mut progress: impl FnMut(u8),
) -> Result<(Ended, &'static str), String> {
    let started = Instant::now();
    let deadline = tokio::time::Instant::now() + Duration::from_secs(limits.max_runtime_s);
    let mut fresh = false;
    loop {
        let reused = key.as_deref().and_then(|k| pool.lock().ok().and_then(|mut p| p.take(k)));
        let (mut warm, how, held) = match reused {
            Some(w) => (w, "warm", true),
            None => {
                let (gone, fits) = match (&key, pool.lock()) {
                    (Some(_), Ok(mut p)) => p.room(limits.ram_gb),
                    _ => (Vec::new(), false),
                };
                for w in gone {
                    info!("warm server of {} stopped to make room", w.shown());
                    tokio::spawn(w.stop());
                }
                fresh = true;
                let (k, s, l, nd, jr) = (key.clone().unwrap_or_default(), svc.clone(), limits.clone(), node_dir.to_path_buf(), jobs_root.to_path_buf());
                let made = tokio::task::spawn_blocking(move || Warm::start(k, &s, &l, server, &nd, &jr, as_node)).await.map_err(|e| e.to_string())?;
                match made {
                    Ok(mut w) => {
                        w.service = service.to_string();
                        info!("server of {} started for job {id}{}", w.shown(), if fits { "" } else { " (no room to keep it warm: it serves this job alone)" });
                        (w, if fits { "cold" } else { "alone" }, fits)
                    }
                    Err(e) => {
                        if fits {
                            if let Ok(mut p) = pool.lock() {
                                p.give_back(None, limits.ram_gb);
                            }
                        }
                        return Err(format!("the server could not start: {e}"));
                    }
                }
            }
        };
        let outcome = warm.handle(jd, id, deadline, &mut cancel, &mut progress).await;
        let ran = started.elapsed();
        let release = |w: Option<Warm>| {
            if held {
                if let Ok(mut p) = pool.lock() {
                    p.give_back(w, limits.ram_gb);
                }
            }
        };
        match outcome {
            Outcome::Done => {
                let ended = warm.ended(0, false, false, ran, None);
                if held && !warm.spent() {
                    release(Some(warm));
                } else {
                    release(None);
                    tokio::spawn(warm.stop());
                }
                return Ok((ended, how));
            }
            Outcome::TimedOut | Outcome::Cancelled => {
                let timed_out = matches!(outcome, Outcome::TimedOut);
                let ended = warm.ended(-1, timed_out, !timed_out, ran, None);
                release(None);
                tokio::spawn(warm.stop());
                return Ok((ended, how));
            }
            Outcome::Died(code) => {
                let broke = format!("the server ended while it did the job (exit code {code}); the next job starts a fresh one");
                warn!("job {id}: {broke}");
                let ended = warm.ended(code, false, false, ran, Some(broke));
                release(None);
                tokio::spawn(warm.stop());
                return Ok((ended, how));
            }
            Outcome::NotTaken(code) => {
                warn!("job {id}: the server of {} ended (exit code {code}) before it took the job", warm.shown());
                release(None);
                let ended = warm.ended(code, false, false, ran, Some(format!(
                    "the server ended before it took the job (exit code {code}): it fails while it loads"
                )));
                tokio::spawn(warm.stop());
                if *cancel.borrow() {
                    return Ok((Ended { cancelled: true, broke: None, ..ended }, how));
                }
                if !fresh {
                    continue; // a warm one that ended while it waited: a fresh one
                }
                if svc.command.trim().is_empty() {
                    return Ok((ended, how));
                }
                // The service's own command, cold, in the time left.
                let left = deadline.saturating_duration_since(tokio::time::Instant::now()).as_secs().max(1);
                let cold = Limits { max_runtime_s: left, ..limits.clone() };
                info!("job {id}: run as the service's command instead");
                let mut ended = exec::execute(jd, svc, &cold, server, node_dir, as_node, cancel, progress).await?;
                ended.ran = started.elapsed();
                return Ok((ended, "command"));
            }
        }
    }
}

/// The reaper: stops what `sweep` finds, every 15 s, until the node stops.
pub async fn reap(pool: Shared, node_dir: PathBuf, mut stop: watch::Receiver<bool>) {
    let mut every = tokio::time::interval(Duration::from_secs(15));
    loop {
        tokio::select! {
            _ = every.tick() => {}
            _ = stop.changed() => return,
        }
        let services = Services::load(&node_dir);
        let gone = pool.lock().map(|mut p| p.sweep(&services)).unwrap_or_default();
        for w in gone {
            info!("warm server of {} stopped (idle, spent, ended or its service changed)", w.shown());
            w.stop().await;
        }
    }
}

/// Every idle process, when the node stops.
pub async fn stop_all(pool: &Shared) {
    let all: Vec<Warm> = pool.lock().map(|mut p| std::mem::take(&mut p.idle)).unwrap_or_default();
    for w in all {
        w.stop().await;
    }
}

/// The folders of servers an earlier run of the node left (it was stopped at once).
pub fn clean_left(jobs_root: &Path) {
    let Ok(rd) = std::fs::read_dir(jobs_root) else { return };
    for e in rd.flatten().take(10_000) {
        let name = e.file_name().to_string_lossy().into_owned();
        if name.starts_with(FOLDER_PREFIX) && e.file_type().is_ok_and(|t| t.is_dir()) {
            sys::remove_job_dir(&e.path());
        }
    }
}

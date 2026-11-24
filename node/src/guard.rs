//! Checks made as the job account itself, through the same spawn path as a job: whether it
//! can write where the node keeps what it trusts (its program, its folders, `bin`, a
//! switched-on service's folder), and which programs it can reach.

use std::io::Read;
use std::path::{Path, PathBuf};
use std::sync::mpsc;
use std::time::Duration;

use crate::config::{self, Services};
use crate::exec::Launch;
use crate::{sys, util};

#[derive(Clone, Copy, PartialEq)]
pub enum Ask {
    /// Can it create (then delete) a file in this folder?
    Write,
    /// Can it see (and run) this file?
    See,
}

const YES: &str = "AMSYOS-YES";
const DONE: &str = "AMSYOS-DONE";
/// How long the clauses of one check process may be: with cmd's path, `/d /s /c` and the
/// variables a job is given, the whole line stays well under Windows' 1024.
#[cfg(windows)]
const BATCH_MAX: usize = 520;

/// TODO(review): isolation — one clause of the check cmd runs as the job account. A path cmd
/// would not take as text (`%`, `"`) is never put in a command: it is answered "unknown".
#[cfg(windows)]
fn clause(i: usize, path: &Path, ask: Ask, tag: &str) -> Option<String> {
    let s = path.display().to_string();
    if s.is_empty() || s.chars().any(|c| matches!(c, '"' | '%' | '\n' | '\r')) {
        return None;
    }
    Some(match ask {
        Ask::Write => {
            let f = format!("{}\\.amsyos-probe-{tag}", s.trim_end_matches('\\'));
            format!("((type nul > \"{f}\") 2>nul && (del /f /q \"{f}\" 2>nul & echo {YES} {i}))")
        }
        Ask::See => format!("(if exist \"{s}\" echo {YES} {i})"),
    })
}

#[cfg(unix)]
fn quote(s: &str) -> String {
    format!("'{}'", s.replace('\'', "'\\''"))
}

/// Runs a launch to its end (at most `secs`) and gives back its exit code and stdout.
fn run_once(l: &Launch, secs: u64) -> Result<(i64, String, String), String> {
    let s = sys::spawn(l)?;
    let (out, err) = (s.stdout, s.stderr);
    let read = |pipe: Box<dyn Read + Send>| {
        std::thread::spawn(move || {
            let mut v = Vec::new();
            let _ = pipe.take(1 << 20).read_to_end(&mut v);
            String::from_utf8_lossy(&v).into_owned()
        })
    };
    let (ro, re) = (read(out), read(err));
    let proc = s.proc.clone();
    let (tx, rx) = mpsc::channel();
    std::thread::spawn(move || {
        let _ = tx.send(proc.wait());
    });
    let code = match rx.recv_timeout(Duration::from_secs(secs)) {
        Ok(c) => c,
        Err(_) => {
            s.proc.kill();
            let _ = rx.recv_timeout(Duration::from_secs(10));
            return Err(format!("the check as {} did not end within {secs} s", sys::JOB_USER));
        }
    };
    Ok((code, ro.join().unwrap_or_default(), re.join().unwrap_or_default()))
}

/// TODO(review): isolation — asks the job account itself about each path, by starting one
/// short process as it (the same `sys::spawn` as a job). `None`: the path was not asked.
/// The check fails closed: without its last line the answer is an error, not a "no".
pub fn ask(paths: &[PathBuf], ask: Ask, node_dir: &Path, jobs_root: &Path) -> Result<Vec<Option<bool>>, String> {
    let mut answers: Vec<Option<bool>> = vec![None; paths.len()];
    if paths.is_empty() {
        return Ok(answers);
    }
    let tag = util::hex(&util::random_bytes::<6>());
    // Windows starts another account's process with a command line of at most 1024
    // characters (CreateProcessWithLogonW): the clauses go in batches, one process each.
    #[cfg(windows)]
    let commands: Vec<String> = {
        let mut batches: Vec<Vec<String>> = vec![Vec::new()];
        let mut used = 0;
        for (i, p) in paths.iter().enumerate().take(64) {
            if let Some(c) = clause(i, p, ask, &tag) {
                if used + c.len() > BATCH_MAX && !batches.last().is_some_and(Vec::is_empty) {
                    batches.push(Vec::new());
                    used = 0;
                }
                used += c.len() + 3;
                batches.last_mut().expect("one batch at least").push(c);
                answers[i] = Some(false);
            }
        }
        batches
            .into_iter()
            .map(|mut parts| {
                parts.push(format!("echo {DONE}"));
                format!("({})", parts.join(" & "))
            })
            .collect()
    };
    #[cfg(unix)]
    let commands: Vec<String> = {
        let _ = &tag;
        answers.iter_mut().for_each(|a| *a = Some(false));
        let what = if ask == Ask::Write { "write" } else { "see" };
        let list: Vec<String> = paths.iter().map(|p| quote(&p.to_string_lossy())).collect();
        vec![format!("{} probe {what} {}", quote(&config::job_bin().to_string_lossy()), list.join(" "))]
    };
    for command in commands {
        let launch = Launch {
            job_dir: jobs_root.to_path_buf(),
            service_dir: sys::neutral_dir(),
            command,
            cpu: 1.0,
            ram_gb: 1.0,
            max_runtime_s: 60,
            server: true,
            node_dir: node_dir.to_path_buf(),
            as_node: false,
            serve_state: None,
        };
        let (code, out, err) = run_once(&launch, 90)?;
        if !out.lines().any(|l| l.trim() == DONE) {
            return Err(format!(
                "the check as {} did not run (exit {code}): {}",
                sys::JOB_USER,
                util::tail(err.trim(), 300)
            ));
        }
        for line in out.lines() {
            let n = line.trim().strip_prefix(YES).and_then(|r| r.trim().parse::<usize>().ok());
            if let Some(i) = n.filter(|i| *i < answers.len()) {
                if answers[i].is_some() {
                    answers[i] = Some(true);
                }
            }
        }
    }
    Ok(answers)
}

fn setup_again() -> String {
    if cfg!(windows) {
        "run `amsyos-node setup` again, in an administrator window".into()
    } else {
        "run `sudo amsyos-node setup` again".into()
    }
}

fn exe_fix() -> String {
    #[cfg(windows)]
    {
        format!(
            "the folder of this program: run the installed copy \"{}\" (amsyos-node setup puts it there)",
            config::installed_exe().display()
        )
    }
    #[cfg(unix)]
    {
        "the folder of this program: put it where only root can write, e.g. `sudo install -m 755 amsyos-node /usr/local/bin/`".into()
    }
}

pub fn service_fix(id: &str, dir: &Path) -> String {
    let d = dir.display();
    if cfg!(windows) {
        format!("the folder of {id}: switch it on again, which protects it: amsyos-node enable {id} --dir \"{d}\" --command \"…\"")
    } else {
        format!("the folder of {id}: make it writable only by you (chmod -R go-w '{d}'), then amsyos-node enable {id} --dir '{d}' --command '…' again")
    }
}

/// TODO(review): isolation — refuses when the job account can write to any of `places`
/// (a folder, and how to fix it), naming each one.
pub fn check(places: &[(PathBuf, String)], node_dir: &Path, jobs_root: &Path) -> Result<(), String> {
    let paths: Vec<PathBuf> = places.iter().map(|p| p.0.clone()).collect();
    let answers = ask(&paths, Ask::Write, node_dir, jobs_root)?;
    let bad: Vec<String> = places
        .iter()
        .zip(&answers)
        .filter_map(|((p, fix), a)| match a {
            Some(false) => None,
            Some(true) => Some(format!("{}: {fix}", p.display())),
            None => Some(format!("{}: cannot be checked (a % or \" in its name): rename it", p.display())),
        })
        .collect();
    if bad.is_empty() {
        return Ok(());
    }
    Err(format!(
        "the job account {} can write to a folder the node trusts, so a job could change what runs here:\n  {}",
        sys::JOB_USER,
        bad.join("\n  ")
    ))
}

/// TODO(review): isolation — before `run` goes online and in `try`: the job account must not
/// write to this program's folder, `bin`, the node's own folder, the jobs folder (itself)
/// or the folder of any switched-on service.
pub fn guard(node_dir: &Path, jobs_root: &Path, services: &Services) -> Result<(), String> {
    let mut places: Vec<(PathBuf, String)> = Vec::new();
    if let Some(d) = std::env::current_exe().ok().and_then(|e| e.parent().map(Path::to_path_buf)) {
        places.push((d, exe_fix()));
    }
    places.push((config::bin_dir(), format!("`bin`: {}", setup_again())));
    places.push((node_dir.to_path_buf(), format!("the node's own folder: {}", setup_again())));
    places.push((jobs_root.to_path_buf(), format!("the jobs folder: {}", setup_again())));
    let managed = config::Config::load(node_dir).services_root();
    places.push((managed, format!("the managed services folder: {}", setup_again())));
    for (id, s) in &services.services {
        places.push((s.dir.clone(), service_fix(id, &s.dir)));
    }
    let mut unique: Vec<(PathBuf, String)> = Vec::new();
    for (p, fix) in places {
        if p.is_dir() && !unique.iter().any(|(q, _)| sys::same_path(q, &p)) {
            unique.push((p, fix));
        }
    }
    check(&unique, node_dir, jobs_root)
}

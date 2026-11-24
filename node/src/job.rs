//! `amsyos-job`: the job contract (sdk/CONTRACT.md §3) for any language, the same commands as
//! `sdk/bin/amsyos-job`. It runs inside a job, as the job account: it reads and writes the
//! job's folder (`$AMSYOS_JOB`) only and never touches the node's own folder.
//!
//! On Linux the same program also starts a job once it runs as the job account (`launch`,
//! started by the node through sudo), checks a folder for it (`can-read`) and empties a job
//! folder the node's user could not (`clean`).
//!
//! Node 2.1.2: a server kept warm (serve.rs) loops on `next` … `done` in any language, as on a
//! rented node (sdk/bin/amsyos-job): `next` and `done` find the server's stdin and stdout
//! through $AMSYOS_SERVE_PID where the machine has /proc (from any child of the server), else
//! they are this command's own; the job now is kept in $AMSYOS_SERVE_STATE.

use std::io::{Read, Write};
#[cfg(unix)]
use std::path::Path;
use std::path::PathBuf;

use serde_json::Value;

const USAGE: &str = "\
    amsyos-job params                  all the params, as JSON (defaults filled in)
    amsyos-job param <name> [--raw]    one param as JSON (null when absent); --raw: a
                                       string as it is, anything else as JSON
    amsyos-job input <name>            the path of the input file, or nothing
    amsyos-job service                 the id of the service the job is for, or nothing
    amsyos-job service-version         that service's version, or nothing
    amsyos-job output <name> [.ext]    the path to write the output <name> to
    amsyos-job progress <0-100>        how far the job is
    amsyos-job fail <hint> [code]      ends the job as failed (writes error.json, exits 1);
                                       code: INVALID_PARAMS (default) or SERVICE_ERROR
    amsyos-job next                    a server: waits for the next job, makes it the one
                                       the commands above are about, prints its folder;
                                       exits 1 when the node ends the server
    amsyos-job done                    a server: the job now is done (after fail too)";

/// A line of the server's protocol, each way (sdk/CONTRACT.md §3).
pub const LINE_MAX: usize = 4096;

fn wrong(why: &str) -> i32 {
    eprintln!("amsyos-job: {why}\n{USAGE}");
    2
}

/// The job's folder: $AMSYOS_JOB (a job started as a command), else the job a server took
/// last with `next`, else the folder it runs in.
fn folder() -> PathBuf {
    let raw = std::env::var_os("AMSYOS_JOB")
        .filter(|v| !v.is_empty())
        .map(PathBuf::from)
        .or_else(|| serving_now().map(|(_, f)| PathBuf::from(f)))
        .unwrap_or_else(|| PathBuf::from("."));
    // Not canonicalize: on Windows that gives \\?\ paths some programs cannot use.
    std::path::absolute(&raw).unwrap_or(raw)
}

fn serve_state() -> Option<PathBuf> {
    std::env::var_os("AMSYOS_SERVE_STATE").filter(|v| !v.is_empty()).map(PathBuf::from)
}

/// The job a server took last (`next`): its id and folder.
fn serving_now() -> Option<(String, String)> {
    let body = std::fs::read(serve_state()?).ok()?;
    let v: Value = serde_json::from_slice(&body).ok()?;
    Some((v.get("job")?.as_str()?.to_string(), v.get("folder")?.as_str()?.to_string()))
}

/// The job and folder a line of the node names, or None for anything else.
pub fn job_line(line: &[u8]) -> Option<(String, String)> {
    if line.len() > LINE_MAX {
        return None;
    }
    let v: Value = serde_json::from_slice(line).ok()?;
    let job = v.get("job")?.as_str()?;
    let folder = v.get("folder")?.as_str()?;
    let ok = (1..=64).contains(&job.len()) && job.chars().all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-');
    ok.then(|| (job.to_string(), folder.to_string()))
}

/// The server's own stdin (`write` false) or stdout, however this command was started.
fn channel(write: bool) -> Result<std::fs::File, String> {
    let what = if write { "stdout" } else { "stdin" };
    #[cfg(unix)]
    {
        let pid = std::env::var("AMSYOS_SERVE_PID").unwrap_or_default();
        if !pid.is_empty() && pid.chars().all(|c| c.is_ascii_digit()) && Path::new("/proc/self").exists() {
            let path = format!("/proc/{pid}/fd/{}", u8::from(write));
            let opened = if write {
                std::fs::OpenOptions::new().write(true).open(&path)
            } else {
                std::fs::File::open(&path)
            };
            return opened.map_err(|e| format!("cannot open the server's {what}: {e}"));
        }
        use std::os::fd::AsFd;
        let owned = if write { std::io::stdout().as_fd().try_clone_to_owned() } else { std::io::stdin().as_fd().try_clone_to_owned() };
        owned.map(std::fs::File::from).map_err(|e| format!("cannot use {what}: {e}"))
    }
    #[cfg(windows)]
    {
        use std::os::windows::io::AsHandle;
        let owned = if write { std::io::stdout().as_handle().try_clone_to_owned() } else { std::io::stdin().as_handle().try_clone_to_owned() };
        owned.map(std::fs::File::from).map_err(|e| format!("cannot use {what}: {e}"))
    }
}

/// `next`: one line of the node's, byte by byte (nothing after it is taken).
fn take_next() -> Result<i32, String> {
    let Some(state) = serve_state() else {
        return Ok(wrong("next: only a server (a service switched on with serve) takes jobs"));
    };
    let mut given = channel(false)?;
    let (job, folder) = loop {
        let (mut line, mut byte, mut ended) = (Vec::new(), [0u8; 1], false);
        loop {
            match given.read(&mut byte) {
                Ok(0) => {
                    ended = true;
                    break;
                }
                Ok(_) if byte[0] == b'\n' => break,
                Ok(_) => {
                    if line.len() <= LINE_MAX {
                        line.push(byte[0]);
                    }
                }
                Err(e) if e.kind() == std::io::ErrorKind::Interrupted => {}
                Err(_) => {
                    ended = true;
                    break;
                }
            }
        }
        if let Some(found) = job_line(&line) {
            break found;
        }
        if ended {
            return Ok(1); // the node ended the server
        }
    };
    let fresh = state.with_extension("new");
    let body = serde_json::json!({"job": job, "folder": folder}).to_string();
    std::fs::write(&fresh, body).map_err(|e| format!("cannot write {}: {e}", fresh.display()))?;
    std::fs::rename(&fresh, &state).map_err(|e| format!("cannot write {}: {e}", state.display()))?;
    println!("{folder}");
    Ok(0)
}

/// `done`: the line that ends the job now, on the server's stdout.
fn say_done() -> Result<i32, String> {
    let Some((job, _)) = serving_now() else { return Ok(wrong("done: no job was taken with next")) };
    let mut said = channel(true)?;
    said.write_all(format!("{}\n", serde_json::json!({"done": job})).as_bytes())
        .map_err(|e| format!("cannot say the job is done: {e}"))?;
    if let Some(state) = serve_state() {
        let _ = std::fs::remove_file(state);
    }
    Ok(0)
}

fn told() -> Result<Value, String> {
    let path = folder().join("job.json");
    let body = std::fs::read(&path).map_err(|e| format!("cannot read {}: {e}", path.display()))?;
    serde_json::from_slice(&body).map_err(|e| format!("{} is not JSON: {e}", path.display()))
}

/// The id and version of the service the job is for: `service` is {"id", "version"}; an
/// older node wrote the id alone, with `version` beside it.
fn service_of(job: &Value) -> (Option<String>, Option<String>) {
    let text = |v: Option<&Value>| v.and_then(Value::as_str).filter(|s| !s.is_empty()).map(String::from);
    match job.get("service") {
        Some(Value::Object(o)) => (text(o.get("id")), text(o.get("version"))),
        Some(id) if id.is_string() => (text(Some(id)), text(job.get("version"))),
        _ => (None, None),
    }
}

fn plain(name: &str) -> bool {
    !name.is_empty() && !name.contains(['/', '\\', ':']) && name != "." && name != ".."
}

pub fn main(args: &[String]) -> i32 {
    match run(args) {
        Ok(code) => code,
        Err(e) => {
            eprintln!("amsyos-job: {e}");
            1
        }
    }
}

fn run(args: &[String]) -> Result<i32, String> {
    let Some(what) = args.first() else { return Ok(wrong("say what to do")) };
    let rest = &args[1..];
    match (what.as_str(), rest.len()) {
        ("params", 0) => {
            let params = told()?.get("params").cloned().unwrap_or(Value::Null);
            let params = if params.is_null() { Value::Object(Default::default()) } else { params };
            println!("{params}");
        }
        ("param", 1) | ("param", 2) if rest.len() == 1 || rest[1] == "--raw" => {
            let value = told()?.get("params").and_then(|p| p.get(&rest[0])).cloned().unwrap_or(Value::Null);
            match (&value, rest.len() == 2) {
                (Value::String(s), true) => println!("{s}"),
                _ => println!("{value}"),
            }
        }
        ("input", 1) => {
            let job = told()?;
            if let Some(rel) = job.get("inputs").and_then(|i| i.get(&rest[0])).and_then(Value::as_str) {
                println!("{}", folder().join(rel.replace('/', std::path::MAIN_SEPARATOR_STR)).display());
            }
        }
        ("service", 0) | ("service-version", 0) => {
            let (id, version) = service_of(&told()?);
            if let Some(said) = if what == "service" { id } else { version } {
                println!("{said}");
            }
        }
        ("output", 1) | ("output", 2) => {
            if !plain(&rest[0]) {
                return Ok(wrong("output: a name such as report"));
            }
            let mut suffix = rest.get(1).cloned().unwrap_or_default();
            if !suffix.is_empty() && !suffix.starts_with('.') {
                suffix.insert(0, '.');
            }
            if suffix.len() > 1 && !plain(&suffix[1..]) {
                return Ok(wrong("output: an extension such as .json"));
            }
            let out = folder().join("out");
            std::fs::create_dir_all(&out).map_err(|e| format!("cannot make {}: {e}", out.display()))?;
            println!("{}", out.join(format!("{}{suffix}", rest[0])).display());
        }
        ("progress", 1) => {
            let Ok(percent) = rest[0].trim().parse::<f64>() else {
                return Ok(wrong(&format!("progress: a number from 0 to 100, not {:?}", crate::util::cut(&rest[0], 20))));
            };
            let percent = if percent.is_nan() { 0.0 } else { percent.clamp(0.0, 100.0) };
            let path = folder().join("progress");
            std::fs::write(&path, format!("{percent:.0}")).map_err(|e| format!("cannot write {}: {e}", path.display()))?;
        }
        ("fail", 1) | ("fail", 2) => {
            let code = rest.get(1).map(String::as_str).unwrap_or("INVALID_PARAMS");
            if code != "INVALID_PARAMS" && code != "SERVICE_ERROR" {
                return Ok(wrong("fail: the code is INVALID_PARAMS or SERVICE_ERROR"));
            }
            let path = folder().join("error.json");
            let body = serde_json::json!({"error": code, "hint": rest[0]}).to_string();
            std::fs::write(&path, body).map_err(|e| format!("cannot write {}: {e}", path.display()))?;
            return Ok(1);
        }
        #[cfg(unix)]
        ("launch", _) => return unix::launch(rest),
        #[cfg(unix)]
        ("can-read", 1) => return Ok(unix::can_read(Path::new(&rest[0]))),
        #[cfg(unix)]
        ("probe", n) if n >= 1 => return Ok(unix::probe(&rest[0], &rest[1..])),
        #[cfg(unix)]
        ("clean", 1) => return Ok(unix::clean(Path::new(&rest[0]))),
        ("next", 0) => return take_next(),
        ("done", 0) => return say_done(),
        _ => return Ok(wrong(&format!("not understood: {:?}", crate::util::cut(&args.join(" "), 80)))),
    }
    Ok(0)
}

#[cfg(unix)]
mod unix {
    //! TODO(review): isolation — this runs as the job account (through sudo, started by the
    //! node) and sets what holds the job: no new privileges, umask 002, nice, rlimits, its
    //! own process group that is killed whole, a clean environment.
    use std::ffi::CString;
    use std::os::unix::process::CommandExt;
    use std::path::Path;
    use std::process::{Command, Stdio};
    use std::sync::atomic::{AtomicI32, Ordering};

    static CHILD_GROUP: AtomicI32 = AtomicI32::new(0);

    extern "C" fn stop(_sig: libc::c_int) {
        let group = CHILD_GROUP.load(Ordering::SeqCst);
        unsafe {
            if group > 0 {
                libc::kill(-group, libc::SIGKILL);
            }
            libc::_exit(143);
        }
    }

    fn value<'a>(args: &'a [String], name: &str) -> Option<&'a str> {
        args.iter().position(|a| a == name).and_then(|i| args.get(i + 1)).map(String::as_str)
    }

    /// `launch --job <folder> --dir <service folder> --bin <dir> --nice <n> --mem <bytes>
    /// --cpu-s <seconds> [--serve <state file>] -- <command line>` (`--as` is the old name of
    /// `--mem`; `--serve`: a server kept warm, node 2.1.2)
    pub fn launch(args: &[String]) -> Result<i32, String> {
        let split = args.iter().position(|a| a == "--").ok_or("launch: -- <command> is missing")?;
        let (opts, command) = (&args[..split], args[split + 1..].join(" "));
        let job = value(opts, "--job").ok_or("launch: --job")?;
        let dir = value(opts, "--dir").ok_or("launch: --dir")?;
        let bin = value(opts, "--bin").unwrap_or("/usr/local/lib/amsyos-node");
        let nice: i32 = value(opts, "--nice").and_then(|v| v.parse().ok()).unwrap_or(0);
        let mem: u64 = value(opts, "--mem").or_else(|| value(opts, "--as")).and_then(|v| v.parse().ok()).unwrap_or(0);
        let cpu_s: u64 = value(opts, "--cpu-s").and_then(|v| v.parse().ok()).unwrap_or(0);
        if command.trim().is_empty() {
            return Err("launch: the command is empty".into());
        }
        unsafe {
            // The job can never gain privileges (setuid programs, sudo) from here on, and dies
            // when the node's sudo goes away.
            libc::prctl(libc::PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0);
            libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGTERM, 0, 0, 0);
            libc::umask(0o002);
            if nice > 0 {
                libc::setpriority(libc::PRIO_PROCESS, 0, nice);
            }
            let limit = |res, soft: u64, hard: u64| {
                let r = libc::rlimit { rlim_cur: soft as libc::rlim_t, rlim_max: hard as libc::rlim_t };
                libc::setrlimit(res, &r);
            };
            limit(libc::RLIMIT_CORE, 0, 0);
            // The memory a process writes (RLIMIT_DATA: its heap and private writable maps),
            // not its address space: Chromium, V8, Go and Java reserve far more address
            // space than they ever use, and die under RLIMIT_AS (node 2.1.1).
            if mem > 0 {
                limit(libc::RLIMIT_DATA, mem, mem);
            }
            if cpu_s > 0 {
                limit(libc::RLIMIT_CPU, cpu_s, cpu_s + 5);
            }
            for sig in [libc::SIGTERM, libc::SIGINT, libc::SIGHUP] {
                libc::signal(sig, stop as *const () as libc::sighandler_t);
            }
        }
        if std::env::set_current_dir(dir).is_err() {
            eprintln!("amsyos-job: the job account cannot enter the service's folder {dir}");
            return Ok(126);
        }
        let mut cmd = Command::new("/bin/sh");
        cmd.arg("-c")
            .arg(&command)
            .env_clear()
            .env("PATH", format!("{bin}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"))
            .env("PYTHONPATH", format!("{bin}/python")) // node 2.1.2: `from amsyos import job`
            .env("HOME", job)
            .env("TMPDIR", format!("{job}/tmp"))
            .env("LANG", "C.UTF-8")
            .env("USER", "amsyos-job")
            .process_group(0);
        match value(opts, "--serve") {
            // Node 2.1.2: a server kept warm (serve.rs). `job` is its own folder, not a job's;
            // its stdin is the node's pipe of jobs, which `amsyos-job next` and `done` reach
            // from any child through this process (it holds the pipes as long as the server).
            Some(state) => {
                cmd.env("AMSYOS_SERVE_PID", std::process::id().to_string())
                    .env("AMSYOS_SERVE_STATE", state)
                    .stdin(Stdio::inherit());
            }
            None => {
                cmd.env("AMSYOS_JOB", job).stdin(Stdio::null());
            }
        }
        let mut child = cmd.spawn().map_err(|e| format!("cannot start /bin/sh: {e}"))?;
        CHILD_GROUP.store(child.id() as i32, Ordering::SeqCst);
        let status = child.wait().map_err(|e| e.to_string())?;
        // Whatever the job left running in its group ends with it.
        unsafe {
            libc::kill(-(child.id() as i32), libc::SIGKILL);
        }
        use std::os::unix::process::ExitStatusExt;
        Ok(status.code().unwrap_or_else(|| 128 + status.signal().unwrap_or(0)))
    }

    /// 0 when the job account may read and enter `dir`.
    pub fn can_read(dir: &Path) -> i32 {
        let Ok(c) = CString::new(dir.as_os_str().as_encoded_bytes()) else { return 1 };
        i32::from(unsafe { libc::access(c.as_ptr(), libc::R_OK | libc::X_OK) } != 0)
    }

    /// TODO(review): isolation — `probe write <folder>…`: can the job account create (then
    /// delete) a file in each folder? `probe see <file>…`: can it read and run each file?
    /// Prints `AMSYOS-YES <index>` for each yes, then `AMSYOS-DONE`.
    pub fn probe(what: &str, paths: &[String]) -> i32 {
        use std::os::unix::fs::OpenOptionsExt;
        for (i, p) in paths.iter().enumerate() {
            let yes = match what {
                "write" => {
                    let f = Path::new(p).join(format!(".amsyos-probe-{}-{i}", std::process::id()));
                    let made = std::fs::OpenOptions::new()
                        .write(true)
                        .create_new(true)
                        .custom_flags(libc::O_NOFOLLOW)
                        .mode(0o600)
                        .open(&f);
                    if made.is_ok() {
                        let _ = std::fs::remove_file(&f);
                    }
                    made.is_ok()
                }
                "see" => match CString::new(p.as_bytes()) {
                    Ok(c) => unsafe { libc::access(c.as_ptr(), libc::R_OK | libc::X_OK) == 0 },
                    Err(_) => false,
                },
                _ => return 2,
            };
            if yes {
                println!("AMSYOS-YES {i}");
            }
        }
        println!("AMSYOS-DONE");
        0
    }

    /// Empties a job folder (not the folder itself), as the job account: what the job made
    /// and closed to the node's user. Links are removed, never followed.
    pub fn clean(dir: &Path) -> i32 {
        let Ok(entries) = std::fs::read_dir(dir) else { return 1 };
        let mut code = 0;
        for entry in entries.flatten() {
            let path = entry.path();
            let gone = match entry.file_type() {
                Ok(t) if t.is_dir() => std::fs::remove_dir_all(&path),
                _ => std::fs::remove_file(&path),
            };
            if gone.is_err() {
                code = 1;
            }
        }
        code
    }
}

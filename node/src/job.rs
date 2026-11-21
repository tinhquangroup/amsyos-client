//! `amsyos-job`: the job contract (sdk/CONTRACT.md §3) for any language, the same commands as
//! `sdk/bin/amsyos-job`. It runs inside a job, as the job account: it reads and writes the
//! job's folder (`$AMSYOS_JOB`) only and never touches the node's own folder.
//!
//! On Linux the same program also starts a job once it runs as the job account (`launch`,
//! started by the node through sudo), checks a folder for it (`can-read`) and empties a job
//! folder the node's user could not (`clean`).

#[cfg(unix)]
use std::path::Path;
use std::path::PathBuf;

use serde_json::Value;

const USAGE: &str = "\
    amsyos-job params                  all the params, as JSON (defaults filled in)
    amsyos-job param <name> [--raw]    one param as JSON (null when absent); --raw: a
                                       string as it is, anything else as JSON
    amsyos-job input <name>            the path of the input file, or nothing
    amsyos-job output <name> [.ext]    the path to write the output <name> to
    amsyos-job progress <0-100>        how far the job is
    amsyos-job fail <hint> [code]      ends the job as failed (writes error.json, exits 1);
                                       code: INVALID_PARAMS (default) or SERVICE_ERROR";

fn wrong(why: &str) -> i32 {
    eprintln!("amsyos-job: {why}\n{USAGE}");
    2
}

fn folder() -> PathBuf {
    let raw = std::env::var_os("AMSYOS_JOB").map(PathBuf::from).unwrap_or_else(|| PathBuf::from("."));
    // Not canonicalize: on Windows that gives \\?\ paths some programs cannot use.
    std::path::absolute(&raw).unwrap_or(raw)
}

fn told() -> Result<Value, String> {
    let path = folder().join("job.json");
    let body = std::fs::read(&path).map_err(|e| format!("cannot read {}: {e}", path.display()))?;
    serde_json::from_slice(&body).map_err(|e| format!("{} is not JSON: {e}", path.display()))
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
        ("next", _) | ("done", _) => {
            return Ok(wrong("next/done: a server kept warm is not part of AMSYOS Node 2.0"));
        }
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

    /// `launch --job <folder> --dir <service folder> --bin <dir> --nice <n> --as <bytes>
    /// --cpu-s <seconds> -- <command line>`
    pub fn launch(args: &[String]) -> Result<i32, String> {
        let split = args.iter().position(|a| a == "--").ok_or("launch: -- <command> is missing")?;
        let (opts, command) = (&args[..split], args[split + 1..].join(" "));
        let job = value(opts, "--job").ok_or("launch: --job")?;
        let dir = value(opts, "--dir").ok_or("launch: --dir")?;
        let bin = value(opts, "--bin").unwrap_or("/usr/local/lib/amsyos-node");
        let nice: i32 = value(opts, "--nice").and_then(|v| v.parse().ok()).unwrap_or(0);
        let as_bytes: u64 = value(opts, "--as").and_then(|v| v.parse().ok()).unwrap_or(0);
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
            if as_bytes > 0 {
                limit(libc::RLIMIT_AS, as_bytes, as_bytes);
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
            .env("HOME", job)
            .env("TMPDIR", format!("{job}/tmp"))
            .env("AMSYOS_JOB", job)
            .env("LANG", "C.UTF-8")
            .env("USER", "amsyos-job")
            .stdin(Stdio::null())
            .process_group(0);
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

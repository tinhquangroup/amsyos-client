//! Small things every part uses: the log, time, random bytes, names checked by hand (no regex
//! crate, to keep the program small), files written whole.

use std::fs::OpenOptions;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

use base64::Engine;

static LOG_FILE: Mutex<Option<PathBuf>> = Mutex::new(None);
const LOG_MAX: u64 = 5 << 20; // node.log is moved to node.log.1 past 5 MB

/// From now on, log lines also go to `path` (the node's own folder, never a job's).
pub fn log_to(path: PathBuf) {
    if let Ok(mut file) = LOG_FILE.lock() {
        *file = Some(path);
    }
}

pub fn log_line(level: &str, msg: &str) {
    let line = format!("{} {level} {msg}\n", now_utc());
    let _ = std::io::stderr().write_all(line.as_bytes());
    let Ok(file) = LOG_FILE.lock() else { return };
    if let Some(path) = file.as_ref() {
        if std::fs::metadata(path).map(|m| m.len() > LOG_MAX).unwrap_or(false) {
            let _ = std::fs::rename(path, path.with_extension("log.1"));
        }
        if let Ok(mut f) = OpenOptions::new().create(true).append(true).open(path) {
            let _ = f.write_all(line.as_bytes());
        }
    }
}

#[macro_export]
macro_rules! info {
    ($($a:tt)*) => { $crate::util::log_line("INFO", &format!($($a)*)) };
}

#[macro_export]
macro_rules! warn {
    ($($a:tt)*) => { $crate::util::log_line("WARN", &format!($($a)*)) };
}

/// `2026-11-18T10:04:05Z`.
pub fn now_utc() -> String {
    let secs = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0);
    let (days, rest) = ((secs / 86400) as i64, secs % 86400);
    // Howard Hinnant's civil_from_days.
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = yoe + era * 400 + i64::from(m <= 2);
    format!(
        "{y:04}-{m:02}-{d:02}T{:02}:{:02}:{:02}Z",
        rest / 3600,
        (rest / 60) % 60,
        rest % 60
    )
}

pub fn random_bytes<const N: usize>() -> [u8; N] {
    let mut buf = [0u8; N];
    getrandom::fill(&mut buf).expect("the system's random source");
    buf
}

pub fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

pub fn b64url(bytes: &[u8]) -> String {
    base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(bytes)
}

/// At most `n` characters of `s`, from the start.
pub fn cut(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}

/// At most the last `n` characters of `s`.
pub fn tail(s: &str, n: usize) -> String {
    let count = s.chars().count();
    s.chars().skip(count.saturating_sub(n)).collect()
}

fn lower_alnum(c: char) -> bool {
    c.is_ascii_lowercase() || c.is_ascii_digit()
}

/// `^[a-z][a-z0-9_]{0,63}$`: an input or output name.
pub fn is_name(s: &str) -> bool {
    let mut chars = s.chars();
    matches!(chars.next(), Some(c) if c.is_ascii_lowercase())
        && s.len() <= 64
        && chars.all(|c| lower_alnum(c) || c == '_')
}

/// `^[a-z0-9]{1,11}$`: a file extension, without the dot.
pub fn is_ext(s: &str) -> bool {
    (1..=11).contains(&s.len()) && s.chars().all(lower_alnum)
}

/// `<name>` or `<name>.<ext>`, as `out/` and `in/` hold them. Returns the name.
pub fn file_stem_ok(s: &str) -> Option<&str> {
    match s.split_once('.') {
        Some((name, ext)) if is_name(name) && is_ext(ext) => Some(name),
        None if is_name(s) => Some(s),
        _ => None,
    }
}

/// `^in/[a-z][a-z0-9_]{0,63}(\.[a-z0-9]{1,11})?$` (sdk/NODE.md §2).
pub fn is_in_file(s: &str) -> bool {
    s.strip_prefix("in/").and_then(file_stem_ok).is_some()
}

/// `^t_[a-z0-9_]+(\.[a-z][a-z0-9_]*){1,4}$`, at most 200 characters.
pub fn is_service_id(s: &str) -> bool {
    if s.len() > 200 {
        return false;
    }
    let mut parts = s.split('.');
    let org = parts.next().unwrap_or("");
    let rest: Vec<&str> = parts.collect();
    let org_ok = org.len() > 2
        && org.starts_with("t_")
        && org[2..].chars().all(|c| lower_alnum(c) || c == '_');
    let rest_ok = (1..=4).contains(&rest.len())
        && rest.iter().all(|p| {
            let mut c = p.chars();
            matches!(c.next(), Some(f) if f.is_ascii_lowercase())
                && c.all(|x| lower_alnum(x) || x == '_')
        });
    org_ok && rest_ok
}

/// `^j_[A-Za-z0-9_-]{1,64}$`: a job id, also used as a folder name.
pub fn is_job_id(s: &str) -> bool {
    s.len() > 2
        && s.len() <= 66
        && s.starts_with("j_")
        && s[2..].chars().all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-')
}

/// `^n_[A-Za-z0-9_]{1,64}$`.
pub fn is_node_id(s: &str) -> bool {
    s.len() > 2
        && s.len() <= 66
        && s.starts_with("n_")
        && s[2..].chars().all(|c| c.is_ascii_alphanumeric() || c == '_')
}

/// `^t_[a-z0-9_]{1,64}$`.
pub fn is_org(s: &str) -> bool {
    s.len() > 2 && s.len() <= 66 && s.starts_with("t_") && s[2..].chars().all(|c| lower_alnum(c) || c == '_')
}

/// Writes a whole file through a temporary one beside it, so a reader never sees half of it.
/// `private`: readable by the node's user only (on Windows the node's folder already is).
pub fn write_file(path: &Path, data: &[u8], private: bool) -> std::io::Result<()> {
    let tmp = path.with_extension("tmp");
    {
        let mut opts = OpenOptions::new();
        opts.write(true).create(true).truncate(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            opts.mode(if private { 0o600 } else { 0o644 });
        }
        #[cfg(not(unix))]
        let _ = private;
        let mut f = opts.open(&tmp)?;
        f.write_all(data)?;
        f.sync_all()?;
    }
    std::fs::rename(&tmp, path)
}

/// Finds `name` (with `.exe` on Windows) on the PATH.
pub fn which(name: &str) -> Option<PathBuf> {
    let path = std::env::var_os("PATH")?;
    for dir in std::env::split_paths(&path) {
        let candidate = dir.join(if cfg!(windows) { format!("{name}.exe") } else { name.to_string() });
        if candidate.is_file() {
            return Some(candidate);
        }
    }
    None
}

/// Runs a program and gives back its stdout and stderr, or None if it did not end within
/// `secs` (it is then killed) or could not start. Used for probes and setup steps.
pub fn run_bounded(program: &Path, args: &[&str], secs: u64) -> Option<(i32, String, String)> {
    use std::io::Read;
    use std::process::{Command, Stdio};
    let mut cmd = Command::new(program);
    cmd.args(args).stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::piped());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x0800_0000); // CREATE_NO_WINDOW: no console flashes
    }
    let mut child = cmd.spawn().ok()?;
    let mut out = child.stdout.take()?;
    let mut err = child.stderr.take()?;
    let reader = std::thread::spawn(move || {
        let mut o = Vec::new();
        let _ = out.by_ref().take(1 << 20).read_to_end(&mut o);
        o
    });
    let reader_err = std::thread::spawn(move || {
        let mut e = Vec::new();
        let _ = err.by_ref().take(1 << 20).read_to_end(&mut e);
        e
    });
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(secs);
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break status,
            Ok(None) if std::time::Instant::now() < deadline => {
                std::thread::sleep(std::time::Duration::from_millis(30))
            }
            _ => {
                let _ = child.kill();
                let _ = child.wait();
                return None;
            }
        }
    };
    let o = reader.join().unwrap_or_default();
    let e = reader_err.join().unwrap_or_default();
    Some((
        status.code().unwrap_or(-1),
        String::from_utf8_lossy(&o).into_owned(),
        String::from_utf8_lossy(&e).into_owned(),
    ))
}

//! Linux: the job account `amsyos-job`, reached through sudo; limits through a systemd user
//! scope when that works, rlimits otherwise; what the machine has.

use std::fs::File;
use std::io;
use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::Duration;

use crate::config::{self, Config};
use crate::exec::Launch;
use crate::{info, util, warn};

pub const JOB_USER: &str = "amsyos-job";
const SUDOERS: &str = "/etc/sudoers.d/amsyos-node";
const SYS_PATH: &str = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin";

pub fn hostname() -> String {
    std::fs::read_to_string("/proc/sys/kernel/hostname").map(|s| s.trim().to_string()).unwrap_or_default()
}

pub fn ram_bytes() -> u64 {
    let info = std::fs::read_to_string("/proc/meminfo").unwrap_or_default();
    info.lines()
        .find_map(|l| l.strip_prefix("MemTotal:"))
        .and_then(|v| v.trim().trim_end_matches("kB").trim().parse::<u64>().ok())
        .map(|kb| kb * 1024)
        .unwrap_or(0)
}

pub fn disk_free_bytes(path: &Path) -> u64 {
    let Ok(c) = std::ffi::CString::new(path.as_os_str().as_encoded_bytes()) else { return 0 };
    let mut st: libc::statvfs = unsafe { std::mem::zeroed() };
    if unsafe { libc::statvfs(c.as_ptr(), &mut st) } != 0 {
        return 0;
    }
    st.f_bavail as u64 * st.f_frsize as u64
}

pub fn is_elevated() -> bool {
    unsafe { libc::geteuid() == 0 }
}

/// The node never runs as root (sdk/NODE.md §4).
pub fn refuse_identity(_allow_elevated: bool) -> Result<(), String> {
    if is_elevated() {
        return Err("the node never runs as root: start it as the user who ran `sudo amsyos-node setup`".into());
    }
    Ok(())
}

/// Linux gives no idle time a background program can read: null (sdk/NODE.md §2).
pub fn idle_seconds() -> Option<u64> {
    None
}

static LAST_CPU: Mutex<Option<(u64, u64)>> = Mutex::new(None);

/// The CPU use since the last call, 0 to 1.
pub fn cpu_load() -> Option<f64> {
    let stat = std::fs::read_to_string("/proc/stat").ok()?;
    let nums: Vec<u64> = stat.lines().next()?.split_whitespace().skip(1).filter_map(|v| v.parse().ok()).collect();
    if nums.len() < 4 {
        return None;
    }
    let total: u64 = nums.iter().take(8).sum();
    let idle = nums[3] + nums.get(4).copied().unwrap_or(0);
    let mut last = LAST_CPU.lock().ok()?;
    let (t0, i0) = last.replace((total, idle)).unwrap_or((0, 0));
    let (dt, di) = (total.saturating_sub(t0), idle.saturating_sub(i0));
    if dt == 0 {
        return Some(0.0);
    }
    Some(((dt - di.min(dt)) as f64 / dt as f64 * 100.0).round() / 100.0)
}

fn sudo_job(args: &[&str]) -> Command {
    let mut cmd = Command::new("sudo");
    cmd.args(["-n", "-u", JOB_USER, "--"]).args(args);
    cmd.env_clear().env("PATH", SYS_PATH).current_dir("/").stdin(Stdio::null());
    cmd
}

pub fn group_id(name: &str) -> Option<u32> {
    let groups = std::fs::read_to_string("/etc/group").ok()?;
    groups.lines().find_map(|l| {
        let mut f = l.split(':');
        (f.next()? == name).then(|| f.nth(1)?.parse().ok())?
    })
}

/// Before `run` or `try`: setup was done and sudo starts jobs as `amsyos-job` without asking.
pub fn preflight() -> Result<(), String> {
    let bin = config::job_bin();
    if !bin.is_file() {
        return Err(format!("{} is missing: run `sudo amsyos-node setup` first", bin.display()));
    }
    let wrapper = bin.to_string_lossy().into_owned();
    let ok = sudo_job(&[&wrapper, "can-read", "/"])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map(|s| s.success())
        .unwrap_or(false);
    if !ok {
        return Err(format!(
            "sudo cannot start {wrapper} as {JOB_USER} without a password: run `sudo amsyos-node setup` (again)"
        ));
    }
    if let Some(gid) = group_id(JOB_USER) {
        let mut groups = vec![0 as libc::gid_t; 256];
        let n = unsafe { libc::getgroups(groups.len() as i32, groups.as_mut_ptr()) };
        let member = n >= 0 && groups[..n as usize].contains(&gid) || unsafe { libc::getegid() } == gid;
        if !member {
            warn!("this session is not yet in the group {JOB_USER}: log in again, or the node may not read or remove what a job made");
        }
    }
    Ok(())
}

/// TODO(review): isolation — `enable` asks the job account itself whether it can read the
/// service's folder; the node never changes the permissions of a person's folder on Linux.
pub fn grant_dir(dir: &Path) -> Result<String, String> {
    let bin = config::job_bin();
    if !bin.is_file() {
        return Err("setup was not run yet, so the job account cannot be asked: run `sudo amsyos-node setup`".into());
    }
    let wrapper = bin.to_string_lossy().into_owned();
    let dir_s = dir.to_string_lossy().into_owned();
    let ok = sudo_job(&[&wrapper, "can-read", &dir_s]).status().map(|s| s.success()).unwrap_or(false);
    if ok {
        Ok(format!("the job account {JOB_USER} can read {dir_s}"))
    } else {
        Err(format!(
            "the job account {JOB_USER} cannot read {dir_s}: it needs read and execute on the folder and \
             execute on every folder above it, e.g. `setfacl -R -m u:{JOB_USER}:rX {dir_s}` and \
             `setfacl -m u:{JOB_USER}:x` on each parent (or `chmod o+rx`)"
        ))
    }
}

/// The folder a check as the job account runs in: one it can always enter.
pub fn neutral_dir() -> PathBuf {
    PathBuf::from("/")
}

/// The node changes no permission on Linux, so only the shared rule applies (`within`).
pub fn too_broad(dir: &Path) -> Option<String> {
    (dir == Path::new("/")).then(|| "/ is the whole machine: give the service a folder of its own".to_string())
}

/// `path` is `dir` or lies inside it.
pub fn within(path: &Path, dir: &Path) -> bool {
    path.starts_with(dir)
}

/// TODO(review): isolation — the job's folder: owned by the node's user, group amsyos-job,
/// mode 2770, inside a jobs folder of mode 2710 (the job account may enter, not list).
pub fn prepare_job_dir(root: &Path, id: &str) -> Result<PathBuf, String> {
    if !root.is_dir() {
        return Err(format!("{} is missing: run `sudo amsyos-node setup` first", root.display()));
    }
    let dir = root.join(id);
    if dir.exists() {
        remove_job_dir(&dir);
    }
    for d in [dir.clone(), dir.join("in"), dir.join("out"), dir.join("tmp")] {
        std::fs::DirBuilder::new()
            .mode(0o2770)
            .create(&d)
            .map_err(|e| format!("cannot make {}: {e}", d.display()))?;
        std::fs::set_permissions(&d, std::fs::Permissions::from_mode(0o2770))
            .map_err(|e| format!("cannot set the mode of {}: {e}", d.display()))?;
    }
    dir.canonicalize().map_err(|e| e.to_string())
}

/// What a job's processes wrote, as text (node 2.1.8; Windows reads a line that is no UTF-8
/// in its console's code page): UTF-8, anything else replaced.
pub fn decode_output(bytes: &[u8]) -> String {
    String::from_utf8_lossy(bytes).into_owned()
}

/// A file the node put in the job's folder (job.json, an input): the job may read it.
pub fn job_may_read(path: &Path) {
    let _ = std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o640));
}

pub fn remove_job_dir(dir: &Path) {
    if std::fs::remove_dir_all(dir).is_ok() || !dir.exists() {
        return;
    }
    // What the job made and closed to the node's user goes as the job account.
    let wrapper = config::job_bin().to_string_lossy().into_owned();
    let _ = sudo_job(&[&wrapper, "clean", &dir.to_string_lossy()]).status();
    if let Err(e) = std::fs::remove_dir_all(dir) {
        warn!("could not remove {}: {e}", dir.display());
    }
}

/// Opens a file the job may have made, never following a link at its last step; the caller
/// checks where it really is (`final_path`).
pub fn open_nofollow(path: &Path) -> io::Result<File> {
    std::fs::OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK).open(path)
}

/// A file of an allowed folder (folder.rs): opened without following a link at its last
/// step; the caller checks where it really lies.
pub fn open_source(path: &Path) -> io::Result<File> {
    open_nofollow(path)
}

/// TODO(review): isolation — a copy of an allowed folder's file or folder in a job's folder
/// (which is setgid amsyos-job): the job account reads it and lists it, never writes in it.
pub fn job_read_only(path: &Path, dir: bool, _top: bool) {
    let mode = if dir { 0o2750 } else { 0o640 };
    let _ = std::fs::set_permissions(path, std::fs::Permissions::from_mode(mode));
}

pub fn is_plain(file: &File) -> bool {
    file.metadata().map(|m| m.is_file()).unwrap_or(false)
}

pub fn final_path(file: &File) -> io::Result<PathBuf> {
    use std::os::fd::AsRawFd;
    std::fs::read_link(format!("/proc/self/fd/{}", file.as_raw_fd()))
}

pub fn same_path(a: &Path, b: &Path) -> bool {
    a == b
}

fn cgroups_ok() -> bool {
    static OK: OnceLock<bool> = OnceLock::new();
    *OK.get_or_init(|| {
        let Some(run) = util::which("systemd-run") else { return false };
        let ok = matches!(
            util::run_bounded(&run, &["--user", "--scope", "--quiet", "--collect", "-p", "MemoryMax=1G", "true"], 5),
            Some((0, _, _))
        );
        info!("job limits: {}", if ok { "a systemd user scope (MemoryMax, CPUQuota)" } else { "rlimits (written memory, CPU time)" });
        ok
    })
}

/// A job's processes: sudo, and under it the job account's `amsyos-job launch`, which holds
/// the job's own process group.
pub struct Proc {
    child: Mutex<Option<Child>>,
    pid: i32,
    done: Arc<AtomicBool>,
    /// The node's own process group (`node.exec`, `as_node`): killed whole by the node itself.
    own_group: bool,
}

impl Proc {
    /// TODO(review): isolation — the node's user cannot signal the job account's processes:
    /// it signals its own sudo, which hands SIGTERM on to `launch`, which kills the job's
    /// whole process group. SIGKILL to sudo after 5 s; `launch` then gets SIGTERM from its
    /// parent's death.
    pub fn kill(&self) {
        if self.done.load(Ordering::SeqCst) {
            return;
        }
        if self.own_group {
            unsafe {
                libc::kill(-self.pid, libc::SIGKILL);
            }
            return;
        }
        unsafe {
            libc::kill(self.pid, libc::SIGTERM);
        }
        let (pid, done) = (self.pid, self.done.clone());
        std::thread::spawn(move || {
            std::thread::sleep(Duration::from_secs(5));
            if !done.load(Ordering::SeqCst) {
                unsafe {
                    libc::kill(pid, libc::SIGKILL);
                }
            }
        });
    }

    pub fn wait(&self) -> i64 {
        let child = self.child.lock().ok().and_then(|mut c| c.take());
        let Some(mut child) = child else { return -1 };
        if self.own_group {
            // Wait without reaping, so the group's id stays taken while what the command left
            // running in it is killed; then reap.
            let mut info: libc::siginfo_t = unsafe { std::mem::zeroed() };
            unsafe {
                libc::waitid(libc::P_PID, self.pid as libc::id_t, &mut info, libc::WEXITED | libc::WNOWAIT);
                libc::kill(-self.pid, libc::SIGKILL);
            }
        }
        let status = child.wait();
        self.done.store(true, Ordering::SeqCst);
        use std::os::unix::process::ExitStatusExt;
        match status {
            Ok(s) => s.code().map(i64::from).unwrap_or_else(|| 128 + i64::from(s.signal().unwrap_or(0))),
            Err(_) => -1,
        }
    }
}

pub struct Spawned {
    pub pid: u32, // kept in the job's log in the node's folder
    pub proc: Arc<Proc>,
    pub stdout: Box<dyn io::Read + Send>,
    pub stderr: Box<dyn io::Read + Send>,
    /// A server kept warm (`Launch::serve_state`): the node's end of its stdin.
    pub stdin: Option<File>,
}

/// The node's end of a server's stdin, if the process was started with one.
fn stdin_of(child: &mut Child) -> Option<File> {
    child.stdin.take().map(|s| File::from(std::os::fd::OwnedFd::from(s)))
}

/// TODO(review): isolation — a pipe of a server kept warm as the job account: `amsyos-job next`
/// and `done`, run by any child of the server, open it again through /proc/<pid>/fd, which
/// takes the pipe's own mode; a pipe the node's user makes is its own, 0600. It is given to
/// the group amsyos-job, 0660 (0666 when the node's session is not in that group yet: /proc
/// still lets only that account's processes, the node's user and root reach it). (read, write)
fn job_pipe() -> Result<(std::os::fd::OwnedFd, std::os::fd::OwnedFd), String> {
    use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
    let mut fds = [0 as libc::c_int; 2];
    if unsafe { libc::pipe2(fds.as_mut_ptr(), libc::O_CLOEXEC) } != 0 {
        return Err(format!("cannot make a pipe: {}", io::Error::last_os_error()));
    }
    let (r, w) = unsafe { (OwnedFd::from_raw_fd(fds[0]), OwnedFd::from_raw_fd(fds[1])) };
    let shared = group_id(JOB_USER).is_some_and(|g| unsafe { libc::fchown(r.as_raw_fd(), libc::uid_t::MAX, g) } == 0);
    unsafe {
        libc::fchmod(r.as_raw_fd(), if shared { 0o660 } else { 0o666 });
    }
    Ok((r, w))
}

/// What of the lines the node wrote to a server's stdin it has not read yet (FIONREAD, which
/// a pipe answers at either end): whether a server that ended had taken its job.
pub fn unread(stdin: &File) -> Option<usize> {
    use std::os::fd::AsRawFd;
    let mut n: libc::c_int = 0;
    let r = unsafe { libc::ioctl(stdin.as_raw_fd(), libc::FIONREAD, &mut n) };
    (r == 0).then_some(n.max(0) as usize)
}

/// TODO(review): isolation — `node.exec` and an `as_node` service's direct job at `full`:
/// the node's own account, `sh -c` in its own process group (killed whole at the timeout,
/// a cancel or its end), the node's environment with the job's folder, no memory cap. A
/// server kept warm (node 2.1.2) the same way, its stdin the node's pipe of jobs, told the
/// pid of its shell, which holds that pipe while it lives.
fn spawn_as_node(l: &Launch) -> Result<Spawned, String> {
    use std::os::unix::process::CommandExt;
    let job = l.job_dir.to_string_lossy().into_owned();
    let path = std::env::var("PATH").unwrap_or_else(|_| SYS_PATH.to_string());
    let inherited = std::env::var("PYTHONPATH").ok();
    let mut cmd = Command::new("/bin/sh");
    cmd.arg("-c")
        .current_dir(&l.service_dir)
        .env("TMPDIR", format!("{job}/tmp"))
        .env("PATH", format!("{}:{path}", config::bin_dir().display()))
        .env("PYTHONPATH", crate::pymod::pythonpath(inherited.as_deref()))
        .envs(l.told()) // 2.1.8: UTF-8, its cores and GPU memory (what the node's own sets stays)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .process_group(0);
    match &l.serve_state {
        Some(state) => {
            cmd.arg(format!("AMSYOS_SERVE_PID=$$; export AMSYOS_SERVE_PID; {}", l.command))
                .env_remove("AMSYOS_JOB")
                .env("AMSYOS_SERVE_STATE", state)
                .stdin(Stdio::piped());
        }
        None => {
            cmd.arg(&l.command).env("AMSYOS_JOB", &job).stdin(Stdio::null());
        }
    }
    let mut child = cmd.spawn().map_err(|e| format!("cannot start /bin/sh in {}: {e}", l.service_dir.display()))?;
    let stdout = child.stdout.take().ok_or("no stdout")?;
    let stderr = child.stderr.take().ok_or("no stderr")?;
    let stdin = stdin_of(&mut child);
    let pid = child.id();
    Ok(Spawned {
        pid,
        proc: Arc::new(Proc { child: Mutex::new(Some(child)), pid: pid as i32, done: Arc::new(AtomicBool::new(false)), own_group: true }),
        stdout: Box::new(stdout),
        stderr: Box::new(stderr),
        stdin,
    })
}

/// TODO(review): isolation — starts the job as `amsyos-job` (sudo -n -u), with its limits.
pub fn spawn(l: &Launch) -> Result<Spawned, String> {
    if l.as_node {
        return spawn_as_node(l);
    }
    let wrapper = config::job_bin().to_string_lossy().into_owned();
    let bin = config::bin_dir().to_string_lossy().into_owned();
    let mem = (l.ram_gb.max(0.1) * (1u64 << 30) as f64) as u64;
    // A server kept warm lives for many jobs: the node holds each to its time (serve.rs), so
    // no CPU-time limit adds up over them.
    let cpu_s = if l.serve_state.is_some() {
        0
    } else {
        l.max_runtime_s.saturating_mul(l.cpu.ceil().max(1.0) as u64).saturating_add(10)
    };
    let scoped = cgroups_ok();
    let mut cmd = if scoped {
        let mut c = Command::new("systemd-run");
        c.args(["--user", "--scope", "--quiet", "--collect", "-p", &format!("MemoryMax={mem}")]);
        if l.cpu < crate::machine::cpus() as f64 {
            c.args(["-p", &format!("CPUQuota={}%", (l.cpu * 100.0).ceil().max(1.0) as u64)]);
        }
        c.args(["--", "sudo"]);
        c
    } else {
        Command::new("sudo")
    };
    let job = l.job_dir.to_string_lossy().into_owned();
    let dir = l.service_dir.to_string_lossy().into_owned();
    let nice = if l.server { "0" } else { "10" };
    let mem_bytes = if scoped { "0".to_string() } else { mem.to_string() };
    cmd.args(["-n", "-u", JOB_USER, "--", &wrapper, "launch"])
        .args(["--job", &job, "--dir", &dir, "--bin", &bin, "--nice", nice])
        .args(["--mem", &mem_bytes, "--cpu-s", &cpu_s.to_string()]);
    // 2.1.8: what the job is told it may use (`launch` sets UTF-8 by itself; an older copy of
    // the program ignores both).
    if l.given.cpus > 0 {
        cmd.args(["--cpus", &l.given.cpus.to_string()]);
    }
    if let Some(gb) = l.given.gpu_mem_gb {
        cmd.args(["--gpu-gb", &format!("{gb}")]);
    }
    if let Some(state) = &l.serve_state {
        cmd.args(["--serve", &state.to_string_lossy()]);
    }
    cmd.args(["--", &l.command]);
    cmd.env_clear().env("PATH", SYS_PATH);
    for keep in ["XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"] {
        if let Some(v) = std::env::var_os(keep) {
            cmd.env(keep, v);
        }
    }
    cmd.current_dir("/").stderr(Stdio::piped());
    // A server's stdin and stdout are pipes its children reach again through /proc (job_pipe).
    let (mut stdin, mut served_out) = (None, None);
    if l.serve_state.is_some() {
        let (in_r, in_w) = job_pipe()?;
        let (out_r, out_w) = job_pipe()?;
        cmd.stdin(Stdio::from(in_r)).stdout(Stdio::from(out_w));
        stdin = Some(File::from(in_w));
        served_out = Some(File::from(out_r));
    } else {
        cmd.stdin(Stdio::null()).stdout(Stdio::piped());
    }
    let spawned = cmd.spawn().map_err(|e| format!("cannot start sudo: {e}"));
    drop(cmd); // the server's own ends, closed here
    let mut child = spawned?;
    let stdout: Box<dyn io::Read + Send> = match served_out {
        Some(f) => Box::new(f),
        None => Box::new(child.stdout.take().ok_or("no stdout")?),
    };
    let stderr = child.stderr.take().ok_or("no stderr")?;
    let pid = child.id();
    Ok(Spawned {
        pid,
        proc: Arc::new(Proc { child: Mutex::new(Some(child)), pid: pid as i32, done: Arc::new(AtomicBool::new(false)), own_group: false }),
        stdout,
        stderr: Box::new(stderr),
        stdin,
    })
}

// ------------------------------------------------------------------------------------ setup

fn sh(program: &str, args: &[&str]) -> Result<String, String> {
    let out = Command::new(program)
        .args(args)
        .env("PATH", SYS_PATH)
        .output()
        .map_err(|e| format!("cannot run {program}: {e}"))?;
    if !out.status.success() {
        return Err(format!(
            "{program} {} failed: {}",
            args.join(" "),
            String::from_utf8_lossy(&out.stderr).trim()
        ));
    }
    Ok(String::from_utf8_lossy(&out.stdout).trim().to_string())
}

/// A user's uid, gid and home, as the system knows them (getent: NSS, not only /etc/passwd).
fn account(user: &str) -> Result<(u32, u32, PathBuf), String> {
    let entry = sh("getent", &["passwd", user]).map_err(|_| format!("there is no user {user}"))?;
    let fields: Vec<&str> = entry.split(':').collect();
    match fields.as_slice() {
        [_, _, uid, gid, _, home, ..] => match (uid.parse(), gid.parse()) {
            (Ok(u), Ok(g)) => Ok((u, g, PathBuf::from(home))),
            _ => Err(format!("cannot read the account of {user}")),
        },
        _ => Err(format!("cannot read the account of {user}")),
    }
}

/// The node's folder whose level `access` changes, and the owner to give its config back to:
/// the caller's own, or under sudo the SUDO_USER's.
pub fn access_target() -> Result<(PathBuf, Option<(u32, u32)>), String> {
    if !is_elevated() {
        return Ok((config::node_dir(), None));
    }
    let user = std::env::var("SUDO_USER")
        .ok()
        .filter(|u| valid_user(u) && u != "root")
        .ok_or("run it with sudo from the user who runs the node: `sudo amsyos-node access full`")?;
    let (uid, gid, home) = account(&user)?;
    Ok((config::node_dir_in(&home), Some((uid, gid))))
}

/// TODO(review): isolation — raising to `full` needs an administrator at the machine: sudo.
pub fn may_raise_to_full() -> bool {
    is_elevated()
}

pub fn give_back(path: &Path, owner: Option<(u32, u32)>) {
    if let (Some((uid, gid)), Ok(c)) = (owner, std::ffi::CString::new(path.as_os_str().as_encoded_bytes())) {
        unsafe {
            libc::chown(c.as_ptr(), uid, gid);
        }
    }
}

fn valid_user(name: &str) -> bool {
    !name.is_empty()
        && name.len() <= 32
        && !name.starts_with('-')
        && name.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '-' | '.'))
}

/// TODO(review): isolation — makes the job account, the jobs folder with its modes, the copy
/// of `amsyos-job`, and the sudoers rule that lets the node's user (only) start it as
/// `amsyos-job` without a password.
pub fn setup(user: Option<String>, jobs: Option<PathBuf>, services: Option<PathBuf>) -> Result<(), String> {
    if !is_elevated() {
        return Err("setup makes an account and a sudoers rule: run it as root (`sudo amsyos-node setup`)".into());
    }
    let user = user
        .or_else(|| std::env::var("SUDO_USER").ok())
        .ok_or("say who runs the node: `sudo amsyos-node setup --user <name>`")?;
    if !valid_user(&user) || user == "root" || user == JOB_USER {
        return Err(format!("{user:?} cannot run the node: name the user who will run it with --user"));
    }
    let (uid, gid, home) = account(&user)?;
    if util::which("sudo").is_none() && !Path::new("/usr/bin/sudo").exists() {
        return Err("sudo is not installed: install it (e.g. `apt install sudo`), then run setup again".into());
    }
    // Node 2.1.9: the folders config.json names are kept when not given here; a profile's
    // defaults are its own (`jobs-<name>`, `services-<name>`), and no two profiles share one.
    let dir = config::node_dir_in(&home);
    let was = Config::load(&dir);
    let jobs = jobs.or_else(|| was.jobs.clone());
    let services = services.or_else(|| was.services_root.clone());
    let root = jobs.clone().unwrap_or_else(config::default_jobs_root);
    if !root.is_absolute() {
        return Err("--jobs: an absolute path".into());
    }
    let managed = services.clone().unwrap_or_else(config::default_services_root);
    if !managed.is_absolute() {
        return Err("--services: an absolute path".into());
    }
    if let Some(why) = config::clash(
        &config::node_base_in(&home),
        config::profile(),
        &[(&root, "the jobs folder"), (&managed, "the managed services folder")],
    ) {
        return Err(why);
    }

    // The account: no login shell, no home.
    if sh("getent", &["group", JOB_USER]).is_err() {
        sh("groupadd", &["--system", JOB_USER])?;
    }
    if sh("getent", &["passwd", JOB_USER]).is_err() {
        let nologin = ["/usr/sbin/nologin", "/sbin/nologin"].into_iter().find(|p| Path::new(p).exists()).unwrap_or("/bin/false");
        sh(
            "useradd",
            &["--system", "--gid", JOB_USER, "--no-create-home", "--home-dir", "/nonexistent", "--shell", nologin, "--comment", "AMSYOS Node jobs", JOB_USER],
        )?;
    }
    sh("usermod", &["-a", "-G", JOB_USER, &user])?;
    println!("account {JOB_USER}: ready; {user} is in its group");

    // The jobs folder: the node's user owns it, the job account may only enter it.
    std::fs::create_dir_all(&root).map_err(|e| format!("cannot make {}: {e}", root.display()))?;
    let root_s = root.to_string_lossy().into_owned();
    sh("chown", &[&format!("{user}:{JOB_USER}"), &root_s])?;
    std::fs::set_permissions(&root, std::fs::Permissions::from_mode(0o2710)).map_err(|e| e.to_string())?;
    println!("jobs folder {root_s}: {user}:{JOB_USER}, mode 2710");

    // TODO(review): isolation — the managed services folder (`deploy`): the node's user owns
    // it, mode 0755: the job account may read and run what is in it, never write.
    std::fs::create_dir_all(&managed).map_err(|e| format!("cannot make {}: {e}", managed.display()))?;
    sh("chown", &[&format!("{user}:"), &managed.to_string_lossy()])?;
    std::fs::set_permissions(&managed, std::fs::Permissions::from_mode(0o755)).map_err(|e| e.to_string())?;
    println!("managed services folder {}: {user}'s, mode 0755", managed.display());

    // The copy of this program every job finds on its PATH as `amsyos-job`.
    let bin_dir = config::bin_dir();
    std::fs::create_dir_all(&bin_dir).map_err(|e| e.to_string())?;
    std::fs::set_permissions(&bin_dir, std::fs::Permissions::from_mode(0o755)).map_err(|e| e.to_string())?;
    let me = std::env::current_exe().map_err(|e| e.to_string())?;
    let target = config::job_bin();
    let tmp = target.with_extension("new");
    std::fs::copy(&me, &tmp).map_err(|e| format!("cannot copy to {}: {e}", tmp.display()))?;
    std::fs::set_permissions(&tmp, std::fs::Permissions::from_mode(0o755)).map_err(|e| e.to_string())?;
    std::fs::rename(&tmp, &target).map_err(|e| e.to_string())?;
    println!("{}: copied", target.display());

    // sudoers: the node's user may start that one program as the job account, no password.
    let rule = format!(
        "# AMSYOS Node (written by `amsyos-node setup`): {user} starts jobs as {JOB_USER}.\n\
         Defaults>{JOB_USER} !use_pty, !lecture\n\
         {user} ALL=({JOB_USER}) NOPASSWD: {}\n",
        target.display()
    );
    let staged = format!("{SUDOERS}.new"); // a name with a dot: sudo ignores it while staged
    {
        let mut f = std::fs::OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(true)
            .mode(0o440)
            .open(&staged)
            .map_err(|e| format!("cannot write {staged}: {e}"))?;
        use std::io::Write;
        f.write_all(rule.as_bytes()).map_err(|e| e.to_string())?;
    }
    if let Err(e) = sh("visudo", &["-cf", &staged]) {
        let _ = std::fs::remove_file(&staged);
        return Err(format!("the sudoers rule did not pass visudo: {e}"));
    }
    std::fs::rename(&staged, SUDOERS).map_err(|e| e.to_string())?;
    println!("{SUDOERS}: {user} may run {} as {JOB_USER}", target.display());

    // The node's own folder, where `run` finds the jobs folder chosen here.
    let mut created = Vec::new();
    let mut at = dir.clone();
    while !at.exists() {
        created.push(at.clone());
        if !at.pop() {
            break;
        }
    }
    config::ensure_node_dir(&dir)?;
    let mut cfg = Config::load(&dir);
    cfg.jobs = jobs;
    cfg.services_root = services;
    cfg.save(&dir)?;
    for p in created.iter().chain([dir.join(config::CONFIG_FILE)].iter()) {
        let c = std::ffi::CString::new(p.as_os_str().as_encoded_bytes()).map_err(|e| e.to_string())?;
        unsafe {
            libc::chown(c.as_ptr(), uid, gid);
        }
    }
    println!("{}: the node's folder", dir.display());
    println!("done. {user} must log in again (a new session) so that the group {JOB_USER} applies.");
    println!("then, as {user}: {} run", config::command());
    Ok(())
}

/// `amsyos-node diag` is for Windows (how a job starts as the job account there).
pub fn diag(_service_dir: Option<std::path::PathBuf>) -> Result<i32, String> {
    Err("diag is for Windows only".into())
}

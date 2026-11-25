//! Windows: the job account `amsyos-job` (password kept with DPAPI), jobs started with
//! CreateProcessWithLogonW into a Job Object; what the machine has.

use std::ffi::{c_void, OsStr};
use std::fs::File;
use std::io;
use std::os::windows::ffi::OsStrExt;
use std::os::windows::fs::{MetadataExt, OpenOptionsExt};
use std::os::windows::io::{AsRawHandle, FromRawHandle};
use std::os::windows::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::ptr::{null, null_mut};
use std::sync::{Arc, Mutex};

use windows_sys::Win32::Foundation::{
    CloseHandle, GetLastError, LocalFree, SetHandleInformation, FILETIME, GENERIC_READ, HANDLE,
    HANDLE_FLAG_INHERIT, INVALID_HANDLE_VALUE,
};
use windows_sys::Win32::NetworkManagement::NetManagement::{
    NetUserAdd, NetUserSetInfo, NERR_UserExists, UF_DONT_EXPIRE_PASSWD, UF_PASSWD_CANT_CHANGE,
    UF_SCRIPT, USER_INFO_1, USER_INFO_1003, USER_PRIV_USER,
};
use windows_sys::Win32::Security::Cryptography::{
    CryptProtectData, CryptUnprotectData, CRYPTPROTECT_UI_FORBIDDEN, CRYPT_INTEGER_BLOB,
};
use windows_sys::Win32::Security::{
    GetTokenInformation, IsWellKnownSid, TokenElevation, TokenUser, WinLocalSystemSid,
    SECURITY_ATTRIBUTES, TOKEN_ELEVATION, TOKEN_QUERY, TOKEN_USER,
};
use windows_sys::Win32::Storage::FileSystem::{
    CreateFileW, GetDiskFreeSpaceExW, GetFinalPathNameByHandleW, FILE_FLAG_BACKUP_SEMANTICS,
    FILE_FLAG_OPEN_REPARSE_POINT, FILE_SHARE_READ, FILE_SHARE_WRITE, OPEN_EXISTING,
};
use windows_sys::Win32::System::JobObjects::{
    AssignProcessToJobObject, CreateJobObjectW, JobObjectBasicUIRestrictions,
    JobObjectCpuRateControlInformation, JobObjectExtendedLimitInformation, SetInformationJobObject,
    TerminateJobObject, JOBOBJECT_BASIC_UI_RESTRICTIONS, JOBOBJECT_CPU_RATE_CONTROL_INFORMATION,
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JOB_OBJECT_CPU_RATE_CONTROL_ENABLE,
    JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP, JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION,
    JOB_OBJECT_LIMIT_JOB_MEMORY, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    JOB_OBJECT_LIMIT_PRIORITY_CLASS, JOB_OBJECT_UILIMIT_DESKTOP,
    JOB_OBJECT_UILIMIT_DISPLAYSETTINGS, JOB_OBJECT_UILIMIT_EXITWINDOWS,
    JOB_OBJECT_UILIMIT_GLOBALATOMS, JOB_OBJECT_UILIMIT_HANDLES, JOB_OBJECT_UILIMIT_READCLIPBOARD,
    JOB_OBJECT_UILIMIT_SYSTEMPARAMETERS, JOB_OBJECT_UILIMIT_WRITECLIPBOARD,
};
use windows_sys::Win32::System::Pipes::CreatePipe;
use windows_sys::Win32::System::Registry::{RegGetValueW, HKEY_LOCAL_MACHINE, RRF_RT_REG_SZ};
use windows_sys::Win32::System::SystemInformation::{
    GetTickCount, GlobalMemoryStatusEx, MEMORYSTATUSEX,
};
use windows_sys::Win32::System::Threading::{
    CreateProcessWithLogonW, GetCurrentProcess, GetExitCodeProcess, GetSystemTimes,
    OpenProcessToken, ResumeThread, SetPriorityClass, TerminateProcess, WaitForSingleObject,
    BELOW_NORMAL_PRIORITY_CLASS, CREATE_SUSPENDED, CREATE_UNICODE_ENVIRONMENT, INFINITE,
    LOGON_WITH_PROFILE, NORMAL_PRIORITY_CLASS, PROCESS_INFORMATION, STARTF_USESHOWWINDOW,
    STARTF_USESTDHANDLES, STARTUPINFOW,
};
use windows_sys::Win32::UI::Input::KeyboardAndMouse::{GetLastInputInfo, LASTINPUTINFO};
use windows_sys::Win32::UI::WindowsAndMessaging::SW_HIDE;

use crate::config::{self, Config, PASSWORD_FILE};
use crate::exec::Launch;
use crate::{util, warn};

pub const JOB_USER: &str = "amsyos-job";
const CREATE_NO_WINDOW: u32 = 0x0800_0000;
const ATTR_DIRECTORY: u32 = 0x10;
const ATTR_REPARSE_POINT: u32 = 0x400;

fn wide(s: impl AsRef<OsStr>) -> Vec<u16> {
    s.as_ref().encode_wide().chain(Some(0)).collect()
}

fn last_error() -> String {
    last_error_of(unsafe { GetLastError() })
}

fn last_error_of(code: u32) -> String {
    format!("{} (Windows error {code})", io::Error::from_raw_os_error(code as i32))
}

fn env(key: &str) -> Option<String> {
    std::env::var(key).ok().filter(|v| !v.is_empty())
}

fn system_root() -> PathBuf {
    PathBuf::from(env("SystemRoot").unwrap_or_else(|| r"C:\Windows".into()))
}

/// The account as icacls names it: `<computer>\amsyos-job`, never a domain account.
fn job_account() -> String {
    format!("{}\\{JOB_USER}", env("COMPUTERNAME").unwrap_or_else(|| ".".into()))
}

/// The person who runs the node: `<domain>\<user>`.
fn node_user() -> String {
    match (env("USERDOMAIN"), env("USERNAME")) {
        (Some(d), Some(u)) => format!("{d}\\{u}"),
        (_, Some(u)) => u,
        _ => String::new(),
    }
}

pub fn hostname() -> String {
    env("COMPUTERNAME").unwrap_or_default()
}

pub fn ram_bytes() -> u64 {
    let mut m: MEMORYSTATUSEX = unsafe { std::mem::zeroed() };
    m.dwLength = std::mem::size_of::<MEMORYSTATUSEX>() as u32;
    if unsafe { GlobalMemoryStatusEx(&mut m) } != 0 {
        m.ullTotalPhys
    } else {
        0
    }
}

/// The physical memory available now (mem.rs).
pub fn ram_free_bytes() -> Option<u64> {
    let mut m: MEMORYSTATUSEX = unsafe { std::mem::zeroed() };
    m.dwLength = std::mem::size_of::<MEMORYSTATUSEX>() as u32;
    (unsafe { GlobalMemoryStatusEx(&mut m) } != 0).then_some(m.ullAvailPhys)
}

pub fn disk_free_bytes(path: &Path) -> u64 {
    let mut free = 0u64;
    let p = wide(path);
    if unsafe { GetDiskFreeSpaceExW(p.as_ptr(), &mut free, null_mut(), null_mut()) } != 0 {
        free
    } else {
        0
    }
}

struct Owned(HANDLE);
impl Drop for Owned {
    fn drop(&mut self) {
        if !self.0.is_null() && self.0 != INVALID_HANDLE_VALUE {
            unsafe { CloseHandle(self.0) };
        }
    }
}

fn token() -> Option<Owned> {
    let mut h: HANDLE = null_mut();
    (unsafe { OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut h) } != 0).then_some(Owned(h))
}

pub fn is_elevated() -> bool {
    let Some(t) = token() else { return false };
    let mut e = TOKEN_ELEVATION { TokenIsElevated: 0 };
    let mut len = 0u32;
    let size = std::mem::size_of::<TOKEN_ELEVATION>() as u32;
    let ok = unsafe { GetTokenInformation(t.0, TokenElevation, &mut e as *mut _ as *mut c_void, size, &mut len) };
    ok != 0 && e.TokenIsElevated != 0
}

fn is_system() -> bool {
    let Some(t) = token() else { return false };
    let mut buf = vec![0u64; 64]; // u64: aligned for TOKEN_USER
    let mut len = 0u32;
    let ok = unsafe { GetTokenInformation(t.0, TokenUser, buf.as_mut_ptr() as *mut c_void, 512, &mut len) };
    if ok == 0 {
        return false;
    }
    let user = unsafe { &*(buf.as_ptr() as *const TOKEN_USER) };
    unsafe { IsWellKnownSid(user.User.Sid, WinLocalSystemSid) != 0 }
}

/// Never as SYSTEM; elevated only when the person says so (`--allow-elevated`, e.g. a machine
/// with UAC off, where no other token exists).
pub fn refuse_identity(allow_elevated: bool) -> Result<(), String> {
    if is_system() {
        return Err("the node never runs as SYSTEM: start it as the person who joined it".into());
    }
    if is_elevated() {
        if !allow_elevated {
            return Err("this window is elevated (administrator): start the node from a normal window, \
                        or add --allow-elevated where no other is possible (UAC off)"
                .into());
        }
        warn!(
            "running elevated (--allow-elevated): the node holds an administrator's token; jobs run as {JOB_USER}, \
             except the organisation's own as_node jobs at full, which run with that token"
        );
    }
    Ok(())
}

pub fn idle_seconds() -> Option<u64> {
    let mut li = LASTINPUTINFO { cbSize: std::mem::size_of::<LASTINPUTINFO>() as u32, dwTime: 0 };
    if unsafe { GetLastInputInfo(&mut li) } == 0 {
        return None;
    }
    let now = unsafe { GetTickCount() };
    Some(u64::from(now.wrapping_sub(li.dwTime) / 1000))
}

static LAST_CPU: Mutex<Option<(u64, u64)>> = Mutex::new(None);

pub fn cpu_load() -> Option<f64> {
    let ft = |f: FILETIME| (u64::from(f.dwHighDateTime) << 32) | u64::from(f.dwLowDateTime);
    let (mut idle, mut kernel, mut user): (FILETIME, FILETIME, FILETIME) = unsafe { std::mem::zeroed() };
    if unsafe { GetSystemTimes(&mut idle, &mut kernel, &mut user) } == 0 {
        return None;
    }
    let (total, idle) = (ft(kernel) + ft(user), ft(idle)); // kernel time includes idle time
    let mut last = LAST_CPU.lock().ok()?;
    let (t0, i0) = last.replace((total, idle)).unwrap_or((0, 0));
    let (dt, di) = (total.saturating_sub(t0), idle.saturating_sub(i0));
    if dt == 0 {
        return Some(0.0);
    }
    Some(((dt - di.min(dt)) as f64 / dt as f64 * 100.0).round() / 100.0)
}

// ------------------------------------------------------------------------------------- DPAPI

/// TODO(review): isolation — the job account's password, protected for the node's user
/// (DPAPI, current user) and kept in the node's folder.
fn protect(data: &[u8]) -> Result<Vec<u8>, String> {
    let input = CRYPT_INTEGER_BLOB { cbData: data.len() as u32, pbData: data.as_ptr() as *mut u8 };
    let mut out = CRYPT_INTEGER_BLOB { cbData: 0, pbData: null_mut() };
    let desc = wide("amsyos-job");
    let ok = unsafe {
        CryptProtectData(&input, desc.as_ptr(), null(), null(), null(), CRYPTPROTECT_UI_FORBIDDEN, &mut out)
    };
    if ok == 0 {
        return Err(format!("cannot protect the password: {}", last_error()));
    }
    let v = unsafe { std::slice::from_raw_parts(out.pbData, out.cbData as usize) }.to_vec();
    unsafe { LocalFree(out.pbData as *mut c_void) };
    Ok(v)
}

fn unprotect(blob: &[u8]) -> Result<Vec<u8>, String> {
    let input = CRYPT_INTEGER_BLOB { cbData: blob.len() as u32, pbData: blob.as_ptr() as *mut u8 };
    let mut out = CRYPT_INTEGER_BLOB { cbData: 0, pbData: null_mut() };
    let ok = unsafe {
        CryptUnprotectData(&input, null_mut(), null(), null(), null(), CRYPTPROTECT_UI_FORBIDDEN, &mut out)
    };
    if ok == 0 {
        return Err(format!(
            "cannot read the job account's password ({}): run `amsyos-node setup` again as this user, in an administrator window",
            last_error()
        ));
    }
    let v = unsafe { std::slice::from_raw_parts(out.pbData, out.cbData as usize) }.to_vec();
    unsafe {
        std::ptr::write_bytes(out.pbData, 0, out.cbData as usize);
        LocalFree(out.pbData as *mut c_void);
    }
    Ok(v)
}

// ------------------------------------------------------------------------------- the checks

fn hidden(program: &Path, args: &[&str]) -> Result<String, String> {
    let out = Command::new(program)
        .args(args)
        .creation_flags(CREATE_NO_WINDOW)
        .output()
        .map_err(|e| format!("cannot run {}: {e}", program.display()))?;
    let text = format!("{}{}", String::from_utf8_lossy(&out.stdout), String::from_utf8_lossy(&out.stderr));
    if !out.status.success() {
        return Err(format!("{} {} failed: {}", program.display(), args.join(" "), text.trim()));
    }
    Ok(text)
}

fn icacls(path: &Path, args: &[&str]) -> Result<(), String> {
    let mut all = vec![path.to_str().ok_or("a path that is not Unicode")?];
    all.extend_from_slice(args);
    all.push("/Q");
    hidden(&system_root().join(r"System32\icacls.exe"), &all).map(|_| ())
}

pub fn preflight() -> Result<(), String> {
    let bin = config::job_bin();
    if !bin.is_file() {
        return Err(format!("{} is missing: run `amsyos-node setup` in an administrator window first", bin.display()));
    }
    if !config::node_dir().join(PASSWORD_FILE).is_file() {
        return Err("the job account is not set up: run `amsyos-node setup` in an administrator window first".into());
    }
    Ok(())
}

const SYSTEM_F: &str = "*S-1-5-18:(OI)(CI)F"; // SIDs: the names are localised
const ADMINS_F: &str = "*S-1-5-32-544:(OI)(CI)F";
/// Traverse, read attributes and synchronize, on the folder itself only (no (OI)(CI)): what
/// opening a path through it, or lstat of it, needs; no listing, no reading, no writing.
const JOB_PASS: &str = "(S,X,RA)";

/// TODO(review): isolation — gives `dir` exactly these grants and nothing else: its own
/// explicit entries are dropped (`/reset`), then what it inherits (`/inheritance:r`), so
/// e.g. `BUILTIN\Users:(OI)(CI)(RX,W)` from D:\ is gone. What lies inside inherits from it.
/// Run again, it gives the same ACL.
fn lock_down(dir: &Path, grants: &[&str]) -> Result<(), String> {
    icacls(dir, &["/reset"])?;
    let mut args = vec!["/inheritance:r", "/grant:r"];
    args.extend_from_slice(grants);
    icacls(dir, &args)
}

/// The folder a check as the job account runs in: one it can always enter.
pub fn neutral_dir() -> PathBuf {
    system_root()
}

/// TODO(review): isolation — a service's folder is given to the job account to read (and
/// its ACL is replaced): never a drive, the system's folders or a profile.
pub fn too_broad(dir: &Path) -> Option<String> {
    if dir.parent().is_none() {
        return Some(format!("{} is a whole drive: give the service a folder of its own", dir.display()));
    }
    let profile = env("USERPROFILE").map(PathBuf::from);
    let users = profile.as_ref().and_then(|p| p.parent().map(Path::to_path_buf));
    let mut system: Vec<PathBuf> = ["SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData", "USERPROFILE"]
        .iter()
        .filter_map(|k| env(k).map(PathBuf::from))
        .collect();
    system.extend(users);
    system
        .iter()
        .find(|s| within(s, dir))
        .map(|s| format!("{} holds {}: give the service a folder of its own", dir.display(), s.display()))
}

/// `path` is `dir` or lies inside it (no case on Windows).
pub fn within(path: &Path, dir: &Path) -> bool {
    let p = path.to_string_lossy().to_lowercase();
    let d = dir.to_string_lossy().to_lowercase();
    let d = d.trim_end_matches('\\');
    p == d || p.starts_with(&format!("{d}\\"))
}

/// TODO(review): isolation — the service's folder gets a protected ACL: SYSTEM,
/// Administrators and the node's user F, the job account (OI)(CI)RX, nothing else.
pub fn grant_dir(dir: &Path) -> Result<String, String> {
    let me = format!("{}:(OI)(CI)F", node_user());
    let job = format!("{}:(OI)(CI)RX", job_account());
    lock_down(dir, &[SYSTEM_F, ADMINS_F, &me, &job]).map_err(|e| {
        format!("{e}\nthe folder could not be protected: run this in an administrator window, or as the folder's owner")
    })?;
    Ok(format!(
        "{}: writable only by you and administrators; {} may read and run it",
        dir.display(),
        job_account()
    ))
}

fn final_of(path: &Path, flags: u32) -> io::Result<String> {
    let f = std::fs::OpenOptions::new().read(true).custom_flags(flags).open(path)?;
    final_path(&f).map(|p| p.to_string_lossy().into_owned())
}

/// TODO(review): isolation — the job's folder: inherits the jobs folder's protected ACL
/// (SYSTEM, Administrators, the node's user) and gets `(OI)(CI)M` for the job account on
/// itself only. It must really be `<jobs root>\<id>` (no junction on the way).
pub fn prepare_job_dir(root: &Path, id: &str) -> Result<PathBuf, String> {
    if !root.is_dir() {
        return Err(format!("{} is missing: run `amsyos-node setup` in an administrator window first", root.display()));
    }
    let dir = root.join(id);
    if dir.exists() {
        remove_job_dir(&dir);
    }
    std::fs::create_dir(&dir).map_err(|e| format!("cannot make {}: {e}", dir.display()))?;
    let real = final_of(&dir, FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT).map_err(|e| e.to_string())?;
    let root_real = final_of(root, FILE_FLAG_BACKUP_SEMANTICS).map_err(|e| e.to_string())?;
    if real.to_lowercase() != format!("{}\\{}", root_real.trim_end_matches('\\'), id).to_lowercase() {
        return Err(format!("{} is not where it should be ({real})", dir.display()));
    }
    icacls(&dir, &["/grant", &format!("{}:(OI)(CI)M", job_account())])?;
    for sub in ["in", "out", "tmp"] {
        std::fs::create_dir(dir.join(sub)).map_err(|e| format!("cannot make {}: {e}", dir.join(sub).display()))?;
    }
    Ok(PathBuf::from(real))
}

pub fn job_may_read(_path: &Path) {}

pub fn remove_job_dir(dir: &Path) {
    if let Err(e) = std::fs::remove_dir_all(dir) {
        if dir.exists() {
            warn!("could not remove {}: {e}", dir.display());
        }
    }
}

/// Opens a file the job may have made without following a link or junction at its last
/// step; the caller checks where it really is (`final_path`) and that it is a plain file.
pub fn open_nofollow(path: &Path) -> io::Result<File> {
    std::fs::OpenOptions::new().read(true).custom_flags(FILE_FLAG_OPEN_REPARSE_POINT).open(path)
}

/// A file of an allowed folder (folder.rs), opened to read as any program would (a file
/// OneDrive keeps in the cloud is fetched); the caller checks that its real path is the one
/// walked to, so a link or a junction swapped in on the way never leads out.
pub fn open_source(path: &Path) -> io::Result<File> {
    std::fs::OpenOptions::new().read(true).open(path)
}

/// TODO(review): isolation — the copy of an allowed folder's file or folder in a job's folder
/// (`top`: the input itself, the rest inherits): its ACL is cut from the job folder's, which
/// lets the job account change what is in it, to SYSTEM, Administrators and the node's user
/// F, and the job account RX: it reads and lists the copy, never writes, renames or deletes it.
pub fn job_read_only(path: &Path, dir: bool, top: bool) {
    if !top {
        return;
    }
    let inherit = if dir { "(OI)(CI)" } else { "" };
    let grants = [
        format!("*S-1-5-18:{inherit}F"),
        format!("*S-1-5-32-544:{inherit}F"),
        format!("{}:{inherit}F", node_user()),
        format!("{}:{inherit}RX", job_account()),
    ];
    let refs: Vec<&str> = grants.iter().map(String::as_str).collect();
    if let Err(e) = lock_down(path, &refs) {
        warn!("the copy of a folder input could not be made read-only for {JOB_USER}: {e}");
    }
}

pub fn is_plain(file: &File) -> bool {
    file.metadata()
        .map(|m| m.file_attributes() & (ATTR_DIRECTORY | ATTR_REPARSE_POINT) == 0)
        .unwrap_or(false)
}

pub fn final_path(file: &File) -> io::Result<PathBuf> {
    let mut buf = vec![0u16; 2048];
    let n = unsafe { GetFinalPathNameByHandleW(file.as_raw_handle() as HANDLE, buf.as_mut_ptr(), buf.len() as u32, 0) };
    if n == 0 || n as usize >= buf.len() {
        return Err(io::Error::last_os_error());
    }
    Ok(PathBuf::from(String::from_utf16_lossy(&buf[..n as usize])))
}

pub fn same_path(a: &Path, b: &Path) -> bool {
    a.to_string_lossy().to_lowercase() == b.to_string_lossy().to_lowercase()
}

// ------------------------------------------------------------------------------- a job

/// A job's process and its Job Object. Closing the Job Object kills what is left in it.
pub struct Proc {
    process: HANDLE,
    job: HANDLE,
}
unsafe impl Send for Proc {}
unsafe impl Sync for Proc {}

impl Proc {
    pub fn kill(&self) {
        unsafe { TerminateJobObject(self.job, 1) };
    }

    pub fn wait(&self) -> i64 {
        let mut code = 0u32;
        unsafe {
            WaitForSingleObject(self.process, INFINITE);
            GetExitCodeProcess(self.process, &mut code);
            TerminateJobObject(self.job, 0); // whatever the job left running ends with it
        }
        i64::from(code)
    }
}

impl Drop for Proc {
    fn drop(&mut self) {
        unsafe {
            CloseHandle(self.process);
            CloseHandle(self.job);
        }
    }
}

pub struct Spawned {
    pub pid: u32,
    pub proc: Arc<Proc>,
    pub stdout: Box<dyn io::Read + Send>,
    pub stderr: Box<dyn io::Read + Send>,
    /// A server kept warm (`Launch::serve_state`): the node's end of its stdin.
    pub stdin: Option<File>,
}

/// An anonymous pipe cannot be asked from its writing end what is unread: a server that ended
/// after its job's line was written is taken to have had the job (serve.rs).
pub fn unread(_stdin: &File) -> Option<usize> {
    None
}

/// A server's stdin (node 2.1.2): the process's end inheritable, the node's end not; or NUL,
/// as for any other job. (the process's end, the node's end or null)
fn stdin_for(l: &Launch, sa: &SECURITY_ATTRIBUTES) -> Result<(HANDLE, HANDLE), String> {
    if l.serve_state.is_none() {
        let nul_name = wide("NUL");
        let nul = unsafe {
            CreateFileW(nul_name.as_ptr(), GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, sa, OPEN_EXISTING, 0, null_mut())
        };
        return Ok((nul, null_mut()));
    }
    let (mut r, mut w): (HANDLE, HANDLE) = (null_mut(), null_mut());
    if unsafe { CreatePipe(&mut r, &mut w, sa, 0) } == 0 {
        return Err(format!("cannot make a pipe: {}", last_error()));
    }
    unsafe { SetHandleInformation(w, HANDLE_FLAG_INHERIT, 0) }; // the node's end stays its own
    Ok((r, w))
}

/// The node's end of a server's stdin as a file (None for any other job).
fn stdin_file(w: HANDLE) -> Option<File> {
    (!w.is_null()).then(|| unsafe { File::from_raw_handle(w as _) })
}

/// The machine's PATH (HKLM), not the person's own.
fn system_path() -> String {
    let key = wide(r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment");
    let name = wide("Path");
    let mut buf = vec![0u16; 16384];
    let mut size = (buf.len() * 2) as u32;
    let r = unsafe {
        RegGetValueW(HKEY_LOCAL_MACHINE, key.as_ptr(), name.as_ptr(), RRF_RT_REG_SZ, null_mut(), buf.as_mut_ptr() as *mut c_void, &mut size)
    };
    if r == 0 && size >= 2 {
        let n = (size as usize / 2).saturating_sub(1);
        return String::from_utf16_lossy(&buf[..n]);
    }
    let root = system_root();
    let r = root.display();
    format!(r"{r}\system32;{r};{r}\System32\Wbem;{r}\System32\WindowsPowerShell\v1.0\")
}

/// TODO(review): isolation — the job's whole environment, nothing of the node's own.
fn env_block(l: &Launch) -> Vec<u16> {
    let job = l.job_dir.display().to_string();
    let root = system_root().display().to_string();
    let mut vars: Vec<(String, String)> = vec![
        ("SystemRoot".into(), root.clone()),
        ("windir".into(), root.clone()),
        ("SystemDrive".into(), env("SystemDrive").unwrap_or_else(|| "C:".into())),
        ("ComSpec".into(), format!(r"{root}\System32\cmd.exe")),
        ("PATHEXT".into(), ".COM;.EXE;.BAT;.CMD;.VBS;.VBE;.JS;.JSE;.WSF;.WSH;.MSC".into()),
        ("PATH".into(), format!("{};{}", config::bin_dir().display(), system_path())),
        ("NUMBER_OF_PROCESSORS".into(), crate::machine::cpus().to_string()),
        ("PROCESSOR_ARCHITECTURE".into(), env("PROCESSOR_ARCHITECTURE").unwrap_or_else(|| "AMD64".into())),
        ("TEMP".into(), format!(r"{job}\tmp")),
        ("TMP".into(), format!(r"{job}\tmp")),
        ("USERPROFILE".into(), job.clone()),
        ("APPDATA".into(), format!(r"{job}\tmp")),
        ("LOCALAPPDATA".into(), format!(r"{job}\tmp")),
        ("USERNAME".into(), JOB_USER.into()),
        ("OS".into(), "Windows_NT".into()),
        ("AMSYOS_JOB".into(), job.clone()),
    ];
    for k in [
        "COMPUTERNAME", "ProgramData", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432",
        "CommonProgramFiles", "CommonProgramFiles(x86)", "CommonProgramW6432",
    ] {
        if let Some(v) = env(k) {
            vars.push((k.into(), v));
        }
    }
    vars.sort_by_key(|(k, _)| k.to_uppercase());
    let mut block: Vec<u16> = Vec::new();
    for (k, v) in vars {
        block.extend(OsStr::new(&format!("{k}={v}")).encode_wide());
        block.push(0);
    }
    block.push(0);
    block
}

/// TODO(review): isolation — what contract 1 adds to the job account's own environment,
/// as `set "NAME=value"&& ` commands cmd runs before the service's command: the job's
/// folder, its temporary folder, amsyos-job.exe first on its PATH and the Python job module
/// on its PYTHONPATH (node 2.1.2; after the node's own PYTHONPATH for a job run as the node's
/// account, which inherits the node's environment). A server kept warm is told where
/// `amsyos-job next` keeps the job now instead of a job's folder. A value cmd would read as
/// more than text is refused.
fn job_vars(l: &Launch) -> Result<String, String> {
    let job = l.job_dir.display().to_string();
    let clean = |v: &str| !v.chars().any(|c| matches!(c, '"' | '%' | '&' | '|' | '<' | '>' | '^' | '\n' | '\r'));
    let inherited = if l.as_node { env("PYTHONPATH").filter(|p| clean(p)) } else { None };
    let given = match &l.serve_state {
        Some(state) => ("AMSYOS_SERVE_STATE", state.display().to_string()),
        None => ("AMSYOS_JOB", job.clone()),
    };
    let vars = [
        given,
        ("TEMP", format!(r"{job}\tmp")),
        ("TMP", format!(r"{job}\tmp")),
        ("PATH", format!("{};%PATH%", config::bin_dir().display())),
        ("PYTHONPATH", crate::pymod::pythonpath(inherited.as_deref())),
    ];
    let mut out = String::new();
    for (k, v) in vars {
        let text = v.replace("%PATH%", "");
        if text.chars().any(|c| matches!(c, '"' | '%' | '&' | '|' | '<' | '>' | '^' | '\n' | '\r')) {
            return Err(format!("the path {text} has a character cmd would not take as text"));
        }
        out.push_str(&format!("set \"{k}={v}\"&& "));
    }
    Ok(out)
}

fn pipe(sa: &SECURITY_ATTRIBUTES) -> Result<(HANDLE, HANDLE), String> {
    let (mut r, mut w): (HANDLE, HANDLE) = (null_mut(), null_mut());
    if unsafe { CreatePipe(&mut r, &mut w, sa, 0) } == 0 {
        return Err(format!("cannot make a pipe: {}", last_error()));
    }
    unsafe { SetHandleInformation(r, HANDLE_FLAG_INHERIT, 0) }; // the node's end stays its own
    Ok((r, w))
}

/// The node's folder whose level `access` changes: the caller's own (an elevated window
/// is the same user's).
pub fn access_target() -> Result<(PathBuf, Option<(u32, u32)>), String> {
    Ok((config::node_dir(), None))
}

/// TODO(review): isolation — raising to `full` needs an administrator at the machine: an
/// elevated process.
pub fn may_raise_to_full() -> bool {
    is_elevated()
}

pub fn give_back(_path: &Path, _owner: Option<(u32, u32)>) {}

/// TODO(review): isolation — `node.exec` and an `as_node` service's direct job at `full`: the
/// node's own account (CreateProcessW, no logon), `cmd.exe /d /s /c` with the contract's
/// variables set first, in a Job Object that kills its whole tree at the timeout, a cancel,
/// its end or the node's exit. No memory cap, no UI limits.
fn spawn_as_node(l: &Launch) -> Result<Spawned, String> {
    use windows_sys::Win32::System::Threading::CreateProcessW;
    let sa = SECURITY_ATTRIBUTES {
        nLength: std::mem::size_of::<SECURITY_ATTRIBUTES>() as u32,
        lpSecurityDescriptor: null_mut(),
        bInheritHandle: 1,
    };
    let prefix = job_vars(l)?;
    let (out_r, out_w) = pipe(&sa)?;
    let (err_r, err_w) = pipe(&sa)?;
    let (nul, in_w) = stdin_for(l, &sa)?;
    let mut si: STARTUPINFOW = unsafe { std::mem::zeroed() };
    si.cb = std::mem::size_of::<STARTUPINFOW>() as u32;
    si.dwFlags = STARTF_USESTDHANDLES | STARTF_USESHOWWINDOW;
    si.wShowWindow = SW_HIDE as u16;
    si.hStdInput = nul;
    si.hStdOutput = out_w;
    si.hStdError = err_w;
    let cmd_exe = system_root().join(r"System32\cmd.exe");
    let app = wide(&cmd_exe);
    let mut line: Vec<u16> = OsStr::new(&format!("\"{}\" /d /s /c \"{prefix}{}\"", cmd_exe.display(), l.command))
        .encode_wide()
        .chain(Some(0))
        .collect();
    let cwd = wide(&l.service_dir);
    let mut pi: PROCESS_INFORMATION = unsafe { std::mem::zeroed() };
    let ok = unsafe {
        CreateProcessW(
            app.as_ptr(),
            line.as_mut_ptr(),
            null(),
            null(),
            1,
            CREATE_SUSPENDED | CREATE_NO_WINDOW,
            null(),
            cwd.as_ptr(),
            &si,
            &mut pi,
        )
    };
    let started = if ok == 0 { Err(last_error()) } else { Ok(()) };
    unsafe {
        CloseHandle(out_w);
        CloseHandle(err_w);
        if nul != INVALID_HANDLE_VALUE {
            CloseHandle(nul);
        }
    }
    if let Err(e) = started {
        unsafe {
            CloseHandle(out_r);
            CloseHandle(err_r);
            if !in_w.is_null() {
                CloseHandle(in_w);
            }
        }
        return Err(format!("cannot start cmd.exe in {}: {e}", l.service_dir.display()));
    }
    let stdout = unsafe { File::from_raw_handle(out_r as _) };
    let stderr = unsafe { File::from_raw_handle(err_r as _) };
    let stdin = stdin_file(in_w);
    let fail = |why: String| -> Result<Spawned, String> {
        unsafe {
            TerminateProcess(pi.hProcess, 1);
            CloseHandle(pi.hThread);
            CloseHandle(pi.hProcess);
        }
        Err(why)
    };
    let job = unsafe { CreateJobObjectW(null(), null()) };
    if job.is_null() {
        return fail(format!("cannot make a Job Object: {}", last_error()));
    }
    let mut ext: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = unsafe { std::mem::zeroed() };
    ext.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION;
    let size = std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32;
    if unsafe { SetInformationJobObject(job, JobObjectExtendedLimitInformation, &ext as *const _ as *const c_void, size) } == 0
        || unsafe { AssignProcessToJobObject(job, pi.hProcess) } == 0
    {
        let why = last_error();
        unsafe { CloseHandle(job) };
        return fail(format!("cannot put the command in its Job Object: {why}"));
    }
    unsafe {
        ResumeThread(pi.hThread);
        CloseHandle(pi.hThread);
    }
    Ok(Spawned {
        pid: pi.dwProcessId,
        proc: Arc::new(Proc { process: pi.hProcess, job }),
        stdout: Box::new(stdout),
        stderr: Box::new(stderr),
        stdin,
    })
}

/// TODO(review): isolation — starts the job as `.\amsyos-job` (CreateProcessWithLogonW,
/// suspended), puts it in a Job Object (memory, CPU rate, priority, UI limits, killed with
/// the node), then lets it run. The job shares the person's desktop (lpDesktop NULL: the
/// secondary logon service grants the job's logon access to it); the UI limits keep it from
/// other windows' handles and the clipboard. A separate desktop is for 2.2's sandbox.
pub fn spawn(l: &Launch) -> Result<Spawned, String> {
    if l.as_node {
        return spawn_as_node(l);
    }
    let blob = std::fs::read(l.node_dir.join(PASSWORD_FILE))
        .map_err(|_| "the job account is not set up: run `amsyos-node setup` in an administrator window".to_string())?;
    let secret = unprotect(&blob)?;
    let mut password: Vec<u16> = String::from_utf8_lossy(&secret).encode_utf16().chain(Some(0)).collect();
    drop(secret);

    let sa = SECURITY_ATTRIBUTES {
        nLength: std::mem::size_of::<SECURITY_ATTRIBUTES>() as u32,
        lpSecurityDescriptor: null_mut(),
        bInheritHandle: 1,
    };
    // The job's environment is its own account's (lpEnvironment NULL: the secondary logon
    // service builds it from the job account's profile; an explicit block was refused with
    // error 87 on Windows 10, tried on PC2 2026-11-18). Nothing of the node's is in it.
    // What contract 1 adds is set by cmd itself before the command runs.
    let prefix = match job_vars(l) {
        Ok(p) => p,
        Err(e) => {
            password.iter_mut().for_each(|c| *c = 0);
            return Err(e);
        }
    };
    let (out_r, out_w) = pipe(&sa)?;
    let (err_r, err_w) = pipe(&sa)?;
    let (nul, in_w) = stdin_for(l, &sa)?;
    let mut si: STARTUPINFOW = unsafe { std::mem::zeroed() };
    si.cb = std::mem::size_of::<STARTUPINFOW>() as u32;
    si.dwFlags = STARTF_USESTDHANDLES | STARTF_USESHOWWINDOW;
    si.wShowWindow = SW_HIDE as u16;
    si.hStdInput = nul;
    si.hStdOutput = out_w;
    si.hStdError = err_w;

    let cmd_exe = system_root().join(r"System32\cmd.exe");
    let app = wide(&cmd_exe);
    let line: Vec<u16> = OsStr::new(&format!("\"{}\" /d /s /c \"{prefix}{}\"", cmd_exe.display(), l.command))
        .encode_wide()
        .chain(Some(0))
        .collect();
    // CreateProcessWithLogonW takes a command line of at most 1024 characters (and says
    // only "the parameter is incorrect" when it is longer).
    if line.len() > 1024 {
        password.iter_mut().for_each(|c| *c = 0);
        unsafe {
            CloseHandle(out_r);
            CloseHandle(out_w);
            CloseHandle(err_r);
            CloseHandle(err_w);
            if nul != INVALID_HANDLE_VALUE {
                CloseHandle(nul);
            }
            if !in_w.is_null() {
                CloseHandle(in_w);
            }
        }
        return Err(format!(
            "the command line is {} characters, and Windows starts a process as another account \
             with at most 1024: shorten the service's command (put it in a script file)",
            line.len() - 1
        ));
    }
    let cwd = wide(&l.service_dir);
    let user = wide(JOB_USER);
    let domain = wide(".");
    let priority = if l.server { NORMAL_PRIORITY_CLASS } else { BELOW_NORMAL_PRIORITY_CLASS };
    let mut pi: PROCESS_INFORMATION = unsafe { std::mem::zeroed() };
    let mut line = line;
    // The secondary logon service takes few creation flags: no priority class here (the
    // Job Object and SetPriorityClass set it below).
    let ok = unsafe {
        CreateProcessWithLogonW(
            user.as_ptr(),
            domain.as_ptr(),
            password.as_ptr(),
            LOGON_WITH_PROFILE,
            app.as_ptr(),
            line.as_mut_ptr(),
            CREATE_SUSPENDED,
            null(),
            cwd.as_ptr(),
            &si,
            &mut pi,
        )
    };
    let tried = if ok == 0 { vec![last_error()] } else { Vec::new() };
    let started = if ok == 0 { Err(tried.join("; ")) } else { Ok(()) };
    password.iter_mut().for_each(|c| *c = 0);
    unsafe {
        CloseHandle(out_w);
        CloseHandle(err_w);
        if nul != INVALID_HANDLE_VALUE {
            CloseHandle(nul);
        }
    }
    if let Err(e) = started {
        unsafe {
            CloseHandle(out_r);
            CloseHandle(err_r);
            if !in_w.is_null() {
                CloseHandle(in_w);
            }
        }
        return Err(format!("cannot start the job as {JOB_USER} in {}: {e}", l.service_dir.display()));
    }
    let stdout = unsafe { File::from_raw_handle(out_r as _) };
    let stderr = unsafe { File::from_raw_handle(err_r as _) };
    let stdin = stdin_file(in_w);
    let fail = |why: String| -> Result<Spawned, String> {
        unsafe {
            TerminateProcess(pi.hProcess, 1);
            CloseHandle(pi.hThread);
            CloseHandle(pi.hProcess);
        }
        Err(why)
    };

    let job = unsafe { CreateJobObjectW(null(), null()) };
    if job.is_null() {
        return fail(format!("cannot make a Job Object: {}", last_error()));
    }
    let mut ext: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = unsafe { std::mem::zeroed() };
    ext.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_JOB_MEMORY
        | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        | JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION
        | JOB_OBJECT_LIMIT_PRIORITY_CLASS;
    ext.BasicLimitInformation.PriorityClass = priority;
    ext.JobMemoryLimit = (l.ram_gb.max(0.1) * (1u64 << 30) as f64) as usize;
    let size = std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32;
    if unsafe { SetInformationJobObject(job, JobObjectExtendedLimitInformation, &ext as *const _ as *const c_void, size) } == 0 {
        unsafe { CloseHandle(job) };
        return fail(format!("cannot limit the Job Object: {}", last_error()));
    }
    let cpus = crate::machine::cpus() as f64;
    if l.cpu < cpus {
        let mut rate: JOBOBJECT_CPU_RATE_CONTROL_INFORMATION = unsafe { std::mem::zeroed() };
        rate.ControlFlags = JOB_OBJECT_CPU_RATE_CONTROL_ENABLE | JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP;
        rate.Anonymous.CpuRate = ((l.cpu / cpus) * 10000.0).round().clamp(1.0, 10000.0) as u32;
        let size = std::mem::size_of::<JOBOBJECT_CPU_RATE_CONTROL_INFORMATION>() as u32;
        if unsafe { SetInformationJobObject(job, JobObjectCpuRateControlInformation, &rate as *const _ as *const c_void, size) } == 0 {
            warn!("the job's CPU rate could not be capped: {}", last_error());
        }
    }
    let ui = JOBOBJECT_BASIC_UI_RESTRICTIONS {
        UIRestrictionsClass: JOB_OBJECT_UILIMIT_HANDLES
            | JOB_OBJECT_UILIMIT_READCLIPBOARD
            | JOB_OBJECT_UILIMIT_WRITECLIPBOARD
            | JOB_OBJECT_UILIMIT_GLOBALATOMS
            | JOB_OBJECT_UILIMIT_SYSTEMPARAMETERS
            | JOB_OBJECT_UILIMIT_DISPLAYSETTINGS
            | JOB_OBJECT_UILIMIT_EXITWINDOWS
            | JOB_OBJECT_UILIMIT_DESKTOP,
    };
    let size = std::mem::size_of::<JOBOBJECT_BASIC_UI_RESTRICTIONS>() as u32;
    if unsafe { SetInformationJobObject(job, JobObjectBasicUIRestrictions, &ui as *const _ as *const c_void, size) } == 0 {
        warn!("the job's UI could not be limited: {}", last_error());
    }
    if unsafe { SetPriorityClass(pi.hProcess, priority) } == 0 {
        warn!("the job's priority could not be set: {}", last_error());
    }
    if unsafe { AssignProcessToJobObject(job, pi.hProcess) } == 0 {
        let why = last_error();
        unsafe { CloseHandle(job) };
        return fail(format!("cannot put the job in its Job Object: {why}"));
    }
    unsafe {
        ResumeThread(pi.hThread);
        CloseHandle(pi.hThread);
    }
    Ok(Spawned {
        pid: pi.dwProcessId,
        proc: Arc::new(Proc { process: pi.hProcess, job }),
        stdout: Box::new(stdout),
        stderr: Box::new(stderr),
        stdin,
    })
}

// ------------------------------------------------------------------------------------ setup

fn new_password() -> String {
    const ABC: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    let bytes: [u8; 36] = util::random_bytes();
    // Every class Windows' complexity rule counts, then 36 random characters.
    let mut p = String::from("Aa1-");
    p.extend(bytes.iter().map(|b| ABC[(*b as usize) % ABC.len()] as char));
    p
}

/// TODO(review): isolation — makes (or resets) the account `amsyos-job` with a random
/// password kept with DPAPI for the node's user, hides it from the sign-in screen, and
/// sets the ACLs of the jobs folder and of the `amsyos-job.exe` copy.
pub fn setup(user: Option<String>, jobs: Option<PathBuf>, services: Option<PathBuf>) -> Result<(), String> {
    if is_system() {
        return Err("run setup as the person who will run the node, in an administrator window (not as SYSTEM)".into());
    }
    if !is_elevated() {
        return Err("setup makes an account: run it in an administrator window (Run as administrator)".into());
    }
    let me = node_user();
    if let Some(u) = user {
        let mine = env("USERNAME").unwrap_or_default();
        if !u.eq_ignore_ascii_case(&mine) && !u.eq_ignore_ascii_case(&me) {
            return Err(format!(
                "setup keeps the job account's password for the user who runs it ({me}): run it as {u}, in an administrator window"
            ));
        }
    }
    if me.is_empty() {
        return Err("cannot tell who runs this (USERNAME is not set)".into());
    }

    // The account and its password.
    let password = new_password();
    let mut name = wide(JOB_USER);
    let mut pw = wide(&password);
    let mut comment = wide("AMSYOS Node jobs");
    let info = USER_INFO_1 {
        usri1_name: name.as_mut_ptr(),
        usri1_password: pw.as_mut_ptr(),
        usri1_password_age: 0,
        usri1_priv: USER_PRIV_USER,
        usri1_home_dir: null_mut(),
        usri1_comment: comment.as_mut_ptr(),
        usri1_flags: UF_SCRIPT | UF_DONT_EXPIRE_PASSWD | UF_PASSWD_CANT_CHANGE,
        usri1_script_path: null_mut(),
    };
    let mut parm = 0u32;
    let mut r = unsafe { NetUserAdd(null(), 1, &info as *const _ as *const u8, &mut parm) };
    let made = r == 0;
    if r == NERR_UserExists {
        let reset = USER_INFO_1003 { usri1003_password: pw.as_mut_ptr() };
        r = unsafe { NetUserSetInfo(null(), name.as_ptr(), 1003, &reset as *const _ as *const u8, &mut parm) };
    }
    pw.iter_mut().for_each(|c| *c = 0);
    if r != 0 {
        return Err(format!("cannot make the account {JOB_USER} (NetUserAdd/NetUserSetInfo error {r})"));
    }
    let dir = config::node_dir();
    config::ensure_node_dir(&dir)?;
    let blob = protect(password.as_bytes())?;
    drop(password);
    util::write_file(&dir.join(PASSWORD_FILE), &blob, true).map_err(|e| e.to_string())?;
    println!("account {JOB_USER}: {}; its password is kept for {me} only (DPAPI)", if made { "made" } else { "password renewed" });

    let reg = system_root().join(r"System32\reg.exe");
    hidden(
        &reg,
        &[
            "add",
            r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon\SpecialAccounts\UserList",
            "/v",
            JOB_USER,
            "/t",
            "REG_DWORD",
            "/d",
            "0",
            "/f",
        ],
    )?;
    println!("account {JOB_USER}: hidden from the sign-in screen");

    // TODO(review): isolation — folders: SYSTEM, Administrators and the node's user only
    // (protected, re-applied at every setup); `bin` adds the job account RX. The program is
    // installed in a folder Users and the job account cannot write to: run from D:\ or a
    // download folder, a job could replace it or drop a DLL beside it.
    let base = config::program_data();
    let root = jobs.clone().unwrap_or_else(config::default_jobs_root);
    if !root.is_absolute() {
        return Err("--jobs: an absolute path such as D:\\AMSYOS\\jobs".into());
    }
    let bin = config::bin_dir();
    let install = config::install_dir();
    let managed = services.clone().unwrap_or_else(config::default_services_root);
    if !managed.is_absolute() {
        return Err("--services: an absolute path such as D:\\AMSYOS\\services".into());
    }
    for d in [&base, &root, &bin, &install, &managed] {
        std::fs::create_dir_all(d).map_err(|e| format!("cannot make {}: {e}", d.display()))?;
    }
    let me_f = format!("{me}:(OI)(CI)F");
    let job_rx = format!("{}:(OI)(CI)RX", job_account());
    // TODO(review): isolation — on the folders above a job's folder and a deployed
    // service's folder, the job account may pass through and read the folder's own
    // attributes (`JOB_PASS`), on that folder only: never list it, read what is in it, or
    // write. Without it Node.js (realpath: lstat of each folder on the way) fails with
    // EPERM, and PowerShell starts in C:\ instead of the service's folder.
    let job_pass = format!("{}:{JOB_PASS}", job_account());
    lock_down(&base, &[SYSTEM_F, ADMINS_F, &me_f, &job_pass])?;
    lock_down(&root, &[SYSTEM_F, ADMINS_F, &me_f, &job_pass])?;
    lock_down(&bin, &[SYSTEM_F, ADMINS_F, &me_f, &job_rx])?;
    lock_down(&install, &[SYSTEM_F, ADMINS_F, &me_f])?;
    // Each deployed service's folder in it gets the job account RX on its own (`deploy`).
    lock_down(&managed, &[SYSTEM_F, ADMINS_F, &me_f, &job_pass])?;
    println!("{}, {}: SYSTEM, Administrators and {me} only", base.display(), root.display());
    println!("managed services folder {}: the same", managed.display());
    println!("  ({JOB_USER} may pass through these three and read their own attributes, nothing else)");
    println!("{}: the same, and {JOB_USER} may read and run it", bin.display());

    let exe = std::env::current_exe().map_err(|e| e.to_string())?;
    let target = config::job_bin();
    std::fs::copy(&exe, &target).map_err(|e| format!("cannot copy to {} (is a job running?): {e}", target.display()))?;
    println!("{}: copied", target.display());
    let installed = config::installed_exe();
    let same = installed.canonicalize().ok().zip(exe.canonicalize().ok()).is_some_and(|(a, b)| a == b);
    if !same {
        std::fs::copy(&exe, &installed)
            .map_err(|e| format!("cannot copy to {} (is the node running from there? stop it first): {e}", installed.display()))?;
    }

    let mut cfg = Config::load(&dir);
    cfg.jobs = jobs;
    cfg.services_root = services;
    cfg.save(&dir)?;
    println!("installed: {} (SYSTEM, Administrators and {me} only)", installed.display());
    println!("done. Run the node from there, and point a scheduled task there too:");
    println!("  \"{}\" run --allow-elevated   (--allow-elevated only where UAC is off)", installed.display());
    Ok(())
}

/// `amsyos-node diag`: tries CreateProcessWithLogonW (and LogonUserW +
/// CreateProcessWithTokenW) as the job account with a matrix of parameters, each starting
/// `cmd.exe /d /c exit 7`, and prints what each answered. For finding why a job does not
/// start on a machine; prints no secret.
pub fn diag(service_dir: Option<PathBuf>) -> Result<i32, String> {
    use windows_sys::Win32::Security::{LogonUserW, LOGON32_LOGON_INTERACTIVE, LOGON32_PROVIDER_DEFAULT};
    use windows_sys::Win32::System::Threading::CreateProcessWithTokenW;

    let blob = std::fs::read(config::node_dir().join(PASSWORD_FILE))
        .map_err(|_| "the job account is not set up: run `amsyos-node setup`".to_string())?;
    let secret = unprotect(&blob)?;
    let password: Vec<u16> = String::from_utf8_lossy(&secret).encode_utf16().chain(Some(0)).collect();
    drop(secret);
    let cmd_exe = system_root().join(r"System32\cmd.exe");
    let app = wide(&cmd_exe);
    let line: Vec<u16> = wide(format!("\"{}\" /d /c exit 7", cmd_exe.display()));
    let dir = service_dir.unwrap_or_else(|| PathBuf::from(r"C:\Windows\System32"));
    let cwd = wide(&dir);
    let fake = Launch {
        job_dir: dir.clone(), service_dir: dir.clone(), command: String::new(), cpu: 1.0,
        ram_gb: 1.0, max_runtime_s: 10, server: true, node_dir: config::node_dir(),
        as_node: false, serve_state: None,
    };
    let block = env_block(&fake);
    let desk = wide(r"winsta0\default");
    println!("STARTUPINFOW.cb = {}, env block = {} UTF-16 units, cwd = {}", std::mem::size_of::<STARTUPINFOW>(), block.len(), dir.display());
    println!("elevated = {}, in a job = {}", is_elevated(), in_job());

    let finish = |pi: &PROCESS_INFORMATION, suspended: bool| -> String {
        unsafe {
            if suspended {
                ResumeThread(pi.hThread);
            }
            let w = WaitForSingleObject(pi.hProcess, 5000);
            let mut code = 0u32;
            GetExitCodeProcess(pi.hProcess, &mut code);
            if w != 0 {
                TerminateProcess(pi.hProcess, 1);
            }
            CloseHandle(pi.hThread);
            CloseHandle(pi.hProcess);
            format!("ok (exit {code})")
        }
    };

    let users: [(&str, &str); 2] = [(JOB_USER, "."), (JOB_USER, "")];
    let mut n = 0;
    for (user, domain) in users {
        let u = wide(user);
        let d = wide(domain);
        let dptr = if domain.is_empty() { null() } else { d.as_ptr() };
        for logon in [LOGON_WITH_PROFILE, 0] {
            for (fname, flags, with_env) in [
                ("0", 0u32, false),
                ("SUSPENDED", CREATE_SUSPENDED, false),
                ("UNICODE_ENV", CREATE_UNICODE_ENVIRONMENT, true),
                ("SUSPENDED|UNICODE_ENV", CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT, true),
            ] {
                for with_cwd in [false, true] {
                    for with_desk in [false, true] {
                        for named in [true, false] {
                            if domain.is_empty() && (with_desk || !named || logon == 0) {
                                continue; // a few with no domain are enough
                            }
                            let mut si: STARTUPINFOW = unsafe { std::mem::zeroed() };
                            si.cb = std::mem::size_of::<STARTUPINFOW>() as u32;
                            if with_desk {
                                si.lpDesktop = desk.as_ptr() as *mut u16;
                            }
                            let mut l2 = line.clone();
                            let mut pi: PROCESS_INFORMATION = unsafe { std::mem::zeroed() };
                            let ok = unsafe {
                                CreateProcessWithLogonW(
                                    u.as_ptr(), dptr, password.as_ptr(), logon,
                                    if named { app.as_ptr() } else { null() }, l2.as_mut_ptr(), flags,
                                    if with_env { block.as_ptr() as *const c_void } else { null() },
                                    if with_cwd { cwd.as_ptr() } else { null() }, &si, &mut pi,
                                )
                            };
                            let said = if ok != 0 { finish(&pi, flags & CREATE_SUSPENDED != 0) } else { last_error() };
                            n += 1;
                            println!(
                                "{n:2} logon_with_logon user={user} domain={} logon={} flags={fname} env={} cwd={} desktop={} app={} -> {said}",
                                if domain.is_empty() { "NULL" } else { domain },
                                if logon == 0 { "0" } else { "PROFILE" }, if with_env { "block" } else { "NULL" },
                                if with_cwd { "dir" } else { "NULL" }, if with_desk { "winsta0" } else { "NULL" },
                                if named { "cmd.exe" } else { "NULL" },
                            );
                        }
                    }
                }
            }
        }
    }
    // The other way in: a token of the job account's own logon, then CreateProcessWithTokenW
    // (needs SeImpersonatePrivilege, which an elevated administrator has).
    let u = wide(JOB_USER);
    let d = wide(".");
    let mut tok: HANDLE = null_mut();
    let logged = unsafe {
        LogonUserW(u.as_ptr(), d.as_ptr(), password.as_ptr(), LOGON32_LOGON_INTERACTIVE, LOGON32_PROVIDER_DEFAULT, &mut tok)
    };
    if logged == 0 {
        println!("LogonUserW (interactive) -> {}", last_error());
    } else {
        println!("LogonUserW (interactive) -> ok");
        for (fname, flags, with_env) in [
            ("SUSPENDED|UNICODE_ENV", CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT, true),
            ("0", 0u32, false),
        ] {
            for logon in [LOGON_WITH_PROFILE, 0] {
                let mut si: STARTUPINFOW = unsafe { std::mem::zeroed() };
                si.cb = std::mem::size_of::<STARTUPINFOW>() as u32;
                let mut l2 = line.clone();
                let mut pi: PROCESS_INFORMATION = unsafe { std::mem::zeroed() };
                let ok = unsafe {
                    CreateProcessWithTokenW(
                        tok, logon, app.as_ptr(), l2.as_mut_ptr(), flags,
                        if with_env { block.as_ptr() as *const c_void } else { null() },
                        cwd.as_ptr(), &si, &mut pi,
                    )
                };
                let said = if ok != 0 { finish(&pi, flags & CREATE_SUSPENDED != 0) } else { last_error() };
                println!("   with_token logon={} flags={fname} env={} cwd=dir -> {said}",
                         if logon == 0 { "0" } else { "PROFILE" }, if with_env { "block" } else { "NULL" });
            }
        }
        unsafe { CloseHandle(tok) };
    }
    Ok(0)
}

fn in_job() -> bool {
    use windows_sys::Win32::System::JobObjects::IsProcessInJob;
    let mut inside = 0;
    unsafe { IsProcessInJob(GetCurrentProcess(), null_mut(), &mut inside) };
    inside != 0
}

//! Node 2.1.9: the node updates itself (sdk/NODE.md §7). A release is AMSYOS's: the Gateway the
//! node joined serves `latest.json` and the programs (scripts/node_release.py makes them), each
//! signed with AMSYOS's release key, whose public half is built in here. The node takes only a
//! newer version, from its own Gateway, of the size and SHA-256 the signed line names, that then
//! answers `version` as that version; it applies it only when it runs no job, and keeps the
//! previous program for `amsyos-node rollback`.
//!
//! Windows: the program in %ProgramFiles%\AMSYOS\Node and the job wrapper in
//! %ProgramData%\AMSYOS\Node\bin are replaced once for the machine (a running program cannot be
//! overwritten but can be renamed: the old one becomes `…prev.exe`). Linux: the installed
//! program is replaced in place when the node's account may write it; else the verified copy is
//! kept in the node's own folder, and `run` checks it again and execs into it at each start.
//! Every node process on the machine (each profile) then restarts itself once it runs no job:
//! it watches the files that decide what it runs (`keep_up`), so none ever ends another's job.
//! Windows restarts through a one-shot scheduled task that ends and starts the task running the
//! process (found by its process id), else the node starts the new program itself; Linux execs
//! (the same PID).

use std::collections::BTreeMap;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant, SystemTime};

use base64::Engine;
use ed25519_dalek::{Signature, VerifyingKey};
use reqwest::Url;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use tokio::io::AsyncWriteExt;
use tokio::sync::watch;

use crate::config::{self, Config};
use crate::{info, net, serve, sys, util, warn};

/// The public half of AMSYOS's release key (Ed25519). Its private half never leaves the
/// operator (`var/node-release/release.key`, scripts/node_release.py).
pub const RELEASE_KEY: &str = "d0390fabdbca007ad6fbf40acbe6385e5113314e223218a2a017e9c80fd61bfc";
const CHECK_EVERY_S: u64 = 6 * 3600;
const JITTER_S: u64 = 1800;
const LOOK_EVERY: Duration = Duration::from_secs(60);
const PROGRAM_MAX: u64 = 100 << 20;
const LATEST_MAX: usize = 64 << 10;
const SELF_TEST_S: u64 = 120;
const LOCK_STALE: Duration = Duration::from_secs(600);
const RECORD_FILE: &str = "update.json";
const LOCK_FILE: &str = "update.lock";
/// Node 2.1.11 (2026-11-26: a node whose jobs ran back to back stayed on the old program for
/// hours): a new program that waited this long for a moment with no job makes the node take no
/// new job (a node.exec still runs) until the running ones end, then it restarts.
const DRAIN_AFTER: Duration = Duration::from_secs(30 * 60);

static RESTARTING: AtomicBool = AtomicBool::new(false);
static DRAINING: AtomicBool = AtomicBool::new(false);

/// Node 2.1.11: while the node lets its running jobs end before a restart into a new program
/// (`DRAIN_AFTER`): a new job is refused (the kernel places it again), a node.exec is not.
pub fn draining() -> bool {
    DRAINING.load(Ordering::SeqCst)
}

/// While the node closes for a restart into a new program: a job that comes now is refused
/// (the kernel places it again), never started and then ended.
pub fn restarting() -> bool {
    RESTARTING.load(Ordering::SeqCst)
}

type Version = (u32, u32, u32);

/// `x.y.z`, each 1 to 4 digits; nothing else.
fn parse_version(s: &str) -> Option<Version> {
    let mut parts = s.split('.');
    let mut next = || -> Option<u32> {
        let p = parts.next()?;
        if p.is_empty() || p.len() > 4 || !p.bytes().all(|b| b.is_ascii_digit()) {
            return None;
        }
        p.parse().ok()
    };
    let v = (next()?, next()?, next()?);
    parts.next().is_none().then_some(v)
}

fn current() -> Version {
    parse_version(env!("CARGO_PKG_VERSION")).unwrap_or((0, 0, 0))
}

fn shown((a, b, c): Version) -> String {
    format!("{a}.{b}.{c}")
}

/// This machine's name for its program in a release; only x86_64 programs are made.
fn platform() -> Option<&'static str> {
    if std::env::consts::ARCH != "x86_64" {
        return None;
    }
    if cfg!(windows) {
        Some("windows-x86_64")
    } else if cfg!(target_os = "linux") {
        Some("linux-x86_64")
    } else {
        None
    }
}

/// The one name a release's program of this version and platform has.
fn file_name(version: &str, platform: &str) -> String {
    if platform.starts_with("windows") {
        format!("amsyos-node-{version}.exe")
    } else {
        format!("amsyos-node-{version}-{platform}")
    }
}

#[derive(Deserialize)]
struct Latest {
    version: String,
    #[serde(default)]
    files: BTreeMap<String, Entry>,
}

#[derive(Serialize, Deserialize, Clone, Debug)]
struct Entry {
    file: String,
    sha256: String,
    size: u64,
    sig: String,
}

/// A release for this machine whose signature checked out.
#[derive(Clone, Debug)]
pub struct Release {
    version: Version,
    text: String,
    entry: Entry,
}

fn key_bytes(hex: &str) -> Option<[u8; 32]> {
    let raw = hex.as_bytes();
    if raw.len() != 64 {
        return None;
    }
    let digit = |c: u8| (c as char).to_digit(16).map(|d| d as u8);
    let mut out = [0u8; 32];
    for (i, pair) in raw.chunks(2).enumerate() {
        out[i] = digit(pair[0])? << 4 | digit(pair[1])?;
    }
    Some(out)
}

/// TODO(review): isolation — the one check that decides whether a program is AMSYOS's: an
/// Ed25519 signature (strict) of `amsyos-node <version> <platform> <sha256 hex>` by `key`.
fn signed_by(key: &[u8; 32], version: &str, platform: &str, sha256: &str, sig: &str) -> Result<(), String> {
    let refused = || format!("the signature of {version} ({platform}) is not AMSYOS's release key's");
    let vk = VerifyingKey::from_bytes(key).map_err(|_| refused())?;
    let raw = base64::engine::general_purpose::URL_SAFE_NO_PAD.decode(sig.trim_end_matches('=')).map_err(|_| refused())?;
    let bytes: [u8; 64] = raw.as_slice().try_into().map_err(|_| refused())?;
    vk.verify_strict(format!("amsyos-node {version} {platform} {sha256}").as_bytes(), &Signature::from_bytes(&bytes))
        .map_err(|_| refused())
}

fn release_key() -> [u8; 32] {
    key_bytes(RELEASE_KEY).unwrap_or([0; 32])
}

fn is_sha256(s: &str) -> bool {
    s.len() == 64 && s.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

/// latest.json for this machine, every problem at once; signed by `key` or refused.
fn check(latest: &Latest, key: &[u8; 32]) -> Result<Release, String> {
    let platform = platform()
        .ok_or_else(|| format!("no release is made for this machine ({} {})", std::env::consts::OS, std::env::consts::ARCH))?;
    let version = parse_version(&latest.version)
        .ok_or_else(|| format!("latest.json: the version {:?} is not x.y.z", util::cut(&latest.version, 40)))?;
    let entry = latest.files.get(platform).cloned().ok_or_else(|| format!("latest.json names no program for {platform}"))?;
    let mut problems = Vec::new();
    let want = file_name(&latest.version, platform);
    if entry.file != want {
        problems.push(format!("file {:?} is not {want}", util::cut(&entry.file, 80)));
    }
    if !is_sha256(&entry.sha256) {
        problems.push("sha256: 64 lowercase hex digits".to_string());
    }
    if entry.size == 0 || entry.size > PROGRAM_MAX {
        problems.push(format!("size: 1 byte to {} MB", PROGRAM_MAX >> 20));
    }
    if problems.is_empty() {
        if let Err(e) = signed_by(key, &latest.version, platform, &entry.sha256, &entry.sig) {
            problems.push(e);
        }
    }
    if !problems.is_empty() {
        return Err(format!("the release {} is refused: {}", util::cut(&latest.version, 40), problems.join("; ")));
    }
    Ok(Release { version, text: latest.version.clone(), entry })
}

// ---------------------------------------------------------------------------- the Gateway

fn http() -> Result<reqwest::Client, String> {
    reqwest::Client::builder()
        .use_preconfigured_tls(net::tls())
        .redirect(reqwest::redirect::Policy::none())
        .connect_timeout(Duration::from_secs(15))
        .read_timeout(Duration::from_secs(120))
        .user_agent(concat!("amsyos-node/", env!("CARGO_PKG_VERSION")))
        .build()
        .map_err(|e| format!("cannot make the HTTP client: {e}"))
}

fn base(gateway: &Url) -> String {
    gateway.as_str().trim_end_matches('/').to_string()
}

/// TODO(review): isolation — latest.json from the node's own Gateway only: the address is made
/// here from the Gateway it joined, and redirects are never followed.
async fn fetch_latest(http: &reqwest::Client, gateway: &Url) -> Result<Release, String> {
    let url = format!("{}/node/release/latest.json", base(gateway));
    let mut resp = http.get(&url).send().await.map_err(|e| format!("cannot reach {}: {e}", base(gateway)))?;
    match resp.status().as_u16() {
        200 => {}
        404 => return Err(format!("{} publishes no release of AMSYOS Node yet", base(gateway))),
        s => return Err(format!("latest.json: HTTP {s}")),
    }
    let mut body = Vec::new();
    while let Some(chunk) = resp.chunk().await.map_err(|e| format!("latest.json: {e}"))? {
        body.extend_from_slice(&chunk);
        if body.len() > LATEST_MAX {
            return Err("latest.json is too long".into());
        }
    }
    let latest: Latest = serde_json::from_slice(&body).map_err(|e| format!("latest.json is not understood: {e}"))?;
    check(&latest, &release_key())
}

fn sha256_file(path: &Path) -> std::io::Result<String> {
    let mut f = std::fs::File::open(path)?;
    let (mut hash, mut buf) = (Sha256::new(), vec![0u8; 1 << 16]);
    loop {
        let n = f.read(&mut buf)?;
        if n == 0 {
            break;
        }
        hash.update(&buf[..n]);
    }
    Ok(util::hex(&hash.finalize()))
}

/// The node's own folder for what it downloads (and on Linux the copy it runs).
fn update_dir(node_dir: &Path) -> PathBuf {
    node_dir.join("update")
}

fn make_update_dir(node_dir: &Path) -> Result<PathBuf, String> {
    let up = update_dir(node_dir);
    std::fs::create_dir_all(&up).map_err(|e| format!("cannot make {}: {e}", up.display()))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(&up, std::fs::Permissions::from_mode(0o700));
    }
    Ok(up)
}

/// The release's program into the node's own folder: the size and SHA-256 its signed line
/// names, or nothing.
async fn download(http: &reqwest::Client, gateway: &Url, rel: &Release, node_dir: &Path) -> Result<PathBuf, String> {
    let up = make_update_dir(node_dir)?;
    let to = up.join(&rel.entry.file);
    if sha256_file(&to).is_ok_and(|s| s == rel.entry.sha256) {
        return Ok(to);
    }
    let part = up.join(format!("{}.part", rel.entry.file));
    let got = fetch_to(http, &format!("{}/node/release/{}", base(gateway), rel.entry.file), &part, rel.entry.size).await;
    match got {
        Ok(sha) if sha == rel.entry.sha256 => {}
        Ok(sha) => {
            let _ = std::fs::remove_file(&part);
            return Err(format!("{}: its SHA-256 is {sha}, not the {} signed", rel.entry.file, rel.entry.sha256));
        }
        Err(e) => {
            let _ = std::fs::remove_file(&part);
            return Err(format!("{}: {e}", rel.entry.file));
        }
    }
    std::fs::rename(&part, &to).map_err(|e| format!("cannot keep {}: {e}", to.display()))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&to, std::fs::Permissions::from_mode(0o700)).map_err(|e| e.to_string())?;
    }
    Ok(to)
}

async fn fetch_to(http: &reqwest::Client, url: &str, path: &Path, size: u64) -> Result<String, String> {
    let mut resp = http.get(url).send().await.map_err(|e| e.to_string())?;
    if resp.status().as_u16() != 200 {
        return Err(format!("HTTP {}", resp.status().as_u16()));
    }
    if resp.content_length().is_some_and(|n| n != size) {
        return Err(format!("the Gateway sends {} bytes, not the {size} signed", resp.content_length().unwrap_or(0)));
    }
    let mut f = tokio::fs::File::create(path).await.map_err(|e| format!("cannot write {}: {e}", path.display()))?;
    let (mut hash, mut total) = (Sha256::new(), 0u64);
    while let Some(chunk) = resp.chunk().await.map_err(|e| e.to_string())? {
        total += chunk.len() as u64;
        if total > size {
            return Err(format!("more than the {size} bytes signed"));
        }
        hash.update(&chunk);
        f.write_all(&chunk).await.map_err(|e| format!("cannot write {}: {e}", path.display()))?;
    }
    f.flush().await.map_err(|e| e.to_string())?;
    if total != size {
        return Err(format!("{total} bytes came, not the {size} signed"));
    }
    Ok(util::hex(&hash.finalize()))
}

/// The new program, run once: `version` must answer this release's version.
fn self_test(file: &Path, rel: &Release) -> Result<(), String> {
    let want = format!("amsyos-node {} ", rel.text);
    match util::run_bounded(file, &["version"], SELF_TEST_S) {
        Some((0, out, _)) if out.trim_start().starts_with(&want) => Ok(()),
        Some((code, out, err)) => Err(format!(
            "the new program failed its self-test (`version`: exit {code}, said {:?})",
            util::cut(format!("{out}{err}").trim(), 200)
        )),
        None => Err(format!("the new program did not answer `version` within {SELF_TEST_S} s")),
    }
}

// ---------------------------------------------------------------------------- what was done

/// Where what was applied is recorded, and the version held back after a rollback: on
/// Windows %ProgramData%\AMSYOS\Node, shared by every profile (so is the program); on Linux
/// the node's own `update` folder (each keeps its own copy).
fn state_dir(node_dir: &Path) -> PathBuf {
    #[cfg(windows)]
    {
        let _ = node_dir;
        config::program_data()
    }
    #[cfg(unix)]
    {
        update_dir(node_dir)
    }
}

#[derive(Serialize, Deserialize, Default)]
struct Record {
    /// The version the last update or rollback put in place, and the one before it.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    version: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    previous: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    at: Option<String>,
    /// Rolled back from: auto-update does not take this version again (a newer one, yes).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    held: Option<String>,
}

impl Record {
    fn load(node_dir: &Path) -> Record {
        std::fs::read(state_dir(node_dir).join(RECORD_FILE)).ok().and_then(|b| serde_json::from_slice(&b).ok()).unwrap_or_default()
    }

    fn save(&self, node_dir: &Path) -> Result<(), String> {
        let dir = state_dir(node_dir);
        std::fs::create_dir_all(&dir).map_err(|e| format!("cannot make {}: {e}", dir.display()))?;
        let body = serde_json::to_vec_pretty(self).map_err(|e| e.to_string())?;
        util::write_file(&dir.join(RECORD_FILE), &body, false).map_err(|e| format!("cannot write {}: {e}", dir.join(RECORD_FILE).display()))
    }
}

/// One update or rollback at a time on the machine (Windows: every profile shares the program).
struct Lock(PathBuf);

impl Lock {
    fn take(dir: &Path) -> Result<Lock, String> {
        std::fs::create_dir_all(dir).map_err(|e| format!("cannot make {}: {e}", dir.display()))?;
        let path = dir.join(LOCK_FILE);
        for _ in 0..2 {
            match std::fs::OpenOptions::new().write(true).create_new(true).open(&path) {
                Ok(mut f) => {
                    let _ = std::io::Write::write_all(&mut f, format!("{} {}\n", std::process::id(), util::now_utc()).as_bytes());
                    return Ok(Lock(path));
                }
                Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {
                    let old = std::fs::metadata(&path).and_then(|m| m.modified()).ok().and_then(|t| t.elapsed().ok());
                    if old.is_some_and(|age| age > LOCK_STALE) {
                        let _ = std::fs::remove_file(&path);
                        continue;
                    }
                    return Err(format!("another node process applies an update now ({}): try again in a minute", path.display()));
                }
                Err(e) => return Err(format!("cannot take {}: {e}", path.display())),
            }
        }
        Err(format!("cannot take {}", path.display()))
    }
}

impl Drop for Lock {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.0);
    }
}

/// `amsyos-node.<tag>.exe` beside `amsyos-node.exe` (`amsyos-node.<tag>` on Linux).
fn sibling(target: &Path, tag: &str) -> PathBuf {
    let name = target.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
    match name.strip_suffix(".exe") {
        Some(stem) => target.with_file_name(format!("{stem}.{tag}.exe")),
        None => target.with_file_name(format!("{name}.{tag}")),
    }
}

/// A program that may still run (Windows refuses to remove it then): removed, else renamed
/// `…old-<hex>` for a later start to remove.
fn set_aside(path: &Path, target: &Path) {
    if path.exists() && std::fs::remove_file(path).is_err() {
        let _ = rename(path, &sibling(target, &format!("old-{}", util::hex(&util::random_bytes::<4>()))));
    }
}

/// A rename, tried again for a few seconds: on Windows a scanner may hold a program just
/// written for a moment.
fn rename(from: &Path, to: &Path) -> std::io::Result<()> {
    let mut tries = 0;
    loop {
        match std::fs::rename(from, to) {
            Err(_) if cfg!(windows) && tries < 5 && from.exists() => {
                tries += 1;
                std::thread::sleep(Duration::from_secs(1));
            }
            done => return done,
        }
    }
}

/// TODO(review): isolation — `target` (the installed program or the job wrapper) replaced by a
/// copy of `src` whose SHA-256 is checked again: the copy written beside it, the program there
/// renamed `…prev` (a running one may be renamed, not overwritten), the copy renamed in its
/// place; the earlier one put back if that fails.
fn place(src: &Path, target: &Path, sha: &str) -> Result<(), String> {
    let (new, prev) = (sibling(target, "new"), sibling(target, "prev"));
    std::fs::copy(src, &new).map_err(|e| format!("cannot write {}: {e}", new.display()))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(&new, std::fs::Permissions::from_mode(0o755));
    }
    if !sha256_file(&new).is_ok_and(|s| s == sha) {
        let _ = std::fs::remove_file(&new);
        return Err(format!("{} is not the release once copied", new.display()));
    }
    set_aside(&prev, target);
    if target.exists() {
        if let Err(e) = rename(target, &prev) {
            let _ = std::fs::remove_file(&new);
            return Err(format!("cannot move {} aside: {e}", target.display()));
        }
    }
    if let Err(e) = rename(&new, target) {
        let _ = rename(&prev, target);
        let _ = std::fs::remove_file(&new);
        return Err(format!("cannot put the new program at {}: {e}", target.display()));
    }
    Ok(())
}

/// Back to `…prev`: the program in place set aside, the previous one renamed back.
fn unplace(target: &Path) -> Result<(), String> {
    let prev = sibling(target, "prev");
    if !prev.is_file() {
        return Err(format!("there is no {} to go back to", prev.display()));
    }
    let aside = sibling(target, &format!("old-{}", util::hex(&util::random_bytes::<4>())));
    if target.exists() {
        rename(target, &aside).map_err(|e| format!("cannot move {} aside: {e}", target.display()))?;
    }
    if let Err(e) = rename(&prev, target) {
        let _ = rename(&aside, target);
        return Err(format!("cannot put {} back: {e}", prev.display()));
    }
    let _ = std::fs::remove_file(&aside); // fails while it runs: a later start removes it
    Ok(())
}

/// What earlier updates left beside a program: copies set aside (`…old-…`), a half copy.
fn clean_beside(target: &Path) {
    let (Some(dir), Some(name)) = (target.parent(), target.file_name().map(|n| n.to_string_lossy().into_owned())) else { return };
    let stem = name.strip_suffix(".exe").unwrap_or(&name).to_string();
    let Ok(rd) = std::fs::read_dir(dir) else { return };
    for e in rd.flatten().take(1000) {
        let n = e.file_name().to_string_lossy().into_owned();
        if n.starts_with(&format!("{stem}.old-")) || n == sibling(target, "new").file_name().map(|s| s.to_string_lossy().into_owned()).unwrap_or_default() {
            let _ = std::fs::remove_file(e.path());
        }
    }
}

/// Puts a verified, self-tested release in place (sdk/NODE.md §7); what it did, for the log.
pub fn apply(node_dir: &Path, program: &Path, rel: &Release, file: &Path) -> Result<String, String> {
    let _lock = Lock::take(&state_dir(node_dir))?;
    let (done, changed) = imp::apply(node_dir, program, rel, file)?;
    if changed {
        let mut rec = Record::load(node_dir);
        rec.previous = Some(env!("CARGO_PKG_VERSION").into());
        rec.version = Some(rel.text.clone());
        rec.at = Some(util::now_utc());
        rec.held = None;
        rec.save(node_dir)?;
    }
    imp::tidy(node_dir);
    Ok(done)
}

// ---------------------------------------------------------------------------- the running node

/// A file's identity as far as a change of it goes.
#[derive(PartialEq, Eq, Clone, Debug)]
struct Seen {
    len: u64,
    modified: Option<SystemTime>,
    ino: u64,
}

fn seen(path: &Path) -> Option<Seen> {
    let m = std::fs::metadata(path).ok()?;
    #[cfg(unix)]
    let ino = std::os::unix::fs::MetadataExt::ino(&m);
    #[cfg(not(unix))]
    let ino = 0;
    Some(Seen { len: m.len(), modified: m.modified().ok(), ino })
}

fn random_below(n: u64) -> u64 {
    u64::from_le_bytes(util::random_bytes::<8>()) % n.max(1)
}

/// Every problem stops nothing: the node goes on with the program it runs.
async fn fetch_ready(http: &reqwest::Client, gateway: &Url, node_dir: &Path) -> Result<Option<(Release, PathBuf)>, String> {
    let rel = fetch_latest(http, gateway).await?;
    if rel.version <= current() || Record::load(node_dir).held.as_deref() == Some(rel.text.as_str()) {
        return Ok(None);
    }
    let file = download(http, gateway, &rel, node_dir).await?;
    let (f, r) = (file.clone(), rel.clone());
    tokio::task::spawn_blocking(move || self_test(&f, &r)).await.map_err(|e| e.to_string())??;
    Ok(Some((rel, file)))
}

/// Closed for a restart when no job runs: a job that comes from now on is refused. `take_job`
/// reads the flag and takes its job without awaiting, so after a second none is half taken.
async fn close(idle: &impl Fn() -> bool) -> bool {
    if !idle() {
        return false;
    }
    RESTARTING.store(true, Ordering::SeqCst);
    tokio::time::sleep(Duration::from_secs(1)).await;
    if idle() {
        return true;
    }
    RESTARTING.store(false, Ordering::SeqCst);
    false
}

/// Node 2.1.9, for `run`: with auto-update on, asks for a newer version some minutes after the
/// start and then every 6 hours (±30 minutes), downloads and checks it, and applies it when no
/// job runs; whatever its setting, restarts into the program once it runs no job when the files
/// that decide what it runs changed (its update, another profile's, `amsyos-node update` or
/// `rollback`). `idle`: no job runs (a busy warm server is a running job's).
pub async fn keep_up(
    node_dir: PathBuf,
    gateway: Url,
    idle: impl Fn() -> bool + Send + Sync + 'static,
    pool: serve::Shared,
    mut stop: watch::Receiver<bool>,
) {
    let program = imp::program();
    let watched: Vec<(PathBuf, Option<Seen>)> = imp::watched(&node_dir, &program).into_iter().map(|p| {
        let s = seen(&p);
        (p, s)
    }).collect();
    imp::clean(&node_dir, &program);
    let rec = Record::load(&node_dir);
    if rec.version.as_deref() == Some(env!("CARGO_PKG_VERSION")) {
        info!("this program came by an update from {} ({})", rec.previous.as_deref().unwrap_or("?"), rec.at.as_deref().unwrap_or("?"));
    }
    let on = Config::load(&node_dir).auto_update == Some(true);
    info!(
        "auto-update: {}",
        if on { "on (new versions signed by AMSYOS, from this Gateway, applied when no job runs)" } else { "off (an administrator here turns it on: amsyos-node auto-update on)" }
    );
    let http = match http() {
        Ok(h) => h,
        Err(e) => {
            warn!("auto-update: {e}");
            return;
        }
    };
    let mut next = Instant::now() + Duration::from_secs(30 + random_below(120));
    let mut ready: Option<(Release, PathBuf)> = None;
    let mut waits_said = false;
    let mut again_at: Option<Instant> = None;
    let mut pending_since: Option<Instant> = None;
    let mut tick = tokio::time::interval(LOOK_EVERY);
    loop {
        tokio::select! {
            _ = tick.tick() => {}
            _ = stop.changed() => return,
        }
        let auto = Config::load(&node_dir).auto_update == Some(true);
        if auto && ready.is_none() && Instant::now() >= next {
            next = Instant::now() + Duration::from_secs(CHECK_EVERY_S - JITTER_S + random_below(2 * JITTER_S));
            match fetch_ready(&http, &gateway, &node_dir).await {
                Ok(Some((rel, file))) => {
                    info!("auto-update: {} is here and checked (AMSYOS's signature, {} bytes, `version`); applied when no job runs", rel.text, rel.entry.size);
                    ready = Some((rel, file));
                }
                Ok(None) => {}
                Err(e) => warn!("auto-update: {e}; asked again in about 6 hours"),
            }
        }
        // Node 2.1.11: a new program waiting since DRAIN_AFTER for a moment with no job.
        let changed = watched.iter().any(|(p, s)| &seen(p) != s);
        if changed || (ready.is_some() && auto) {
            let since = *pending_since.get_or_insert_with(Instant::now);
            let may_restart = again_at.is_none_or(|t| Instant::now() >= t);
            if may_restart && since.elapsed() >= DRAIN_AFTER && !idle() && !DRAINING.swap(true, Ordering::SeqCst) {
                info!("a new program has waited {} minutes for a moment with no job: this node takes no new job (node.exec still runs) until the running ones end, then restarts", DRAIN_AFTER.as_secs() / 60);
            }
        } else {
            pending_since = None;
            DRAINING.store(false, Ordering::SeqCst);
        }
        if ready.is_some() && auto && idle() {
            if let Some((rel, file)) = ready.take() {
                let (nd, pg, text) = (node_dir.clone(), program.clone(), rel.text.clone());
                match tokio::task::spawn_blocking(move || apply(&nd, &pg, &rel, &file)).await {
                    Ok(Ok(done)) => info!("auto-update: {done}"),
                    Ok(Err(e)) => warn!("auto-update: {text} not applied: {e}"),
                    Err(e) => warn!("auto-update: {text} not applied: {e}"),
                }
            }
        }
        if !watched.iter().any(|(p, s)| &seen(p) != s) || again_at.is_some_and(|t| Instant::now() < t) {
            continue;
        }
        if !close(&idle).await {
            if !waits_said {
                info!("the program here was updated: this node restarts into it once no job runs");
                waits_said = true;
            }
            continue;
        }
        info!("restarting into the program updated here (no job runs)");
        serve::stop_all(&pool).await;
        let why = imp::restart(&node_dir, &program).await; // returns only when it could not
        warn!("restart: {why}; this node goes on with the program it runs and tries again in an hour");
        RESTARTING.store(false, Ordering::SeqCst);
        DRAINING.store(false, Ordering::SeqCst);
        pending_since = None;
        again_at = Some(Instant::now() + Duration::from_secs(3600));
    }
}

/// Node 2.1.9, before `run` starts: on Linux, the verified copy kept by an update, when it is
/// newer than this program (sdk/NODE.md §7). Nothing elsewhere.
pub fn at_start() {
    #[cfg(unix)]
    imp::at_start();
}

// ---------------------------------------------------------------------------- the commands

fn current_thread() -> Result<tokio::runtime::Runtime, String> {
    tokio::runtime::Builder::new_current_thread().enable_all().build().map_err(|e| format!("cannot start: {e}"))
}

#[cfg(unix)]
fn refuse_root(what: &str) -> Result<(), String> {
    if sys::is_elevated() {
        return Err(format!(
            "run `amsyos-node {what}` as the user the node runs as, not root (the program it cannot write is kept in its node folder); `sudo amsyos-node setup` of a new program updates amsyos-job too"
        ));
    }
    Ok(())
}

#[cfg(not(unix))]
fn refuse_root(_what: &str) -> Result<(), String> {
    Ok(())
}

/// `amsyos-node update [--check]`: the newest release now. Its files are put in place at once;
/// a node running here restarts into it once it runs no job.
pub fn update_cmd(args: &[String]) -> Result<i32, String> {
    let check_only = match args {
        [] => false,
        [a] if a == "--check" => true,
        _ => return Err("amsyos-node update [--check]".into()),
    };
    refuse_root("update")?;
    let dir = config::node_dir();
    let cfg = Config::load(&dir);
    if !cfg.joined() {
        return Err("this machine has not joined yet: a new version comes from the Gateway it joined".into());
    }
    let gateway = net::check_gateway(&cfg.gateway)?;
    let program = imp::program();
    let now = shown(current());
    current_thread()?.block_on(async {
        let http = http()?;
        let rel = fetch_latest(&http, &gateway).await?;
        if rel.version <= current() {
            println!("up to date: this program is {now}, the newest release {}", rel.text);
            return Ok(0);
        }
        if check_only {
            println!("{} is newer than this program's {now}: `amsyos-node update` takes it", rel.text);
            return Ok(0);
        }
        println!("downloading {} ({} bytes) from {}", rel.entry.file, rel.entry.size, base(&gateway));
        let file = download(&http, &gateway, &rel, &dir).await?;
        let (f, r) = (file.clone(), rel.clone());
        tokio::task::spawn_blocking(move || self_test(&f, &r)).await.map_err(|e| e.to_string())??;
        println!("checked: AMSYOS's signature, the size, SHA-256 {}, and `version` says {}", rel.entry.sha256, rel.text);
        let done = apply(&dir, &program, &rel, &file)?;
        util::log_to(dir.join("node.log"));
        info!("amsyos-node update: {done}");
        println!("a node running here restarts into {} once it runs no job (its node.log says so); `amsyos-node rollback` goes back", rel.text);
        Ok(0)
    })
}

/// `amsyos-node rollback`: back to the program before the last update; the version rolled
/// back from is held back from auto-update.
pub fn rollback_cmd(args: &[String]) -> Result<i32, String> {
    if !args.is_empty() {
        return Err("amsyos-node rollback".into());
    }
    refuse_root("rollback")?;
    let dir = config::node_dir();
    let program = imp::program();
    let _lock = Lock::take(&state_dir(&dir))?;
    let mut rec = Record::load(&dir);
    let (from, to) = imp::rollback(&dir, &program, &rec)?;
    rec.held = from.clone().filter(|v| parse_version(v).is_some());
    rec.version = to.clone().filter(|v| parse_version(v).is_some());
    rec.previous = None;
    rec.at = Some(util::now_utc());
    rec.save(&dir)?;
    let (from, to) = (from.unwrap_or_else(|| "the program updated".into()), to.unwrap_or_else(|| "the earlier program".into()));
    util::log_to(dir.join("node.log"));
    info!("amsyos-node rollback: from {from} back to {to}; {from} is held back");
    println!("back to {to}: a node running here restarts into it once it runs no job");
    println!("{from} is held back: auto-update takes the next newer release, `amsyos-node update` takes {from} again");
    Ok(0)
}

/// `amsyos-node auto-update [on|off]`: at the machine, by an administrator, never from afar.
pub fn auto_update_cmd(args: &[String]) -> Result<i32, String> {
    let (dir, owner) = sys::access_target()?;
    let mut cfg = Config::load(&dir);
    let on = match args {
        [] => {
            let rec = Record::load(&dir);
            println!("auto-update: {}", if cfg.auto_update == Some(true) { "on" } else { "off" });
            println!("this program: {}", env!("CARGO_PKG_VERSION"));
            if let (Some(v), Some(at)) = (&rec.version, &rec.at) {
                println!("last put in place: {v} ({at})");
            }
            if let Some(h) = &rec.held {
                println!("held back after a rollback: {h}");
            }
            return Ok(0);
        }
        [a] if a == "on" => true,
        [a] if a == "off" => false,
        _ => return Err("amsyos-node auto-update on|off".into()),
    };
    if !dir.is_dir() {
        return Err(format!("there is no node folder at {}: join this machine first", dir.display()));
    }
    // TODO(review): isolation — whether new programs are put on this machine by themselves is
    // the machine's administrator's choice, made here; no message from afar changes it.
    if !sys::may_raise_to_full() {
        return Err(if cfg!(windows) {
            "auto-update needs an administrator: run this in an elevated window (Run as administrator)".into()
        } else {
            "auto-update needs an administrator: sudo amsyos-node auto-update on|off".into()
        });
    }
    cfg.auto_update = Some(on);
    cfg.save(&dir)?;
    sys::give_back(&dir.join(config::CONFIG_FILE), owner);
    if on {
        println!("auto-update: on. The running node asks its Gateway for a newer version at its start and every 6 hours, takes only one signed by AMSYOS, and restarts into it when it runs no job (node.log says what it did)");
    } else {
        println!("auto-update: off. `amsyos-node update` takes a new version when you say so");
    }
    println!("a running node reads it within a minute");
    Ok(0)
}

// ---------------------------------------------------------------------------- Windows

#[cfg(windows)]
mod imp {
    use std::io::IsTerminal;
    use std::os::windows::process::CommandExt;
    use std::path::{Path, PathBuf};
    use std::process::Command;
    use std::time::Duration;

    use super::{clean_beside, place, sha256_file, unplace, update_dir, Record, Release};
    use crate::{config, info, sys, util};

    const CREATE_NO_WINDOW: u32 = 0x0800_0000;
    const TR_MOST: usize = 261; // characters of `schtasks /TR`

    pub fn program() -> PathBuf {
        std::env::current_exe().unwrap_or_else(|_| config::installed_exe())
    }

    /// The program every profile's task runs: the node restarts when it changed.
    pub fn watched(_node_dir: &Path, _program: &Path) -> Vec<PathBuf> {
        vec![config::installed_exe()]
    }

    /// And the word a restart script left for a process that was ended before it read it.
    pub fn clean(node_dir: &Path, _program: &Path) {
        clean_beside(&config::installed_exe());
        clean_beside(&config::job_bin());
        let Ok(rd) = std::fs::read_dir(update_dir(node_dir)) else { return };
        for e in rd.flatten().take(1000) {
            let n = e.file_name().to_string_lossy().into_owned();
            if n.starts_with("restart-") && n.ends_with(".result") {
                let _ = std::fs::remove_file(e.path());
            }
        }
    }

    /// The program and the job wrapper, each unless it is the release already (another
    /// profile's node may have put it there).
    pub fn apply(_node_dir: &Path, _program: &Path, rel: &Release, file: &Path) -> Result<(String, bool), String> {
        let (installed, job) = (config::installed_exe(), config::job_bin());
        let is_it = |p: &Path| sha256_file(p).is_ok_and(|s| s == rel.entry.sha256);
        let (put_program, put_job) = (!is_it(&installed), !is_it(&job));
        if !put_program && !put_job {
            return Ok((format!("{} is in place already (another node process here put it there)", rel.text), false));
        }
        if put_program {
            place(file, &installed, &rel.entry.sha256)?;
        }
        if put_job {
            if let Err(e) = place(file, &job, &rel.entry.sha256) {
                let back = if put_program { unplace(&installed).err() } else { None };
                return Err(match back {
                    None => format!("{e}; the program was put back"),
                    Some(b) => format!("{e}; and the program could not be put back: {b}"),
                });
            }
        }
        Ok((format!("{} and {} are {} now (the earlier ones kept as …prev.exe)", installed.display(), job.display(), rel.text), true))
    }

    /// The downloaded program, once it is in place.
    pub fn tidy(node_dir: &Path) {
        let Ok(rd) = std::fs::read_dir(update_dir(node_dir)) else { return };
        for e in rd.flatten().take(1000) {
            if e.file_name().to_string_lossy().starts_with("amsyos-node-") {
                let _ = std::fs::remove_file(e.path());
            }
        }
    }

    pub fn rollback(_node_dir: &Path, _program: &Path, rec: &Record) -> Result<(Option<String>, Option<String>), String> {
        let (installed, job) = (config::installed_exe(), config::job_bin());
        unplace(&installed)?;
        if let Err(e) = unplace(&job) {
            println!("warning: {e}: the job wrapper stays as it is (`amsyos-node setup` of that program in an administrator window copies it)");
        }
        Ok((rec.version.clone(), rec.previous.clone()))
    }

    fn system_root() -> PathBuf {
        PathBuf::from(std::env::var("SystemRoot").ok().filter(|v| !v.is_empty()).unwrap_or_else(|| r"C:\Windows".into()))
    }

    fn schtasks(args: &[&str]) -> Result<(), String> {
        let out = Command::new(system_root().join(r"System32\schtasks.exe"))
            .args(args)
            .creation_flags(CREATE_NO_WINDOW)
            .output()
            .map_err(|e| format!("cannot run schtasks: {e}"))?;
        if out.status.success() {
            return Ok(());
        }
        let said = format!("{}{}", String::from_utf8_lossy(&out.stderr), String::from_utf8_lossy(&out.stdout));
        Err(format!("schtasks {} failed: {}", args.first().unwrap_or(&""), util::cut(said.split_whitespace().collect::<Vec<_>>().join(" ").as_str(), 300)))
    }

    fn ps_quoted(s: &str) -> String {
        format!("'{}'", s.replace('\'', "''"))
    }

    /// Run by a one-shot task of its own, outside the node's (ending the node's task ends
    /// whatever the node started): finds the running task whose process is the node (or the
    /// .cmd that started it) by its process id, ends it and starts it again; says which it did.
    const RESTART_PS1: &str = r#"$ErrorActionPreference = 'Continue'
$node = @NODE@
$result = @RESULT@
$log = @LOG@
function Say([string]$t) {
  $at = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
  Add-Content -LiteralPath $log -Value ($at + ' INFO restart: ' + $t) -Encoding UTF8
}
$parent = 0
try { $parent = [int](Get-CimInstance Win32_Process -Filter "ProcessId=$node").ParentProcessId } catch { }
$task = $null
try {
  $svc = New-Object -ComObject Schedule.Service
  $svc.Connect()
  foreach ($t in @($svc.GetRunningTasks(1))) {
    $engine = [int]$t.EnginePID
    if ($engine -eq $node -or ($parent -gt 0 -and $engine -eq $parent)) { $task = $t.Path; break }
  }
} catch { Say ('the running tasks could not be read: ' + $_) }
if ($task) {
  Set-Content -LiteralPath $result -Value 'task' -Encoding ASCII
  Say ("node process $node runs in the scheduled task $task; ending and starting it")
  schtasks.exe /End /TN $task | Out-Null
  Start-Sleep -Seconds 3
  schtasks.exe /Run /TN $task | Out-Null
  if ($LASTEXITCODE -eq 0) { Say ("the task $task is started again") } else { Say ("the task $task did not start again (schtasks exit $LASTEXITCODE): start it by hand") }
} else {
  Set-Content -LiteralPath $result -Value 'none' -Encoding ASCII
  Say ("node process $node runs in no scheduled task: it starts the new program itself")
}
schtasks.exe /Delete /TN @TASK@ /F | Out-Null
Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
"#;

    /// Starts the program in place with this process's arguments, and ends this process.
    fn start_again(why: &str) -> String {
        let installed = config::installed_exe();
        let exe = if installed.is_file() { installed } else { program() };
        info!("restart: {why}: the node starts {} itself", exe.display());
        let mut cmd = Command::new(&exe);
        cmd.args(std::env::args_os().skip(1));
        if !std::io::stderr().is_terminal() {
            cmd.creation_flags(CREATE_NO_WINDOW);
        }
        match cmd.spawn() {
            Ok(_) => std::process::exit(0),
            Err(e) => format!("could not start {}: {e}", exe.display()),
        }
    }

    /// TODO(review): isolation — restarts this node process only (never another profile's,
    /// which may run a job): a one-shot task of this user (elevated only when the node is)
    /// runs a script of the node's own folder; returns only when that could not be done.
    pub async fn restart(node_dir: &Path, _program: &Path) -> String {
        let up = update_dir(node_dir);
        let pid = std::process::id();
        let (script, result) = (up.join(format!("restart-{pid}.ps1")), up.join(format!("restart-{pid}.result")));
        let task = format!("AMSYOS Node (restart {pid})"); // never a profile's task name
        let _ = std::fs::remove_file(&result);
        let body = RESTART_PS1
            .replace("@NODE@", &pid.to_string())
            .replace("@RESULT@", &ps_quoted(&result.display().to_string()))
            .replace("@LOG@", &ps_quoted(&node_dir.join("node.log").display().to_string()))
            .replace("@TASK@", &ps_quoted(&task));
        let mut bytes = vec![0xEF, 0xBB, 0xBF]; // UTF-8 with a BOM: Windows PowerShell 5.1 reads it so
        bytes.extend_from_slice(body.as_bytes());
        if let Err(e) = std::fs::create_dir_all(&up).and_then(|_| std::fs::write(&script, &bytes)) {
            return start_again(&format!("cannot write {}: {e}", script.display()));
        }
        let ps = system_root().join(r"System32\WindowsPowerShell\v1.0\powershell.exe");
        let tr = format!(
            "\"{}\" -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File \"{}\"",
            ps.display(),
            script.display()
        );
        if tr.chars().count() > TR_MOST {
            return start_again(&format!("the task's command would be {} characters, over the {TR_MOST} schtasks takes", tr.chars().count()));
        }
        let mut create = vec!["/Create", "/TN", task.as_str(), "/TR", tr.as_str(), "/SC", "ONCE", "/ST", "00:00", "/F"];
        if sys::is_elevated() {
            create.extend(["/RL", "HIGHEST"]);
        }
        if let Err(e) = schtasks(&create) {
            return start_again(&e);
        }
        if let Err(e) = schtasks(&["/Run", "/TN", task.as_str()]) {
            let _ = schtasks(&["/Delete", "/TN", task.as_str(), "/F"]);
            return start_again(&e);
        }
        info!("restart: the one-shot task \"{task}\" ends and starts the scheduled task that runs this process ({pid})");
        for _ in 0..90 {
            tokio::time::sleep(Duration::from_secs(1)).await;
            match std::fs::read_to_string(&result).map(|s| s.trim().to_string()).as_deref() {
                Ok("task") => {
                    tokio::time::sleep(Duration::from_secs(180)).await; // ended long before, normally
                    let _ = std::fs::remove_file(&result);
                    return start_again("the scheduled task was not ended within 3 minutes");
                }
                Ok("none") => {
                    let _ = std::fs::remove_file(&result);
                    return start_again("no scheduled task runs this process");
                }
                _ => {}
            }
        }
        start_again("the one-shot task said nothing within 90 s")
    }
}

// ---------------------------------------------------------------------------- Linux

#[cfg(unix)]
mod imp {
    use std::ffi::OsString;
    use std::io::Read;
    use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
    use std::os::unix::process::CommandExt;
    use std::path::{Path, PathBuf};
    use std::process::Command;

    use serde::{Deserialize, Serialize};
    use sha2::{Digest, Sha256};

    use super::{
        clean_beside, current, file_name, parse_version, place, platform, release_key, sha256_file, signed_by, unplace,
        update_dir, Record, Release,
    };
    use crate::{config, info, util, warn};

    /// Set in the copy an update kept, when the installed program execs into it: the copy
    /// does not look for one again; `PROGRAM` names the installed program, for a restart.
    const STAGED_ENV: &str = "AMSYOS_NODE_STAGED";
    const PROGRAM_ENV: &str = "AMSYOS_NODE_PROGRAM";
    const STAGED: &str = "staged.json";
    const STAGED_PREV: &str = "staged.prev.json";

    /// What an update kept in the node's folder, as its signed line says it.
    #[derive(Serialize, Deserialize, Clone)]
    struct Staged {
        version: String,
        platform: String,
        file: String,
        sha256: String,
        size: u64,
        sig: String,
    }

    impl Staged {
        fn load(path: &Path) -> Option<Staged> {
            std::fs::read(path).ok().and_then(|b| serde_json::from_slice(&b).ok())
        }

        /// TODO(review): isolation — the program a start execs into, checked again each time:
        /// AMSYOS's signature, this machine's, a plain file of the node's own folder (never a
        /// link), this account's and writable by it alone, of the size and SHA-256 signed.
        fn verified(&self, up: &Path) -> Result<PathBuf, String> {
            let platform = platform().ok_or("no release is made for this machine")?;
            if self.platform != platform || parse_version(&self.version).is_none() || self.file != file_name(&self.version, platform) {
                return Err(format!("{STAGED} is not understood"));
            }
            signed_by(&release_key(), &self.version, platform, &self.sha256, &self.sig)?;
            let path = up.join(&self.file);
            let mut f = std::fs::OpenOptions::new()
                .read(true)
                .custom_flags(libc::O_NOFOLLOW)
                .open(&path)
                .map_err(|e| format!("{}: {e}", path.display()))?;
            let m = f.metadata().map_err(|e| e.to_string())?;
            if !m.is_file() || m.uid() != unsafe { libc::geteuid() } || m.mode() & 0o022 != 0 {
                return Err(format!("{} is not a plain file of this account's, writable by it alone", path.display()));
            }
            if m.len() != self.size {
                return Err(format!("{} is {} bytes, not the {} signed", path.display(), m.len(), self.size));
            }
            let (mut hash, mut buf) = (Sha256::new(), vec![0u8; 1 << 16]);
            loop {
                let n = f.read(&mut buf).map_err(|e| e.to_string())?;
                if n == 0 {
                    break;
                }
                hash.update(&buf[..n]);
            }
            if util::hex(&hash.finalize()) != self.sha256 {
                return Err(format!("{} is not the program signed", path.display()));
            }
            Ok(path)
        }
    }

    /// The installed program: the one `run` was started as (the copy an update kept names it).
    pub fn program() -> PathBuf {
        if std::env::var_os(STAGED_ENV).is_some() {
            if let Some(p) = std::env::var_os(PROGRAM_ENV).filter(|p| !p.is_empty()) {
                return PathBuf::from(p);
            }
        }
        std::env::current_exe().unwrap_or_else(|_| PathBuf::from("/usr/local/bin/amsyos-node"))
    }

    /// The installed program and what an update kept: the node restarts when either changed.
    pub fn watched(node_dir: &Path, program: &Path) -> Vec<PathBuf> {
        vec![program.to_path_buf(), update_dir(node_dir).join(STAGED)]
    }

    pub fn clean(_node_dir: &Path, program: &Path) {
        if writable(program) {
            clean_beside(program);
        }
    }

    /// This account may replace the program: the file and its folder are writable by it.
    fn writable(path: &Path) -> bool {
        let ok = |p: &Path| {
            std::ffi::CString::new(p.as_os_str().as_encoded_bytes()).is_ok_and(|c| unsafe { libc::access(c.as_ptr(), libc::W_OK) } == 0)
        };
        ok(path) && path.parent().is_some_and(ok)
    }

    pub fn apply(node_dir: &Path, program: &Path, rel: &Release, file: &Path) -> Result<(String, bool), String> {
        if writable(program) {
            if sha256_file(program).is_ok_and(|s| s == rel.entry.sha256) {
                return Ok((format!("{} is in place already", rel.text), false));
            }
            place(file, program, &rel.entry.sha256)?;
            let job = config::job_bin();
            let note = if writable(&job) {
                match place(file, &job, &rel.entry.sha256) {
                    Ok(()) => format!("; {} too", job.display()),
                    Err(e) => format!("; {} stays as it was ({e})", job.display()),
                }
            } else {
                format!("; {} stays as it was until `sudo amsyos-node setup` of the new program", job.display())
            };
            return Ok((format!("{} is {} now (the earlier one kept as {}){note}", program.display(), rel.text, super::sibling(program, "prev").display()), true));
        }
        let up = update_dir(node_dir);
        let staged = Staged::load(&up.join(STAGED));
        if staged.as_ref().is_some_and(|s| s.sha256 == rel.entry.sha256) {
            return Ok((format!("{} is kept here already", rel.text), false));
        }
        if let Some(name) = file.file_name().map(|n| n.to_string_lossy().into_owned()).filter(|n| *n != rel.entry.file) {
            return Err(format!("{name} is not {}", rel.entry.file));
        }
        if staged.is_some() {
            std::fs::rename(up.join(STAGED), up.join(STAGED_PREV)).map_err(|e| format!("cannot keep the earlier copy: {e}"))?;
        }
        let entry = Staged {
            version: rel.text.clone(),
            platform: platform().unwrap_or_default().to_string(),
            file: rel.entry.file.clone(),
            sha256: rel.entry.sha256.clone(),
            size: rel.entry.size,
            sig: rel.entry.sig.clone(),
        };
        let body = serde_json::to_vec_pretty(&entry).map_err(|e| e.to_string())?;
        util::write_file(&up.join(STAGED), &body, true).map_err(|e| format!("cannot write {}: {e}", up.join(STAGED).display()))?;
        Ok((
            format!(
                "{} cannot be replaced by this account: {} is kept in {} and each start runs it from there, its signature checked again ({} stays as it was until `sudo amsyos-node setup` of the new program)",
                program.display(),
                rel.text,
                up.display(),
                config::job_bin().display()
            ),
            true,
        ))
    }

    /// Downloads no copy names any more.
    pub fn tidy(node_dir: &Path) {
        let up = update_dir(node_dir);
        let keep: Vec<String> = [STAGED, STAGED_PREV].iter().filter_map(|f| Staged::load(&up.join(f))).map(|s| s.file).collect();
        let Ok(rd) = std::fs::read_dir(&up) else { return };
        for e in rd.flatten().take(1000) {
            let n = e.file_name().to_string_lossy().into_owned();
            if n.starts_with("amsyos-node-") && !keep.contains(&n) {
                let _ = std::fs::remove_file(e.path());
            }
        }
    }

    pub fn rollback(node_dir: &Path, program: &Path, rec: &Record) -> Result<(Option<String>, Option<String>), String> {
        let up = update_dir(node_dir);
        if let Some(staged) = Staged::load(&up.join(STAGED)) {
            let to = match Staged::load(&up.join(STAGED_PREV)) {
                Some(prev) => {
                    std::fs::rename(up.join(STAGED_PREV), up.join(STAGED)).map_err(|e| e.to_string())?;
                    prev.version
                }
                None => {
                    std::fs::remove_file(up.join(STAGED)).map_err(|e| e.to_string())?;
                    format!("the installed program ({})", program.display())
                }
            };
            super::imp::tidy(node_dir);
            return Ok((Some(staged.version), Some(to)));
        }
        if !writable(program) {
            return Err(format!("no update was kept in {} and {} cannot be written by this account: nothing to go back from", up.display(), program.display()));
        }
        unplace(program)?;
        let job = config::job_bin();
        if writable(&job) && super::sibling(&job, "prev").is_file() {
            let _ = unplace(&job);
        }
        Ok((rec.version.clone(), rec.previous.clone()))
    }

    /// Before `run`: the copy an update kept, when it is newer than this program, checked
    /// again and exec'd into (the same PID; the person sees no difference). This program goes
    /// on when there is none, it is not newer (updated in place since) or it does not check out.
    pub fn at_start() {
        if std::env::var_os(STAGED_ENV).is_some() {
            return;
        }
        let node_dir = config::node_dir();
        let up = update_dir(&node_dir);
        let Some(staged) = Staged::load(&up.join(STAGED)) else { return };
        if parse_version(&staged.version).is_none_or(|v| v <= current()) {
            return;
        }
        util::log_to(node_dir.join("node.log"));
        let path = match staged.verified(&up) {
            Ok(p) => p,
            Err(why) => {
                warn!("the update kept in {} is not used: {why}", up.display());
                return;
            }
        };
        let program = std::env::current_exe().unwrap_or_default();
        info!("starting {} kept in {} by an update (the installed {} is {})", staged.version, up.display(), program.display(), env!("CARGO_PKG_VERSION"));
        let args: Vec<OsString> = std::env::args_os().collect();
        let err = Command::new(&path)
            .arg0(args.first().cloned().unwrap_or_default())
            .args(args.iter().skip(1))
            .env(STAGED_ENV, "1")
            .env(PROGRAM_ENV, &program)
            .exec();
        warn!("could not start {}: {err}; going on with {}", path.display(), env!("CARGO_PKG_VERSION"));
    }

    /// The installed program again, with this process's arguments, in this process (execv:
    /// the same PID); its start takes the copy an update kept. Returns only when it could not.
    pub async fn restart(_node_dir: &Path, program: &Path) -> String {
        let args: Vec<OsString> = std::env::args_os().collect();
        let err = Command::new(program)
            .arg0(args.first().cloned().unwrap_or_default())
            .args(args.iter().skip(1))
            .env_remove(STAGED_ENV)
            .env_remove(PROGRAM_ENV)
            .exec();
        format!("could not start {} again: {err}", program.display())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use ed25519_dalek::{Signer, SigningKey};

    fn latest(key: &SigningKey, version: &str, platform: &str, sha: &str) -> Latest {
        let sig = key.sign(format!("amsyos-node {version} {platform} {sha}").as_bytes());
        let entry = Entry { file: file_name(version, platform), sha256: sha.into(), size: 10, sig: util::b64url(&sig.to_bytes()) };
        Latest { version: version.into(), files: [(platform.to_string(), entry)].into_iter().collect() }
    }

    #[test]
    fn versions() {
        assert_eq!(parse_version("2.1.9"), Some((2, 1, 9)));
        assert!(parse_version("2.1").is_none() && parse_version("2.1.9.1").is_none() && parse_version("2.1.x").is_none());
        assert!(parse_version("2.1.10").unwrap() > parse_version("2.1.9").unwrap());
        assert!(key_bytes(RELEASE_KEY).is_some_and(|k| VerifyingKey::from_bytes(&k).is_ok()));
    }

    #[test]
    fn only_a_signed_release() {
        let Some(platform) = platform() else { return };
        let key = SigningKey::from_bytes(&[7u8; 32]);
        let public = key.verifying_key().to_bytes();
        let sha = "ab".repeat(32);
        let good = latest(&key, "2.1.99", platform, &sha);
        let rel = check(&good, &public).expect("signed");
        assert_eq!(rel.version, (2, 1, 99));
        assert!(check(&good, &release_key()).is_err(), "another key");
        let mut other_sha = latest(&key, "2.1.99", platform, &sha);
        other_sha.files.get_mut(platform).unwrap().sha256 = "cd".repeat(32);
        assert!(check(&other_sha, &public).is_err(), "the sha256 is signed");
        let mut renamed = latest(&key, "2.1.99", platform, &sha);
        renamed.files.get_mut(platform).unwrap().file = "../amsyos-node".into();
        assert!(check(&renamed, &public).is_err(), "the file's name is fixed");
        let mut relabelled = latest(&key, "2.1.99", platform, &sha);
        relabelled.version = "2.2.0".into();
        relabelled.files.get_mut(platform).unwrap().file = file_name("2.2.0", platform);
        assert!(check(&relabelled, &public).is_err(), "the version is signed");
    }

    #[test]
    fn place_and_unplace() {
        let dir = std::env::temp_dir().join(format!("amsyos-update-test-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let (target, src) = (dir.join("amsyos-node"), dir.join("amsyos-node-2.1.99-linux-x86_64"));
        std::fs::write(&target, b"old").unwrap();
        std::fs::write(&src, b"new").unwrap();
        let sha = sha256_file(&src).unwrap();
        place(&src, &target, &sha).unwrap();
        assert_eq!(std::fs::read(&target).unwrap(), b"new");
        assert_eq!(std::fs::read(sibling(&target, "prev")).unwrap(), b"old");
        assert!(place(&src, &target, &"00".repeat(32)).is_err(), "a copy that is not the release");
        assert_eq!(std::fs::read(&target).unwrap(), b"new");
        unplace(&target).unwrap();
        assert_eq!(std::fs::read(&target).unwrap(), b"old");
        let _ = std::fs::remove_dir_all(&dir);
    }
}

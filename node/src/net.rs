//! Protocol 1 (sdk/NODE.md): joining, the WebSocket the node dials out on, jobs and their
//! files. TLS is rustls with the webpki roots (no OpenSSL); plain http/ws only to loopback.
//! The node talks to the Gateway the person typed and to nothing else: every link a job
//! message carries must be of the same origin.

use std::collections::{BTreeMap, HashMap};
use std::io::{Seek, SeekFrom};
use std::net::IpAddr;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use ed25519_dalek::{Signer, SigningKey};
use futures_util::{SinkExt, StreamExt};
use reqwest::header::{CONTENT_LENGTH, CONTENT_TYPE};
use reqwest::Url;
use serde_json::{json, Map, Value};
use tokio::io::AsyncWriteExt;
use tokio::sync::{mpsc, watch, Notify, RwLock};
use tokio_tungstenite::tungstenite::protocol::WebSocketConfig;
use tokio_tungstenite::tungstenite::Message;
use tokio_tungstenite::Connector;

use crate::config::{self, Access, Config, Service, Services};
use crate::exec::{self, Limits};
use crate::{builds, folder, info, machine, mem, pymod, serve, sys, util, warn};

const FRAME_MAX: usize = 64 << 10;
const HINT_MAX: usize = 500;
/// Node 2.1.5: the disk is measured at most this often (a walk of a big folder costs), and
/// soon after a deploy or an undeploy, but never twice within MEASURE_GAP.
const MEASURE_S: u64 = 600;
const MEASURE_GAP: Duration = Duration::from_secs(60);

pub fn tls() -> rustls::ClientConfig {
    let mut roots = rustls::RootCertStore::empty();
    roots.extend(webpki_roots::TLS_SERVER_ROOTS.iter().cloned());
    rustls::ClientConfig::builder_with_provider(Arc::new(rustls::crypto::ring::default_provider()))
        .with_safe_default_protocol_versions()
        .expect("TLS 1.2 and 1.3 with ring")
        .with_root_certificates(roots)
        .with_no_client_auth()
}

fn http() -> Result<reqwest::Client, String> {
    reqwest::Client::builder()
        .use_preconfigured_tls(tls())
        .redirect(reqwest::redirect::Policy::none())
        .connect_timeout(Duration::from_secs(15))
        .read_timeout(Duration::from_secs(120))
        .user_agent(concat!("amsyos-node/", env!("CARGO_PKG_VERSION")))
        .build()
        .map_err(|e| format!("cannot make the HTTP client: {e}"))
}

fn loopback(url: &Url) -> bool {
    match url.host_str() {
        Some("localhost") => true,
        Some(h) => h.trim_start_matches('[').trim_end_matches(']').parse::<IpAddr>().is_ok_and(|ip| ip.is_loopback()),
        None => false,
    }
}

/// The Gateway's base URL: https (http only to this machine), no user, query or fragment.
pub fn check_gateway(raw: &str) -> Result<Url, String> {
    let url = Url::parse(raw.trim()).map_err(|e| format!("--gateway {raw:?}: {e}"))?;
    let ok_scheme = url.scheme() == "https" || (url.scheme() == "http" && loopback(&url));
    if !ok_scheme || url.host_str().is_none() || !url.username().is_empty() || url.password().is_some()
        || url.query().is_some() || url.fragment().is_some()
    {
        return Err(format!("--gateway {raw:?}: an https:// address such as https://103.77.173.205 (http:// only for this machine)"));
    }
    Ok(url)
}

fn base(url: &Url) -> String {
    url.as_str().trim_end_matches('/').to_string()
}

fn same_origin(u: &Url, g: &Url) -> bool {
    u.scheme() == g.scheme() && u.host_str() == g.host_str() && u.port_or_known_default() == g.port_or_known_default()
}

/// The WebSocket address: the Gateway's answer if it is on the Gateway's own host (wss, or
/// ws to this machine), else made from the Gateway's address.
fn ws_url(told: &str, gateway: &Url) -> Url {
    if let Ok(u) = Url::parse(told) {
        let scheme_ok = u.scheme() == "wss" || (u.scheme() == "ws" && loopback(&u));
        if scheme_ok && u.host_str() == gateway.host_str() {
            return u;
        }
    }
    let scheme = if gateway.scheme() == "https" { "wss" } else { "ws" };
    let mut u = gateway.clone();
    let _ = u.set_scheme(scheme);
    u.set_path(&format!("{}/node/ws", gateway.path().trim_end_matches('/')));
    u
}

fn load_key(dir: &Path) -> Result<SigningKey, String> {
    let raw = std::fs::read(dir.join(config::KEY_FILE)).map_err(|_| "the node's key is missing: join again".to_string())?;
    let seed: [u8; 32] = raw.as_slice().try_into().map_err(|_| "the node's key is damaged: join again".to_string())?;
    Ok(SigningKey::from_bytes(&seed))
}

fn round1(x: f64) -> f64 {
    (x * 10.0).round() / 10.0
}

// --------------------------------------------------------------------------------- joining

pub async fn join(code: &str, gateway: &str, server: bool, name: Option<String>) -> Result<(), String> {
    let code = code.trim().to_uppercase();
    if !(4..=64).contains(&code.len()) || !code.chars().all(|c| c.is_ascii_alphanumeric() || c == '-') {
        return Err("the join code looks like K7QD-M2XP-9HFA".into());
    }
    let g = check_gateway(gateway)?;
    let dir = config::node_dir();
    config::ensure_node_dir(&dir)?;
    let mut cfg = Config::load(&dir);
    let (jobs_root, nd) = (cfg.jobs_root(), dir.clone());
    let (machine, _, note) = tokio::task::spawn_blocking(move || machine::probe_checked(&jobs_root, &nd)).await.map_err(|e| e.to_string())?;
    println!("{note}");
    let name = util::cut(name.as_deref().unwrap_or(&machine.hostname).trim(), 60);
    let key = SigningKey::from_bytes(&util::random_bytes::<32>());
    let kind = if server { "server" } else { "workstation" };
    let body = json!({
        "code": code, "key": util::b64url(&key.verifying_key().to_bytes()),
        "kind": kind, "name": name, "machine": machine,
    });
    let resp = http()?
        .post(format!("{}/node/join", base(&g)))
        .header(CONTENT_TYPE, "application/json")
        .body(body.to_string())
        .timeout(Duration::from_secs(60))
        .send()
        .await
        .map_err(|e| format!("cannot reach {}: {e}", base(&g)))?;
    let status = resp.status();
    let text = resp.text().await.unwrap_or_default();
    let told: Value = serde_json::from_str(&text).unwrap_or(Value::Null);
    if !status.is_success() {
        let error = told.get("error").and_then(Value::as_str).unwrap_or("REFUSED");
        let hint = told.get("hint").and_then(Value::as_str).map(String::from).unwrap_or_else(|| util::cut(&text, 200));
        return Err(format!("the Gateway refused to join this machine (HTTP {}): {error}: {hint}", status.as_u16()));
    }
    let field = |k: &str| told.get(k).and_then(Value::as_str).unwrap_or("").to_string();
    let (node, org, kind_told, ws) = (field("node"), field("org"), field("kind"), field("ws"));
    if !util::is_node_id(&node) || !util::is_org(&org) || !matches!(kind_told.as_str(), "workstation" | "server") {
        return Err(format!("the Gateway's answer is not understood: {}", util::cut(&text, 200)));
    }
    util::write_file(&dir.join(config::KEY_FILE), key.to_bytes().as_slice(), true)
        .map_err(|e| format!("cannot keep the key: {e}"))?;
    if !cfg.node.is_empty() && cfg.node != node {
        println!("this machine was {} before; it is {node} now", cfg.node);
    }
    cfg.gateway = base(&g);
    cfg.ws = ws_url(&ws, &g).to_string();
    cfg.node = node.clone();
    cfg.org = org.clone();
    cfg.kind = kind_told.clone();
    cfg.name = Some(name);
    cfg.save(&dir)?;
    println!("joined: node {node} of {org}, a {kind_told}");
    println!("next: switch a service on (amsyos-node enable …), then: amsyos-node run");
    Ok(())
}

// ------------------------------------------------------------------------------ the node

struct JobMsg {
    id: String,
    service: String,
    version: String,
    params: Value,
    inputs: BTreeMap<String, (Url, String)>, // name → (link, in/<file>)
    outputs: Vec<String>,
    put: String,
    limits: Limits,
    /// `node.exec`: its working folder (None: the job's folder).
    exec: Option<Option<PathBuf>>,
    /// Runs as the node's own account (node.exec, or an `as_node` service's direct job at full).
    as_node: bool,
    /// Node 2.1: input → (folder name, path under it), read in place (folder.rs).
    folders: BTreeMap<String, (String, String)>,
    /// Node 2.1.2: who may share a warm server with this job, as the kernel says it (a digest
    /// of its workspace and who pays); None: a server of its own (serve.rs).
    serve_key: Option<String>,
}

struct Ctx {
    node_dir: PathBuf,
    jobs_root: PathBuf,
    gateway: Url,
    http: reqwest::Client,
    server: bool,
    slots: u32,
    out: mpsc::UnboundedSender<String>,
    running: Mutex<HashMap<String, watch::Sender<bool>>>,
    /// The level and services the last hello or beat said.
    said: Mutex<String>,
    /// Node 2.1.2: the servers kept warm.
    pool: serve::Shared,
    /// Node 2.1.5: held (read) by each node.exec while it runs, taken (write) by a cleaning of
    /// the builds, which waits for none and is done at the next chance instead (builds.rs).
    builds: Arc<RwLock<()>>,
    /// Node 2.1.5: the command lines of the jobs running now (they name their build's image).
    using: Mutex<HashMap<String, Vec<String>>>,
    /// Node 2.1.5: measure the disk soon (a deploy or an undeploy changed it).
    disk_now: Notify,
}

impl Ctx {
    fn send(&self, v: Value) {
        let _ = self.out.send(v.to_string());
    }

    /// Node 2.1.5: every command line that may name a build's image: each service of
    /// services.json (on or off, deployed or switched on here) and each job running now.
    fn lines_in_use(&self) -> Vec<String> {
        let mut lines: Vec<String> = Services::load(&self.node_dir).services.values().flat_map(Service::lines).collect();
        if let Ok(u) = self.using.lock() {
            lines.extend(u.values().flatten().cloned());
        }
        lines
    }

    fn running_ids(&self) -> Vec<String> {
        self.running.lock().map(|r| r.keys().cloned().collect()).unwrap_or_default()
    }

    fn access(&self) -> Access {
        Config::load(&self.node_dir).access()
    }

    /// The level, the default `as_node`, the services that are on and the names of the
    /// folders allowed here (never their paths), as a hello or beat says them.
    fn said_now(&self) -> (Access, bool, Vec<String>, Vec<String>, String) {
        let cfg = Config::load(&self.node_dir);
        let (access, default, ids) = (cfg.access(), cfg.as_node_by_default(), Services::load(&self.node_dir).ids());
        let folders = cfg.folder_names();
        let key = format!("{}|{default}|{}|{}", access.as_str(), ids.join(","), folders.join(","));
        (access, default, ids, folders, key)
    }

    fn activity(&self, t: &str) -> Map<String, Value> {
        let idle = if self.server { None } else { sys::idle_seconds() };
        let (access, default, ids, folders, key) = self.said_now();
        if let Ok(mut said) = self.said.lock() {
            *said = key;
        }
        let v = json!({
            "t": t,
            "services": ids,
            "running": self.running_ids(),
            "slots": self.slots,
            "idle_s": idle,
            "load": sys::cpu_load(),
            "access": access.as_str(),
            "as_node_default": default,
            "folders": folders,
        });
        match v {
            Value::Object(m) => m,
            _ => Map::new(),
        }
    }

    fn stop_all(&self) {
        if let Ok(r) = self.running.lock() {
            for tx in r.values() {
                let _ = tx.send(true);
            }
        }
    }

    fn state(&self, id: &str, status: &str) -> Map<String, Value> {
        let mut m = Map::new();
        m.insert("t".into(), "state".into());
        m.insert("job".into(), id.into());
        m.insert("status".into(), status.into());
        m
    }

    fn refuse(&self, id: &str, hint: &str) {
        info!("job {id}: refused: {hint}");
        let mut m = self.state(id, "refused");
        m.insert("hint".into(), util::cut(hint, HINT_MAX).into());
        self.send(Value::Object(m));
    }

    fn failed(&self, id: &str, error: &str, hint: &str, said: &str) -> Value {
        let mut m = self.state(id, "failed");
        m.insert("error".into(), error.into());
        m.insert("hint".into(), util::cut(hint, HINT_MAX).into());
        m.insert("said".into(), util::tail(said, 2000).into());
        Value::Object(m)
    }
}

enum End {
    Lost(String, bool), // why, whether a welcome came first
    JoinAgain(u16),
    Replaced,
}

fn closed(code: Option<u16>, welcomed: bool) -> End {
    match code {
        Some(c @ (4401 | 4403)) => End::JoinAgain(c),
        Some(4409) => End::Replaced,
        Some(c) => End::Lost(format!("closed by the Gateway ({c})"), welcomed),
        None => End::Lost("closed by the Gateway".into(), welcomed),
    }
}

async fn wait_signal() {
    #[cfg(unix)]
    {
        use tokio::signal::unix::{signal, SignalKind};
        match signal(SignalKind::terminate()) {
            Ok(mut term) => {
                tokio::select! {
                    _ = tokio::signal::ctrl_c() => {}
                    _ = term.recv() => {}
                }
            }
            Err(_) => {
                let _ = tokio::signal::ctrl_c().await;
            }
        }
    }
    #[cfg(not(unix))]
    {
        let _ = tokio::signal::ctrl_c().await;
    }
}

pub async fn run(allow_elevated: bool) -> Result<i32, String> {
    sys::refuse_identity(allow_elevated)?;
    let node_dir = config::node_dir();
    let cfg = Config::load(&node_dir);
    if !cfg.joined() {
        return Err("this machine has not joined yet: amsyos-node join <code> --gateway https://<host>".into());
    }
    util::log_to(node_dir.join("node.log"));
    sys::preflight()?;
    let (dir, root, on) = (node_dir.clone(), cfg.jobs_root(), Services::load(&node_dir));
    tokio::task::spawn_blocking(move || crate::guard::guard(&dir, &root, &on)).await.map_err(|e| e.to_string())??;
    info!("checked as {}: it cannot write to this program's folder, bin, the node's folders or a service's folder", sys::JOB_USER);
    let key = load_key(&node_dir)?;
    let gateway = check_gateway(&cfg.gateway)?;
    let ws = ws_url(&cfg.ws, &gateway);
    let jobs_root = cfg.jobs_root();
    let root = jobs_root.clone();
    let nd = node_dir.clone();
    let (machine, _, note) = tokio::task::spawn_blocking(move || machine::probe_checked(&root, &nd)).await.map_err(|e| e.to_string())?;
    info!("{note}");
    // Node 2.1.2: the Python job module on every job's PYTHONPATH, and `amsyos-job` (`next`,
    // `done`) the same program as this one; `setup` writes both where the node's user may not.
    match tokio::task::spawn_blocking(pymod::install).await.map_err(|e| e.to_string())? {
        Ok(true) => info!("the Python job module: written in {}", config::python_dir().display()),
        Ok(false) => {}
        Err(e) => warn!("the Python job module is missing or old ({e}): run `{}` again", setup_hint()),
    }
    if !same_program() {
        warn!("{} is not this program's copy: run `{}` again, or servers (`next`, `done`) and new job features fail", config::job_bin().display(), setup_hint());
    }
    let left = jobs_root.clone();
    let _ = tokio::task::spawn_blocking(move || serve::clean_left(&left)).await;
    let (tx, mut rx) = mpsc::unbounded_channel();
    let slots = cfg.slots(machine.cpus);
    let ctx = Arc::new(Ctx {
        node_dir,
        jobs_root: jobs_root.clone(),
        gateway,
        http: http()?,
        server: cfg.is_server(),
        slots,
        out: tx,
        running: Mutex::new(HashMap::new()),
        said: Mutex::new(String::new()),
        pool: serve::Pool::new(slots.max(2) as usize, machine.ram_gb),
        builds: Arc::new(RwLock::new(())),
        using: Mutex::new(HashMap::new()),
        disk_now: Notify::new(),
    });
    info!(
        "AMSYOS Node {} — node {} of {}, a {}, {} slot(s), jobs in {}, Gateway {}",
        env!("CARGO_PKG_VERSION"), cfg.node, cfg.org, cfg.kind, ctx.slots, jobs_root.display(), ws
    );
    info!(
        "access level: {}{}",
        cfg.access().as_str(),
        if cfg.as_node_by_default() { "; the default as_node: on (what is switched on here runs as this account, unless --as-job)" } else { "" }
    );
    if !cfg.folders.is_empty() {
        info!("folders read in place (the Gateway is told the names only): {}", cfg.folder_names().join(", "));
    }
    let (stop_tx, mut stop) = watch::channel(false);
    tokio::spawn(async move {
        wait_signal().await;
        let _ = stop_tx.send(true);
    });
    tokio::spawn(serve::reap(ctx.pool.clone(), ctx.node_dir.clone(), stop.clone()));
    tokio::spawn(watch_disk(ctx.clone(), stop.clone()));

    let mut backoff = 1u64;
    let mut pending: Option<String> = None;
    loop {
        let end = tokio::select! {
            end = session(&ctx, &key, &cfg.node, &ws, &machine, &mut rx, &mut pending) => end,
            _ = stop.changed() => break,
        };
        let pause = match end {
            End::Replaced => {
                warn!("a newer connection of this node took over (4409): this one stops");
                ctx.stop_all();
                serve::stop_all(&ctx.pool).await;
                return Ok(0);
            }
            End::JoinAgain(code) => {
                warn!("the Gateway does not know this node any more ({code}): join again (amsyos-node join <code> --gateway …); trying again in 10 minutes");
                backoff = 1;
                Duration::from_secs(600)
            }
            End::Lost(why, welcomed) => {
                if welcomed {
                    backoff = 1;
                }
                let jitter = u64::from(u16::from_le_bytes(util::random_bytes::<2>())) % (backoff * 250 + 1);
                let pause = Duration::from_millis(backoff * 1000 + jitter);
                warn!("connection: {why}; again in {:.1} s", pause.as_secs_f64());
                backoff = (backoff * 2).min(60);
                pause
            }
        };
        tokio::select! {
            _ = tokio::time::sleep(pause) => {}
            _ = stop.changed() => break,
        }
    }
    info!("stopping: the running jobs are stopped");
    ctx.stop_all();
    for _ in 0..100 {
        if ctx.running_ids().is_empty() {
            break;
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    serve::stop_all(&ctx.pool).await;
    Ok(0)
}

fn setup_hint() -> &'static str {
    if cfg!(windows) { "amsyos-node setup (in an administrator window)" } else { "sudo amsyos-node setup" }
}

/// Whether the `amsyos-job` copy jobs run is this very program (`setup` copies it).
fn same_program() -> bool {
    let read = |p: &Path| std::fs::read(p).ok();
    match (std::env::current_exe().ok().as_deref().and_then(read), read(&config::job_bin())) {
        (Some(a), Some(b)) => a == b,
        _ => false,
    }
}

type Ws = tokio_tungstenite::WebSocketStream<tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>>;

/// The next text message as JSON; Err(close code) when the connection ends.
async fn next_json(ws: &mut Ws) -> Result<Value, Option<u16>> {
    loop {
        match ws.next().await {
            Some(Ok(Message::Text(t))) => return Ok(serde_json::from_str(t.as_str()).unwrap_or(Value::Null)),
            Some(Ok(Message::Close(frame))) => return Err(frame.map(|f| u16::from(f.code))),
            Some(Ok(_)) => continue,
            Some(Err(_)) | None => return Err(None),
        }
    }
}

async fn session(
    ctx: &Arc<Ctx>,
    key: &SigningKey,
    node: &str,
    ws_url: &Url,
    machine: &machine::Machine,
    rx: &mut mpsc::UnboundedReceiver<String>,
    pending: &mut Option<String>,
) -> End {
    let config = WebSocketConfig::default().max_message_size(Some(FRAME_MAX)).max_frame_size(Some(FRAME_MAX));
    let connect = tokio_tungstenite::connect_async_tls_with_config(
        ws_url.as_str(),
        Some(config),
        true,
        Some(Connector::Rustls(Arc::new(tls()))),
    );
    let mut ws = match tokio::time::timeout(Duration::from_secs(20), connect).await {
        Ok(Ok((ws, _))) => ws,
        Ok(Err(e)) => return End::Lost(format!("cannot connect: {e}"), false),
        Err(_) => return End::Lost("cannot connect: timed out".into(), false),
    };

    // 1. challenge → 2. hello
    let nonce = match tokio::time::timeout(Duration::from_secs(10), next_json(&mut ws)).await {
        Ok(Ok(v)) if v.get("t").and_then(Value::as_str) == Some("challenge") => {
            match v.get("nonce").and_then(Value::as_str) {
                Some(n) if !n.is_empty() && n.len() <= 200 => n.to_string(),
                _ => return End::Lost("the challenge has no nonce".into(), false),
            }
        }
        Ok(Err(code)) => return closed(code, false),
        _ => return End::Lost("no challenge came".into(), false),
    };
    let sig = key.sign(format!("amsyos-node-hello\n{node}\n{nonce}").as_bytes());
    let mut fresh = machine.clone();
    fresh.disk_free_gb = machine::disk_free_gb(&ctx.jobs_root);
    let mut hello = ctx.activity("hello");
    hello.insert("node".into(), node.into());
    hello.insert("sig".into(), util::b64url(&sig.to_bytes()).into());
    hello.insert("machine".into(), serde_json::to_value(&fresh).unwrap_or(Value::Null));
    if let Err(e) = ws.send(Message::text(Value::Object(hello).to_string())).await {
        return End::Lost(format!("cannot say hello: {e}"), false);
    }

    // 3. welcome, or refused and a close
    let beat_s = match tokio::time::timeout(Duration::from_secs(15), next_json(&mut ws)).await {
        Ok(Ok(v)) if v.get("t").and_then(Value::as_str) == Some("welcome") => {
            let beat = v.get("beat_s").and_then(Value::as_u64).unwrap_or(30).clamp(5, 300);
            info!(
                "online: node {} of {}, a {}, a beat every {beat} s",
                v.get("node").and_then(Value::as_str).unwrap_or(node),
                v.get("org").and_then(Value::as_str).unwrap_or("?"),
                v.get("kind").and_then(Value::as_str).unwrap_or("?")
            );
            beat
        }
        Ok(Ok(v)) if v.get("t").and_then(Value::as_str) == Some("refused") => {
            warn!(
                "refused by the Gateway: {}: {}",
                util::cut(v.get("error").and_then(Value::as_str).unwrap_or("?"), 40),
                util::cut(v.get("hint").and_then(Value::as_str).unwrap_or(""), HINT_MAX)
            );
            let code = match tokio::time::timeout(Duration::from_secs(5), next_json(&mut ws)).await {
                Ok(Err(code)) => code,
                _ => None,
            };
            return closed(code, false);
        }
        Ok(Err(code)) => return closed(code, false),
        _ => return End::Lost("no welcome came".into(), false),
    };

    if let Some(m) = pending.take() {
        if let Err(e) = ws.send(Message::text(m.clone())).await {
            *pending = Some(m);
            return End::Lost(format!("cannot send: {e}"), true);
        }
    }
    let period = Duration::from_secs(beat_s);
    let mut beat = tokio::time::interval_at(tokio::time::Instant::now() + period, period);
    let mut look = tokio::time::interval(Duration::from_secs(2));
    let mut heard = Instant::now();
    loop {
        tokio::select! {
            _ = look.tick() => {
                // A beat at once when the level, the default or the services changed
                // (`access`, `enable`…).
                let (access, default, _, _, key) = ctx.said_now();
                let before = ctx.said.lock().map(|s| s.clone()).unwrap_or_default();
                if key != before {
                    let mut was = before.split('|');
                    if was.next() != Some(access.as_str()) {
                        info!("access level: {}", access.as_str());
                    }
                    if was.next() != Some(if default { "true" } else { "false" }) {
                        info!("the default as_node: {}", if default { "on" } else { "off" });
                    }
                    let b = Value::Object(ctx.activity("beat")).to_string();
                    if let Err(e) = ws.send(Message::text(b)).await {
                        return End::Lost(format!("cannot beat: {e}"), true);
                    }
                }
            }
            msg = ws.next() => {
                heard = Instant::now();
                match msg {
                    Some(Ok(Message::Text(t))) => handle(ctx, t.as_str()),
                    Some(Ok(Message::Ping(_))) => {
                        let _ = ws.flush().await; // the pong tungstenite queued goes now
                    }
                    Some(Ok(Message::Close(frame))) => return closed(frame.map(|f| u16::from(f.code)), true),
                    Some(Ok(_)) => {}
                    Some(Err(e)) => return End::Lost(e.to_string(), true),
                    None => return End::Lost("the connection ended".into(), true),
                }
            }
            Some(out) = rx.recv() => {
                if let Err(e) = ws.send(Message::text(out.clone())).await {
                    *pending = Some(out);
                    return End::Lost(format!("cannot send: {e}"), true);
                }
            }
            _ = beat.tick() => {
                if heard.elapsed() > period * 3 + Duration::from_secs(10) {
                    return End::Lost("nothing heard from the Gateway for too long".into(), true);
                }
                let b = Value::Object(ctx.activity("beat")).to_string();
                if let Err(e) = ws.send(Message::text(b)).await {
                    return End::Lost(format!("cannot beat: {e}"), true);
                }
                let _ = ws.send(Message::Ping(Default::default())).await;
            }
        }
    }
}

fn handle(ctx: &Arc<Ctx>, text: &str) {
    let v: Value = serde_json::from_str(text).unwrap_or(Value::Null);
    match v.get("t").and_then(Value::as_str) {
        Some("job") => take_job(ctx, &v),
        Some("action") => take_action(ctx, &v),
        Some("cancel") => {
            let id = v.get("job").and_then(Value::as_str).unwrap_or("");
            let found = ctx.running.lock().ok().and_then(|r| r.get(id).map(|tx| tx.send(true)));
            if found.is_some() {
                info!("job {id}: cancel asked");
            }
        }
        Some("welcome") | Some("challenge") => {}
        other => warn!("a message not understood: {:?}", other.map(|t| util::cut(t, 20))),
    }
}

/// Checks a job message (every problem at once) and takes, refuses or fails it.
fn take_job(ctx: &Arc<Ctx>, v: &Value) {
    let Some(id) = v.get("job").and_then(Value::as_str).filter(|j| util::is_job_id(j)) else {
        warn!("a job message without a valid job id: left alone");
        return;
    };
    if ctx.running.lock().map(|r| r.contains_key(id)).unwrap_or(false) {
        ctx.send(Value::Object(ctx.state(id, "running")));
        return;
    }
    let service = v.get("service").and_then(Value::as_str).unwrap_or("").to_string();
    let direct = v.get("direct").and_then(Value::as_bool).unwrap_or(false);
    let cfg = Config::load(&ctx.node_dir);
    let access = cfg.access();
    let mut problems = Vec::new();
    // TODO(review): isolation — what runs as the node's own account: `node.exec` only at
    // `full` and only for a direct job (the organisation's own call, never an order or a
    // step); an `as_node` service the same way, else as amsyos-job like any other.
    let (svc, exec, as_node) = if service == "node.exec" {
        if access != Access::Full {
            ctx.refuse(id, &format!(
                "node.exec needs the access level full at this machine (it is {}): an administrator there runs `amsyos-node access full`",
                access.as_str()
            ));
            return;
        }
        if !direct {
            ctx.refuse(id, "node.exec runs only for a direct call of the organisation itself, never for an order or a step");
            return;
        }
        let e = v.get("exec").cloned().unwrap_or(Value::Null);
        let command = e.get("command").and_then(Value::as_str).unwrap_or("").trim().to_string();
        if command.is_empty() || command.len() > 8000 || command.chars().any(|c| c.is_control() && c != '\t') {
            problems.push("exec.command: one line, at most 8000 characters".to_string());
        }
        let cwd = match e.get("cwd") {
            None | Some(Value::Null) => None,
            Some(Value::String(d)) if Path::new(d).is_absolute() && Path::new(d).is_dir() => Some(PathBuf::from(d)),
            Some(other) => {
                problems.push(format!("exec.cwd: a folder that exists here, or null (not {})", util::cut(&other.to_string(), 200)));
                None
            }
        };
        let by = util::cut(e.get("by").and_then(Value::as_str).unwrap_or("?"), 80);
        info!("node.exec {id} by {by}: {}{}", util::cut(&command, 500),
              cwd.as_ref().map(|d| format!(" (in {})", d.display())).unwrap_or_default());
        let dir = cwd.clone().unwrap_or_default();
        (Service::new(dir, command, false), Some(cwd), true)
    } else {
        let services = Services::load(&ctx.node_dir);
        let Some(svc) = services.services.get(&service).filter(|s| s.on).cloned() else {
            ctx.refuse(id, &format!("the service {} is not switched on at this machine", util::cut(&service, 200)));
            return;
        };
        // TODO(review): isolation — the account is chosen here. A service deployed `as_node`,
        // or switched on here while the node's default `as_node` is on (and not `--as-job`),
        // runs as the node's own account only for a DIRECT job (the owning organisation's own
        // call) at `full`. A job of an order (another organisation, the buyer) is never
        // direct, nor a step of a recipe or a mission: it always runs as amsyos-job, whatever
        // the service or the default.
        let wants = svc.runs_as_node(cfg.as_node_by_default());
        let as_node = wants && direct && access == Access::Full;
        if wants {
            let by = util::cut(v.get("by").and_then(Value::as_str).unwrap_or("?"), 80);
            info!(
                "job {id} of {service} by {by}: {}",
                if as_node {
                    format!("as the node's own account ({}, direct, full)", svc.as_node_why())
                } else {
                    "as amsyos-job (as_node needs a direct job at full)".to_string()
                }
            );
        }
        (svc, None, as_node)
    };
    let version = util::cut(v.get("version").and_then(Value::as_str).unwrap_or(""), 64);
    let params = v.get("params").cloned().unwrap_or_else(|| json!({}));
    if !params.is_object() {
        problems.push("params: an object".to_string());
    }
    let mut inputs = BTreeMap::new();
    let mut files = std::collections::BTreeSet::new();
    match v.get("inputs") {
        None | Some(Value::Null) => {}
        Some(Value::Object(m)) if m.len() <= 64 => {
            for (name, spec) in m {
                let url = spec.get("url").and_then(Value::as_str).and_then(|u| Url::parse(u).ok());
                let file = spec.get("file").and_then(Value::as_str).unwrap_or("");
                let shown = util::cut(name, 64);
                let mut ok = true;
                if !util::is_name(name) {
                    problems.push(format!("inputs: {shown:?} is no input name"));
                    ok = false;
                }
                if !util::is_in_file(file) || !files.insert(file.to_string()) {
                    problems.push(format!("inputs.{shown}.file: in/<name>.<ext>, each once"));
                    ok = false;
                }
                match url.filter(|u| same_origin(u, &ctx.gateway)) {
                    Some(url) if ok => {
                        inputs.insert(name.clone(), (url, file.to_string()));
                    }
                    Some(_) => {}
                    None => problems.push(format!("inputs.{shown}.url: a link of the Gateway {}", base(&ctx.gateway))),
                }
            }
        }
        _ => problems.push("inputs: an object of at most 64 inputs".into()),
    }
    // Node 2.1 (spec v3 §8.3): inputs read in place from a folder the person allowed here.
    // TODO(review): isolation — only a folder allowed at this machine (checked here and again
    // when the job starts); the path is checked as the kernel checks it and resolved without
    // following a link (folder.rs).
    let mut folders = BTreeMap::new();
    let mut not_allowed = Vec::new();
    match v.get("folders") {
        None | Some(Value::Null) => {}
        Some(Value::Object(m)) if m.len() <= 16 => {
            for (input, spec) in m {
                let shown = util::cut(input, 64);
                let name = spec.get("name").and_then(Value::as_str).unwrap_or("");
                let rel = spec.get("path").and_then(Value::as_str).unwrap_or("");
                let taken = |f: &String| *f == format!("in/{input}") || f.starts_with(&format!("in/{input}."));
                if !util::is_name(input) || inputs.contains_key(input) || files.iter().any(taken) {
                    problems.push(format!("folders: {shown:?} is no input name, or one given twice"));
                } else if !folder::is_folder_name(name) {
                    problems.push(format!("folders.{shown}.name: a folder's name"));
                } else if let Err(e) = folder::parts(rel) {
                    problems.push(format!("folders.{shown}.path: {e}"));
                } else {
                    if !cfg.folders.contains_key(name) {
                        not_allowed.push(name.to_string());
                    }
                    folders.insert(input.clone(), (name.to_string(), rel.to_string()));
                }
            }
        }
        _ => problems.push("folders: an object of at most 16 inputs".into()),
    }
    let mut outputs = Vec::new();
    match v.get("outputs") {
        None | Some(Value::Null) => {}
        Some(Value::Array(a)) if a.len() <= 64 => {
            for o in a {
                match o.as_str() {
                    Some(n) if util::is_name(n) => outputs.push(n.to_string()),
                    _ => problems.push(format!("outputs: {} is no output name", util::cut(&o.to_string(), 64))),
                }
            }
        }
        _ => problems.push("outputs: a list of at most 64 names".into()),
    }
    let put = v.get("put").and_then(Value::as_str).unwrap_or("").to_string();
    let put_ok = Url::parse(&put).is_ok_and(|u| same_origin(&u, &ctx.gateway)) && put.ends_with('/');
    if !outputs.is_empty() && !put_ok {
        problems.push(format!("put: a link of the Gateway {} ending in /", base(&ctx.gateway)));
    }
    let limits = Limits::parse(v.get("limits"), &mut problems);
    // Node 2.1.2: who may share a warm server (serve.rs); anything else is no key at all, and
    // the job gets a server of its own. TODO(review): isolation.
    let serve_key = v.get("serve_key").and_then(Value::as_str).filter(|k| serve::is_kernel_key(k)).map(String::from);
    if !problems.is_empty() {
        let hint = format!("the node refused the job: {}", problems.join("; "));
        warn!("job {id}: {hint}");
        ctx.send(ctx.failed(id, "SERVICE_ERROR", &hint, ""));
        return;
    }
    if !not_allowed.is_empty() {
        not_allowed.sort();
        not_allowed.dedup();
        ctx.refuse(id, &format!(
            "the folder(s) {} not allowed at this machine (its person allows one with amsyos-node allow <name> <folder>)",
            not_allowed.join(", ")
        ));
        return;
    }
    let busy = ctx.running.lock().map(|r| r.len()).unwrap_or(0);
    if busy as u32 >= ctx.slots {
        ctx.refuse(id, &format!("no free slot ({busy} of {} running)", ctx.slots));
        return;
    }
    if (limits.gpu_job() || svc.gpu) && !ctx.server {
        if let Some(idle) = sys::idle_seconds().filter(|s| *s < 300) {
            ctx.refuse(id, &format!("the person is using this machine (idle {idle} s): a GPU job waits until it has been idle 5 minutes"));
            return;
        }
    }
    let (tx, rx) = watch::channel(false);
    if let Ok(mut r) = ctx.running.lock() {
        r.insert(id.to_string(), tx);
    }
    if exec.is_none() {
        if let Ok(mut u) = ctx.using.lock() {
            u.insert(id.to_string(), svc.lines());
        }
    }
    let mut taken = ctx.state(id, "running");
    if as_node && exec.is_none() {
        taken.insert("as_node".into(), true.into()); // the kernel logs it (node.list)
    }
    ctx.send(Value::Object(taken));
    let job = JobMsg { id: id.to_string(), service, version, params, inputs, outputs, put, limits, exec, as_node, folders, serve_key };
    let ctx = ctx.clone();
    tokio::spawn(async move {
        let id = job.id.clone();
        // Node 2.1.5: no cleaning of the builds while a node.exec runs (it may be a build).
        let held = if job.exec.is_some() { Some(ctx.builds.clone().read_owned().await) } else { None };
        let started = Instant::now();
        info!("job {id} of {}: taken", job.service);
        let msg = do_job(&ctx, &job, &svc, rx, started).await;
        drop(held);
        info!("job {id}: {}", msg.get("status").and_then(Value::as_str).unwrap_or("?"));
        ctx.send(msg);
        if let Ok(mut r) = ctx.running.lock() {
            r.remove(&id);
        }
        if let Ok(mut u) = ctx.using.lock() {
            u.remove(&id);
        }
    });
}

/// Each folder input's source, as the config says now (the person may have disallowed one
/// since the job came): input → (its `in/` name, what it names). Every problem at once; no
/// path of this machine is said, only the name and the path under the folder.
fn resolve_folders(node_dir: &Path, wanted: &BTreeMap<String, (String, String)>) -> Result<Vec<(String, String, folder::Source)>, String> {
    let cfg = Config::load(node_dir);
    let (mut found, mut problems) = (Vec::new(), Vec::new());
    for (input, (name, rel)) in wanted {
        let shown = format!("folder:{name}/{rel}");
        match cfg.folders.get(name).filter(|_| folder::is_folder_name(name)) {
            None => problems.push(format!("{input} ({shown}): the folder {name} is no longer allowed at this machine")),
            Some(root) => match folder::resolve(root, rel) {
                Ok(src) => found.push((input.clone(), src.file_for(input), src)),
                Err(e) => problems.push(format!("{input} ({shown}): {e}")),
            },
        }
    }
    if problems.is_empty() { Ok(found) } else { Err(format!("the node could not read {}", problems.join("; "))) }
}

async fn do_job(ctx: &Arc<Ctx>, job: &JobMsg, svc: &Service, cancel: watch::Receiver<bool>, started: Instant) -> Value {
    let id = job.id.as_str();
    let fail = |error: &str, hint: String, said: &str| ctx.failed(id, error, &hint, said);
    let (root, jid, service, version, params) = (
        ctx.jobs_root.clone(), job.id.clone(), job.service.clone(), job.version.clone(), job.params.clone(),
    );
    let mut told: BTreeMap<String, String> = job.inputs.iter().map(|(n, (_, f))| (n.clone(), f.clone())).collect();
    // Node 2.1: what each folder input names, found before the job's folder is made (job.json
    // says where the job finds it).
    let mut sources = Vec::new();
    if !job.folders.is_empty() {
        let (node_dir, wanted) = (ctx.node_dir.clone(), job.folders.clone());
        match tokio::task::spawn_blocking(move || resolve_folders(&node_dir, &wanted)).await {
            Ok(Ok(found)) => {
                for (input, file, src) in found {
                    told.insert(input.clone(), file.clone());
                    sources.push((input, file, src));
                }
            }
            Ok(Err(hint)) => return fail("INVALID_PARAMS", hint, ""),
            Err(e) => return fail("SERVICE_ERROR", e.to_string(), ""),
        }
    }
    let prepared = tokio::task::spawn_blocking(move || exec::prepare(&root, &jid, &service, &version, &params, &told)).await;
    let jd = match prepared {
        Ok(Ok(jd)) => jd,
        Ok(Err(e)) => return fail("SERVICE_ERROR", format!("the node could not make the job's folder: {e}"), ""),
        Err(e) => return fail("SERVICE_ERROR", e.to_string(), ""),
    };
    let cleanup = |jd: exec::JobDir| async move {
        let _ = tokio::task::spawn_blocking(move || sys::remove_job_dir(&jd.dir)).await;
    };

    for (name, (url, file)) in &job.inputs {
        let path = exec::input_path(&jd, file);
        if let Err(e) = fetch(&ctx.http, url, &path).await {
            cleanup(jd).await;
            return fail("SERVICE_ERROR", format!("the node could not fetch the input {name}: {e}"), "");
        }
        sys::job_may_read(&path);
    }
    let mut read_notes = Vec::new();
    for (input, file, src) in sources {
        let (to, stop) = (exec::input_path(&jd, &file), cancel.clone());
        match tokio::task::spawn_blocking(move || folder::copy(&src, &to, &stop)).await {
            Ok(Ok(copied)) => {
                info!("job {id}: folder input {input} copied as {file} ({} files, {} bytes)", copied.files, copied.bytes);
                read_notes.extend(copied.note(&input));
            }
            Ok(Err(e)) if e == folder::CANCELLED => {
                cleanup(jd).await;
                return Value::Object(ctx.state(id, "cancelled"));
            }
            Ok(Err(e)) => {
                cleanup(jd).await;
                return fail("INVALID_PARAMS", format!("the node could not read the folder input {input}: {e}"), "");
            }
            Err(e) => {
                cleanup(jd).await;
                return fail("SERVICE_ERROR", e.to_string(), "");
            }
        }
    }
    if *cancel.borrow() {
        cleanup(jd).await;
        return Value::Object(ctx.state(id, "cancelled"));
    }

    let progress = |p: u8| {
        let mut m = ctx.state(id, "running");
        m.insert("progress".into(), p.into());
        ctx.send(Value::Object(m));
    };
    let mut svc = svc.clone();
    if let Some(None) = job.exec {
        svc.dir = jd.dir.clone(); // node.exec without a cwd runs in the job's folder
    }
    let kills = mem::Kills::now(); // 2.1.4: the OOM killer's counts, read by the node itself
    let ran = if job.exec.is_none() && svc.served().is_some() {
        // Node 2.1.2: through a server kept warm (serve.rs).
        let key = serve::key(&job.service, &job.version, job.as_node, job.serve_key.as_deref(), &svc, &job.limits);
        serve::execute(
            &ctx.pool, key, &jd, id, &svc, &job.service, &job.limits, ctx.server, &ctx.node_dir, &ctx.jobs_root,
            job.as_node, cancel.clone(), progress,
        )
        .await
        .map(|(ended, how)| {
            info!("job {id}: served ({how})");
            ended
        })
    } else {
        exec::execute(&jd, &svc, &job.limits, ctx.server, &ctx.node_dir, job.as_node, cancel.clone(), progress).await
    };
    let mut ended = match ran {
        Ok(e) => e,
        Err(e) => {
            cleanup(jd).await;
            return fail("SERVICE_ERROR", format!("the node could not start the service: {e}"), "");
        }
    };
    if ended.exit == 137 && !ended.cancelled && !ended.timed_out {
        let pool = ctx.pool.clone();
        let warm = tokio::task::spawn_blocking(move || serve::idle_held(&pool)).await.ok().flatten();
        ended.oom = mem::oom_note(ended.exit, &kills, (!job.as_node).then_some(job.limits.ram_gb), warm);
        if let Some(oom) = &ended.oom {
            warn!("job {id}: {oom}");
        }
    }
    exec::keep_log(&ctx.node_dir, id, &ended);
    if ended.cancelled {
        cleanup(jd).await;
        return Value::Object(ctx.state(id, "cancelled"));
    }
    let said = ended.said();
    let is_exec = job.exec.is_some();
    let with_exit = |v: Value| match v {
        Value::Object(mut m) if is_exec => {
            m.insert("exit_code".into(), ended.exit.into());
            Value::Object(m)
        }
        other => other,
    };
    if is_exec {
        // node.exec: its stdout and stderr together are the output `log`; its exit code decides.
        let log = if ended.log.is_empty() { "(nothing was printed)\n".to_string() } else { ended.log.clone() };
        let _ = std::fs::write(jd.dir.join("out").join("log.txt"), log.as_bytes());
        if ended.timed_out {
            let hint = format!("the command ran longer than max_runtime_s ({} s) and was stopped", job.limits.max_runtime_s);
            cleanup(jd).await;
            return with_exit(fail("TIMEOUT", hint, &said));
        }
        if ended.exit != 0 {
            cleanup(jd).await;
            let hint = ended.oom.clone().unwrap_or_else(|| format!("the command exited with code {}", ended.exit));
            return with_exit(fail("SERVICE_ERROR", hint, &said));
        }
    } else if let Err((error, hint)) = ended.judge(&jd, job.limits.max_runtime_s) {
        cleanup(jd).await;
        return fail(&error, hint, &said);
    }
    let (mut outs, mut notes) = exec::collect_outputs(&jd, Some(&job.outputs));
    notes.splice(0..0, read_notes);
    if is_exec {
        // Any other output only if the command wrote it; an empty one is left out.
        outs.retain(|o| {
            if o.size == 0 {
                notes.push(format!("out/{} is empty: left out", o.file_name));
            }
            o.size > 0
        });
    }
    // Node 2.1.3: a service that ended well but left a declared output unmade: the kernel
    // fails the job (unless the output is optional), so the end of its stderr goes with
    // `succeeded`, beside the notes, to say why.
    let unmade = !is_exec && job.outputs.iter().any(|n| !outs.iter().any(|o| &o.name == n));
    let most = job.limits.max_output_mb.saturating_mul(1 << 20);
    for o in &outs {
        let problem = if o.size == 0 {
            Some(format!("the output {} is empty", o.name))
        } else if o.size > most {
            Some(format!("the output {} is {} MB, more than max_output_mb {}", o.name, o.size >> 20, job.limits.max_output_mb))
        } else {
            None
        };
        if let Some(hint) = problem {
            cleanup(jd).await;
            return fail("SERVICE_ERROR", hint, &notes.join("\n"));
        }
    }
    let mut put = Map::new();
    for o in &outs {
        if let Err(e) = put_output(&ctx.http, &job.put, o).await {
            cleanup(jd).await;
            return fail("SERVICE_ERROR", format!("the node could not put the output {}: {e}", o.name), &notes.join("\n"));
        }
        put.insert(o.name.clone(), o.file_name.clone().into());
    }
    drop(outs);
    cleanup(jd).await;
    let mut m = ctx.state(id, "succeeded");
    m.insert("outputs".into(), Value::Object(put));
    m.insert("took_s".into(), round1(started.elapsed().as_secs_f64()).into());
    let said = said_with(&notes.join("\n"), if unmade { said.as_str() } else { "" });
    if !said.is_empty() {
        m.insert("said".into(), said.into());
    }
    with_exit(Value::Object(m))
}

/// A `succeeded` message's `said`: the node's notes, then the end of the service's stderr
/// (2.1.3, when it left an output unmade); at most 2000 characters, the notes at most 600 of
/// them when both are there.
fn said_with(notes: &str, stderr: &str) -> String {
    match (notes.is_empty(), stderr.is_empty()) {
        (_, true) => util::tail(notes, 2000),
        (true, false) => util::tail(stderr, 2000),
        (false, false) => {
            let head = util::tail(notes, 600);
            let room = 2000 - head.chars().count() - 1;
            format!("{head}\n{}", util::tail(stderr, room))
        }
    }
}

// ------------------------------------------------------------------------------ actions

const PACKAGE_MAX: u64 = 200 << 20;

fn is_action_id(s: &str) -> bool {
    (1..=80).contains(&s.len()) && s.chars().all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-')
}

/// The answer to an action, logged with who asked.
fn answer(ctx: &Ctx, id: &str, what: &str, by: &str, done: Result<String, String>) {
    answer_with(ctx, id, what, by, done, Map::new());
}

/// The answer to an action with more to say (2.1.5: `undeploy`'s `removed` and `freed_gb`).
fn answer_with(ctx: &Ctx, id: &str, what: &str, by: &str, done: Result<String, String>, more: Map<String, Value>) {
    let (ok, hint) = match done {
        Ok(h) => (true, h),
        Err(h) => (false, h),
    };
    let access = ctx.access();
    if ok {
        info!("action {id} {what} by {by}: done: {hint}");
    } else {
        warn!("action {id} {what} by {by}: refused: {hint}");
    }
    let mut msg = Map::new();
    msg.insert("t".into(), "done".into());
    msg.insert("id".into(), id.into());
    msg.insert("ok".into(), ok.into());
    msg.insert("hint".into(), util::cut(&hint, HINT_MAX).into());
    msg.insert("access".into(), access.as_str().into());
    msg.extend(more);
    ctx.send(Value::Object(msg));
    ctx.send(Value::Object(ctx.activity("beat")));
}

/// TODO(review): isolation — actions from afar (sdk/NODE.md §5): `deploy`, `undeploy` (2.1.5)
/// and `switch` only at `managed` or `full`, `as_node` only at `full`, `lower` only downwards.
/// The level is read from the machine's own config.json at each action, never taken from the
/// message.
fn take_action(ctx: &Arc<Ctx>, v: &Value) {
    let Some(id) = v.get("id").and_then(Value::as_str).filter(|s| is_action_id(s)).map(String::from) else {
        warn!("an action without a valid id: left alone");
        return;
    };
    let what = util::cut(v.get("what").and_then(Value::as_str).unwrap_or(""), 20);
    let by = util::cut(v.get("by").and_then(Value::as_str).unwrap_or("?"), 80);
    let level = ctx.access();
    let need_managed = |what: &str| -> Result<(), String> {
        if level < Access::Managed {
            return Err(format!(
                "{what} needs the access level managed or full at this machine (it is {}): the person there runs `amsyos-node access managed`",
                level.as_str()
            ));
        }
        Ok(())
    };
    match what.as_str() {
        "lower" => {
            let done = match v.get("access").and_then(Value::as_str).and_then(Access::parse) {
                None => Err("access: services or managed".to_string()),
                Some(to) if to >= level => Err(format!(
                    "{} is not lower than {}: only the person at the machine raises a level",
                    to.as_str(),
                    level.as_str()
                )),
                Some(to) => {
                    // Below `full` the default `as_node` is off too; turning it on again is the
                    // person's, at the machine.
                    let mut cfg = Config::load(&ctx.node_dir);
                    let had = cfg.as_node_by_default();
                    cfg.access = Some(to.as_str().into());
                    cfg.as_node_default = false;
                    cfg.save(&ctx.node_dir).map(|_| {
                        format!(
                            "the access level is {} now (was {}){}",
                            to.as_str(),
                            level.as_str(),
                            if had { "; the default as_node is off" } else { "" }
                        )
                    })
                }
            };
            answer(ctx, &id, &what, &by, done);
        }
        "switch" | "deploy" | "undeploy" => {
            if let Err(e) = need_managed(&what) {
                answer(ctx, &id, &what, &by, Err(e));
                return;
            }
            let (ctx, v) = (ctx.clone(), v.clone());
            tokio::spawn(async move {
                let (done, more) = match what.as_str() {
                    "switch" => (switch(&ctx, &v).await, Map::new()),
                    "deploy" => (deploy(&ctx, &v, level, &by).await, Map::new()),
                    _ => match undeploy(&ctx, &v, &by).await {
                        Ok((hint, more)) => (Ok(hint), more),
                        Err(e) => (Err(e), Map::new()),
                    },
                };
                answer_with(&ctx, &id, &what, &by, done, more);
            });
        }
        _ => answer(ctx, &id, &what, &by, Err("what: deploy, undeploy, switch or lower".into())),
    }
}

fn service_of(v: &Value) -> Result<String, String> {
    v.get("service")
        .and_then(Value::as_str)
        .filter(|s| util::is_service_id(s))
        .map(String::from)
        .ok_or_else(|| "service: an id like t_org.name.sub".to_string())
}

/// A service of the organisation this node serves (`deploy`, `undeploy`): the node checks it
/// itself. TODO(review): isolation.
fn own_service_of(ctx: &Ctx, v: &Value) -> Result<String, String> {
    let service = service_of(v)?;
    let org = Config::load(&ctx.node_dir).org;
    if org.is_empty() || !service.starts_with(&format!("{org}.")) {
        return Err(format!("service: {service} is not of {org}, the organisation this node serves"));
    }
    Ok(service)
}

/// `switch`: a service of services.json on or off. On again only if the job account still
/// cannot write to its folder.
async fn switch(ctx: &Arc<Ctx>, v: &Value) -> Result<String, String> {
    let service = service_of(v)?;
    let on = v.get("on").and_then(Value::as_bool).ok_or("on: true or false")?;
    let svc = Services::load(&ctx.node_dir).services.get(&service).cloned().ok_or_else(|| format!("{service} is not on this machine"))?;
    if on {
        let (dir, nd, jr) = (svc.dir.clone(), ctx.node_dir.clone(), ctx.jobs_root.clone());
        let fix = crate::guard::service_fix(&service, &dir);
        tokio::task::spawn_blocking(move || crate::guard::check(&[(dir, fix)], &nd, &jr)).await.map_err(|e| e.to_string())??;
    }
    Services::update(&ctx.node_dir, |s| {
        let svc = s.services.get_mut(&service).ok_or_else(|| format!("{service} is not on this machine"))?;
        svc.on = on;
        Ok(())
    })?;
    Ok(format!("{service} is {}", if on { "on" } else { "off" }))
}

/// TODO(review): isolation — `deploy`: the package from the Gateway's own origin only, at
/// most 200 MB, its SHA-256 checked before it is opened; unpacked into a new folder of the
/// managed root (deploy.rs), protected as `enable` protects a folder, checked as the job
/// account; only then switched on. The earlier folder is removed only if it was a deploy's
/// inside the managed root, never a folder the person switched on.
async fn deploy(ctx: &Arc<Ctx>, v: &Value, level: Access, by: &str) -> Result<String, String> {
    let service = own_service_of(ctx, v)?;
    let as_node = v.get("as_node").and_then(Value::as_bool).unwrap_or(false);
    if as_node && level < Access::Full {
        return Err(format!("as_node needs the access level full at this machine (it is {})", level.as_str()));
    }
    // Node 2.1.2: `serve`, a server kept warm (serve.rs), beside or instead of `command`.
    let line = |k: &str| v.get(k).and_then(Value::as_str).unwrap_or("").trim().to_string();
    let (command, serve) = (line("command"), line("serve"));
    let bad = |c: &String| c.len() > 2000 || c.chars().any(char::is_control);
    if (command.is_empty() && serve.is_empty()) || bad(&command) || bad(&serve) {
        return Err("command or serve (or both): each one line, at most 2000 characters".into());
    }
    let serve = (!serve.is_empty()).then_some(serve);
    // Node 2.1.5: the image a build of `amsyos deploy` made must still be here (a cleaning
    // removes one no deployment names, builds.rs).
    let lines: Vec<String> = std::iter::once(command.clone()).chain(serve.clone()).collect();
    if let Some(image) = builds::image_of(&lines).filter(|i| !builds::is_image(i)) {
        return Err(format!(
            "the build {} is not on this machine (removed once no deployment used it, or never made): deploy again with a build (amsyos deploy --force)",
            image.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_else(|| image.display().to_string())
        ));
    }
    let package = v.get("package").cloned().unwrap_or(Value::Null);
    let url = package
        .get("url")
        .and_then(Value::as_str)
        .and_then(|u| Url::parse(u).ok())
        .filter(|u| same_origin(u, &ctx.gateway))
        .ok_or_else(|| format!("package.url: a link of the Gateway {}", base(&ctx.gateway)))?;
    let sha = package.get("sha256").and_then(Value::as_str).unwrap_or("").to_lowercase();
    if sha.len() != 64 || !sha.chars().all(|c| c.is_ascii_hexdigit()) {
        return Err("package.sha256: 64 hex digits".into());
    }
    if package.get("size").and_then(Value::as_u64).is_some_and(|n| n > PACKAGE_MAX) {
        return Err(format!("the package is more than {} MB", PACKAGE_MAX >> 20));
    }
    let root = Config::load(&ctx.node_dir).services_root();
    if std::fs::create_dir_all(&root).is_err() || !root.is_dir() {
        return Err(format!("the managed services folder {} is missing: run `amsyos-node setup` again", root.display()));
    }
    let incoming = ctx.node_dir.join("incoming");
    std::fs::create_dir_all(&incoming).map_err(|e| e.to_string())?;
    let tag = util::hex(&util::random_bytes::<4>());
    let zip = incoming.join(format!("{service}-{tag}.zip"));
    let fetched = fetch_package(&ctx.http, &url, &zip, PACKAGE_MAX).await;
    let (size, got) = match fetched {
        Ok(x) => x,
        Err(e) => {
            let _ = std::fs::remove_file(&zip);
            return Err(format!("the package could not be fetched: {e}"));
        }
    };
    if got != sha {
        let _ = std::fs::remove_file(&zip);
        return Err(format!("the package's SHA-256 is {got}, not {sha}"));
    }
    let folder = root.join(format!("{service}-{tag}"));
    let (z, f) = (zip.clone(), folder.clone());
    let unpacked = tokio::task::spawn_blocking(move || crate::deploy::unpack(&z, &f)).await.map_err(|e| e.to_string())?;
    let _ = std::fs::remove_file(&zip);
    let (files, bytes) = unpacked?;
    let (f, nd, jr, fix) = (folder.clone(), ctx.node_dir.clone(), ctx.jobs_root.clone(), crate::guard::service_fix(&service, &folder));
    let checked = tokio::task::spawn_blocking(move || {
        sys::grant_dir(&f)?;
        crate::guard::check(&[(f, fix)], &nd, &jr)
    })
    .await
    .map_err(|e| e.to_string())?;
    if let Err(e) = checked {
        let _ = std::fs::remove_dir_all(&folder);
        return Err(format!("the unpacked folder could not be protected: {e}"));
    }
    let served = serve.is_some();
    let entry = Service {
        dir: folder.clone(),
        command,
        serve,
        gpu: false,
        on: true,
        managed: true,
        by: Some(by.to_string()),
        sha256: Some(sha.clone()),
        as_node,
        as_job: false,
        since: Some(util::now_utc()),
    };
    let earlier = Services::update(&ctx.node_dir, |s| Ok(s.services.insert(service.clone(), entry)))?;
    if let Some(old) = earlier.filter(|o| o.managed && o.dir != folder && sys::within(&o.dir, &root) && o.dir != root) {
        if let Err(e) = std::fs::remove_dir_all(&old.dir) {
            warn!("the earlier folder of {service}, {}, could not be removed: {e}", old.dir.display());
        }
    }
    info!("deploy of {service} by {by}: {files} files, {bytes} bytes (package {size} bytes, sha256 {sha}) in {}{}{}",
          folder.display(), if as_node { ", as_node" } else { "" }, if served { ", served warm" } else { "" });
    let freed = clean_builds(ctx, "a deploy").await;
    ctx.disk_now.notify_one();
    Ok(format!("{service} is on, from {files} files in {}{}{}", folder.display(),
               if served { "; its jobs go through a server kept warm (serve)" } else { "" },
               if freed > 0 { format!("; {} GB of builds no deployment uses freed", builds::gb(freed)) } else { String::new() }))
}

/// Node 2.1.5: `undeploy` (sdk/NODE.md §5): a service a deploy put here: its idle warm servers
/// stopped, its entry and its folder removed, then the builds no deployment uses. A service the
/// person switched on here is theirs (refused); one that is not here: nothing to remove.
/// TODO(review): isolation — only what a deploy put here: an entry `managed`, its folder only
/// inside the managed root.
async fn undeploy(ctx: &Arc<Ctx>, v: &Value, by: &str) -> Result<(String, Map<String, Value>), String> {
    let service = own_service_of(ctx, v)?;
    let found = Services::load(&ctx.node_dir).services.get(&service).cloned();
    if found.as_ref().is_some_and(|s| !s.managed) {
        return Err(format!(
            "{service} was switched on at this machine by its person, not deployed: only they remove it, there (amsyos-node disable {service})"
        ));
    }
    let mut freed = 0u64;
    let mut said = Vec::new();
    let removed = found.is_some();
    if let Some(svc) = found {
        let stopped = serve::stop_service(&ctx.pool, &service).await;
        if stopped > 0 {
            said.push(format!("{stopped} warm server(s) stopped"));
        }
        Services::update(&ctx.node_dir, |s| Ok(s.services.remove(&service)))?;
        let root = Config::load(&ctx.node_dir).services_root();
        if sys::within(&svc.dir, &root) && svc.dir != root {
            let dir = svc.dir.clone();
            let gone = tokio::task::spawn_blocking(move || {
                let n = builds::freed_by(&dir);
                std::fs::remove_dir_all(&dir).map(|_| n).map_err(|e| format!("{}: {e}", dir.display()))
            })
            .await
            .map_err(|e| e.to_string())?;
            match gone {
                Ok(n) => freed += n,
                Err(e) => said.push(format!("its folder could not be removed ({e})")),
            }
        }
    } else {
        said.push("it was not deployed here".to_string());
    }
    freed += clean_builds(ctx, "an undeploy").await;
    ctx.disk_now.notify_one();
    let notes: String = said.iter().map(|s| format!("{s}; ")).collect();
    info!("undeploy of {service} by {by}: {notes}{} GB freed", builds::gb(freed));
    let mut more = Map::new();
    more.insert("removed".into(), removed.into());
    more.insert("freed_gb".into(), builds::gb(freed).into());
    let head = if removed { format!("{service} removed") } else { format!("{service}: nothing to remove") };
    Ok((format!("{head}; {notes}{} GB freed", builds::gb(freed)), more))
}

/// Node 2.1.5: the builds no deployment (nor a job running now) uses, removed (builds.rs);
/// skipped while a node.exec runs, done at the next deploy, undeploy or start. The bytes freed.
async fn clean_builds(ctx: &Arc<Ctx>, after: &str) -> u64 {
    let Some(home) = builds::home() else { return 0 };
    let Ok(held) = ctx.builds.clone().try_write_owned() else {
        info!("builds: not cleaned after {after}: a node.exec (or another cleaning) runs; done at the next deploy, undeploy or start");
        return 0;
    };
    let lines = ctx.lines_in_use();
    let done = tokio::task::spawn_blocking(move || {
        let _held = held;
        builds::clean(&home, &lines, builds::MIN_AGE)
    })
    .await
    .map_err(|e| e.to_string())
    .and_then(|r| r);
    match done {
        Ok(c) => {
            if !c.is_empty() {
                info!(
                    "builds no deployment uses, removed after {after}: images [{}], environments [{}], {} GB freed{}",
                    c.images.join(", "),
                    c.envs.join(", "),
                    builds::gb(c.freed),
                    if c.failed.is_empty() { String::new() } else { format!("; not removed: {}", c.failed.join("; ")) }
                );
            }
            c.freed
        }
        Err(e) => {
            warn!("builds: not cleaned after {after}: {e}");
            0
        }
    }
}

/// Node 2.1.5: the builds cleaned at the node's start, then what each deployment holds of the
/// disk and the builds no deployment uses (`disk`, sdk/NODE.md §4), measured every MEASURE_S
/// and soon after a deploy or an undeploy (never twice within MEASURE_GAP), said to the
/// Gateway when it changed.
async fn watch_disk(ctx: Arc<Ctx>, mut stop: watch::Receiver<bool>) {
    clean_builds(&ctx, "the node's start").await;
    let mut last = String::new();
    loop {
        let measured_at = Instant::now();
        let nd = ctx.node_dir.clone();
        let using: Vec<String> = ctx.using.lock().map(|u| u.values().flatten().cloned().collect()).unwrap_or_default();
        let measured = tokio::task::spawn_blocking(move || {
            let deployments: Vec<builds::Deployment> = Services::load(&nd)
                .services
                .into_iter()
                .filter(|(_, s)| s.managed)
                .map(|(id, s)| builds::Deployment { lines: s.lines(), dir: s.dir, since: s.since, id })
                .collect();
            builds::measure(builds::home().as_deref(), &deployments, &using)
        })
        .await;
        if let Ok(Value::Object(mut m)) = measured {
            let text = Value::Object(m.clone()).to_string();
            if text != last {
                last = text;
                m.insert("t".into(), "disk".into());
                ctx.send(Value::Object(m));
            }
        }
        tokio::select! {
            _ = tokio::time::sleep(Duration::from_secs(MEASURE_S)) => {}
            _ = ctx.disk_now.notified() => {
                tokio::time::sleep(MEASURE_GAP.saturating_sub(measured_at.elapsed()).max(Duration::from_secs(2))).await;
            }
            _ = stop.changed() => return,
        }
    }
}

/// GET a package into the node's own folder, at most `most` bytes, and its SHA-256.
async fn fetch_package(http: &reqwest::Client, url: &Url, path: &Path, most: u64) -> Result<(u64, String), String> {
    use sha2::{Digest, Sha256};
    let mut resp = http.get(url.clone()).send().await.map_err(|e| e.to_string())?;
    if !resp.status().is_success() {
        let status = resp.status().as_u16();
        let text = resp.text().await.unwrap_or_default();
        return Err(format!("HTTP {status}: {}", util::cut(&text, 200)));
    }
    if resp.content_length().is_some_and(|n| n > most) {
        return Err(format!("more than {} MB", most >> 20));
    }
    let mut f = tokio::fs::File::create(path).await.map_err(|e| format!("cannot write {}: {e}", path.display()))?;
    let (mut hash, mut total) = (Sha256::new(), 0u64);
    while let Some(chunk) = resp.chunk().await.map_err(|e| e.to_string())? {
        total += chunk.len() as u64;
        if total > most {
            return Err(format!("more than {} MB", most >> 20));
        }
        hash.update(&chunk);
        f.write_all(&chunk).await.map_err(|e| e.to_string())?;
    }
    f.flush().await.map_err(|e| e.to_string())?;
    Ok((total, util::hex(&hash.finalize())))
}

/// GET an input's link into the job's folder. A failed connection is tried once more; an
/// answer that is not 200 is not.
async fn fetch(http: &reqwest::Client, url: &Url, path: &Path) -> Result<u64, String> {
    let mut last = String::new();
    for attempt in 0..2 {
        if attempt > 0 {
            tokio::time::sleep(Duration::from_secs(2)).await;
        }
        let mut resp = match http.get(url.clone()).send().await {
            Ok(r) => r,
            Err(e) => {
                last = e.to_string();
                continue;
            }
        };
        if !resp.status().is_success() {
            let status = resp.status().as_u16();
            let text = resp.text().await.unwrap_or_default();
            return Err(format!("HTTP {status}: {}", util::cut(&text, 200)));
        }
        let mut f = tokio::fs::File::create(path).await.map_err(|e| format!("cannot write {}: {e}", path.display()))?;
        let mut total = 0u64;
        let copied: Result<(), String> = async {
            while let Some(chunk) = resp.chunk().await.map_err(|e| e.to_string())? {
                total += chunk.len() as u64;
                f.write_all(&chunk).await.map_err(|e| e.to_string())?;
            }
            f.flush().await.map_err(|e| e.to_string())
        }
        .await;
        match copied {
            Ok(()) => return Ok(total),
            Err(e) => last = e,
        }
    }
    Err(last)
}

async fn put_output(http: &reqwest::Client, put: &str, o: &exec::OutFile) -> Result<(), String> {
    let url = format!("{put}{}", o.file_name);
    let mut last = String::new();
    for attempt in 0..2 {
        if attempt > 0 {
            tokio::time::sleep(Duration::from_secs(2)).await;
        }
        let mut f = o.file.try_clone().map_err(|e| e.to_string())?;
        f.seek(SeekFrom::Start(0)).map_err(|e| e.to_string())?;
        let body = reqwest::Body::from(tokio::fs::File::from_std(f));
        let sent = http
            .put(&url)
            .header(CONTENT_LENGTH, o.size)
            .header(CONTENT_TYPE, "application/octet-stream")
            .body(body)
            .send()
            .await;
        match sent {
            Ok(r) if r.status().is_success() => return Ok(()),
            Ok(r) => {
                let status = r.status().as_u16();
                let text = r.text().await.unwrap_or_default();
                return Err(format!("HTTP {status}: {}", util::cut(&text, 200)));
            }
            Err(e) => last = e.to_string(),
        }
    }
    Err(last)
}

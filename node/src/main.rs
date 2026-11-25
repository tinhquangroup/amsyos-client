//! AMSYOS Node 2.1: one program that makes an organisation's own computer a place where its
//! services run (spec v2 §26; the protocol is sdk/NODE.md, the job contract sdk/CONTRACT.md).
//! Named `amsyos-job`, the same program is the job contract's command. 2.1 reads the folders
//! the person at the machine allowed, in place (spec v3 §8.3, folder.rs). 2.1.2 ships the
//! Python job module on every job's PYTHONPATH (pymod.rs) and keeps servers warm (serve.rs).
//! 2.1.4 plans with the memory its cgroup allows and says when a job ran out of it (mem.rs).
//! 2.1.5 removes a deployment from afar (`undeploy`), frees the builds no deployment uses and
//! says what each deployment holds of the disk (builds.rs). 2.1.6 sends a job's log it kept, when
//! the kernel asks (`log`, net.rs). 2.1.7 keeps job logs 7 days (exec.rs `sweep_logs`).

mod builds;
mod config;
mod deploy;
mod exec;
mod folder;
mod guard;
mod job;
mod machine;
mod mem;
mod net;
mod pymod;
mod serve;
mod util;

#[cfg(unix)]
#[path = "sys_unix.rs"]
mod sys;
#[cfg(windows)]
#[path = "sys_windows.rs"]
mod sys;

use std::collections::{BTreeMap, HashMap, HashSet};
use std::path::{Path, PathBuf};

use serde_json::Value;

use config::{Access, Config, Service, Services};

const USAGE: &str = "\
AMSYOS Node 2.1.7: this computer runs its organisation's services. Nothing dials into it.

  amsyos-node setup [--user <name>] [--jobs <folder>] [--services <folder>]
        once, as an administrator (Windows) or root (Linux: sudo), and again after each new
        version: the job account amsyos-job, the jobs folder (--jobs), the managed services
        folder (--services), their permissions, and what every job finds (amsyos-job, the
        Python module `amsyos` on its PYTHONPATH);
        on Windows it also installs the program in %ProgramFiles%\\AMSYOS\\Node: run it from there
  amsyos-node join <code> --gateway https://<host> [--server] [--name <name>]
        join the organisation with a code a manager made (node.join_code)
  amsyos-node enable <service id> --dir <folder> --command \"<command line>\" [--serve \"<command line>\"] [--gpu] [--as-job]
        switch a service on: its command runs in <folder> as amsyos-job, which may read the
        folder but never write to it (on Windows the folder gets a protected ACL); at full
        with the default as_node on, as this account instead, unless --as-job. --serve
        (beside or instead of --command): a server kept warm, started once, that does the
        jobs one after the other (Python: from amsyos import serve)
  amsyos-node disable <service id>
  amsyos-node access services|managed|full [--as-node-default]
        what the organisation may do here from afar: services (the default) call what is
        switched on; managed also deploy and switch services; full also run any command
        (node.exec) and services as this account. Raising needs the person at the machine,
        full an administrator (Windows: an elevated window; Linux: sudo); lowering, nothing
  amsyos-node access as-node-default on|off
        at full only: the services switched on here run as this account (as_node) unless
        switched on with --as-job, and an admin's node.deploy is as_node unless it says
        as_node: false. Jobs of an order and steps still run as amsyos-job. On needs an
        administrator; off, or any level below full (here or from afar), turns it off
  amsyos-node allow <name> <folder>
        let the organisation's jobs read this folder in place, read-only, as
        folder:<name>/<path> (a session of theirs needs the name in its grant). The Gateway
        learns the name only, never the folder; only here, never from afar
  amsyos-node disallow <name>
  amsyos-node allowed           the folders allowed here
  amsyos-node services          what is switched on, and the software found here
  amsyos-node try <service id> [--params '<json>'] [--input <name>=<file>]…
                   [--folder <input>=<name>/<path>]… [--timeout <s>] [--limits '<json>']
        run a switched-on service once, here, as a job; the job's folder is kept
  amsyos-node run [--allow-elevated]
        go online: take jobs until stopped (Ctrl+C)
  amsyos-node info              what this machine has (JSON)
  amsyos-node job <params|param|input|output|progress|fail|next|done> …   (also as amsyos-job)
  amsyos-node version";

struct Args {
    pos: Vec<String>,
    opts: HashMap<String, Vec<String>>,
    flags: HashSet<String>,
}

impl Args {
    fn parse(args: &[String], valued: &[&str], flags: &[&str]) -> Result<Args, String> {
        let mut a = Args { pos: Vec::new(), opts: HashMap::new(), flags: HashSet::new() };
        let mut i = 0;
        while i < args.len() {
            let arg = &args[i];
            if let Some((k, v)) = arg.split_once('=').filter(|(k, _)| valued.contains(k)) {
                a.opts.entry(k.to_string()).or_default().push(v.to_string());
            } else if valued.contains(&arg.as_str()) {
                let v = args.get(i + 1).ok_or_else(|| format!("{arg} needs a value"))?;
                a.opts.entry(arg.clone()).or_default().push(v.clone());
                i += 1;
            } else if flags.contains(&arg.as_str()) {
                a.flags.insert(arg.clone());
            } else if arg.starts_with("--") {
                return Err(format!("unknown option {arg}"));
            } else {
                a.pos.push(arg.clone());
            }
            i += 1;
        }
        Ok(a)
    }

    fn one(&self, k: &str) -> Option<&str> {
        self.opts.get(k).and_then(|v| v.last()).map(String::as_str)
    }

    fn all(&self, k: &str) -> Vec<String> {
        self.opts.get(k).cloned().unwrap_or_default()
    }

    fn flag(&self, k: &str) -> bool {
        self.flags.contains(k)
    }
}

fn main() {
    let argv: Vec<String> = std::env::args_os().map(|a| a.to_string_lossy().into_owned()).collect();
    let stem = argv
        .first()
        .and_then(|a| Path::new(a).file_stem())
        .map(|s| s.to_string_lossy().to_lowercase())
        .unwrap_or_default();
    if stem == "amsyos-job" {
        std::process::exit(job::main(&argv[1..]));
    }
    let rest = argv.get(2..).unwrap_or(&[]);
    let what = argv.get(1).map(String::as_str).unwrap_or("");
    if what != "job" && rest.iter().any(|a| a == "--help" || a == "-h") {
        println!("{}", usage_of(what));
        std::process::exit(0);
    }
    let result = match argv.get(1).map(String::as_str) {
        Some("job") => std::process::exit(job::main(rest)),
        Some("setup") => setup(rest),
        Some("join") => join(rest),
        Some("run") => run(rest),
        Some("info") => info(),
        Some("services") => services(),
        Some("enable") => enable(rest),
        Some("disable") => disable(rest),
        Some("access") => access(rest),
        Some("allow") => allow(rest),
        Some("disallow") => disallow_folder(rest),
        Some("allowed") => allowed(),
        Some("try") => try_service(rest),
        // Not in USAGE: for finding why a job does not start as the job account (Windows).
        Some("diag") => sys::diag(rest.first().map(PathBuf::from)),
        Some("version" | "--version" | "-V") => {
            println!("amsyos-node {} (protocol 1)", env!("CARGO_PKG_VERSION"));
            Ok(0)
        }
        Some("help" | "--help" | "-h") => {
            println!("{USAGE}");
            Ok(0)
        }
        _ => {
            eprintln!("{USAGE}");
            Ok(2)
        }
    };
    match result {
        Ok(code) => std::process::exit(code),
        Err(e) => {
            eprintln!("amsyos-node: {e}");
            std::process::exit(1);
        }
    }
}

/// The lines of USAGE about one command (all of it for an unknown one).
fn usage_of(what: &str) -> String {
    let head = format!("  amsyos-node {what}");
    let mut out = Vec::new();
    let mut inside = false;
    for line in USAGE.lines() {
        if line.starts_with("  amsyos-node ") {
            inside = line == head || line.starts_with(&format!("{head} "));
        } else if !line.starts_with("    ") {
            inside = false;
        }
        if inside {
            out.push(line);
        }
    }
    if out.is_empty() { USAGE.to_string() } else { out.join("\n") }
}

fn runtime() -> Result<tokio::runtime::Runtime, String> {
    tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        .enable_all()
        .build()
        .map_err(|e| format!("cannot start: {e}"))
}

fn setup(args: &[String]) -> Result<i32, String> {
    let a = Args::parse(args, &["--user", "--jobs", "--services"], &[])?;
    let jobs = a.one("--jobs").map(PathBuf::from);
    let services = a.one("--services").map(PathBuf::from);
    sys::setup(a.one("--user").map(String::from), jobs, services)?;
    // Node 2.1.2: the Python job module every job finds (`from amsyos import job, serve`).
    pymod::install()?;
    println!("{}: the Python job module, on every job's PYTHONPATH", pymod::module_path().display());
    Ok(0)
}

fn join(args: &[String]) -> Result<i32, String> {
    let a = Args::parse(args, &["--gateway", "--name"], &["--server"])?;
    let [code] = a.pos.as_slice() else { return Err("amsyos-node join <code> --gateway https://<host>".into()) };
    let gateway = a.one("--gateway").ok_or("--gateway https://<host> is missing")?;
    runtime()?.block_on(net::join(code, gateway, a.flag("--server"), a.one("--name").map(String::from)))?;
    Ok(0)
}

fn run(args: &[String]) -> Result<i32, String> {
    let a = Args::parse(args, &[], &["--allow-elevated"])?;
    runtime()?.block_on(net::run(a.flag("--allow-elevated")))
}

fn info() -> Result<i32, String> {
    let dir = config::node_dir();
    let cfg = Config::load(&dir);
    let (machine, _, note) = machine::probe_checked(&cfg.jobs_root(), &dir);
    eprintln!("{note}");
    let mut shown = serde_json::to_value(&machine).map_err(|e| e.to_string())?;
    if let Value::Object(m) = &mut shown {
        m.insert("access".into(), cfg.access().as_str().into());
        m.insert("as_node_default".into(), cfg.as_node_by_default().into());
        m.insert("folders".into(), cfg.folder_names().into());
    }
    println!("{}", serde_json::to_string_pretty(&shown).map_err(|e| e.to_string())?);
    Ok(0)
}

fn services() -> Result<i32, String> {
    let dir = config::node_dir();
    let cfg = Config::load(&dir);
    let on = Services::load(&dir);
    let default = cfg.as_node_by_default();
    println!("Access level: {} ({})", cfg.access().as_str(), what_level_allows(cfg.access()));
    if default {
        println!("The default as_node is on: what is switched on here runs as this account, unless --as-job");
    }
    if !cfg.folders.is_empty() {
        println!("Folders read in place (amsyos-node allowed): {}", cfg.folder_names().join(", "));
    }
    if on.services.is_empty() {
        println!("Services: nothing yet.");
    } else {
        println!("Services ({}):", dir.join(config::SERVICES_FILE).display());
        for (id, s) in &on.services {
            let mut tags = vec![if s.on { "on" } else { "off" }.to_string()];
            if s.gpu {
                tags.push("GPU".into());
            }
            if s.managed {
                tags.push(format!("deployed by {}", s.by.as_deref().unwrap_or("?")));
            }
            if s.runs_as_node(default) {
                tags.push(s.as_node_why().into());
            } else if s.as_job {
                tags.push("as_job".into());
            }
            let how = if s.served().is_some() { "" } else { "command: " };
            println!("  {id}  ({})\n      dir: {}\n      {how}{}", tags.join(", "), s.dir.display(), s.shown());
        }
    }
    let (_, found) = machine::probe(&cfg.jobs_root());
    if found.is_empty() {
        println!("\nSoftware found: none of blender, ffmpeg, ollama, python.");
    } else {
        // Runnable only if the job account itself can reach it.
        let paths: Vec<PathBuf> = found.iter().map(|f| f.path.clone()).collect();
        let seen = sys::preflight().and_then(|_| guard::ask(&paths, guard::Ask::See, &dir, &cfg.jobs_root()));
        println!("\nSoftware found (what this machine could serve):");
        for (i, f) in found.iter().enumerate() {
            let note = match &seen {
                Ok(v) if v.get(i) == Some(&Some(true)) => String::new(),
                Ok(_) => format!("  (not reachable by {})", sys::JOB_USER),
                Err(_) => "  (not checked as the job account: run setup first)".to_string(),
            };
            println!("  {:<8} {:<10} {}{note}", f.name, f.version, f.path.display());
        }
        if let Err(e) = &seen {
            eprintln!("warning: {e}");
        }
    }
    let org = if cfg.org.is_empty() { "t_<org>" } else { cfg.org.as_str() };
    println!("\nSwitch one on: amsyos-node enable {org}.node.<name> --dir <folder> --command \"<command line>\" [--gpu] [--as-job]");
    Ok(0)
}

fn enable(args: &[String]) -> Result<i32, String> {
    let a = Args::parse(args, &["--dir", "--command", "--serve"], &["--gpu", "--as-job"])?;
    let mut problems = Vec::new();
    let id = a.pos.first().cloned().unwrap_or_default();
    if a.pos.len() != 1 || !util::is_service_id(&id) {
        problems.push("<service id>: like t_amsyos.node.hello (t_<org>.<name>[.<name>…])".to_string());
    }
    let dir = a.one("--dir").map(|d| std::path::absolute(d).unwrap_or_else(|_| PathBuf::from(d)));
    match &dir {
        Some(d) if d.is_dir() => {}
        Some(d) => problems.push(format!("--dir: {} is not a folder", d.display())),
        None => problems.push("--dir <folder>: the service's folder, where its command runs".into()),
    }
    let command = a.one("--command").unwrap_or("").trim().to_string();
    let serve = a.one("--serve").unwrap_or("").trim().to_string();
    let bad = |c: &String| c.len() > 2000 || c.chars().any(char::is_control);
    if (command.is_empty() && serve.is_empty()) || bad(&command) || bad(&serve) {
        problems.push("--command \"<command line>\" (or --serve, or both): each one line, at most 2000 characters".into());
    }
    if !problems.is_empty() {
        return Err(problems.join("\n"));
    }
    let dir = dir.unwrap_or_default();
    let node_dir = config::node_dir();
    config::ensure_node_dir(&node_dir)?;
    let cfg = Config::load(&node_dir);
    if !cfg.org.is_empty() && !id.starts_with(&format!("{}.", cfg.org)) {
        eprintln!("warning: this node belongs to {}; the kernel sends it jobs of {}'s services only", cfg.org, cfg.org);
    }
    // TODO(review): isolation — the job account will read this folder: never one that holds
    // the node's own folder, the jobs or `bin` (nor, on Windows, a drive, the system, a profile).
    let jobs_root = cfg.jobs_root();
    if let Some(why) = sys::too_broad(&dir) {
        return Err(why);
    }
    for (p, what) in [(&node_dir, "the node's own folder"), (&jobs_root, "the jobs folder"), (&config::bin_dir(), "bin")] {
        if sys::within(p, &dir) {
            return Err(format!("{} holds {what} ({}): give the service a folder of its own", dir.display(), p.display()));
        }
    }
    match sys::grant_dir(&dir) {
        Ok(msg) => println!("{msg}"),
        Err(msg) => eprintln!("warning: {msg}"),
    }
    // The same check as before going online: the job account must not write to the folder.
    match sys::preflight() {
        Ok(()) => guard::check(&[(dir.clone(), guard::service_fix(&id, &dir))], &node_dir, &jobs_root)
            .map_err(|e| format!("{e}\n{id} is not switched on"))?,
        Err(e) => eprintln!("warning: not checked as {} yet ({e}); `run` checks it", sys::JOB_USER),
    }
    let mut svc = Service::new(dir.clone(), command, a.flag("--gpu"));
    svc.as_job = a.flag("--as-job");
    svc.serve = (!serve.is_empty()).then_some(serve);
    let as_node = svc.runs_as_node(cfg.as_node_by_default());
    let mut on = Services::load(&node_dir);
    on.services.insert(id.clone(), svc);
    on.save(&node_dir)?;
    if as_node {
        println!(
            "switched on: {id} (runs in {} as this account: the default as_node, for the organisation's own calls; jobs of an order as {}; --as-job to change it)",
            dir.display(),
            sys::JOB_USER
        );
    } else {
        println!("switched on: {id} (runs in {} as {})", dir.display(), sys::JOB_USER);
    }
    println!("check it here first: amsyos-node try {id} --params '{{}}'");
    Ok(0)
}

fn what_level_allows(level: Access) -> &'static str {
    match level {
        Access::Services => "the organisation calls only the services switched on here",
        Access::Managed => "its managers may also deploy services here and switch them on and off; jobs run as amsyos-job",
        Access::Full => "its admins may also run any command here as this account (node.exec), and services as_node",
    }
}

fn need_admin(what: &str) -> String {
    if cfg!(windows) {
        format!("{what} needs an administrator: run this in an elevated window (Run as administrator)")
    } else {
        format!("{what} needs an administrator: sudo amsyos-node access …")
    }
}

/// `access`: the level, and at `full` the default `as_node`, set at the machine only.
/// TODO(review): isolation — a level is raised only here, by the person at the machine; `full`
/// and the default `as_node` only by an administrator (an elevated window, or sudo, which
/// writes the SUDO_USER's own config.json and gives it back to them). Lowering, or turning
/// the default off, needs nothing; any level below `full` turns the default off.
fn access(args: &[String]) -> Result<i32, String> {
    let a = Args::parse(args, &[], &["--as-node-default"])?;
    let (dir, owner) = sys::access_target()?;
    if !dir.is_dir() {
        return Err(format!("there is no node folder at {}: join this machine first", dir.display()));
    }
    let mut cfg = Config::load(&dir);
    let (was, had) = (cfg.access(), cfg.as_node_by_default());
    match a.pos.as_slice() {
        [what, on] if what == "as-node-default" && !a.flag("--as-node-default") => {
            let on = match on.as_str() {
                "on" => true,
                "off" => false,
                _ => return Err("amsyos-node access as-node-default on|off".into()),
            };
            if on && was != Access::Full {
                return Err(format!(
                    "the default as_node is for the level full only (it is {}): an administrator runs `amsyos-node access full --as-node-default`",
                    was.as_str()
                ));
            }
            if on && !had && !sys::may_raise_to_full() {
                return Err(need_admin("the default as_node"));
            }
            cfg.as_node_default = on;
        }
        [level] => {
            let want = Access::parse(level).ok_or("the level is services, managed or full (or: as-node-default on|off)")?;
            let default = a.flag("--as-node-default");
            if default && want != Access::Full {
                return Err("--as-node-default goes with full only".into());
            }
            let raising = (want == Access::Full && was != Access::Full) || (default && !had);
            if raising && !sys::may_raise_to_full() {
                return Err(need_admin(if default { "full with the default as_node" } else { "full" }));
            }
            cfg.access = Some(want.as_str().into());
            // On only when asked now, or kept at a node already at `full`.
            cfg.as_node_default = default || (want == Access::Full && had);
        }
        _ => return Err(usage_of("access")),
    }
    cfg.save(&dir)?;
    sys::give_back(&dir.join(config::CONFIG_FILE), owner);
    let (now, default) = (cfg.access(), cfg.as_node_by_default());
    println!("access level: {} (was {})", now.as_str(), was.as_str());
    println!("{}", what_level_allows(now));
    if default != had || default {
        println!(
            "the default as_node: {}{}",
            if default { "on" } else { "off" },
            if default {
                " (what is switched on here runs as this account unless --as-job, and an admin's node.deploy unless as_node: false; jobs of an order and steps still as amsyos-job)"
            } else {
                ""
            }
        );
    }
    println!("a running node says it to the Gateway within 2 s");
    Ok(0)
}

/// The node's folder `allow` and `disallow` change: the caller's own, or under sudo the
/// SUDO_USER's (as `access`), made when it is not there yet (a folder may be allowed before
/// the machine joins).
fn folders_target() -> Result<(PathBuf, Option<(u32, u32)>), String> {
    let (dir, owner) = sys::access_target()?;
    if !dir.is_dir() {
        if owner.is_some() {
            return Err(format!("there is no node folder at {}: join this machine first", dir.display()));
        }
        config::ensure_node_dir(&dir)?;
    }
    Ok((dir, owner))
}

/// `allow <name> <folder>`: Node 2.1 (spec v3 §8.3). TODO(review): isolation — set only at
/// the machine, by the person whose account the node runs as (it reads the folder for the
/// jobs); never a whole drive or the machine's root, never a folder that holds or lies in the
/// node's own folder (its key), the jobs, bin or the managed services. The Gateway learns the
/// name only.
fn allow(args: &[String]) -> Result<i32, String> {
    let a = Args::parse(args, &[], &[])?;
    let [name, path] = a.pos.as_slice() else { return Err(usage_of("allow")) };
    let mut problems = Vec::new();
    if !folder::is_folder_name(name) {
        problems.push("<name>: lowercase letters, digits, - and _, starting with a letter, at most 40 (all the Gateway learns of the folder)".to_string());
    }
    let real = match std::fs::canonicalize(path) {
        Ok(r) if r.is_dir() => Some(r),
        Ok(_) => {
            problems.push(format!("{path} is not a folder"));
            None
        }
        Err(e) => {
            problems.push(format!("{path}: {e}"));
            None
        }
    };
    if !problems.is_empty() {
        return Err(problems.join("\n"));
    }
    let real = real.unwrap_or_default();
    if real.parent().is_none() {
        return Err(format!("{} is a whole drive or the whole machine: allow a folder of your own files", real.display()));
    }
    let (dir, owner) = folders_target()?;
    let mut cfg = Config::load(&dir);
    let mine = folder::plain(&real);
    for (p, what) in [
        (dir.clone(), "the node's own folder (its key)"),
        (cfg.jobs_root(), "the jobs folder"),
        (config::bin_dir(), "bin"),
        (cfg.services_root(), "the managed services folder"),
    ] {
        let theirs = folder::plain(&p);
        if sys::within(&theirs, &mine) || sys::within(&mine, &theirs) {
            return Err(format!("{} holds or lies in {what} ({}): allow a folder of your own files", real.display(), p.display()));
        }
    }
    if cfg.folders.len() >= folder::MOST_FOLDERS && !cfg.folders.contains_key(name) {
        return Err(format!("at most {} folders are allowed at one machine: disallow one first", folder::MOST_FOLDERS));
    }
    let before = cfg.folders.insert(name.clone(), real.clone());
    cfg.save(&dir)?;
    sys::give_back(&dir.join(config::CONFIG_FILE), owner);
    if let Some(was) = before.filter(|w| w != &real) {
        println!("{name} was {}", was.display());
    }
    println!("allowed: {name} = {}", real.display());
    println!(
        "the organisation's jobs may read it in place, read-only, as folder:{name}/<path> (a copy in the job's folder, made here); a session of theirs needs {name} in its grant, a call without one a manager. The Gateway learns the name {name} only"
    );
    println!("a running node says it to the Gateway within 2 s");
    Ok(0)
}

fn disallow_folder(args: &[String]) -> Result<i32, String> {
    let a = Args::parse(args, &[], &[])?;
    let [name] = a.pos.as_slice() else { return Err(usage_of("disallow")) };
    let (dir, owner) = folders_target()?;
    let mut cfg = Config::load(&dir);
    if cfg.folders.remove(name).is_none() {
        return Err(format!("{name} is not allowed here (amsyos-node allowed)"));
    }
    cfg.save(&dir)?;
    sys::give_back(&dir.join(config::CONFIG_FILE), owner);
    println!("disallowed: {name} (a job that reads it already goes on with its copy; the next one is refused)");
    Ok(0)
}

fn allowed() -> Result<i32, String> {
    let (dir, _) = sys::access_target()?;
    let cfg = Config::load(&dir);
    if cfg.folders.is_empty() {
        println!("No folder is allowed here: amsyos-node allow <name> <folder>");
        return Ok(0);
    }
    println!("Folders the organisation's jobs may read in place, as folder:<name>/<path> (the Gateway knows the names only):");
    for (name, path) in &cfg.folders {
        let state = if !folder::is_folder_name(name) {
            "  (not a valid name: disallow it)"
        } else if path.is_dir() {
            ""
        } else {
            "  (not there any more)"
        };
        println!("  {name:<16} {}{state}", path.display());
    }
    Ok(0)
}

fn disable(args: &[String]) -> Result<i32, String> {
    let a = Args::parse(args, &[], &[])?;
    let [id] = a.pos.as_slice() else { return Err("amsyos-node disable <service id>".into()) };
    let node_dir = config::node_dir();
    let mut on = Services::load(&node_dir);
    if on.services.remove(id).is_none() {
        return Err(format!("{id} is not switched on here (amsyos-node services)"));
    }
    on.save(&node_dir)?;
    println!("switched off: {id}");
    Ok(0)
}

/// `try`: a job the person starts here, through the same path as a job from the Gateway
/// (the job account, the same folder layout and limits), without the network.
fn try_service(args: &[String]) -> Result<i32, String> {
    let a = Args::parse(args, &["--params", "--input", "--folder", "--timeout", "--limits"], &["--allow-elevated"])?;
    sys::refuse_identity(a.flag("--allow-elevated"))?;
    let [id] = a.pos.as_slice() else { return Err("amsyos-node try <service id> [--params '<json>'] [--input <name>=<file>]…".into()) };
    let node_dir = config::node_dir();
    let cfg = Config::load(&node_dir);
    let Some(svc) = Services::load(&node_dir).services.get(id).cloned() else {
        return Err(format!("{id} is not switched on here: amsyos-node enable {id} --dir <folder> --command \"…\""));
    };
    sys::preflight()?;
    guard::guard(&node_dir, &cfg.jobs_root(), &Services::load(&node_dir))?;
    let params: Value = serde_json::from_str(a.one("--params").unwrap_or("{}")).map_err(|e| format!("--params: JSON: {e}"))?;
    if !params.is_object() {
        return Err("--params: a JSON object such as '{\"name\": \"An\"}'".into());
    }
    let mut problems = Vec::new();
    let limits_json: Option<Value> = match a.one("--limits") {
        Some(t) => Some(serde_json::from_str(t).map_err(|e| format!("--limits: JSON: {e}"))?),
        None => None,
    };
    let mut limits = exec::Limits {
        cpu: machine::cpus() as f64,
        ram_gb: mem::total_gb().max(1.0),
        gpu: if svc.gpu { "optional".into() } else { "none".into() },
        ..Default::default()
    };
    if let Some(l) = limits_json.as_ref() {
        let given = exec::Limits::parse(Some(l), &mut problems);
        let has = |k: &str| l.get(k).is_some();
        if has("cpu") { limits.cpu = given.cpu }
        if has("ram_gb") { limits.ram_gb = given.ram_gb }
        if has("gpu") { limits.gpu = given.gpu }
        if has("max_runtime_s") { limits.max_runtime_s = given.max_runtime_s }
    }
    if let Some(t) = a.one("--timeout") {
        match t.parse::<u64>() {
            Ok(s) if s >= 1 => limits.max_runtime_s = s,
            _ => problems.push("--timeout: seconds, 1 or more".into()),
        }
    }
    let mut inputs = BTreeMap::new();
    let mut sources = Vec::new();
    for spec in a.all("--input") {
        let Some((name, path)) = spec.split_once('=') else {
            problems.push(format!("--input {spec}: <name>=<file>"));
            continue;
        };
        let path = PathBuf::from(path);
        if !util::is_name(name) || !path.is_file() {
            problems.push(format!("--input {spec}: a name like photo, and a file that exists"));
            continue;
        }
        let ext = path.extension().map(|e| e.to_string_lossy().to_lowercase()).filter(|e| util::is_ext(e));
        let file = match ext {
            Some(e) => format!("in/{name}.{e}"),
            None => format!("in/{name}"),
        };
        inputs.insert(name.to_string(), file.clone());
        sources.push((file, path));
    }
    // Node 2.1: a folder input, read in place as a job from the Gateway reads it.
    let mut folders = Vec::new();
    for spec in a.all("--folder") {
        let Some((input, rest)) = spec.split_once('=') else {
            problems.push(format!("--folder {spec}: <input>=<name>/<path>"));
            continue;
        };
        let (name, rel) = rest.split_once('/').unwrap_or((rest, ""));
        let root = cfg.folders.get(name).filter(|_| folder::is_folder_name(name));
        match root {
            _ if !util::is_name(input) || inputs.contains_key(input) => {
                problems.push(format!("--folder {spec}: an input name like docs, once"));
            }
            None => problems.push(format!("--folder {spec}: {name} is not allowed here (amsyos-node allow {name} <folder>)")),
            Some(root) => match folder::resolve(root, rel) {
                Ok(src) => {
                    let file = src.file_for(input);
                    inputs.insert(input.to_string(), file.clone());
                    folders.push((input.to_string(), file, src));
                }
                Err(e) => problems.push(format!("--folder {spec}: {e}")),
            },
        }
    }
    if !problems.is_empty() {
        return Err(problems.join("\n"));
    }

    let job_id = format!("j_try{}", util::hex(&util::random_bytes::<4>()));
    // TODO(review): isolation — `try` is the person's own call at the machine, so it runs as
    // a direct job of the organisation would: as this account only for a service that runs
    // `as_node` at `full`, else as amsyos-job.
    let as_node = cfg.access() == Access::Full && svc.runs_as_node(cfg.as_node_by_default());
    if as_node {
        println!("as this account ({}, full), as the organisation's own calls run it", svc.as_node_why());
    }
    let jd = exec::prepare(&cfg.jobs_root(), &job_id, id, "try", &params, &inputs)?;
    for (file, src) in &sources {
        let to = exec::input_path(&jd, file);
        std::fs::copy(src, &to).map_err(|e| format!("cannot copy {}: {e}", src.display()))?;
        sys::job_may_read(&to);
    }
    let (_keep, never) = tokio::sync::watch::channel(false);
    for (input, file, src) in &folders {
        let copied = folder::copy(src, &exec::input_path(&jd, file), &never).map_err(|e| format!("--folder {input}: {e}"))?;
        println!("folder input {input}: {file} ({} files, {} bytes)", copied.files, copied.bytes);
        if let Some(note) = copied.note(input) {
            println!("  {note}");
        }
    }
    println!("job {job_id}: {} in {}", svc.shown(), svc.dir.display());
    println!("job folder: {}", jd.dir.display());
    let kills = mem::Kills::now();
    let mut ended = runtime()?.block_on(async {
        let (tx, rx) = tokio::sync::watch::channel(false);
        tokio::spawn(async move {
            if tokio::signal::ctrl_c().await.is_ok() {
                let _ = tx.send(true);
            }
        });
        if svc.served().is_some() {
            // A server of its own, for this job alone (nothing is kept warm by `try`).
            let pool = serve::Pool::new(0, 0.0);
            let (ended, how) = serve::execute(
                &pool, None, &jd, &job_id, &svc, id, &limits, cfg.is_server(), &node_dir, &cfg.jobs_root(), as_node, rx,
                |p| println!("progress: {p}"),
            )
            .await?;
            println!("served: {how}");
            Ok(ended)
        } else {
            exec::execute(&jd, &svc, &limits, cfg.is_server(), &node_dir, as_node, rx, |p| println!("progress: {p}")).await
        }
    })?;
    if !ended.cancelled && !ended.timed_out {
        ended.oom = mem::oom_note(ended.exit, &kills, (!as_node).then_some(limits.ram_gb), None);
    }
    exec::keep_log(&node_dir, &job_id, &ended);
    println!("exit code: {} (ran {:.1} s)", ended.exit, ended.ran.as_secs_f64());
    let verdict = if ended.cancelled {
        Err(("cancelled".to_string(), "stopped with Ctrl+C".to_string()))
    } else {
        ended.judge(&jd, limits.max_runtime_s)
    };
    let (outs, notes) = exec::collect_outputs(&jd, None);
    match &verdict {
        Ok(()) => println!("status: succeeded"),
        Err((error, hint)) => println!("status: failed: {error}: {hint}"),
    }
    if outs.is_empty() {
        println!("outputs: none");
    } else {
        println!("outputs:");
        for o in &outs {
            println!("  {} -> out/{} ({} bytes)", o.name, o.file_name, o.size);
        }
    }
    for n in &notes {
        println!("  {n}");
    }
    let show = |label: &str, text: &str| {
        let t = util::tail(text.trim_end(), 2000);
        if !t.is_empty() {
            println!("{label} (the end):\n{t}");
        }
    };
    show("stdout", &ended.stdout);
    show("stderr", &ended.stderr);
    println!("the job's folder is kept: {}", jd.dir.display());
    Ok(if verdict.is_ok() { 0 } else { 1 })
}

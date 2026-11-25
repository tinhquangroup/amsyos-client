//! Node 2.1.4: the memory the node may use, and why a job was killed (sdk/NODE.md §4).
//!
//! In a container or a systemd slice the node may use less than the machine has: the limit of
//! its own cgroup (v2 `memory.max`, v1 `memory.limit_in_bytes`; the lowest of its folder and
//! the folders above it). Everything the node plans or reports takes the lower of that and the
//! machine's RAM: the `Machine` it sends, the room of the servers kept warm (serve.rs), `try`'s
//! default limits. The CPUs need nothing here: Rust's `available_parallelism` already takes the
//! cgroup's `cpu.max` (v1 `cpu.cfs_quota_us`) and the CPUs the node may run on.
//!
//! A job ended by SIGKILL (exit 137) while the kernel's OOM killer killed (the node's cgroup's
//! `memory.events` `oom_kill`, v1 `memory.oom_control`, or the machine's `/proc/vmstat`
//! `oom_kill` rose) ran out of memory: its hint and `said` say so. The node reads every count
//! itself, before and after the job: never a count a job wrote.

const GB: f64 = (1u64 << 30) as f64;

/// The memory the node may use, in bytes: the machine's RAM, or its cgroup's limit when lower.
pub fn total_bytes() -> u64 {
    let ram = crate::sys::ram_bytes();
    match imp::limit() {
        Some(l) if ram == 0 || l < ram => l,
        _ => ram,
    }
}

pub fn total_gb() -> f64 {
    total_bytes() as f64 / GB
}

/// What the node may still use, in GB: the machine's available memory, or what its cgroup's
/// limit leaves (its page cache not counted) when less. None when it cannot be read.
pub fn free_gb() -> Option<f64> {
    imp::free().map(|b| b as f64 / GB)
}

/// The OOM killer's counts, as the node read them.
#[derive(Clone, Copy, Debug, Default)]
pub struct Kills {
    cgroup: Option<u64>,
    machine: Option<u64>,
}

impl Kills {
    pub fn now() -> Kills {
        imp::kills()
    }

    /// Whether either count rose since `before`.
    pub fn rose_since(&self, before: &Kills) -> bool {
        let rose = |a: Option<u64>, b: Option<u64>| matches!((a, b), (Some(a), Some(b)) if a > b);
        rose(self.cgroup, before.cgroup) || rose(self.machine, before.machine)
    }
}

/// What the node says of a job killed by the OOM killer: its exit was SIGKILL (137) and a count
/// rose since `before`. `own_gb` is the job's own memory limit (None: it has none, as the node's
/// own account); `warm` the idle servers kept warm (how many, the GB they held) as it ended.
pub fn oom_note(exit: i64, before: &Kills, own_gb: Option<f64>, warm: Option<(usize, f64)>) -> Option<String> {
    if exit != 137 || !Kills::now().rose_since(before) {
        return None;
    }
    let own = own_gb.map(|gb| format!(", the job's own {gb:.1} GB (limits.ram_gb)")).unwrap_or_default();
    let held = match warm {
        Some((n, gb)) if n > 0 => format!("; {n} idle warm server(s) held {gb:.1} GB then"),
        _ => String::new(),
    };
    Some(format!("out of memory: the kernel killed the job (exit 137); the machine's limit is {:.1} GB{own}{held}", total_gb()))
}

/// The memory each process tree holds (resident, its own and its children's), in GB, for each
/// root pid; None where it cannot be read.
pub fn trees_gb(roots: &[u32]) -> Vec<Option<f64>> {
    imp::trees(roots).into_iter().map(|b| b.map(|b| b as f64 / GB)).collect()
}

#[cfg(unix)]
mod imp {
    use std::collections::HashMap;
    use std::path::{Path, PathBuf};
    use std::sync::OnceLock;

    use super::Kills;

    const ROOT: &str = "/sys/fs/cgroup";
    const UNLIMITED: u64 = 1 << 60; // v1 says "no limit" as about 2^63

    /// The node's memory cgroup: its folder and those above it, its own first; whether v2.
    pub struct Cg {
        pub folders: Vec<PathBuf>,
        pub v2: bool,
    }

    fn cg() -> &'static Cg {
        static CG: OnceLock<Cg> = OnceLock::new();
        CG.get_or_init(|| find(Path::new(ROOT), &std::fs::read_to_string("/proc/self/cgroup").unwrap_or_default()))
    }

    /// The memory cgroup that `proc_cgroup` (the text of /proc/self/cgroup) names under the
    /// mount `root`. A path not found under it (another cgroup namespace) is the mount's top,
    /// as inside a container.
    pub fn find(root: &Path, proc_cgroup: &str) -> Cg {
        let v2 = root.join("cgroup.controllers").is_file();
        let (top, rel) = if v2 {
            (root.to_path_buf(), proc_cgroup.lines().find_map(|l| l.strip_prefix("0::")).unwrap_or("/"))
        } else {
            let rel = proc_cgroup
                .lines()
                .find_map(|l| {
                    let mut parts = l.splitn(3, ':');
                    let (_, ctrl, path) = (parts.next()?, parts.next()?, parts.next()?);
                    ctrl.split(',').any(|c| c == "memory").then_some(path)
                })
                .unwrap_or("/");
            (root.join("memory"), rel)
        };
        let mut at = top.clone();
        for part in rel.split('/').filter(|p| !p.is_empty() && *p != "." && *p != "..") {
            at.push(part);
        }
        if !at.is_dir() {
            at = top.clone();
        }
        let mut folders = Vec::new();
        loop {
            folders.push(at.clone());
            if at == top || folders.len() >= 32 || !at.pop() {
                break;
            }
        }
        Cg { folders, v2 }
    }

    fn num(path: &Path) -> Option<u64> {
        std::fs::read_to_string(path).ok()?.trim().parse().ok()
    }

    /// `key N` of a flat-keyed file (memory.stat, memory.events, memory.oom_control).
    fn field(path: &Path, key: &str) -> Option<u64> {
        let text = std::fs::read_to_string(path).ok()?;
        text.lines().find_map(|l| {
            let (k, v) = l.split_once(' ')?;
            (k == key).then(|| v.trim().parse().ok()).flatten()
        })
    }

    /// The lowest limit of `cg`'s folders, and what each limit leaves (the least): its limit less
    /// what the folder uses, its inactive page cache not counted.
    pub fn read(cg: &Cg) -> (Option<u64>, Option<u64>) {
        let (max, cur, inactive) = if cg.v2 {
            ("memory.max", "memory.current", "inactive_file")
        } else {
            ("memory.limit_in_bytes", "memory.usage_in_bytes", "total_inactive_file")
        };
        let (mut limit, mut left): (Option<u64>, Option<u64>) = (None, None);
        for f in &cg.folders {
            let Some(l) = num(&f.join(max)).filter(|&l| l > 0 && l < UNLIMITED) else { continue }; // "max": none
            limit = Some(limit.map_or(l, |x| x.min(l)));
            if let Some(used) = num(&f.join(cur)) {
                let cache = field(&f.join("memory.stat"), inactive).unwrap_or(0);
                let l = l.saturating_sub(used.saturating_sub(cache));
                left = Some(left.map_or(l, |x| x.min(l)));
            }
        }
        (limit, left)
    }

    /// The OOM kills of `cg`'s own folder (and those below it).
    pub fn cg_kills(cg: &Cg) -> Option<u64> {
        let f = cg.folders.first()?;
        field(&f.join(if cg.v2 { "memory.events" } else { "memory.oom_control" }), "oom_kill")
    }

    pub fn limit() -> Option<u64> {
        read(cg()).0
    }

    pub fn free() -> Option<u64> {
        let info = std::fs::read_to_string("/proc/meminfo").unwrap_or_default();
        let available = info
            .lines()
            .find_map(|l| l.strip_prefix("MemAvailable:"))
            .and_then(|v| v.trim().trim_end_matches("kB").trim().parse::<u64>().ok())
            .map(|kb| kb * 1024);
        match (available, read(cg()).1) {
            (Some(a), Some(c)) => Some(a.min(c)),
            (a, c) => a.or(c),
        }
    }

    pub fn kills() -> Kills {
        Kills { cgroup: cg_kills(cg()), machine: field(Path::new("/proc/vmstat"), "oom_kill") }
    }

    /// One pass over /proc: each process's parent and resident pages; then each root's tree.
    pub fn trees(roots: &[u32]) -> Vec<Option<u64>> {
        if roots.is_empty() {
            return Vec::new();
        }
        let page = match unsafe { libc::sysconf(libc::_SC_PAGESIZE) } {
            n if n > 0 => n as u64,
            _ => 4096,
        };
        let (mut rss, mut kids): (HashMap<u32, u64>, HashMap<u32, Vec<u32>>) = (HashMap::new(), HashMap::new());
        if let Ok(rd) = std::fs::read_dir("/proc") {
            for e in rd.flatten().take(100_000) {
                let Some(pid) = e.file_name().to_str().and_then(|s| s.parse::<u32>().ok()) else { continue };
                let Ok(stat) = std::fs::read_to_string(format!("/proc/{pid}/stat")) else { continue };
                // After the command's ")": state (3), ppid (4), … rss (24), in pages.
                let Some((_, rest)) = stat.rsplit_once(')') else { continue };
                let f: Vec<&str> = rest.split_whitespace().collect();
                let (Some(ppid), Some(pages)) = (f.get(1).and_then(|v| v.parse::<u32>().ok()), f.get(21).and_then(|v| v.parse::<u64>().ok())) else {
                    continue;
                };
                rss.insert(pid, pages * page);
                kids.entry(ppid).or_default().push(pid);
            }
        }
        roots
            .iter()
            .map(|root| {
                let mut total = *rss.get(root)?;
                let (mut todo, mut seen) = (kids.get(root).cloned().unwrap_or_default(), 0usize);
                while let Some(p) = todo.pop() {
                    seen += 1;
                    if seen > 10_000 {
                        break;
                    }
                    total += rss.get(&p).copied().unwrap_or(0);
                    if let Some(k) = kids.get(&p) {
                        todo.extend(k);
                    }
                }
                Some(total)
            })
            .collect()
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        fn write(p: &Path, name: &str, body: &str) {
            std::fs::create_dir_all(p).unwrap();
            std::fs::write(p.join(name), body).unwrap();
        }

        #[test]
        fn v2_limit_of_a_container_and_of_a_slice_above() {
            let root = std::env::temp_dir().join(format!("amsyos-mem-{}", std::process::id()));
            let _ = std::fs::remove_dir_all(&root);
            write(&root, "cgroup.controllers", "cpu memory\n");
            // A slice of 16 GB above the node's own folder, which has no limit ("max").
            let slice = root.join("node.slice");
            write(&slice, "memory.max", &format!("{}\n", 16u64 << 30));
            write(&slice, "memory.current", &format!("{}\n", 10u64 << 30));
            write(&slice, "memory.stat", &format!("anon 1\ninactive_file {}\n", 2u64 << 30));
            let own = slice.join("amsyos-node.service");
            write(&own, "memory.max", "max\n");
            write(&own, "memory.events", "low 0\nhigh 0\nmax 4\noom 2\noom_kill 3\n");
            let cg = find(&root, "0::/node.slice/amsyos-node.service\n");
            assert_eq!(cg.folders.first(), Some(&own));
            assert!(cg.v2);
            // 16 GB, and 16 - (10 - 2) = 8 GB left.
            assert_eq!(read(&cg), (Some(16u64 << 30), Some(8u64 << 30)));
            assert_eq!(cg_kills(&cg), Some(3));
            // Inside a container: /proc/self/cgroup says "/" and the mount's top is the limit.
            write(&root, "memory.max", &format!("{}\n", 12u64 << 30));
            let cg = find(&root, "0::/\n");
            assert_eq!(cg.folders, vec![root.clone()]);
            assert_eq!(read(&cg).0, Some(12u64 << 30));
            // A path of another namespace: the mount's top.
            assert_eq!(find(&root, "0::/elsewhere/x\n").folders, vec![root.clone()]);
            let _ = std::fs::remove_dir_all(&root);
        }

        #[test]
        fn v1_limit_and_no_limit() {
            let root = std::env::temp_dir().join(format!("amsyos-mem1-{}", std::process::id()));
            let _ = std::fs::remove_dir_all(&root);
            let mem = root.join("memory");
            write(&mem, "memory.limit_in_bytes", "9223372036854771712\n");
            let own = mem.join("docker").join("abc");
            write(&own, "memory.limit_in_bytes", &format!("{}\n", 4u64 << 30));
            write(&own, "memory.usage_in_bytes", &format!("{}\n", 1u64 << 30));
            write(&own, "memory.stat", "total_inactive_file 0\n");
            write(&own, "memory.oom_control", "oom_kill_disable 0\nunder_oom 0\noom_kill 1\n");
            let cg = find(&root, "12:pids:/docker/abc\n4:memory:/docker/abc\n");
            assert!(!cg.v2);
            assert_eq!(cg.folders, vec![own.clone(), mem.join("docker"), mem.clone()]);
            assert_eq!(read(&cg), (Some(4u64 << 30), Some(3u64 << 30)));
            assert_eq!(cg_kills(&cg), Some(1));
            assert_eq!(read(&find(&root, "4:memory:/\n")), (None, None));
            let _ = std::fs::remove_dir_all(&root);
        }

        #[test]
        fn this_process_tree() {
            let me = std::process::id();
            let got = trees(&[me, u32::MAX]);
            assert!(got[0].is_some_and(|b| b > 0));
            assert_eq!(got[1], None);
        }
    }
}

#[cfg(windows)]
mod imp {
    use super::Kills;

    pub fn limit() -> Option<u64> {
        None
    }

    pub fn free() -> Option<u64> {
        crate::sys::ram_free_bytes()
    }

    pub fn kills() -> Kills {
        Kills::default() // a Job Object's memory limit fails an allocation; it kills nothing
    }

    pub fn trees(roots: &[u32]) -> Vec<Option<u64>> {
        vec![None; roots.len()]
    }
}

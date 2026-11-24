//! Node 2.1.2: the Python job module (`from amsyos import job, serve`, sdk/amsyos) ships inside
//! this program and lies on every job's PYTHONPATH (`config::python_dir`), whatever account the
//! job runs as. `build.sh` copies sdk/amsyos/__init__.py into `py/` before it builds, so the
//! module is always the contract's own.

use std::path::PathBuf;

use crate::config;

pub const MODULE: &str = include_str!("../py/amsyos/__init__.py");

pub fn module_path() -> PathBuf {
    config::python_dir().join("amsyos").join("__init__.py")
}

/// Whether the module on the machine is this program's.
pub fn current() -> bool {
    std::fs::read(module_path()).is_ok_and(|b| b == MODULE.as_bytes())
}

/// TODO(review): isolation — writes the module where every job reads it: inside `bin`, which
/// only the node's user and administrators may write (on Linux 0755 folders and a 0644 file;
/// on Windows `bin`'s ACL, inherited). Ok(false) when it was there already.
pub fn install() -> Result<bool, String> {
    if current() {
        return Ok(false);
    }
    let top = config::python_dir();
    let dir = top.join("amsyos");
    std::fs::create_dir_all(&dir).map_err(|e| format!("cannot make {}: {e}", dir.display()))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        for d in [&top, &dir] {
            std::fs::set_permissions(d, std::fs::Permissions::from_mode(0o755))
                .map_err(|e| format!("cannot set the mode of {}: {e}", d.display()))?;
        }
    }
    let path = module_path();
    let fresh = path.with_extension("py.new");
    std::fs::write(&fresh, MODULE).map_err(|e| format!("cannot write {}: {e}", fresh.display()))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&fresh, std::fs::Permissions::from_mode(0o644)).map_err(|e| e.to_string())?;
    }
    std::fs::rename(&fresh, &path).map_err(|e| format!("cannot write {}: {e}", path.display()))?;
    Ok(true)
}

/// A job's PYTHONPATH: what it inherited first (a job run as the node's own account inherits
/// the node's), so that a service's own path still wins, then the module's folder.
pub fn pythonpath(inherited: Option<&str>) -> String {
    let ours = config::python_dir().display().to_string();
    let sep = if cfg!(windows) { ';' } else { ':' };
    match inherited.map(str::trim).filter(|p| !p.is_empty()) {
        Some(p) => format!("{p}{sep}{ours}"),
        None => ours,
    }
}

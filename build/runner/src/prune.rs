// Copyright: Anki Software, LLC and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

use std::fs;
use std::io;
use std::path::Path;
use std::path::PathBuf;
use std::time::Duration;
use std::time::SystemTime;

use camino::Utf8Path;

/// Incremental caches not used for this long are deleted.
const STALE_AFTER: Duration = Duration::from_secs(7 * 24 * 60 * 60);
/// Minimum time between two prune passes.
const PRUNE_INTERVAL: Duration = Duration::from_secs(24 * 60 * 60);

/// Deletes Rust incremental caches that no build has used for a week, at most
/// once a day (throttled by `<build_root>/last-prune`).
///
/// Only immediate subdirectories of `<build_root>/rust/<profile>/incremental/`
/// are removed. rustc adds a session dir to a crate's dir on each compile, so
/// that dir's mtime is its last use; cargo fingerprints don't track these dirs,
/// so deleting one only costs a non-incremental compile of that crate. Nothing
/// else is touched: `deps/` and `build/` outputs are reused without their mtime
/// changing, so their age says nothing about whether they are still needed.
/// Symlinks are never followed.
///
/// Runs under the build lock after a successful build, so no other build in
/// this checkout is compiling into these dirs. Errors are reported as warnings
/// and never fail the build.
pub fn maybe_prune_stale_build_caches(build_root: &Utf8Path) {
    let stamp = build_root.join("last-prune");
    let now = SystemTime::now();
    if let Ok(last) = fs::metadata(&stamp).and_then(|meta| meta.modified()) {
        if now
            .duration_since(last)
            .is_ok_and(|age| age < PRUNE_INTERVAL)
        {
            return;
        }
    }

    let (count, bytes) = prune_stale_incremental_dirs(build_root.as_std_path(), now);
    if count > 0 {
        println!(
            "Pruned {count} stale Rust incremental caches ({}).",
            format_size(bytes)
        );
    }

    // Set the mtime explicitly: truncating an already empty file may not.
    if let Err(err) = fs::File::create(&stamp).and_then(|file| file.set_modified(now)) {
        println!("Warning: could not write {stamp}: {err}");
    }
}

/// Returns the number of crate dirs deleted and their total size in bytes.
fn prune_stale_incremental_dirs(build_root: &Path, now: SystemTime) -> (usize, u64) {
    let mut count = 0;
    let mut bytes = 0;
    for profile in real_subdirs(&build_root.join("rust")) {
        for crate_dir in real_subdirs(&profile.join("incremental")) {
            let modified = match fs::symlink_metadata(&crate_dir).and_then(|m| m.modified()) {
                Ok(modified) => modified,
                Err(err) => {
                    warn(&crate_dir, &err);
                    continue;
                }
            };
            if !now
                .duration_since(modified)
                .is_ok_and(|age| age > STALE_AFTER)
            {
                continue;
            }
            let size = dir_size(&crate_dir);
            match fs::remove_dir_all(&crate_dir) {
                Ok(()) => {
                    count += 1;
                    bytes += size;
                }
                Err(err) => warn(&crate_dir, &err),
            }
        }
    }
    (count, bytes)
}

/// Immediate subdirectories of `dir`, skipping symlinks. Returns nothing when
/// `dir` is missing or is itself a symlink.
fn real_subdirs(dir: &Path) -> Vec<PathBuf> {
    match fs::symlink_metadata(dir) {
        Ok(meta) if meta.is_dir() => {}
        Ok(_) => return vec![],
        Err(err) if err.kind() == io::ErrorKind::NotFound => return vec![],
        Err(err) => {
            warn(dir, &err);
            return vec![];
        }
    }
    let entries = match fs::read_dir(dir) {
        Ok(entries) => entries,
        Err(err) => {
            warn(dir, &err);
            return vec![];
        }
    };
    let mut dirs = vec![];
    for entry in entries {
        match entry.and_then(|entry| Ok((entry.path(), entry.file_type()?))) {
            Ok((path, file_type)) if file_type.is_dir() => dirs.push(path),
            Ok(_) => {}
            Err(err) => warn(dir, &err),
        }
    }
    dirs
}

/// Best-effort total size of the files under `dir`, without following
/// symlinks.
fn dir_size(dir: &Path) -> u64 {
    let Ok(entries) = fs::read_dir(dir) else {
        return 0;
    };
    entries
        .flatten()
        .filter_map(|entry| fs::symlink_metadata(entry.path()).ok().map(|m| (entry, m)))
        .map(|(entry, meta)| {
            if meta.is_dir() {
                dir_size(&entry.path())
            } else {
                meta.len()
            }
        })
        .sum()
}

fn format_size(bytes: u64) -> String {
    if bytes >= 1_000_000_000 {
        format!("{:.1} GB", bytes as f64 / 1e9)
    } else {
        format!("{:.0} MB", bytes as f64 / 1e6)
    }
}

fn warn(path: &Path, err: &io::Error) {
    println!(
        "Warning: skipping Rust cache prune of {}: {err}",
        path.display()
    );
}

#[cfg(all(test, unix))]
mod tests {
    use std::fs::File;
    use std::time::UNIX_EPOCH;

    use super::*;

    fn temp_dir() -> PathBuf {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let dir = std::env::temp_dir().join(format!("anki-prune-test-{unique}"));
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    fn set_mtime(path: &Path, time: SystemTime) {
        File::open(path).unwrap().set_modified(time).unwrap();
    }

    #[test]
    fn prunes_only_stale_real_incremental_dirs() {
        let root = temp_dir();
        let now = SystemTime::now();
        let old = now - STALE_AFTER - Duration::from_secs(60);
        let debug = root.join("rust/debug");

        let stale = debug.join("incremental/anki-old");
        fs::create_dir_all(stale.join("s-session")).unwrap();
        fs::write(stale.join("s-session/query-cache.bin"), [0u8; 100]).unwrap();
        set_mtime(&stale, old);

        let fresh = debug.join("incremental/anki-new");
        fs::create_dir_all(&fresh).unwrap();

        let outside = root.join("outside-old");
        fs::create_dir_all(&outside).unwrap();
        set_mtime(&outside, old);
        std::os::unix::fs::symlink(&outside, debug.join("incremental/link")).unwrap();

        let dep = debug.join("deps/libold.rlib");
        fs::create_dir_all(dep.parent().unwrap()).unwrap();
        fs::write(&dep, b"rlib").unwrap();
        set_mtime(&dep, old);
        set_mtime(&debug.join("deps"), old);

        // A symlinked profile must not be followed either.
        let linked_profile = root.join("linked-profile/incremental/anki-old");
        fs::create_dir_all(&linked_profile).unwrap();
        set_mtime(&linked_profile, old);
        std::os::unix::fs::symlink(root.join("linked-profile"), root.join("rust/linked")).unwrap();

        assert_eq!(prune_stale_incremental_dirs(&root, now), (1, 100));
        assert!(!stale.exists());
        assert!(fresh.exists());
        assert!(outside.exists());
        assert!(debug.join("incremental/link").exists());
        assert!(dep.exists());
        assert!(linked_profile.exists());

        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn stamp_throttles_pruning() {
        let root = temp_dir();
        let build_root = Utf8Path::from_path(&root).unwrap();
        let stale = root.join("rust/debug/incremental/anki-old");
        let old = SystemTime::now() - STALE_AFTER - Duration::from_secs(60);

        fs::create_dir_all(&stale).unwrap();
        set_mtime(&stale, old);
        maybe_prune_stale_build_caches(build_root);
        assert!(!stale.exists());
        assert!(root.join("last-prune").exists());

        // Within the interval, nothing is pruned.
        fs::create_dir_all(&stale).unwrap();
        set_mtime(&stale, old);
        maybe_prune_stale_build_caches(build_root);
        assert!(stale.exists());

        // Once the stamp is older than the interval, pruning runs again.
        set_mtime(
            &root.join("last-prune"),
            SystemTime::now() - PRUNE_INTERVAL - Duration::from_secs(60),
        );
        maybe_prune_stale_build_caches(build_root);
        assert!(!stale.exists());

        fs::remove_dir_all(root).unwrap();
    }
}

// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

use std::fs;
use std::process::Command;

use camino::Utf8Path;
use clap::Args;

use crate::run::run_command;

#[derive(Args)]
pub struct PyenvArgs {
    uv_bin: String,
    pyenv_folder: String,
    python: String,
    #[arg(trailing_var_arg = true)]
    extra_args: Vec<String>,
}

/// Set up a venv if one doesn't already exist, and then sync packages with
/// provided requirements file.
pub fn setup_pyenv(args: PyenvArgs) {
    let pyenv_folder = Utf8Path::new(&args.pyenv_folder);

    // On first run, ninja creates an empty bin/ folder which breaks the initial
    // install. But we don't want to indiscriminately remove the folder, or
    // macOS Gatekeeper needs to rescan the files each time.
    if pyenv_folder.exists() {
        let cache_tag = pyenv_folder.join("CACHEDIR.TAG");
        if !cache_tag.exists() {
            fs::remove_dir_all(pyenv_folder).expect("Failed to remove existing pyenv folder");
        }
    }

    let mut command = Command::new(args.uv_bin);

    // remove UV_* environment variables to avoid interference
    for (key, _) in std::env::vars() {
        if key.starts_with("UV_") || key == "VIRTUAL_ENV" {
            command.env_remove(key);
        }
    }

    // Never use `--no-config` here: `[tool.uv] exclude-newer` must be read so the
    // lockfile cutoff matches `uv sync --locked`. UV_* env vars are cleared
    // above for isolation.
    run_command(
        command
            .env("UV_PROJECT_ENVIRONMENT", args.pyenv_folder.clone())
            .args(["sync", "--locked"])
            .args(["--python", &args.python])
            .args(args.extra_args),
    );

    repair_macos_anki_audio(pyenv_folder).expect("Failed to repair macOS anki-audio package");

    // Write empty stamp file
    fs::write(pyenv_folder.join(".stamp"), "").expect("Failed to write stamp file");
}

#[cfg(any(target_os = "macos", test))]
fn repair_anki_audio_library_layout(audio_dir: &Utf8Path) -> std::io::Result<bool> {
    let lib_dir = audio_dir.join("lib");
    let libs_dir = audio_dir.join("libs");
    if lib_dir.exists() && !libs_dir.exists() {
        fs::rename(lib_dir, libs_dir)?;
        return Ok(true);
    }

    Ok(false)
}

#[cfg(target_os = "macos")]
fn repair_macos_anki_audio(pyenv_folder: &Utf8Path) -> std::io::Result<()> {
    if let Some(audio_dir) = find_anki_audio_dir(pyenv_folder)? {
        if repair_anki_audio_library_layout(&audio_dir)? {
            println!("Repaired anki-audio macOS library directory");
        }
        codesign_anki_audio_files(&audio_dir)?;
    }

    Ok(())
}

#[cfg(not(target_os = "macos"))]
fn repair_macos_anki_audio(_pyenv_folder: &Utf8Path) -> std::io::Result<()> {
    Ok(())
}

#[cfg(target_os = "macos")]
fn find_anki_audio_dir(pyenv_folder: &Utf8Path) -> std::io::Result<Option<camino::Utf8PathBuf>> {
    let lib_dir = pyenv_folder.join("lib");
    if !lib_dir.exists() {
        return Ok(None);
    }

    for entry in fs::read_dir(lib_dir)? {
        let path = entry?.path();
        let Ok(path) = camino::Utf8PathBuf::from_path_buf(path) else {
            continue;
        };
        if !path
            .file_name()
            .is_some_and(|name| name.starts_with("python"))
        {
            continue;
        }
        let audio_dir = path.join("site-packages").join("anki_audio");
        if audio_dir.exists() {
            return Ok(Some(audio_dir));
        }
    }

    Ok(None)
}

#[cfg(target_os = "macos")]
fn codesign_anki_audio_files(audio_dir: &Utf8Path) -> std::io::Result<()> {
    let mut files = vec![];
    for binary in ["mpv", "lame"] {
        let path = audio_dir.join(binary);
        if path.exists() {
            files.push(path);
        }
    }

    let libs_dir = audio_dir.join("libs");
    if libs_dir.exists() {
        for entry in fs::read_dir(libs_dir)? {
            let path = entry?.path();
            let Ok(path) = camino::Utf8PathBuf::from_path_buf(path) else {
                continue;
            };
            if path.extension() == Some("dylib") {
                files.push(path);
            }
        }
    }

    if !files.is_empty() {
        println!("Re-signing anki-audio binaries for local macOS venv");
    }
    for file in files {
        run_command(
            Command::new("codesign")
                .args(["--force", "--sign", "-"])
                .arg(file.as_str()),
        );
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn temp_dir() -> camino::Utf8PathBuf {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        camino::Utf8PathBuf::from_path_buf(
            std::env::temp_dir().join(format!("anki-pyenv-test-{unique}")),
        )
        .unwrap()
    }

    #[test]
    fn repair_anki_audio_library_layout_renames_lib_to_libs() {
        let dir = temp_dir();
        let audio_dir = dir.join("anki_audio");
        let lib_dir = audio_dir.join("lib");
        fs::create_dir_all(&lib_dir).unwrap();
        fs::write(lib_dir.join("libass.9.dylib"), b"fake").unwrap();

        assert!(repair_anki_audio_library_layout(&audio_dir).unwrap());
        assert_eq!(
            fs::read(audio_dir.join("libs").join("libass.9.dylib")).unwrap(),
            b"fake"
        );
        assert!(!lib_dir.exists());

        fs::remove_dir_all(dir).unwrap();
    }
}

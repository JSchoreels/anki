// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! The file that keeps the offline RWKV state between app starts
//! (`collection.rwkv-offline`, next to the collection), so that a start
//! replays only the reviews that came after it instead of the whole history.
//!
//! It holds the model state (the engine's warm-up states and cache state)
//! and the identity of the review history it has absorbed. The header ties it
//! to one state version and one model file: anything else is ignored, and the
//! history is replayed in full as before. `offline.rs` decides when to write
//! and read it; the history check there catches a file that no longer fits
//! the collection.
//!
//! The states are streamed to and from the file: with a big history the file
//! was over 1 GB, and holding it in memory next to the engine's own copy got
//! the app's neighbours killed on phones. Card and note states are written as
//! the engine keeps them (int8 by default), the few others as half floats.
//!
//! The file never leaves the device (it is not synced; the desktop keeps its
//! own state), so its format can change freely: a file in an old format is
//! ignored and the history replayed once.

use std::fs;
use std::hash::Hasher;
use std::io;
use std::io::BufReader;
use std::io::BufWriter;
use std::io::Read;
use std::io::Write;
use std::path::Path;
use std::path::PathBuf;
use std::sync::Mutex;

use super::offline::OfflineModel;
use super::offline_engine::Engine;
use super::offline_engine::Snapshot;
use crate::rwkv::StatePrecision;

/// 3: card/note states as the engine keeps them (2: all as half floats, 1:
/// full floats).
const MAGIC: &[u8] = b"KUMA3RWKVOFFLINE3\n";
const BUFFER: usize = 1 << 20;

/// How a saved state was made, besides the model: the engine, the review
/// inputs and the history walk. **Bump it with any change to those** (what
/// `history_plan` feeds `warm_up_reviews`, the model code in `rwkv/`): a file
/// from before is then ignored and the history replayed. It used to be the
/// backend build hash, so every app update replayed the whole history (~10
/// min for 3.7M reviews on a Pixel 8a) even when nothing about it changed.
const STATE_VERSION: u32 = 3; // 3: fork build 102 (reset boundaries, int8 states)

/// What the file must have been written for: this [STATE_VERSION], this
/// state precision and this model file (its size and an FNV-1a hash of its
/// bytes). The model doesn't change while the app runs, so its 11 MB are read
/// and hashed once, not at every save and start.
pub(super) fn header(model: &OfflineModel) -> io::Result<String> {
    static HASHED: Mutex<Option<(PathBuf, String)>> = Mutex::new(None);
    let mut cached = HASHED.lock().unwrap();
    let hashed = match cached.as_ref() {
        Some((path, hashed)) if *path == model.path => hashed.clone(),
        _ => {
            let bytes = fs::read(&model.path)?;
            let mut hash = fnv::FnvHasher::default();
            hash.write(&bytes);
            let hashed = format!("{} {:016x}", bytes.len(), hash.finish());
            *cached = Some((model.path.clone(), hashed.clone()));
            hashed
        }
    };
    // int4 keeps note states in int8 since kuma3 17: a file from before
    // holds int4 note states and is rebuilt. Since the fork's build 107 that
    // is its Int4CardsInt8Notes; the header stays, so no rebuild.
    let precision = match model.precision {
        StatePrecision::Int4CardsInt8Notes => "Int4Int8Notes".to_string(),
        other => format!("{other:?}"),
    };
    Ok(format!("state{STATE_VERSION} {precision} {hashed}"))
}

/// Writes the file whole (to a temporary file, then renamed), so a crash
/// never leaves half a file behind. `state` is a snapshot, so this can run on
/// any thread.
pub(super) fn write(
    path: &Path,
    header: &str,
    identity: &[u8],
    state: &Snapshot,
) -> io::Result<()> {
    // a name of its own: two builds for one collection (profile switching) don't
    // write into the same file, and a failed write leaves nothing behind
    let mut temporary = tempfile::Builder::new()
        .suffix(".rwkv-offline.tmp")
        .tempfile_in(path.parent().unwrap_or(Path::new(".")))?;
    let mut out = BufWriter::with_capacity(BUFFER, temporary.as_file_mut());
    out.write_all(MAGIC)?;
    put_bytes(&mut out, header.as_bytes())?;
    put_bytes(&mut out, identity)?;
    state.write_warm_up_states(&mut out)?;
    put_bytes(&mut out, state.cache_state())?;
    out.into_inner()
        .map_err(|err| err.into_error())?
        .sync_all()?;
    temporary.persist(path).map(drop).map_err(|err| err.error)
}

/// Reads the file written by [write] into `inference` and returns the history
/// identity; an error when it is missing, damaged or was written for another
/// header. After an error `inference` may hold part of the file: reset it.
pub(super) fn read(path: &Path, header: &str, inference: &mut Engine) -> io::Result<Vec<u8>> {
    let mut input = BufReader::with_capacity(BUFFER, fs::File::open(path)?);
    let identity = read_head(&mut input, header)?;
    inference.read_warm_up_states(&mut input)?;
    inference.restore_cache_state(&get_bytes(&mut input, 1 << 30)?)?;
    if input.read(&mut [0])? != 0 {
        return Err(invalid("trailing data"));
    }
    Ok(identity)
}

/// Just the history identity of the file at `path` (it comes first), without
/// the states.
pub(super) fn read_identity(path: &Path, header: &str) -> io::Result<Vec<u8>> {
    read_head(&mut BufReader::new(fs::File::open(path)?), header)
}

fn read_head(input: &mut impl Read, header: &str) -> io::Result<Vec<u8>> {
    let mut magic = [0_u8; MAGIC.len()];
    input.read_exact(&mut magic)?;
    if magic != MAGIC {
        return Err(invalid("not an offline RWKV state file"));
    }
    if get_bytes(input, 4096)? != header.as_bytes() {
        return Err(invalid(
            "written by another state version or for another model",
        ));
    }
    get_bytes(input, 1 << 20)
}

fn put_bytes(out: &mut impl Write, bytes: &[u8]) -> io::Result<()> {
    out.write_all(&(bytes.len() as u64).to_le_bytes())?;
    out.write_all(bytes)
}

fn get_bytes(input: &mut impl Read, max: u64) -> io::Result<Vec<u8>> {
    let mut len = [0_u8; 8];
    input.read_exact(&mut len)?;
    let len = u64::from_le_bytes(len);
    if len > max {
        return Err(invalid("length"));
    }
    // grows with what is actually there, so a damaged length can't reserve memory
    let mut bytes = Vec::new();
    input.take(len).read_to_end(&mut bytes)?;
    if bytes.len() as u64 != len {
        return Err(io::ErrorKind::UnexpectedEof.into());
    }
    Ok(bytes)
}

fn invalid(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message.to_string())
}

#[cfg(test)]
mod tests {
    use std::env;
    use std::path::PathBuf;
    use std::time::Instant;

    use super::*;

    /// Reads a real state file (`KUMA3_RWKV_OFFLINE_FILE`, with the model it
    /// was written for in `KUMA3_RWKV_OFFLINE_MODEL`) and writes it back: the
    /// bytes must not change. The header is taken from the file, since this
    /// test build may have another state version.
    #[test]
    #[ignore]
    fn offline_state_file_round_trip() {
        let (Ok(file), Ok(model)) = (
            env::var("KUMA3_RWKV_OFFLINE_FILE"),
            env::var("KUMA3_RWKV_OFFLINE_MODEL"),
        ) else {
            eprintln!("set KUMA3_RWKV_OFFLINE_FILE and KUMA3_RWKV_OFFLINE_MODEL");
            return;
        };
        let file = PathBuf::from(file);
        let mut input = BufReader::new(fs::File::open(&file).unwrap());
        input.read_exact(&mut [0; MAGIC.len()]).unwrap();
        let header = String::from_utf8(get_bytes(&mut input, 4096).unwrap()).unwrap();
        drop(input);

        let mut inference =
            Engine::load_with_precision(PathBuf::from(model), 0.9, 36_500, StatePrecision::F32)
                .unwrap();
        let started = Instant::now();
        let identity = read(&file, &header, &mut inference).unwrap();
        eprintln!("read {} ms", started.elapsed().as_millis());
        let copy = file.with_extension("roundtrip");
        let started = Instant::now();
        write(&copy, &header, &identity, &inference.state_snapshot()).unwrap();
        eprintln!("write {} ms", started.elapsed().as_millis());
        let same = fs::read(&copy).unwrap() == fs::read(&file).unwrap();
        fs::remove_file(&copy).unwrap();
        assert!(same, "the written file differs from the one read");
    }

    /// A damaged length is an error, without reserving that much memory.
    #[test]
    fn get_bytes_checks_the_length() {
        let mut data = (1_u64 << 30).to_le_bytes().to_vec();
        data.extend_from_slice(b"abc");
        let err = get_bytes(&mut data.as_slice(), 1 << 30).unwrap_err();
        assert_eq!(err.kind(), io::ErrorKind::UnexpectedEof);
        let mut data = 3_u64.to_le_bytes().to_vec();
        data.extend_from_slice(b"abc");
        assert_eq!(get_bytes(&mut data.as_slice(), 1 << 30).unwrap(), b"abc");
    }
}

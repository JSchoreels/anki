// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

use std::collections::HashMap;
use std::fs;
use std::io;
use std::path::PathBuf;
use std::sync::Arc;

use rayon::prelude::*;

mod bulk;
#[cfg(target_os = "macos")]
mod matmul;

const D_MODEL: usize = 128;
const CARD_FEATURES: usize = 92;
const HEADS: usize = 4;
const HEAD_SIZE: usize = D_MODEL / HEADS;
const HEAD_DIM: usize = 4 * D_MODEL;
const NUM_CURVES: usize = 128;
const ANSWER_EASES: [u8; 4] = [1, 2, 3, 4];
const S90_TARGET_RETENTION: f32 = 0.9;

const MODULE_LAYERS: [usize; 5] = [3, 4, 2, 3, 4];
const CHANNEL_MIXER_DIMS: [usize; 5] = [192, 256, 192, 256, 256];
/// Rows per block in the bulk replay's blocked projection kernels. Bounds the
/// stack scratch used per block; larger blocks amortize weight-matrix streaming
/// over more rows.
const LINEAR_BLOCK_ROWS: usize = 32;

#[cfg(test)]
#[derive(Clone)]
struct RwkvScanCapturedStep {
    r: Vec<f32>,
    k: Vec<f32>,
    v: Vec<f32>,
    w: Vec<f32>,
    a: Vec<f32>,
    k_deformed: Vec<f32>,
}

#[cfg(test)]
static RWKV_SINGLE_TIMESTEP_PROFILE_ENABLED: std::sync::atomic::AtomicBool =
    std::sync::atomic::AtomicBool::new(false);
#[cfg(test)]
static RWKV_SINGLE_TIMESTEP_PROFILE_CALLS: std::sync::atomic::AtomicU64 =
    std::sync::atomic::AtomicU64::new(0);
#[cfg(test)]
static RWKV_SINGLE_TIMESTEP_PROFILE_NANOS: std::sync::atomic::AtomicU64 =
    std::sync::atomic::AtomicU64::new(0);

#[cfg(test)]
const RWKV_WARMUP_PROFILE_BUCKETS: usize = 16;

#[cfg(test)]
#[derive(Clone, Copy)]
enum RwkvWarmupProfileBucket {
    FeaturesFor = 0,
    FeaturesStore = 1,
    ModelReview = 2,
    FeatureMlp = 3,
    ModuleCard = 4,
    ModuleDeck = 5,
    ModuleNote = 6,
    ModulePreset = 7,
    ModuleGlobal = 8,
    Heads = 9,
    ModuleRun = 10,
    TimeMixer = 11,
    ChannelMixer = 12,
    Linear = 13,
    Norm = 14,
    SingleTimestep = 15,
}

#[cfg(test)]
static RWKV_WARMUP_PROFILE_ENABLED: std::sync::atomic::AtomicBool =
    std::sync::atomic::AtomicBool::new(false);
#[cfg(test)]
static RWKV_WARMUP_PROFILE_CALLS: [std::sync::atomic::AtomicU64; RWKV_WARMUP_PROFILE_BUCKETS] =
    [const { std::sync::atomic::AtomicU64::new(0) }; RWKV_WARMUP_PROFILE_BUCKETS];
#[cfg(test)]
static RWKV_WARMUP_PROFILE_NANOS: [std::sync::atomic::AtomicU64; RWKV_WARMUP_PROFILE_BUCKETS] =
    [const { std::sync::atomic::AtomicU64::new(0) }; RWKV_WARMUP_PROFILE_BUCKETS];

#[cfg(test)]
fn start_rwkv_warmup_profile() {
    for index in 0..RWKV_WARMUP_PROFILE_BUCKETS {
        RWKV_WARMUP_PROFILE_CALLS[index].store(0, std::sync::atomic::Ordering::Relaxed);
        RWKV_WARMUP_PROFILE_NANOS[index].store(0, std::sync::atomic::Ordering::Relaxed);
    }
    RWKV_WARMUP_PROFILE_ENABLED.store(true, std::sync::atomic::Ordering::Relaxed);
}

#[cfg(test)]
fn stop_rwkv_warmup_profile() -> Vec<(&'static str, u64, u64)> {
    RWKV_WARMUP_PROFILE_ENABLED.store(false, std::sync::atomic::Ordering::Relaxed);
    [
        ("features_for", RwkvWarmupProfileBucket::FeaturesFor),
        ("features_store", RwkvWarmupProfileBucket::FeaturesStore),
        ("model_review", RwkvWarmupProfileBucket::ModelReview),
        ("feature_mlp", RwkvWarmupProfileBucket::FeatureMlp),
        ("module_card", RwkvWarmupProfileBucket::ModuleCard),
        ("module_deck", RwkvWarmupProfileBucket::ModuleDeck),
        ("module_note", RwkvWarmupProfileBucket::ModuleNote),
        ("module_preset", RwkvWarmupProfileBucket::ModulePreset),
        ("module_global", RwkvWarmupProfileBucket::ModuleGlobal),
        ("heads", RwkvWarmupProfileBucket::Heads),
        ("module_run", RwkvWarmupProfileBucket::ModuleRun),
        ("time_mixer", RwkvWarmupProfileBucket::TimeMixer),
        ("channel_mixer", RwkvWarmupProfileBucket::ChannelMixer),
        ("linear", RwkvWarmupProfileBucket::Linear),
        ("norm", RwkvWarmupProfileBucket::Norm),
        ("single_timestep", RwkvWarmupProfileBucket::SingleTimestep),
    ]
    .into_iter()
    .map(|(name, bucket)| {
        let index = bucket as usize;
        (
            name,
            RWKV_WARMUP_PROFILE_CALLS[index].load(std::sync::atomic::Ordering::Relaxed),
            RWKV_WARMUP_PROFILE_NANOS[index].load(std::sync::atomic::Ordering::Relaxed),
        )
    })
    .collect()
}

#[cfg(test)]
fn rwkv_warmup_profile_start() -> Option<std::time::Instant> {
    RWKV_WARMUP_PROFILE_ENABLED
        .load(std::sync::atomic::Ordering::Relaxed)
        .then(std::time::Instant::now)
}

#[cfg(test)]
fn rwkv_warmup_profile_record(
    bucket: RwkvWarmupProfileBucket,
    started: Option<std::time::Instant>,
) {
    if let Some(started) = started {
        let index = bucket as usize;
        RWKV_WARMUP_PROFILE_CALLS[index].fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        RWKV_WARMUP_PROFILE_NANOS[index].fetch_add(
            started.elapsed().as_nanos().min(u128::from(u64::MAX)) as u64,
            std::sync::atomic::Ordering::Relaxed,
        );
    }
}

#[cfg(test)]
fn start_rwkv_single_timestep_profile() {
    RWKV_SINGLE_TIMESTEP_PROFILE_CALLS.store(0, std::sync::atomic::Ordering::Relaxed);
    RWKV_SINGLE_TIMESTEP_PROFILE_NANOS.store(0, std::sync::atomic::Ordering::Relaxed);
    RWKV_SINGLE_TIMESTEP_PROFILE_ENABLED.store(true, std::sync::atomic::Ordering::Relaxed);
}

#[cfg(test)]
fn stop_rwkv_single_timestep_profile() -> (u64, u64) {
    RWKV_SINGLE_TIMESTEP_PROFILE_ENABLED.store(false, std::sync::atomic::Ordering::Relaxed);
    (
        RWKV_SINGLE_TIMESTEP_PROFILE_CALLS.load(std::sync::atomic::Ordering::Relaxed),
        RWKV_SINGLE_TIMESTEP_PROFILE_NANOS.load(std::sync::atomic::Ordering::Relaxed),
    )
}

#[cfg(test)]
struct RwkvScanCaptureState {
    module_id: usize,
    layer_id: usize,
    steps: Vec<RwkvScanCapturedStep>,
}

#[cfg(test)]
std::thread_local! {
    static RWKV_SCAN_CAPTURE: std::cell::RefCell<Option<RwkvScanCaptureState>> =
        const { std::cell::RefCell::new(None) };
}

#[cfg(test)]
fn start_rwkv_scan_capture(module_id: usize, layer_id: usize) {
    RWKV_SCAN_CAPTURE.with(|capture| {
        *capture.borrow_mut() = Some(RwkvScanCaptureState {
            module_id,
            layer_id,
            steps: Vec::new(),
        });
    });
}

#[cfg(test)]
fn rwkv_scan_capture_active() -> bool {
    RWKV_SCAN_CAPTURE.with(|capture| capture.borrow().is_some())
}

#[cfg(test)]
fn take_rwkv_scan_capture() -> Vec<RwkvScanCapturedStep> {
    RWKV_SCAN_CAPTURE.with(|capture| {
        capture
            .borrow_mut()
            .take()
            .map(|capture| capture.steps)
            .unwrap_or_default()
    })
}

#[cfg(test)]
#[allow(clippy::too_many_arguments)]
fn record_rwkv_scan_step(
    module_id: usize,
    layer_id: usize,
    r: &[f32],
    k: &[f32],
    v: &[f32],
    w: &[f32],
    a: &[f32],
    k_deformed: &[f32],
) {
    RWKV_SCAN_CAPTURE.with(|capture| {
        let mut capture = capture.borrow_mut();
        let Some(capture) = capture.as_mut() else {
            return;
        };
        if capture.module_id != module_id || capture.layer_id != layer_id {
            return;
        }
        capture.steps.push(RwkvScanCapturedStep {
            r: r.to_vec(),
            k: k.to_vec(),
            v: v.to_vec(),
            w: w.to_vec(),
            a: a.to_vec(),
            k_deformed: k_deformed.to_vec(),
        });
    });
}

#[cfg(any(not(target_os = "macos"), test))]
const MAX_CHANNEL_MIXER_DIM: usize = 256;
#[cfg(any(not(target_os = "macos"), test))]
const MAX_LORA_RANK: usize = 16;
#[cfg(any(target_os = "macos", test))]
const RETRIEVABILITY_GEMM_BATCH_SIZE: usize = 128;
const ID_PLACEHOLDER: i64 = 314_159_265_358_979_323;
const ID_SPLIT: u64 = 4;
const TORCH_ID_RNG_STATE_LEN: usize = 624;
const TORCH_ID_RNG_INITIAL_INDEX: usize = 4;
const TORCH_ID_RNG_INITIAL_STATE: [u32; TORCH_ID_RNG_STATE_LEN] = [
    1682285388, 1851481669, 966820414, 1804407443, 3932621889, 3075156175, 1736360318, 934395819,
    4040256764, 1428315994, 3735821200, 3330369387, 3669296551, 4124277305, 1875917743, 3028165727,
    2848364260, 455193708, 1084726271, 4219366876, 4052358395, 3170678824, 468221764, 161941628,
    2640816207, 267628614, 4070986878, 1225344559, 697494497, 1246641310, 1864086393, 2343787891,
    956379933, 132227689, 1804503562, 801719826, 2866035087, 3697914572, 3618475839, 3714856595,
    3045796150, 2653947806, 695225985, 3299305861, 2299901543, 282374836, 2677848525, 308337505,
    1649403860, 3241582120, 1641323885, 211358346, 1648589179, 3479631618, 11568893, 2867282711,
    4042451176, 611732436, 3506334995, 3562753239, 1234760878, 2196749548, 3668376444, 2428528228,
    631426791, 4017895429, 3026146201, 4191008964, 1932380089, 3380014424, 3547882387, 3952077360,
    386896944, 3997977197, 1529608344, 4132999748, 2534739147, 4182207012, 1114211877, 2611407325,
    442230326, 2804113464, 4095607840, 2203942301, 3208806807, 4217725735, 851085756, 3111179814,
    2893561443, 1737762208, 3364884061, 3768266261, 1951940754, 1219713113, 661792618, 2429297141,
    2854706310, 2555881125, 1720186105, 2231784162, 1816231541, 2459472524, 2303395945, 2802874606,
    164142194, 3722278385, 965390944, 1789460144, 2277056390, 2785283897, 557026319, 1822107998,
    2156805627, 1809760430, 751565145, 1320417968, 760565420, 3675458714, 266229863, 3071308558,
    2588875244, 520697952, 404211066, 3757472951, 1592302302, 1301622844, 283875151, 2264576214,
    1513690933, 3186056547, 503942925, 3452110716, 4068977508, 1386120901, 35467672, 3736425491,
    3479928582, 4057588406, 1156892529, 2666030005, 1947426597, 1513426209, 2298519457, 951648,
    2662765076, 1819076585, 991609795, 3445620136, 4263402744, 683674655, 125305024, 400363870,
    4175058246, 345274265, 1393354662, 4168368724, 1992205233, 333760424, 430638818, 2038831240,
    2393249574, 2277698148, 3834699795, 854369076, 1949138442, 879235983, 2687356990, 74489205,
    675756224, 1108887942, 3850167999, 3202550759, 346927610, 1219004248, 1933710611, 4232192948,
    127380890, 3413854308, 4259402585, 1947897797, 2281478841, 1250526173, 3584776535, 3962965149,
    2436415337, 3700599595, 2305091483, 441956968, 98602741, 1004357398, 2171103081, 3555585131,
    25494344, 474746862, 2168816300, 1445677778, 2762834911, 167135277, 1609410667, 342499195,
    2658948465, 96705736, 307223780, 2817802860, 1095652744, 1458155099, 2129035584, 3380124286,
    4277476542, 3149780470, 201762377, 3878283276, 2943856629, 1484419446, 536631262, 3610340050,
    2021510328, 405479479, 126321448, 112807861, 206360937, 2329863092, 2176182667, 761094335,
    3055987424, 3047673301, 1636351146, 730123282, 1603106892, 3241403281, 64478013, 2355203790,
    3571782552, 1977824032, 3103684787, 1440206087, 3529256035, 4029036380, 1727403531, 2250763223,
    2609572110, 301200603, 2499812360, 1079194345, 1873534699, 592458645, 3367330921, 2157823439,
    2377315769, 2124247689, 3501423240, 44290279, 2658883840, 3187367635, 2223108434, 621774141,
    2862251779, 58506969, 1447924885, 3417741267, 3295650570, 2061828420, 656716143, 722185440,
    2516683235, 2575345773, 2104327887, 3735805058, 1317382073, 297471524, 1841093725, 3008896502,
    106767431, 125566800, 2298510351, 1201930425, 907367885, 3447227635, 647146338, 926864998,
    673001384, 2537138065, 1291689589, 1065989187, 640479966, 2169443668, 1796563022, 3530758358,
    566177121, 1707919249, 1474913925, 3765619977, 3740101935, 3130400739, 2400651968, 1474401929,
    526695285, 943720479, 874038331, 330689749, 3219805999, 3116943707, 2803955215, 2165155639,
    4244261781, 2888630181, 252759016, 3783988093, 1530768853, 2944465398, 303528941, 3002511155,
    2323759006, 3019190001, 2122243812, 1121230145, 1730608278, 741152631, 1508537030, 3728973132,
    3150372859, 1696219691, 580153578, 2686308765, 1065154441, 582354200, 1629450702, 3804950383,
    1377430830, 3231855250, 716684766, 2347913152, 3723768936, 3995049093, 1640930380, 2518706901,
    3418463630, 541497465, 3219798303, 1706475941, 306647767, 1641899345, 1570832171, 1052204431,
    3913625992, 215023677, 1412164449, 110620318, 1060606607, 702131657, 1847887127, 209232170,
    3730056702, 1139625814, 1887242045, 3850643319, 3750397920, 2071715462, 3479548451, 1555884857,
    4190680751, 1832210452, 2962479459, 3822065681, 4014873921, 2886096746, 2233784512, 2238930674,
    3655246617, 927454496, 148905975, 1631004036, 96233864, 1481847180, 2120974892, 2577516900,
    1638964877, 1481137923, 1414769434, 3161093534, 112755530, 1764064957, 4190363343, 3887127551,
    1834924570, 83845051, 1194548396, 4079088278, 2045362678, 1769415013, 4005488543, 3853058044,
    3236458945, 2957047901, 1695491053, 3109347023, 1966942432, 1450132000, 2200494752, 608196473,
    3106709220, 4028691397, 1194244113, 3728413990, 3069222360, 3780538931, 2072954337, 3718173202,
    4049735703, 309723989, 3035905309, 1497948243, 1014496292, 3798948403, 2803094444, 342979147,
    1189029508, 1965636522, 3735884288, 851551988, 763723908, 4250649925, 1551115212, 2057541610,
    3495494782, 637069736, 2304408148, 815858117, 2236554473, 1483594123, 1278501755, 2394803515,
    1955783100, 1792990840, 2122870956, 530734808, 3030700278, 723763396, 2270626039, 2629780941,
    2984465025, 3688570684, 2776316291, 900046415, 2828563145, 1683929076, 3635512397, 2298110775,
    218175329, 1069560764, 2297081356, 2401672963, 1007365472, 421703457, 3609147592, 3680957553,
    2021586893, 3380432468, 3986237638, 1178822340, 3062404559, 1622982873, 3690602096, 2439524694,
    3587851041, 2408352887, 2166555969, 3451949389, 1782985112, 1721002099, 2722187334, 3644256530,
    3166709237, 666319864, 3937101570, 2322768459, 2640925986, 3091708170, 176155768, 2070897063,
    31962327, 1980355715, 1579825601, 2980116859, 1296346288, 3253021476, 1566345931, 1333641409,
    1466299323, 3535059214, 3755906045, 2436710697, 2624047850, 2773027064, 2735441585, 2397006833,
    2403155593, 2322419556, 3211067371, 2902277310, 855808633, 2088710794, 2365540733, 3598761409,
    96395577, 2793454657, 293134636, 997092999, 1516229869, 3830922510, 1456537668, 2543557055,
    2250567838, 47738566, 3310116596, 429974987, 2597497913, 3570401473, 3554123533, 3733732981,
    65117535, 89824483, 467063829, 2980094347, 459068879, 288615762, 505566088, 161491799,
    1950807394, 4134005883, 832113435, 383539149, 3289684623, 984364633, 3767074980, 349818067,
    3687375566, 2799114350, 945518296, 1410152104, 3876984338, 2571021970, 3352118476, 4178487553,
    1203153759, 611118555, 3259506420, 2891465405, 3445709652, 3949641380, 2875186838, 2779811328,
    2510853343, 4275646002, 2491038814, 2422179298, 1650323772, 3000898162, 3627492445, 1678944205,
    4261066493, 2595842431, 3948513109, 2750405154, 2564814475, 2584030305, 2613484438, 3805313548,
    3123055455, 1702940891, 1364814166, 3578222670, 1736412364, 2853289958, 182827375, 3335617072,
    3347856282, 771668471, 2881257662, 1618901303, 2911271774, 2283475553, 1640165049, 3561675959,
    384011161, 1071788681, 2189107920, 2737255457, 1011242690, 1067383324, 2768630076, 35989731,
    3477113433, 3502877655, 3126304174, 2125983443, 1065189709, 3085270280, 2313355736, 2406873069,
    1095216321, 1458394816, 1881192803, 2616673852, 425572748, 1377770342, 4183972521, 154875079,
    1436212463, 1213363850, 2416318273, 3309606622, 3757797616, 936646994, 3996477797, 836380753,
    1177367651, 2904456246, 600155443, 2732456183, 2305820104, 1912638872, 271951098, 119535942,
];
const DAY_OFFSET_ENCODE_PERIODS: [f32; 7] = [3.0, 7.0, 30.0, 100.0, 365.0, 3650.0, 36500.0];
const SECONDS_PER_DAY: i64 = 86_400;

const ELAPSED_DAYS_MEAN: f32 = 1.51;
const ELAPSED_DAYS_STD: f32 = 1.62;
const ELAPSED_DAYS_CUMULATIVE_MEAN: f32 = 2.14;
const ELAPSED_DAYS_CUMULATIVE_STD: f32 = 2.25;
const ELAPSED_SECONDS_MEAN: f32 = 9.96;
const ELAPSED_SECONDS_STD: f32 = 5.21;
const ELAPSED_SECONDS_CUMULATIVE_MEAN: f32 = 10.86;
const ELAPSED_SECONDS_CUMULATIVE_STD: f32 = 5.8;
const DURATION_MEAN: f32 = 8.9;
const DURATION_STD: f32 = 1.07;
const DIFF_NEW_CARDS_MEAN: f32 = 2.945;
const DIFF_NEW_CARDS_STD: f32 = 2.011;
const DIFF_REVIEWS_MEAN: f32 = 4.64;
const DIFF_REVIEWS_STD: f32 = 2.59;
const CUM_NEW_CARDS_TODAY_MEAN: f32 = 2.55;
const CUM_NEW_CARDS_TODAY_STD: f32 = 1.41;
const CUM_REVIEWS_TODAY_MEAN: f32 = 4.59;
const CUM_REVIEWS_TODAY_STD: f32 = 1.30;

#[derive(Debug, Clone)]
pub struct ReviewInput {
    pub card_id: i64,
    pub note_id: Option<i64>,
    pub deck_id: Option<i64>,
    pub preset_id: Option<i64>,
    pub is_query: bool,
    pub ease: Option<u8>,
    pub duration_millis: Option<i64>,
    pub card_type: Option<i64>,
    pub day_offset: Option<i64>,
    pub current_elapsed_days: Option<i64>,
    pub current_elapsed_seconds: Option<i64>,
    pub target_retentions: [Option<f32>; 4],
}

pub struct ReviewState<'a> {
    pub card: Option<&'a [u8]>,
    pub deck: Option<&'a [u8]>,
    pub note: Option<&'a [u8]>,
    pub preset: Option<&'a [u8]>,
    pub global: Option<&'a [u8]>,
}

pub struct ReviewOutput {
    pub retrievability: f32,
    pub button_probabilities: [f32; 4],
    pub current_interval: Option<u32>,
    pub current_s90: Option<u32>,
    pub intervals: [Option<u32>; 4],
    pub s90s: [Option<u32>; 4],
    pub card_state: Vec<u8>,
    pub deck_state: Vec<u8>,
    pub note_state: Vec<u8>,
    pub preset_state: Vec<u8>,
    pub global_state: Vec<u8>,
}

pub struct ReviewPredictionRequest {
    pub input: ReviewInput,
    pub state: ReviewStateOwned,
}

pub struct ReviewStateOwned {
    pub card: Option<Vec<u8>>,
    pub deck: Option<Vec<u8>>,
    pub note: Option<Vec<u8>>,
    pub preset: Option<Vec<u8>>,
    pub global: Option<Vec<u8>>,
}

pub struct ReviewPredictionOutput {
    pub retrievability: f32,
    pub button_probabilities: [f32; 4],
    pub current_interval: Option<u32>,
    pub current_s90: Option<u32>,
    pub intervals: [Option<u32>; 4],
    pub s90s: [Option<u32>; 4],
}

pub struct RwkvInference {
    model: Arc<SrsModel>,
    features: FeatureState,
    curves: HashMap<i64, ReviewCurve>,
    warm_up_states: ReviewStateMaps,
    target_retention: f32,
    max_interval_days: u32,
}

#[derive(Clone)]
pub struct RwkvInferenceState {
    feature_state: FeatureStateForCard,
    card_id: i64,
    curve: Option<ReviewCurve>,
}

pub struct RwkvWarmUpSnapshot {
    pub card_states: Vec<(i64, Vec<u8>)>,
    pub note_states: Vec<(i64, Vec<u8>)>,
    pub deck_states: Vec<(i64, Vec<u8>)>,
    pub preset_states: Vec<(i64, Vec<u8>)>,
    pub global_state: Option<Vec<u8>>,
}

pub struct RwkvWorkloadSimulationSnapshot {
    pub card_states: Vec<(i64, Vec<u8>)>,
    pub note_states: Vec<(i64, Vec<u8>)>,
    pub deck_states: Vec<(i64, Vec<u8>)>,
    pub preset_states: Vec<(i64, Vec<u8>)>,
    pub global_state: Option<Vec<u8>>,
    pub runtime_state: Option<Vec<u8>>,
}

#[derive(Clone)]
pub struct RwkvWorkloadSimulationInput {
    pub review_input: ReviewInput,
    pub interval_days: Option<i64>,
    pub reps: Option<i64>,
    pub lapses: Option<i64>,
}

pub struct RwkvWorkloadSimulationPoint {
    pub memorized: f32,
    pub weighted_memorized: f32,
    pub cost: f32,
    pub review_count: u32,
}

pub struct RwkvWorkloadSimulationOutput {
    pub reviewless_end_memorized: f32,
    pub reviewless_end_weighted_memorized: f32,
    pub points: Vec<(u32, RwkvWorkloadSimulationPoint)>,
}

struct RwkvWorkloadQueryPrediction {
    retrievability: f32,
    current_interval: Option<u32>,
    current_s90: Option<u32>,
}

pub struct RwkvWorkloadReviewModel {
    pub grade_seconds: [f32; 4],
    pub bucket_probabilities: Vec<(u32, [f32; 4])>,
}

pub struct RwkvWorkloadSimulationConfig {
    pub min_dr: u32,
    pub max_dr: u32,
    pub target_dr_step: u32,
    pub days_to_simulate: u32,
    pub review_limit: u32,
    pub state_update_interval: u32,
    pub review_model: RwkvWorkloadReviewModel,
}

struct ReviewPredictionWorkItem {
    features: Vec<f32>,
    state: SrsStateOwned,
}

struct ReviewPredictionBorrowedWorkItem<'a> {
    features: Vec<f32>,
    state: SrsStateRef<'a>,
}

#[derive(Clone, Copy)]
struct ReviewPredictionQueryRef<'a> {
    features: &'a [f32],
    state: SrsStateRef<'a>,
}

impl ReviewPredictionQueryRef<'_> {
    #[cfg(any(target_os = "macos", test))]
    fn layer_state(&self, module_id: usize, layer_id: usize) -> Option<&LayerState> {
        self.state
            .module(module_id)
            .and_then(|state| state.layers.get(layer_id))
    }
}

struct RwkvSimulationCard {
    review_input: RwkvWorkloadSimulationInput,
    due_day: i64,
    last_review_day: i64,
    reps: i64,
    lapses: i64,
}

struct RwkvWorkloadTargetConfig<'a> {
    dr: u32,
    days_to_simulate: i64,
    review_limit: usize,
    state_update_interval: u32,
    review_model: &'a RwkvWorkloadReviewModel,
}

struct RwkvWorkloadTargetSweep {
    inputs: Vec<RwkvWorkloadSimulationInput>,
    snapshot: RwkvWorkloadSimulationSnapshot,
    target_drs: Vec<u32>,
    days_to_simulate: i64,
    review_limit: usize,
    state_update_interval: u32,
    review_model: RwkvWorkloadReviewModel,
    base_features: FeatureState,
    base_curves: HashMap<i64, ReviewCurve>,
    total_steps: u32,
}

impl RwkvInference {
    pub fn load(path: PathBuf, target_retention: f32, max_interval_days: u32) -> io::Result<Self> {
        Ok(Self {
            model: Arc::new(SrsModel::load(&path)?),
            features: FeatureState::default(),
            curves: HashMap::new(),
            warm_up_states: ReviewStateMaps::default(),
            target_retention,
            max_interval_days,
        })
    }

    pub fn review(
        &mut self,
        input: ReviewInput,
        state: ReviewState<'_>,
    ) -> io::Result<ReviewOutput> {
        let heads = self.review_heads(&input, &state)?;

        if !input.is_query {
            self.features.store_review(&input);
            self.curves.insert(input.card_id, heads.curve.clone());
        }

        let answer_heads = if input.is_query {
            Some(self.answer_heads(&input, &state)?)
        } else {
            None
        };
        let (intervals, s90s) = answer_heads
            .as_ref()
            .map(|heads| self.answer_intervals(&input, heads))
            .unwrap_or(([None; 4], [None; 4]));
        let (current_interval, current_s90) = self.current_intervals(&input, &heads);

        let card_state = serialize_module_state(&heads.next_state.card);
        let deck_state = serialize_module_state(&heads.next_state.deck);
        let note_state = serialize_module_state(&heads.next_state.note);
        let preset_state = serialize_module_state(&heads.next_state.preset);
        let global_state = serialize_module_state(&heads.next_state.global);

        if !input.is_query {
            self.warm_up_states.store(&input, heads.next_state);
        }

        Ok(ReviewOutput {
            retrievability: heads.retrievability,
            button_probabilities: heads.button_probabilities,
            current_interval,
            current_s90,
            intervals,
            s90s,
            card_state,
            deck_state,
            note_state,
            preset_state,
            global_state,
        })
    }

    pub fn predict_many(
        &mut self,
        requests: Vec<ReviewPredictionRequest>,
    ) -> io::Result<Vec<ReviewPredictionOutput>> {
        let mut work_items = Vec::with_capacity(requests.len() * (1 + ANSWER_EASES.len()));
        for request in &requests {
            if !request.input.is_query {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "RWKV batched prediction only supports query inputs",
                ));
            }

            work_items.push(self.prediction_work_item(&request.input, &request.state)?);
            for ease in ANSWER_EASES {
                work_items.push(self.prediction_work_item(
                    &simulated_answer_input(&request.input, ease),
                    &request.state,
                )?);
            }
        }

        let heads = self.model.review_many(&work_items);
        let chunk_size = 1 + ANSWER_EASES.len();
        Ok(heads
            .chunks_exact(chunk_size)
            .zip(requests)
            .map(|(heads, request)| {
                let query_heads = &heads[0];
                let answer_heads = &heads[1..];
                let (intervals, s90s) = self.answer_intervals(&request.input, answer_heads);
                let (current_interval, current_s90) =
                    self.current_intervals(&request.input, query_heads);
                ReviewPredictionOutput {
                    retrievability: query_heads.retrievability,
                    button_probabilities: query_heads.button_probabilities,
                    current_interval,
                    current_s90,
                    intervals,
                    s90s,
                }
            })
            .collect())
    }

    pub fn predict_retrievability_many(
        &mut self,
        requests: Vec<ReviewPredictionRequest>,
    ) -> io::Result<Vec<f32>> {
        let mut work_items = Vec::with_capacity(requests.len());
        for request in &requests {
            if !request.input.is_query {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "RWKV batched retrievability prediction only supports query inputs",
                ));
            }

            work_items.push(self.prediction_work_item(&request.input, &request.state)?);
        }

        Ok(self.model.review_retrievability_many(&work_items))
    }

    pub fn predict_retrievability_many_from_warm_up(
        &mut self,
        inputs: Vec<ReviewInput>,
    ) -> io::Result<Vec<f32>> {
        for input in &inputs {
            if !input.is_query {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "RWKV batched retrievability prediction only supports query inputs",
                ));
            }
        }

        let features = inputs
            .iter()
            .map(|input| self.features.features_for(input))
            .collect::<Vec<_>>();
        let work_items = inputs
            .iter()
            .zip(features)
            .map(|(input, features)| ReviewPredictionBorrowedWorkItem {
                features,
                state: self.warm_up_states.state_ref(input),
            })
            .collect::<Vec<_>>();

        Ok(self.model.review_retrievability_many_borrowed(&work_items))
    }

    pub fn predict_retrievability_many_after_review(
        &self,
        answer: ReviewInput,
        query_inputs: Vec<ReviewInput>,
        snapshot: RwkvWorkloadSimulationSnapshot,
    ) -> io::Result<Vec<f32>> {
        if answer.is_query || answer.ease.is_none() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "RWKV future prediction requires an answered review input",
            ));
        }
        if query_inputs.iter().any(|input| !input.is_query) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "RWKV future prediction only supports query inputs",
            ));
        }

        let Some(runtime_state) = snapshot.runtime_state.as_deref() else {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "RWKV future prediction requires a runtime state snapshot",
            ));
        };
        let mut worker = self.worker_from_cache_state(runtime_state)?;
        let mut state_maps = ReviewStateMaps::from_serialized(
            &snapshot.card_states,
            &snapshot.note_states,
            &snapshot.deck_states,
            &snapshot.preset_states,
            snapshot.global_state.as_deref(),
        )?;
        worker.apply_simulation_answer(&answer, &mut state_maps)?;

        Ok(worker
            .workload_query_predictions_for_inputs(&query_inputs, &state_maps)
            .into_iter()
            .map(|prediction| prediction.retrievability)
            .collect())
    }

    pub fn predict_retrievability_many_after_reviews(
        &self,
        answers: Vec<ReviewInput>,
        query_inputs: Vec<ReviewInput>,
        snapshot: RwkvWorkloadSimulationSnapshot,
    ) -> io::Result<Vec<Vec<f32>>> {
        for answer in &answers {
            if answer.is_query || answer.ease.is_none() {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "RWKV future prediction requires answered review inputs",
                ));
            }
        }
        if query_inputs.iter().any(|input| !input.is_query) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "RWKV future prediction only supports query inputs",
            ));
        }

        let Some(runtime_state) = snapshot.runtime_state.as_deref() else {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "RWKV future prediction requires a runtime state snapshot",
            ));
        };
        let base_features = read_runtime_feature_state(runtime_state)?;
        let base_state_maps = ReviewStateMaps::from_serialized(
            &snapshot.card_states,
            &snapshot.note_states,
            &snapshot.deck_states,
            &snapshot.preset_states,
            snapshot.global_state.as_deref(),
        )?;

        if query_inputs.is_empty() {
            return Ok(answers.iter().map(|_| Vec::new()).collect());
        }

        if answers.first().is_some_and(|first| {
            answers
                .iter()
                .chain(&query_inputs)
                .all(|input| same_feature_identity(first, input))
        }) {
            let mut features = base_features;
            let answer_work_items = answers
                .iter()
                .map(|answer| ReviewPredictionWorkItem {
                    features: features.features_for(answer),
                    state: base_state_maps.state_owned(answer),
                })
                .collect::<Vec<_>>();
            let answer_heads = self.model.review_many(&answer_work_items);

            let query_count = query_inputs.len();
            let mut query_work_items = Vec::with_capacity(answers.len() * query_count);
            for (answer, heads) in answers.into_iter().zip(answer_heads) {
                let feature_state = features.state_for_card(answer.card_id);
                features.store_review(&answer);
                for query_input in &query_inputs {
                    query_work_items.push(ReviewPredictionWorkItem {
                        features: features.features_for(query_input),
                        state: base_state_maps.state_owned_after_review(
                            &answer,
                            query_input,
                            &heads.next_state,
                        ),
                    });
                }
                features.restore_state(&feature_state);
            }

            return Ok(self
                .model
                .review_many(&query_work_items)
                .chunks_exact(query_count)
                .map(|heads| heads.iter().map(|heads| heads.retrievability).collect())
                .collect());
        }

        let answer_work_items = answers
            .iter()
            .map(|answer| {
                let mut features = base_features.clone();
                ReviewPredictionWorkItem {
                    features: features.features_for(answer),
                    state: base_state_maps.state_owned(answer),
                }
            })
            .collect::<Vec<_>>();
        let answer_heads = self.model.review_many(&answer_work_items);

        let query_count = query_inputs.len();
        let mut query_work_items = Vec::with_capacity(answers.len() * query_count);
        for (answer, heads) in answers.into_iter().zip(answer_heads) {
            let mut features = base_features.clone();
            features.store_review(&answer);
            for query_input in &query_inputs {
                query_work_items.push(ReviewPredictionWorkItem {
                    features: features.features_for(query_input),
                    state: base_state_maps.state_owned_after_review(
                        &answer,
                        query_input,
                        &heads.next_state,
                    ),
                });
            }
        }

        Ok(self
            .model
            .review_many(&query_work_items)
            .chunks_exact(query_count)
            .map(|heads| {
                heads
                    .iter()
                    .map(|heads| heads.retrievability)
                    .collect::<Vec<_>>()
            })
            .collect())
    }

    pub fn warm_up_reviews(
        &mut self,
        reviews: Vec<ReviewInput>,
        record_predictions: bool,
    ) -> io::Result<Vec<(usize, f32)>> {
        // The scan-capture harness records per-timestep coefficients from the
        // answer pass only, so it needs the per-review path.
        #[cfg(test)]
        if rwkv_scan_capture_active() {
            return self.warm_up_reviews_sequential(reviews, record_predictions);
        }
        bulk::warm_up_reviews_bulk(self, reviews, record_predictions)
    }

    /// The per-review reference implementation of `warm_up_reviews`. The bulk
    /// path must match it bit-for-bit; parity tests compare the two.
    #[cfg_attr(not(test), allow(dead_code))]
    fn warm_up_reviews_sequential(
        &mut self,
        reviews: Vec<ReviewInput>,
        record_predictions: bool,
    ) -> io::Result<Vec<(usize, f32)>> {
        let mut predictions = vec![];
        for (index, input) in reviews.into_iter().enumerate() {
            if input.ease.is_none() {
                continue;
            }

            // Query features must be assigned before answer features so
            // first-seen ID encodings keep the benchmark-compatible order.
            let query_features = record_predictions.then(|| {
                let mut query_input = input.clone();
                query_input.is_query = true;
                query_input.ease = None;
                query_input.duration_millis = None;
                self.features.features_for(&query_input)
            });

            let features = self.features.features_for(&input);
            let model = &*self.model;
            // The query and answer inputs share card/note/deck/preset ids, so
            // both passes read the same pre-review state and can run in
            // parallel; only the answer pass advances state below.
            let state = self.warm_up_states.state_ref(&input);
            let (query_retrievability, heads) = match &query_features {
                Some(query_features) => {
                    let (retrievability, heads) = rayon::join(
                        || {
                            model.review_retrievability_query_refs(&[
                                ReviewPredictionQueryRef {
                                    features: query_features,
                                    state,
                                },
                            ])[0]
                        },
                        || model.review(&features, state),
                    );
                    (Some(retrievability), heads)
                }
                None => (None, model.review(&features, state)),
            };
            if let Some(retrievability) = query_retrievability {
                predictions.push((index, retrievability));
            }

            self.features.store_review(&input);
            self.curves.insert(input.card_id, heads.curve.clone());
            self.warm_up_states.store(&input, heads.next_state);
        }

        Ok(predictions)
    }

    #[cfg(test)]
    fn warm_up_reviews_with_state_compression(
        &mut self,
        reviews: &[ReviewInput],
        compression: Option<StateCompression>,
    ) -> Vec<f32> {
        let mut predictions = Vec::new();
        for input in reviews {
            if input.ease.is_none() {
                continue;
            }

            let mut query_input = input.clone();
            query_input.is_query = true;
            query_input.ease = None;
            query_input.duration_millis = None;
            let features = self.features.features_for(&query_input);
            let query_heads = self
                .model
                .review(&features, self.warm_up_states.state_ref(&query_input));
            predictions.push(query_heads.retrievability);

            let features = self.features.features_for(input);
            let heads = self
                .model
                .review(&features, self.warm_up_states.state_ref(input));
            self.features.store_review(input);
            self.curves.insert(input.card_id, heads.curve.clone());
            let next_state = match compression {
                Some(compression) => compressed_srs_state(heads.next_state, compression),
                None => heads.next_state,
            };
            self.warm_up_states.store(input, next_state);
        }

        predictions
    }

    pub fn warm_up_snapshot(&self) -> RwkvWarmUpSnapshot {
        RwkvWarmUpSnapshot {
            card_states: serialize_state_map(&self.warm_up_states.card),
            note_states: serialize_state_map(&self.warm_up_states.note),
            deck_states: serialize_state_map(&self.warm_up_states.deck),
            preset_states: serialize_state_map(&self.warm_up_states.preset),
            global_state: self
                .warm_up_states
                .global
                .as_ref()
                .map(serialize_module_state),
        }
    }

    pub fn restore_warm_up_snapshot(&mut self, snapshot: RwkvWarmUpSnapshot) -> io::Result<()> {
        self.warm_up_states = ReviewStateMaps::from_serialized(
            &snapshot.card_states,
            &snapshot.note_states,
            &snapshot.deck_states,
            &snapshot.preset_states,
            snapshot.global_state.as_deref(),
        )?;
        Ok(())
    }

    pub fn restore_warm_up_state(
        &mut self,
        card_id: i64,
        note_id: Option<i64>,
        deck_id: Option<i64>,
        preset_id: Option<i64>,
        state: ReviewStateOwned,
    ) -> io::Result<()> {
        self.warm_up_states
            .restore_serialized(card_id, note_id, deck_id, preset_id, &state)
    }

    pub fn reset_warm_up_state(&mut self) {
        self.warm_up_states = ReviewStateMaps::default();
    }

    fn review_heads(
        &mut self,
        input: &ReviewInput,
        state: &ReviewState<'_>,
    ) -> io::Result<ReviewHeads> {
        let card_state = deserialize_module_state(state.card)?;
        let deck_state = deserialize_module_state(state.deck)?;
        let note_state = deserialize_module_state(state.note)?;
        let preset_state = deserialize_module_state(state.preset)?;
        let global_state = deserialize_module_state(state.global)?;
        self.review_heads_for_state(
            input,
            SrsStateRef {
                card: card_state.as_ref(),
                deck: deck_state.as_ref(),
                note: note_state.as_ref(),
                preset: preset_state.as_ref(),
                global: global_state.as_ref(),
            },
        )
    }

    fn review_heads_for_state(
        &mut self,
        input: &ReviewInput,
        state: SrsStateRef<'_>,
    ) -> io::Result<ReviewHeads> {
        let features = self.features.features_for(input);
        Ok(self.model.review(&features, state))
    }

    fn prediction_work_item(
        &mut self,
        input: &ReviewInput,
        state: &ReviewStateOwned,
    ) -> io::Result<ReviewPredictionWorkItem> {
        Ok(ReviewPredictionWorkItem {
            state: state.deserialize()?,
            features: self.features.features_for(input),
        })
    }

    fn answer_heads(
        &mut self,
        input: &ReviewInput,
        state: &ReviewState<'_>,
    ) -> io::Result<[ReviewHeads; 4]> {
        let mut heads = Vec::with_capacity(ANSWER_EASES.len());
        for ease in ANSWER_EASES {
            heads.push(self.review_heads(&simulated_answer_input(input, ease), state)?);
        }

        match heads.try_into() {
            Ok(heads) => Ok(heads),
            Err(_) => unreachable!("answer head count should be fixed"),
        }
    }

    fn answer_intervals(
        &self,
        input: &ReviewInput,
        answer_heads: &[ReviewHeads],
    ) -> ([Option<u32>; 4], [Option<u32>; 4]) {
        let mut intervals = [None; 4];
        let mut s90s = [None; 4];

        for (index, heads) in answer_heads.iter().enumerate().take(4) {
            let target_retention = input.target_retentions[index].unwrap_or(self.target_retention);
            intervals[index] =
                interval_for_curve(&heads.curve, target_retention, self.max_interval_days);
            s90s[index] =
                interval_for_curve(&heads.curve, S90_TARGET_RETENTION, self.max_interval_days);
        }

        (intervals, s90s)
    }

    fn current_intervals(
        &self,
        input: &ReviewInput,
        heads: &ReviewHeads,
    ) -> (Option<u32>, Option<u32>) {
        let target_retention = input.target_retentions[2].unwrap_or(self.target_retention);
        (
            interval_for_curve(&heads.curve, target_retention, self.max_interval_days),
            interval_for_curve(&heads.curve, S90_TARGET_RETENTION, self.max_interval_days),
        )
    }

    pub fn state_for_card(&self, card_id: i64) -> RwkvInferenceState {
        RwkvInferenceState {
            feature_state: self.features.state_for_card(card_id),
            card_id,
            curve: self.curves.get(&card_id).cloned(),
        }
    }

    pub fn restore_state(&mut self, state: &RwkvInferenceState) {
        self.features.restore_state(&state.feature_state);
        if let Some(curve) = &state.curve {
            self.curves.insert(state.card_id, curve.clone());
        } else {
            self.curves.remove(&state.card_id);
        }
    }

    pub fn cache_state(&self) -> Vec<u8> {
        let mut out = Vec::new();
        out.extend_from_slice(b"ARWKVPROCSTATE2");
        self.features.write_cache_state(&mut out);
        write_u32(&mut out, self.curves.len() as u32);
        let mut curves: Vec<_> = self.curves.iter().collect();
        curves.sort_by_key(|(card_id, _)| *card_id);
        for (card_id, curve) in curves {
            write_i64(&mut out, *card_id);
            curve.write_cache_state(&mut out);
        }
        out
    }

    pub fn restore_cache_state(&mut self, bytes: &[u8]) -> io::Result<()> {
        let (features, curves) = read_runtime_cache_state(bytes)?;
        self.features = features;
        self.curves = curves;
        Ok(())
    }

    fn worker_from_cache_state(&self, bytes: &[u8]) -> io::Result<RwkvInference> {
        let (features, curves) = read_runtime_cache_state(bytes)?;
        Ok(self.workload_worker(features, curves))
    }

    pub fn simulate_workload(
        &mut self,
        inputs: Vec<RwkvWorkloadSimulationInput>,
        mut snapshot: RwkvWorkloadSimulationSnapshot,
        config: RwkvWorkloadSimulationConfig,
        progress: &mut dyn FnMut(u32, u32) -> io::Result<()>,
    ) -> io::Result<RwkvWorkloadSimulationOutput> {
        if config.min_dr > config.max_dr {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "RWKV workload min DR must be <= max DR",
            ));
        }
        let Some(runtime_state) = snapshot.runtime_state.take() else {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "RWKV workload simulation requires a runtime state snapshot",
            ));
        };

        let target_dr_step = config.target_dr_step.max(1);
        let target_drs = target_drs(config.min_dr, config.max_dr, target_dr_step);
        let total_steps = target_drs.len() as u32 + 1;
        progress(0, total_steps)?;
        let mut reviewless_worker = self.worker_from_cache_state(&runtime_state)?;
        let base_features = reviewless_worker.features.clone();
        let base_curves = reviewless_worker.curves.clone();
        let reviewless_state_maps = ReviewStateMaps::from_serialized(
            &snapshot.card_states,
            &snapshot.note_states,
            &snapshot.deck_states,
            &snapshot.preset_states,
            snapshot.global_state.as_deref(),
        )?;
        let (reviewless_end_memorized, reviewless_end_weighted_memorized) = reviewless_worker
            .simulation_memorized_for_inputs(
                &inputs,
                &reviewless_state_maps,
                S90_TARGET_RETENTION,
                config.days_to_simulate as i64,
            )?;
        progress(1, total_steps)?;

        let mut points = self.simulate_workload_targets(
            RwkvWorkloadTargetSweep {
                inputs,
                snapshot,
                target_drs,
                days_to_simulate: config.days_to_simulate as i64,
                review_limit: config.review_limit as usize,
                state_update_interval: config.state_update_interval.max(1),
                review_model: config.review_model,
                base_features,
                base_curves,
                total_steps,
            },
            progress,
        )?;
        enforce_monotonic_workload_review_counts(&mut points);

        Ok(RwkvWorkloadSimulationOutput {
            reviewless_end_memorized,
            reviewless_end_weighted_memorized,
            points,
        })
    }

    fn simulate_workload_targets(
        &self,
        sweep: RwkvWorkloadTargetSweep,
        progress: &mut dyn FnMut(u32, u32) -> io::Result<()>,
    ) -> io::Result<Vec<(u32, RwkvWorkloadSimulationPoint)>> {
        let RwkvWorkloadTargetSweep {
            inputs,
            snapshot,
            target_drs,
            days_to_simulate,
            review_limit,
            state_update_interval,
            review_model,
            base_features,
            base_curves,
            total_steps,
        } = sweep;

        let target_count = target_drs.len();
        let mut results = Vec::with_capacity(target_count);
        // The warmed runtime state can be large, so keep target DR workers
        // sequential instead of cloning the state for every target at once.
        for (offset, dr) in target_drs.into_iter().enumerate() {
            let mut worker = self.workload_worker(base_features.clone(), base_curves.clone());
            let mut state_maps = ReviewStateMaps::from_serialized(
                &snapshot.card_states,
                &snapshot.note_states,
                &snapshot.deck_states,
                &snapshot.preset_states,
                snapshot.global_state.as_deref(),
            )?;
            let point = worker.simulate_workload_for_target(
                &inputs,
                &mut state_maps,
                RwkvWorkloadTargetConfig {
                    dr,
                    days_to_simulate,
                    review_limit,
                    state_update_interval,
                    review_model: &review_model,
                },
            )?;
            results.push((offset, dr, point));
            progress(offset as u32 + 2, total_steps)?;
        }

        results.sort_by_key(|(offset, _, _)| *offset);
        Ok(results
            .into_iter()
            .map(|(_, dr, point)| (dr, point))
            .collect())
    }

    fn workload_worker(
        &self,
        features: FeatureState,
        curves: HashMap<i64, ReviewCurve>,
    ) -> RwkvInference {
        RwkvInference {
            model: Arc::clone(&self.model),
            features,
            curves,
            warm_up_states: ReviewStateMaps::default(),
            target_retention: self.target_retention,
            max_interval_days: self.max_interval_days,
        }
    }

    fn simulate_workload_for_target(
        &mut self,
        inputs: &[RwkvWorkloadSimulationInput],
        state_maps: &mut ReviewStateMaps,
        config: RwkvWorkloadTargetConfig<'_>,
    ) -> io::Result<RwkvWorkloadSimulationPoint> {
        let target_retention = config.dr as f32 / 100.0;
        let query_inputs: Vec<_> = inputs
            .iter()
            .map(|input| simulation_query_input(input, target_retention, 0))
            .collect();
        let predictions = self.workload_query_predictions_for_inputs(&query_inputs, state_maps);
        let mut cards = inputs
            .iter()
            .zip(predictions.iter())
            .map(|(input, prediction)| simulation_card(input, prediction, target_retention))
            .collect::<Vec<_>>();

        let mut total_cost = 0.0;
        let mut review_count: u32 = 0;
        for day in 0..config.days_to_simulate {
            let mut due_indexes: Vec<_> = cards
                .iter()
                .enumerate()
                .filter_map(|(index, card)| (card.due_day <= day).then_some(index))
                .collect();
            due_indexes.sort_by_key(|index| {
                let card = &cards[*index];
                (card.due_day, card.review_input.review_input.card_id)
            });
            if config.review_limit > 0 && due_indexes.len() > config.review_limit {
                due_indexes.truncate(config.review_limit);
            } else if config.review_limit == 0 {
                due_indexes.clear();
            }

            let mut offset = 0;
            while offset < due_indexes.len() {
                let reviews_until_state_update =
                    config.state_update_interval - (review_count % config.state_update_interval);
                let chunk_len =
                    (reviews_until_state_update as usize).min(due_indexes.len() - offset);
                let chunk_indexes = &due_indexes[offset..offset + chunk_len];
                let query_inputs = chunk_indexes
                    .iter()
                    .map(|index| {
                        simulation_query_input_for_card(&cards[*index], target_retention, day)
                    })
                    .collect::<Vec<_>>();
                let predictions =
                    self.workload_query_predictions_for_inputs(&query_inputs, state_maps);
                let eases = chunk_indexes
                    .iter()
                    .zip(&predictions)
                    .map(|(index, prediction)| {
                        let retrievability = valid_probability_or_default(
                            prediction.retrievability,
                            target_retention,
                        );
                        let probabilities = config.review_model.probabilities_for(retrievability);
                        simulation_grade(
                            probabilities,
                            cards[*index].review_input.review_input.card_id,
                            day,
                            cards[*index].reps,
                        )
                    })
                    .collect::<Vec<_>>();
                let intervals =
                    self.selected_answer_intervals_for_inputs(&query_inputs, state_maps, &eases);

                for ((position, index), ease) in chunk_indexes.iter().enumerate().zip(&eases) {
                    let grade_seconds = config.review_model.grade_seconds[(*ease - 1) as usize];
                    total_cost += grade_seconds;
                    review_count += 1;

                    let duration_millis = (grade_seconds * 1000.0).round().max(1.0) as i64;
                    let answer_input = simulation_answer_input(
                        &cards[*index],
                        target_retention,
                        day,
                        *ease,
                        duration_millis,
                    );
                    let mut interval = intervals[position].unwrap_or(1);
                    if review_count % config.state_update_interval == 0 {
                        self.apply_simulation_answer(&answer_input, state_maps)?;
                    }

                    if interval < 1 {
                        interval = 1;
                    }
                    cards[*index].last_review_day = day;
                    cards[*index].due_day = day + interval as i64;
                    cards[*index].reps += 1;
                    if *ease == 1 {
                        cards[*index].lapses += 1;
                    }
                }
                offset += chunk_len;
            }
        }

        let (memorized, weighted_memorized) = self.simulation_memorized_for_cards(
            &cards,
            state_maps,
            target_retention,
            config.days_to_simulate,
        )?;
        Ok(RwkvWorkloadSimulationPoint {
            memorized,
            weighted_memorized,
            cost: total_cost,
            review_count,
        })
    }

    fn workload_query_predictions_for_inputs(
        &mut self,
        inputs: &[ReviewInput],
        state_maps: &ReviewStateMaps,
    ) -> Vec<RwkvWorkloadQueryPrediction> {
        let work_items = inputs
            .iter()
            .map(|input| ReviewPredictionWorkItem {
                features: self.features.features_for(input),
                state: state_maps.state_owned(input),
            })
            .collect::<Vec<_>>();
        self.model
            .review_many(&work_items)
            .into_iter()
            .zip(inputs)
            .map(|(heads, input)| {
                let (current_interval, current_s90) = self.current_intervals(input, &heads);
                RwkvWorkloadQueryPrediction {
                    retrievability: heads.retrievability,
                    current_interval,
                    current_s90,
                }
            })
            .collect()
    }

    fn selected_answer_intervals_for_inputs(
        &mut self,
        inputs: &[ReviewInput],
        state_maps: &ReviewStateMaps,
        eases: &[u8],
    ) -> Vec<Option<u32>> {
        debug_assert_eq!(inputs.len(), eases.len());
        let answer_inputs = inputs
            .iter()
            .zip(eases)
            .map(|(input, ease)| simulated_answer_input(input, *ease))
            .collect::<Vec<_>>();
        let work_items = answer_inputs
            .iter()
            .map(|input| ReviewPredictionWorkItem {
                features: self.features.features_for(input),
                state: state_maps.state_owned(input),
            })
            .collect::<Vec<_>>();
        self.model
            .review_many(&work_items)
            .into_iter()
            .zip(inputs.iter().zip(eases))
            .map(|(heads, (input, ease))| {
                let target_retention =
                    input.target_retentions[(*ease - 1) as usize].unwrap_or(self.target_retention);
                interval_for_curve(&heads.curve, target_retention, self.max_interval_days)
            })
            .collect()
    }

    fn apply_simulation_answer(
        &mut self,
        input: &ReviewInput,
        state_maps: &mut ReviewStateMaps,
    ) -> io::Result<()> {
        let heads = self.review_heads_for_state(input, state_maps.state_ref(input))?;
        self.features.store_review(input);
        self.curves.insert(input.card_id, heads.curve.clone());
        state_maps.store(input, heads.next_state);
        Ok(())
    }

    fn simulation_memorized_for_inputs(
        &mut self,
        inputs: &[RwkvWorkloadSimulationInput],
        state_maps: &ReviewStateMaps,
        target_retention: f32,
        day: i64,
    ) -> io::Result<(f32, f32)> {
        let query_inputs = inputs
            .iter()
            .map(|input| simulation_query_input(input, target_retention, day))
            .collect::<Vec<_>>();
        let predictions = self.workload_query_predictions_for_inputs(&query_inputs, state_maps);
        Ok(memorized_from_workload_predictions(&predictions))
    }

    fn simulation_memorized_for_cards(
        &mut self,
        cards: &[RwkvSimulationCard],
        state_maps: &ReviewStateMaps,
        target_retention: f32,
        day: i64,
    ) -> io::Result<(f32, f32)> {
        let query_inputs = cards
            .iter()
            .map(|card| simulation_query_input_for_card(card, target_retention, day))
            .collect::<Vec<_>>();
        let predictions = self.workload_query_predictions_for_inputs(&query_inputs, state_maps);
        Ok(memorized_from_workload_predictions(&predictions))
    }
}

fn same_feature_identity(left: &ReviewInput, right: &ReviewInput) -> bool {
    left.card_id == right.card_id
        && left.note_id == right.note_id
        && left.deck_id == right.deck_id
        && left.preset_id == right.preset_id
}

fn read_runtime_feature_state(bytes: &[u8]) -> io::Result<FeatureState> {
    let mut cursor = Cursor::new(bytes);
    cursor.expect_magic(b"ARWKVPROCSTATE2")?;
    let features = FeatureState::read_cache_state(&mut cursor)?;
    let curve_count = cursor.u32()? as usize;
    for _ in 0..curve_count {
        cursor.i64()?;
        cursor.skip_f32_vec()?;
        cursor.skip_f32_vec()?;
    }
    cursor.expect_end()?;
    Ok(features)
}

fn read_runtime_cache_state(bytes: &[u8]) -> io::Result<(FeatureState, HashMap<i64, ReviewCurve>)> {
    let mut cursor = Cursor::new(bytes);
    cursor.expect_magic(b"ARWKVPROCSTATE2")?;
    let features = FeatureState::read_cache_state(&mut cursor)?;
    let curve_count = cursor.u32()? as usize;
    let mut curves = HashMap::with_capacity(curve_count);
    for _ in 0..curve_count {
        let card_id = cursor.i64()?;
        let curve = ReviewCurve::read_cache_state(&mut cursor)?;
        curves.insert(card_id, curve);
    }
    cursor.expect_end()?;
    Ok((features, curves))
}

fn simulated_answer_input(input: &ReviewInput, ease: u8) -> ReviewInput {
    let mut input = input.clone();
    input.is_query = false;
    input.ease = Some(ease);
    input
}

fn target_drs(min_dr: u32, max_dr: u32, step: u32) -> Vec<u32> {
    let mut values = Vec::new();
    let mut dr = min_dr;
    while dr <= max_dr {
        values.push(dr);
        match dr.checked_add(step) {
            Some(next) => dr = next,
            None => break,
        }
    }
    if values.last().copied() != Some(max_dr) {
        values.push(max_dr);
    }
    values
}

fn simulation_card(
    input: &RwkvWorkloadSimulationInput,
    prediction: &RwkvWorkloadQueryPrediction,
    target_retention: f32,
) -> RwkvSimulationCard {
    let elapsed_days = input_elapsed_days(&input.review_input);
    let mut due_day = 0;
    if prediction.retrievability.is_finite()
        && prediction.retrievability > target_retention
        && prediction.current_interval.is_some()
    {
        due_day = (prediction.current_interval.unwrap() as i64 - elapsed_days).max(1);
    }
    RwkvSimulationCard {
        review_input: input.clone(),
        due_day,
        last_review_day: -elapsed_days,
        reps: input.reps.unwrap_or(0),
        lapses: input.lapses.unwrap_or(0),
    }
}

fn simulation_query_input(
    input: &RwkvWorkloadSimulationInput,
    target_retention: f32,
    day: i64,
) -> ReviewInput {
    let elapsed_days = input_elapsed_days(&input.review_input) + day;
    simulation_input(
        &input.review_input,
        target_retention,
        day,
        elapsed_days,
        true,
        None,
        None,
    )
}

fn simulation_query_input_for_card(
    card: &RwkvSimulationCard,
    target_retention: f32,
    day: i64,
) -> ReviewInput {
    let elapsed_days = (day - card.last_review_day).max(0);
    simulation_input(
        &card.review_input.review_input,
        target_retention,
        day,
        elapsed_days,
        true,
        None,
        None,
    )
}

fn simulation_answer_input(
    card: &RwkvSimulationCard,
    target_retention: f32,
    day: i64,
    ease: u8,
    duration_millis: i64,
) -> ReviewInput {
    let elapsed_days = (day - card.last_review_day).max(0);
    simulation_input(
        &card.review_input.review_input,
        target_retention,
        day,
        elapsed_days,
        false,
        Some(ease),
        Some(duration_millis),
    )
}

fn simulation_input(
    input: &ReviewInput,
    target_retention: f32,
    day: i64,
    elapsed_days: i64,
    is_query: bool,
    ease: Option<u8>,
    duration_millis: Option<i64>,
) -> ReviewInput {
    let mut input = input.clone();
    input.is_query = is_query;
    input.ease = ease;
    input.duration_millis = duration_millis;
    input.day_offset = input.day_offset.map(|day_offset| day_offset + day);
    input.current_elapsed_days = Some(elapsed_days);
    input.current_elapsed_seconds = None;
    input.target_retentions = [
        Some(target_retention),
        Some(target_retention),
        Some(target_retention),
        Some(target_retention),
    ];
    input
}

fn input_elapsed_days(input: &ReviewInput) -> i64 {
    input.current_elapsed_days.unwrap_or(0).max(0)
}

fn memorized_from_workload_predictions(predictions: &[RwkvWorkloadQueryPrediction]) -> (f32, f32) {
    let mut memorized = 0.0;
    let mut weighted = 0.0;
    for prediction in predictions {
        if !valid_probability(prediction.retrievability) {
            continue;
        }
        memorized += prediction.retrievability;
        weighted += prediction.retrievability * s90_weight(prediction.current_s90);
    }
    (memorized, weighted)
}

fn s90_weight(current_s90: Option<u32>) -> f32 {
    let Some(current_s90) = current_s90 else {
        return 1.0;
    };
    if current_s90 == 0 {
        return 1.0;
    }
    1.0 - ((-8.0 / 365.0) * current_s90 as f32).exp()
}

fn valid_probability(value: f32) -> bool {
    value.is_finite() && (0.0..=1.0).contains(&value)
}

fn valid_probability_or_default(value: f32, default: f32) -> f32 {
    if valid_probability(value) {
        value
    } else {
        default
    }
}

impl RwkvWorkloadReviewModel {
    fn probabilities_for(&self, retrievability: f32) -> [f32; 4] {
        let bucket = simulator_bucket(retrievability);
        self.bucket_probabilities
            .iter()
            .find_map(|(candidate, probabilities)| (*candidate == bucket).then_some(*probabilities))
            .unwrap_or_else(|| fallback_grade_probabilities(retrievability))
    }
}

fn fallback_grade_probabilities(retrievability: f32) -> [f32; 4] {
    let r = if retrievability.is_finite() {
        retrievability.clamp(0.0, 1.0)
    } else {
        0.9
    };
    let again = (1.0 - r).clamp(0.02, 0.85);
    let success = 1.0 - again;
    let mut hard_share = ((0.95 - r) / 0.45).clamp(0.10, 0.45);
    let mut easy_share = ((r - 0.75) / 0.25).clamp(0.05, 0.35);
    if hard_share + easy_share > 0.90 {
        let scale = 0.90 / (hard_share + easy_share);
        hard_share *= scale;
        easy_share *= scale;
    }
    let hard = success * hard_share;
    let easy = success * easy_share;
    let good = (success - hard - easy).max(0.0);
    let total = again + hard + good + easy;
    [again / total, hard / total, good / total, easy / total]
}

fn simulation_grade(probabilities: [f32; 4], card_id: i64, day: i64, reps: i64) -> u8 {
    let threshold = simulation_unit_hash(card_id, day, reps);
    let mut cumulative = 0.0;
    for (index, probability) in probabilities.iter().enumerate() {
        cumulative += *probability as f64;
        if threshold <= cumulative {
            return (index + 1) as u8;
        }
    }
    4
}

fn enforce_monotonic_workload_review_counts(points: &mut [(u32, RwkvWorkloadSimulationPoint)]) {
    let mut indexes: Vec<_> = (0..points.len()).collect();
    indexes.sort_by_key(|index| points[*index].0);

    let mut running = 0;
    for index in indexes {
        running = running.max(points[index].1.review_count);
        points[index].1.review_count = running;
    }
}

fn simulation_unit_hash(card_id: i64, day: i64, reps: i64) -> f64 {
    let mut value =
        (card_id as u64) ^ ((day as u64).wrapping_add(1)).wrapping_mul(0x9E3779B185EBCA87);
    value ^= ((reps as u64).wrapping_add(1)).wrapping_mul(0x165667B19E3779F9);
    value ^= value >> 33;
    value = value.wrapping_mul(0xFF51AFD7ED558CCD);
    value ^= value >> 33;
    value = value.wrapping_mul(0xC4CEB9FE1A85EC53);
    value ^= value >> 33;
    value as f64 / u64::MAX as f64
}

fn simulator_bucket(retrievability: f32) -> u32 {
    const BUCKET_COUNT: u32 = 20;
    if !retrievability.is_finite() {
        return BUCKET_COUNT - 1;
    }
    ((retrievability * BUCKET_COUNT as f32) as u32).clamp(0, BUCKET_COUNT - 1)
}

impl ReviewStateOwned {
    fn deserialize(&self) -> io::Result<SrsStateOwned> {
        Ok(SrsStateOwned {
            card: deserialize_module_state(self.card.as_deref())?,
            deck: deserialize_module_state(self.deck.as_deref())?,
            note: deserialize_module_state(self.note.as_deref())?,
            preset: deserialize_module_state(self.preset.as_deref())?,
            global: deserialize_module_state(self.global.as_deref())?,
        })
    }
}

#[derive(Clone)]
struct FeatureState {
    first_day_offset: Option<i64>,
    previous_day_offset: Option<i64>,
    card_set: HashMap<i64, ()>,
    last_new_cards: HashMap<i64, i64>,
    last_i: HashMap<i64, i64>,
    today: i64,
    today_reviews: i64,
    today_new_cards: i64,
    card_first_day_offset: HashMap<i64, i64>,
    card_elapsed_days_cumulative: HashMap<i64, i64>,
    card_elapsed_seconds_cumulative: HashMap<i64, i64>,
    id_encodings: HashMap<(IdKind, i64), Vec<f32>>,
    id_rng: TorchIdRng,
    review_index: i64,
}

impl Default for FeatureState {
    fn default() -> Self {
        Self {
            first_day_offset: None,
            previous_day_offset: None,
            card_set: HashMap::new(),
            last_new_cards: HashMap::new(),
            last_i: HashMap::new(),
            today: -1,
            today_reviews: 0,
            today_new_cards: 0,
            card_first_day_offset: HashMap::new(),
            card_elapsed_days_cumulative: HashMap::new(),
            card_elapsed_seconds_cumulative: HashMap::new(),
            id_encodings: HashMap::new(),
            id_rng: TorchIdRng::default(),
            review_index: 0,
        }
    }
}

#[derive(Clone)]
struct FeatureStateForCard {
    card_id: i64,
    first_day_offset: Option<i64>,
    previous_day_offset: Option<i64>,
    card_was_seen: bool,
    last_new_cards: Option<i64>,
    last_i: Option<i64>,
    today: i64,
    today_reviews: i64,
    today_new_cards: i64,
    card_first_day_offset: Option<i64>,
    card_elapsed_days_cumulative: Option<i64>,
    card_elapsed_seconds_cumulative: Option<i64>,
    review_index: i64,
}

impl FeatureState {
    fn state_for_card(&self, card_id: i64) -> FeatureStateForCard {
        FeatureStateForCard {
            card_id,
            first_day_offset: self.first_day_offset,
            previous_day_offset: self.previous_day_offset,
            card_was_seen: self.card_set.contains_key(&card_id),
            last_new_cards: self.last_new_cards.get(&card_id).copied(),
            last_i: self.last_i.get(&card_id).copied(),
            today: self.today,
            today_reviews: self.today_reviews,
            today_new_cards: self.today_new_cards,
            card_first_day_offset: self.card_first_day_offset.get(&card_id).copied(),
            card_elapsed_days_cumulative: self.card_elapsed_days_cumulative.get(&card_id).copied(),
            card_elapsed_seconds_cumulative: self
                .card_elapsed_seconds_cumulative
                .get(&card_id)
                .copied(),
            review_index: self.review_index,
        }
    }

    fn restore_state(&mut self, state: &FeatureStateForCard) {
        self.first_day_offset = state.first_day_offset;
        self.previous_day_offset = state.previous_day_offset;
        self.today = state.today;
        self.today_reviews = state.today_reviews;
        self.today_new_cards = state.today_new_cards;
        self.review_index = state.review_index;

        if state.card_was_seen {
            self.card_set.insert(state.card_id, ());
        } else {
            self.card_set.remove(&state.card_id);
        }
        restore_map_entry(
            &mut self.last_new_cards,
            state.card_id,
            state.last_new_cards,
        );
        restore_map_entry(&mut self.last_i, state.card_id, state.last_i);
        restore_map_entry(
            &mut self.card_first_day_offset,
            state.card_id,
            state.card_first_day_offset,
        );
        restore_map_entry(
            &mut self.card_elapsed_days_cumulative,
            state.card_id,
            state.card_elapsed_days_cumulative,
        );
        restore_map_entry(
            &mut self.card_elapsed_seconds_cumulative,
            state.card_id,
            state.card_elapsed_seconds_cumulative,
        );
    }

    fn features_for(&mut self, input: &ReviewInput) -> Vec<f32> {
        #[cfg(test)]
        let profile_started = rwkv_warmup_profile_start();

        let elapsed_seconds = elapsed_seconds(input);
        let elapsed_days = elapsed_days(input, elapsed_seconds);
        let elapsed_days_cumulative = self
            .card_elapsed_days_cumulative
            .get(&input.card_id)
            .copied()
            .unwrap_or(0)
            + elapsed_days;
        let elapsed_seconds_cumulative = self
            .card_elapsed_seconds_cumulative
            .get(&input.card_id)
            .copied()
            .unwrap_or(0)
            + elapsed_seconds;

        let raw_day_offset = input.day_offset.unwrap_or(0);
        let day_offset = self
            .first_day_offset
            .map_or(0, |first_day_offset| raw_day_offset - first_day_offset);
        let day_offset_first = self
            .card_first_day_offset
            .get(&input.card_id)
            .copied()
            .unwrap_or(day_offset);
        let previous_day_offset = self.previous_day_offset.unwrap_or(0);
        let diff_new_cards = self
            .last_new_cards
            .get(&input.card_id)
            .map_or(0, |last_new_cards| {
                self.card_set.len() as i64 - last_new_cards
            });
        let diff_reviews = self
            .last_i
            .get(&input.card_id)
            .map_or(0, |last_i| (self.review_index - last_i - 1).max(0));

        let mut today_new_cards = self.today_new_cards;
        let mut today_reviews = self.today_reviews;
        if day_offset != self.today {
            today_new_cards = 0;
            today_reviews = -1;
        }
        today_reviews += 1;
        if !self.card_set.contains_key(&input.card_id) {
            today_new_cards += 1;
        }

        let mut features = Vec::with_capacity(CARD_FEATURES);
        features.extend([
            scale_elapsed_days(elapsed_days),
            scale_elapsed_days_cumulative(elapsed_days_cumulative),
            scale_elapsed_seconds(elapsed_seconds),
            cyclic_sin(elapsed_seconds, SECONDS_PER_DAY),
            cyclic_cos(elapsed_seconds, SECONDS_PER_DAY),
            scale_elapsed_seconds_cumulative(elapsed_seconds_cumulative),
            cyclic_sin(elapsed_seconds_cumulative, SECONDS_PER_DAY),
            cyclic_cos(elapsed_seconds_cumulative, SECONDS_PER_DAY),
            scaled_duration(input),
        ]);

        let rating = input.ease.unwrap_or(0);
        for ease in 1..=4 {
            features.push(if !input.is_query && rating == ease {
                1.0
            } else {
                0.0
            });
        }

        let note_id = input.note_id.unwrap_or(ID_PLACEHOLDER + input.card_id);
        let deck_id = input.deck_id.unwrap_or(ID_PLACEHOLDER);
        let preset_id = input.preset_id.unwrap_or(ID_PLACEHOLDER);
        features.extend([
            if input.note_id.is_none() { 1.0 } else { 0.0 },
            if input.deck_id.is_none() { 1.0 } else { 0.0 },
            if input.preset_id.is_none() { 1.0 } else { 0.0 },
            scale_day_offset_diff(day_offset - previous_day_offset),
            ((day_offset.rem_euclid(7) as f32) - 3.0) / 3.0,
            scale_diff_new_cards(diff_new_cards),
            scale_diff_reviews(diff_reviews),
            scale_cum_new_cards_today(today_new_cards),
            scale_cum_reviews_today(today_reviews),
            if input.is_query {
                0.0
            } else {
                scale_state(input.card_type.unwrap_or(0))
            },
            if input.is_query { 1.0 } else { 0.0 },
        ]);

        self.append_id_encoding(&mut features, IdKind::Card, input.card_id);
        self.append_id_encoding(&mut features, IdKind::Note, note_id);
        self.append_id_encoding(&mut features, IdKind::Deck, deck_id);
        self.append_id_encoding(&mut features, IdKind::Preset, preset_id);
        append_day_offset_encoding(&mut features, day_offset, day_offset_first);
        debug_assert_eq!(features.len(), CARD_FEATURES);
        #[cfg(test)]
        rwkv_warmup_profile_record(RwkvWarmupProfileBucket::FeaturesFor, profile_started);
        features
    }

    fn store_review(&mut self, input: &ReviewInput) {
        #[cfg(test)]
        let profile_started = rwkv_warmup_profile_start();

        let elapsed_seconds = elapsed_seconds(input);
        let elapsed_days = elapsed_days(input, elapsed_seconds);
        *self
            .card_elapsed_days_cumulative
            .entry(input.card_id)
            .or_default() += elapsed_days;
        *self
            .card_elapsed_seconds_cumulative
            .entry(input.card_id)
            .or_default() += elapsed_seconds;

        let raw_day_offset = input.day_offset.unwrap_or(0);
        let day_offset = self
            .first_day_offset
            .map_or(0, |first_day_offset| raw_day_offset - first_day_offset);
        if self.first_day_offset.is_none() {
            self.first_day_offset = Some(raw_day_offset);
        }

        if day_offset != self.today {
            self.today = day_offset;
            self.today_new_cards = 0;
            self.today_reviews = -1;
        }
        self.today_reviews += 1;
        if !self.card_set.contains_key(&input.card_id) {
            self.today_new_cards += 1;
            self.card_set.insert(input.card_id, ());
            self.card_first_day_offset.insert(input.card_id, day_offset);
        }

        self.previous_day_offset = Some(day_offset);
        self.last_i.insert(input.card_id, self.review_index);
        self.last_new_cards
            .insert(input.card_id, self.card_set.len() as i64);
        self.review_index += 1;

        #[cfg(test)]
        rwkv_warmup_profile_record(RwkvWarmupProfileBucket::FeaturesStore, profile_started);
    }

    fn append_id_encoding(&mut self, features: &mut Vec<f32>, kind: IdKind, value: i64) {
        let encoding = self.id_encodings.entry((kind, value)).or_insert_with(|| {
            let dim = id_encoding_dim(kind);
            self.id_rng.encoding(dim)
        });
        features.extend(encoding.iter().copied());
    }

    fn write_cache_state(&self, out: &mut Vec<u8>) {
        write_option_i64(out, self.first_day_offset);
        write_option_i64(out, self.previous_day_offset);
        write_i64_set(out, self.card_set.keys().copied());
        write_i64_map(out, &self.last_new_cards);
        write_i64_map(out, &self.last_i);
        write_i64(out, self.today);
        write_i64(out, self.today_reviews);
        write_i64(out, self.today_new_cards);
        write_i64_map(out, &self.card_first_day_offset);
        write_i64_map(out, &self.card_elapsed_days_cumulative);
        write_i64_map(out, &self.card_elapsed_seconds_cumulative);
        write_i64(out, self.review_index);
        write_id_encodings(out, &self.id_encodings);
        self.id_rng.write_cache_state(out);
    }

    fn read_cache_state(cursor: &mut Cursor<'_>) -> io::Result<Self> {
        Ok(Self {
            first_day_offset: cursor.option_i64()?,
            previous_day_offset: cursor.option_i64()?,
            card_set: read_i64_set(cursor)?,
            last_new_cards: read_i64_map(cursor)?,
            last_i: read_i64_map(cursor)?,
            today: cursor.i64()?,
            today_reviews: cursor.i64()?,
            today_new_cards: cursor.i64()?,
            card_first_day_offset: read_i64_map(cursor)?,
            card_elapsed_days_cumulative: read_i64_map(cursor)?,
            card_elapsed_seconds_cumulative: read_i64_map(cursor)?,
            review_index: cursor.i64()?,
            id_encodings: read_id_encodings(cursor)?,
            id_rng: TorchIdRng::read_cache_state(cursor)?,
        })
    }
}

fn restore_map_entry(map: &mut HashMap<i64, i64>, key: i64, value: Option<i64>) {
    if let Some(value) = value {
        map.insert(key, value);
    } else {
        map.remove(&key);
    }
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
enum IdKind {
    Card,
    Note,
    Deck,
    Preset,
}

fn id_encoding_dim(kind: IdKind) -> usize {
    match kind {
        IdKind::Card | IdKind::Note => 12,
        IdKind::Deck | IdKind::Preset => 8,
    }
}

fn write_id_encodings(out: &mut Vec<u8>, values: &HashMap<(IdKind, i64), Vec<f32>>) {
    let mut values: Vec<_> = values.iter().collect();
    values.sort_by_key(|((kind, value), _)| (kind.cache_code(), *value));
    write_u32(out, values.len() as u32);
    for ((kind, value), encoding) in values {
        out.push(kind.cache_code());
        write_i64(out, *value);
        write_f32_slice(out, encoding);
    }
}

fn read_id_encodings(cursor: &mut Cursor<'_>) -> io::Result<HashMap<(IdKind, i64), Vec<f32>>> {
    let count = cursor.u32()? as usize;
    let mut values = HashMap::with_capacity(count);
    for _ in 0..count {
        let kind = IdKind::from_cache_code(cursor.u8()?)?;
        let value = cursor.i64()?;
        let encoding = cursor.f32_vec()?;
        values.insert((kind, value), encoding);
    }
    Ok(values)
}

impl IdKind {
    fn cache_code(self) -> u8 {
        match self {
            IdKind::Card => 0,
            IdKind::Note => 1,
            IdKind::Deck => 2,
            IdKind::Preset => 3,
        }
    }

    fn from_cache_code(code: u8) -> io::Result<Self> {
        match code {
            0 => Ok(IdKind::Card),
            1 => Ok(IdKind::Note),
            2 => Ok(IdKind::Deck),
            3 => Ok(IdKind::Preset),
            _ => Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "invalid RWKV id kind",
            )),
        }
    }
}

#[derive(Clone)]
struct TorchIdRng {
    state: [u32; TORCH_ID_RNG_STATE_LEN],
    index: usize,
}

impl Default for TorchIdRng {
    fn default() -> Self {
        Self {
            state: TORCH_ID_RNG_INITIAL_STATE,
            index: TORCH_ID_RNG_INITIAL_INDEX,
        }
    }
}

impl TorchIdRng {
    fn encoding(&mut self, dim: usize) -> Vec<f32> {
        (0..dim)
            .map(|_| (self.next_u32() as u64 % ID_SPLIT) as f32 - ((ID_SPLIT - 1) as f32 / 2.0))
            .collect()
    }

    fn next_u32(&mut self) -> u32 {
        if self.index >= TORCH_ID_RNG_STATE_LEN {
            self.twist();
        }
        let mut value = self.state[self.index];
        self.index += 1;
        value ^= value >> 11;
        value ^= (value << 7) & 0x9d2c_5680;
        value ^= (value << 15) & 0xefc6_0000;
        value ^ (value >> 18)
    }

    fn twist(&mut self) {
        const UPPER_MASK: u32 = 0x8000_0000;
        const LOWER_MASK: u32 = 0x7fff_ffff;
        const MATRIX_A: u32 = 0x9908_b0df;

        for index in 0..TORCH_ID_RNG_STATE_LEN {
            let next = (index + 1) % TORCH_ID_RNG_STATE_LEN;
            let twist = (self.state[index] & UPPER_MASK) | (self.state[next] & LOWER_MASK);
            let mut value = self.state[(index + 397) % TORCH_ID_RNG_STATE_LEN] ^ (twist >> 1);
            if twist & 1 != 0 {
                value ^= MATRIX_A;
            }
            self.state[index] = value;
        }
        self.index = 0;
    }

    fn write_cache_state(&self, out: &mut Vec<u8>) {
        write_u32(out, self.index as u32);
        for value in self.state {
            write_u32(out, value);
        }
    }

    fn read_cache_state(cursor: &mut Cursor<'_>) -> io::Result<Self> {
        let index = cursor.u32()? as usize;
        if index > TORCH_ID_RNG_STATE_LEN {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "invalid RWKV id RNG index",
            ));
        }
        let mut state = [0; TORCH_ID_RNG_STATE_LEN];
        for value in &mut state {
            *value = cursor.u32()?;
        }
        Ok(Self { state, index })
    }
}

fn append_day_offset_encoding(features: &mut Vec<f32>, day_offset: i64, first_day_offset: i64) {
    for period in DAY_OFFSET_ENCODE_PERIODS {
        let phase = 2.0 * std::f32::consts::PI / period;
        let day = day_offset.rem_euclid(period as i64) as f32;
        let first_day = first_day_offset.rem_euclid(period as i64) as f32;
        features.push((phase * day).sin());
        features.push((phase * day).cos());
        features.push((phase * first_day).sin());
        features.push((phase * first_day).cos());
    }
}

fn elapsed_seconds(input: &ReviewInput) -> i64 {
    input
        .current_elapsed_seconds
        .or_else(|| {
            input
                .current_elapsed_days
                .map(|days| days * SECONDS_PER_DAY)
        })
        .unwrap_or(-1)
}

fn elapsed_days(input: &ReviewInput, elapsed_seconds: i64) -> i64 {
    input.current_elapsed_days.unwrap_or({
        if elapsed_seconds >= 0 {
            elapsed_seconds / SECONDS_PER_DAY
        } else {
            -1
        }
    })
}

fn scaled_duration(input: &ReviewInput) -> f32 {
    input
        .duration_millis
        .map_or(0.0, |millis| scale_duration(millis as f32))
}

fn scale_elapsed_days(x: i64) -> f32 {
    (log_elapsed(x) - ELAPSED_DAYS_MEAN) / ELAPSED_DAYS_STD
}

fn scale_elapsed_days_cumulative(x: i64) -> f32 {
    (log_elapsed(x) - ELAPSED_DAYS_CUMULATIVE_MEAN) / ELAPSED_DAYS_CUMULATIVE_STD
}

fn scale_elapsed_seconds(x: i64) -> f32 {
    (log_elapsed(x) - ELAPSED_SECONDS_MEAN) / ELAPSED_SECONDS_STD
}

fn scale_elapsed_seconds_cumulative(x: i64) -> f32 {
    (log_elapsed(x) - ELAPSED_SECONDS_CUMULATIVE_MEAN) / ELAPSED_SECONDS_CUMULATIVE_STD
}

fn scale_duration(x: f32) -> f32 {
    ((10.0 + x).ln() - DURATION_MEAN) / DURATION_STD
}

fn scale_diff_new_cards(x: i64) -> f32 {
    ((3.0 + x as f32).ln() - DIFF_NEW_CARDS_MEAN) / DIFF_NEW_CARDS_STD
}

fn scale_diff_reviews(x: i64) -> f32 {
    ((3.0 + x as f32).ln() - DIFF_REVIEWS_MEAN) / DIFF_REVIEWS_STD
}

fn scale_cum_new_cards_today(x: i64) -> f32 {
    ((3.0 + x as f32).ln() - CUM_NEW_CARDS_TODAY_MEAN) / CUM_NEW_CARDS_TODAY_STD
}

fn scale_cum_reviews_today(x: i64) -> f32 {
    ((3.0 + x as f32).ln() - CUM_REVIEWS_TODAY_MEAN) / CUM_REVIEWS_TODAY_STD
}

fn scale_state(x: i64) -> f32 {
    x as f32 - 2.0
}

fn scale_day_offset_diff(x: i64) -> f32 {
    (std::f32::consts::E + x as f32).ln().ln()
}

fn log_elapsed(x: i64) -> f32 {
    if x == -1 {
        0.0
    } else {
        (1.0 + 1e-5 + x as f32).ln()
    }
}

fn cyclic_sin(value: i64, period: i64) -> f32 {
    ((value.rem_euclid(period) as f32) * 2.0 * std::f32::consts::PI / period as f32).sin()
}

fn cyclic_cos(value: i64, period: i64) -> f32 {
    ((value.rem_euclid(period) as f32) * 2.0 * std::f32::consts::PI / period as f32).cos()
}

struct WeightMap {
    tensors: HashMap<String, Tensor>,
}

struct Tensor {
    shape: Vec<usize>,
    values: Vec<f32>,
}

impl WeightMap {
    fn load(path: &PathBuf) -> io::Result<Self> {
        let data = fs::read(path)?;
        let mut cursor = Cursor::new(&data);
        cursor.expect_magic(b"ARWKVWEIGHTS1")?;
        let count = cursor.u32()? as usize;
        let mut tensors = HashMap::with_capacity(count);

        for _ in 0..count {
            let name_len = cursor.u16()? as usize;
            let name = cursor.string(name_len)?;
            let rank = cursor.u8()? as usize;
            let mut shape = Vec::with_capacity(rank);
            let mut len = 1_usize;
            for _ in 0..rank {
                let dim = cursor.u32()? as usize;
                len = len.checked_mul(dim).ok_or_else(|| {
                    io::Error::new(io::ErrorKind::InvalidData, "tensor is too large")
                })?;
                shape.push(dim);
            }
            let mut values = Vec::with_capacity(len);
            for _ in 0..len {
                values.push(cursor.f32()?);
            }
            tensors.insert(name, Tensor { shape, values });
        }

        cursor.expect_end()?;
        Ok(Self { tensors })
    }

    fn values(&self, name: &str) -> io::Result<Vec<f32>> {
        self.tensors
            .get(name)
            .map(|tensor| tensor.values.clone())
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, missing_weight(name)))
    }

    fn linear(&self, name: &str, input: usize, output: usize, bias: bool) -> io::Result<Linear> {
        let weight_name = format!("{name}.weight");
        let bias_name = format!("{name}.bias");
        let weight = &self.tensor(&weight_name, &[output, input])?.values;
        let mut weight_by_input = vec![0.0; weight.len()];
        for row in 0..output {
            for column in 0..input {
                weight_by_input[column * output + row] = weight[row * input + column];
            }
        }
        let bias = if bias {
            Some(self.tensor(&bias_name, &[output])?.values.clone())
        } else {
            None
        };
        Ok(Linear {
            input,
            output,
            weight_by_input,
            bias,
        })
    }

    fn layer_norm(&self, name: &str, dim: usize, eps: f32) -> io::Result<Norm> {
        Ok(Norm {
            groups: 1,
            dim,
            eps,
            weight: self
                .tensor(&format!("{name}.weight"), &[dim])?
                .values
                .clone(),
            bias: self.tensor(&format!("{name}.bias"), &[dim])?.values.clone(),
        })
    }

    fn group_norm(&self, name: &str, groups: usize, dim: usize, eps: f32) -> io::Result<Norm> {
        Ok(Norm {
            groups,
            dim,
            eps,
            weight: self
                .tensor(&format!("{name}.weight"), &[dim])?
                .values
                .clone(),
            bias: self.tensor(&format!("{name}.bias"), &[dim])?.values.clone(),
        })
    }

    fn tensor(&self, name: &str, shape: &[usize]) -> io::Result<&Tensor> {
        let tensor = self
            .tensors
            .get(name)
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, missing_weight(name)))?;
        if tensor.shape != shape {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                format!(
                    "weight {name} has shape {:?}, expected {shape:?}",
                    tensor.shape
                ),
            ));
        }
        Ok(tensor)
    }
}

fn missing_weight(name: &str) -> String {
    format!("missing weight: {name}")
}

struct Cursor<'a> {
    data: &'a [u8],
    offset: usize,
}

impl<'a> Cursor<'a> {
    fn new(data: &'a [u8]) -> Self {
        Self { data, offset: 0 }
    }

    fn expect_magic(&mut self, magic: &[u8]) -> io::Result<()> {
        let found = self.bytes(magic.len())?;
        if found != magic {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "unexpected file magic",
            ));
        }
        Ok(())
    }

    fn expect_end(&self) -> io::Result<()> {
        if self.offset == self.data.len() {
            Ok(())
        } else {
            Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "trailing bytes in file",
            ))
        }
    }

    fn bytes(&mut self, len: usize) -> io::Result<&'a [u8]> {
        let end = self
            .offset
            .checked_add(len)
            .ok_or_else(|| io::Error::new(io::ErrorKind::UnexpectedEof, "offset overflow"))?;
        let bytes = self
            .data
            .get(self.offset..end)
            .ok_or_else(|| io::Error::new(io::ErrorKind::UnexpectedEof, "file ended early"))?;
        self.offset = end;
        Ok(bytes)
    }

    fn string(&mut self, len: usize) -> io::Result<String> {
        let bytes = self.bytes(len)?;
        String::from_utf8(bytes.to_vec())
            .map_err(|_| io::Error::new(io::ErrorKind::InvalidData, "invalid utf-8 string"))
    }

    fn u8(&mut self) -> io::Result<u8> {
        Ok(self.bytes(1)?[0])
    }

    fn u16(&mut self) -> io::Result<u16> {
        let mut bytes = [0; 2];
        bytes.copy_from_slice(self.bytes(2)?);
        Ok(u16::from_le_bytes(bytes))
    }

    fn u32(&mut self) -> io::Result<u32> {
        let mut bytes = [0; 4];
        bytes.copy_from_slice(self.bytes(4)?);
        Ok(u32::from_le_bytes(bytes))
    }

    fn i64(&mut self) -> io::Result<i64> {
        let mut bytes = [0; 8];
        bytes.copy_from_slice(self.bytes(8)?);
        Ok(i64::from_le_bytes(bytes))
    }

    fn option_i64(&mut self) -> io::Result<Option<i64>> {
        match self.u8()? {
            0 => Ok(None),
            1 => Ok(Some(self.i64()?)),
            _ => Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "invalid optional i64 tag",
            )),
        }
    }

    fn f32(&mut self) -> io::Result<f32> {
        let mut bytes = [0; 4];
        bytes.copy_from_slice(self.bytes(4)?);
        Ok(f32::from_le_bytes(bytes))
    }

    fn f32_vec(&mut self) -> io::Result<Vec<f32>> {
        let len = self.u32()? as usize;
        let mut values = Vec::with_capacity(len);
        for _ in 0..len {
            values.push(self.f32()?);
        }
        Ok(values)
    }

    fn skip_f32_vec(&mut self) -> io::Result<()> {
        let byte_len = (self.u32()? as usize)
            .checked_mul(std::mem::size_of::<f32>())
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "vector too large"))?;
        self.bytes(byte_len)?;
        Ok(())
    }
}

struct SrsModel {
    features_0: Linear,
    features_norm: Norm,
    features_3: Linear,
    modules: Vec<RwkvModule>,
    prehead_norm: Norm,
    head_w_0: Linear,
    head_w_norm: Norm,
    head_w_4: Linear,
    w_linear: Linear,
    head_ahead_0: Linear,
    ahead_linear: Linear,
    head_p_0: Linear,
    p_linear: Linear,
}

#[cfg(any(not(target_os = "macos"), test))]
struct ReviewRetrievabilityScratch {
    feature_hidden: [f32; HEAD_DIM],
    normalized_features: [f32; HEAD_DIM],
    module: ModuleQueryScratch,
    prehead: [f32; D_MODEL],
    head: [f32; HEAD_DIM],
    logits: [f32; 4],
    probabilities: [f32; 4],
}

#[cfg(any(target_os = "macos", test))]
#[derive(Default)]
struct ReviewRetrievabilityBatchScratch {
    feature_input: Vec<f32>,
    feature_hidden: Vec<f32>,
    normalized_features: Vec<f32>,
    x: Vec<f32>,
    module: ModuleQueryBatchScratch,
    prehead: Vec<f32>,
    head: Vec<f32>,
    logits: Vec<f32>,
}

#[cfg(any(not(target_os = "macos"), test))]
impl Default for ReviewRetrievabilityScratch {
    fn default() -> Self {
        Self {
            feature_hidden: [0.0; HEAD_DIM],
            normalized_features: [0.0; HEAD_DIM],
            module: ModuleQueryScratch::default(),
            prehead: [0.0; D_MODEL],
            head: [0.0; HEAD_DIM],
            logits: [0.0; 4],
            probabilities: [0.0; 4],
        }
    }
}

impl SrsModel {
    fn load(path: &PathBuf) -> io::Result<Self> {
        let weights = WeightMap::load(path)?;
        let modules = MODULE_LAYERS
            .iter()
            .enumerate()
            .map(|(module_id, layer_count)| RwkvModule::load(&weights, module_id, *layer_count))
            .collect::<io::Result<Vec<_>>>()?;

        Ok(Self {
            features_0: weights.linear("features2card.0", CARD_FEATURES, HEAD_DIM, true)?,
            features_norm: weights.layer_norm("features2card.2", HEAD_DIM, 1e-5)?,
            features_3: weights.linear("features2card.3", HEAD_DIM, D_MODEL, true)?,
            modules,
            prehead_norm: weights.layer_norm("prehead_norm", D_MODEL, 1e-5)?,
            head_w_0: weights.linear("head_w.0", D_MODEL, D_MODEL, true)?,
            head_w_norm: weights.layer_norm("head_w.2", D_MODEL, 1e-5)?,
            head_w_4: weights.linear("head_w.4", D_MODEL, HEAD_DIM, true)?,
            w_linear: weights.linear("w_linear", HEAD_DIM, NUM_CURVES, true)?,
            head_ahead_0: weights.linear("head_ahead_logits.0", D_MODEL, HEAD_DIM, true)?,
            ahead_linear: weights.linear("ahead_linear", HEAD_DIM, NUM_CURVES, true)?,
            head_p_0: weights.linear("head_p.0", D_MODEL, HEAD_DIM, true)?,
            p_linear: weights.linear("p_linear", HEAD_DIM, 4, true)?,
        })
    }

    fn review(&self, features: &[f32], state: SrsStateRef<'_>) -> ReviewHeads {
        self.review_features(features, state)
    }

    fn review_many(&self, items: &[ReviewPredictionWorkItem]) -> Vec<ReviewHeads> {
        items
            .par_iter()
            .map(|item| self.review_features(&item.features, item.state.as_ref()))
            .collect()
    }

    fn review_retrievability_many(&self, items: &[ReviewPredictionWorkItem]) -> Vec<f32> {
        let items = items
            .iter()
            .map(|item| ReviewPredictionQueryRef {
                features: &item.features,
                state: item.state.as_ref(),
            })
            .collect::<Vec<_>>();
        self.review_retrievability_query_refs(&items)
    }

    fn review_retrievability_many_borrowed(
        &self,
        items: &[ReviewPredictionBorrowedWorkItem<'_>],
    ) -> Vec<f32> {
        let items = items
            .iter()
            .map(|item| ReviewPredictionQueryRef {
                features: &item.features,
                state: item.state,
            })
            .collect::<Vec<_>>();
        self.review_retrievability_query_refs(&items)
    }

    fn review_retrievability_query_refs(&self, items: &[ReviewPredictionQueryRef<'_>]) -> Vec<f32> {
        #[cfg(target_os = "macos")]
        {
            self.review_retrievability_query_refs_batched(items)
        }

        #[cfg(not(target_os = "macos"))]
        {
            self.review_retrievability_query_refs_scalar(items)
        }
    }

    #[cfg(any(not(target_os = "macos"), test))]
    fn review_retrievability_query_refs_scalar(
        &self,
        items: &[ReviewPredictionQueryRef<'_>],
    ) -> Vec<f32> {
        items
            .par_iter()
            .map_init(ReviewRetrievabilityScratch::default, |scratch, item| {
                self.review_retrievability_features(item.features, item.state, scratch)
            })
            .collect()
    }

    #[cfg(any(target_os = "macos", test))]
    fn review_retrievability_query_refs_batched(
        &self,
        items: &[ReviewPredictionQueryRef<'_>],
    ) -> Vec<f32> {
        let mut retrievabilities = vec![0.0; items.len()];
        retrievabilities
            .par_chunks_mut(RETRIEVABILITY_GEMM_BATCH_SIZE)
            .zip(items.par_chunks(RETRIEVABILITY_GEMM_BATCH_SIZE))
            .for_each_init(
                ReviewRetrievabilityBatchScratch::default,
                |scratch, (retrievabilities, items)| {
                    self.review_retrievability_query_batch(items, scratch, retrievabilities);
                },
            );
        retrievabilities
    }

    #[cfg(any(target_os = "macos", test))]
    fn review_retrievability_query_batch(
        &self,
        items: &[ReviewPredictionQueryRef<'_>],
        scratch: &mut ReviewRetrievabilityBatchScratch,
        retrievabilities: &mut [f32],
    ) {
        let rows = items.len();
        scratch.feature_input.clear();
        scratch.feature_input.reserve(rows * CARD_FEATURES);
        for item in items {
            scratch.feature_input.extend_from_slice(item.features);
        }

        self.features_0
            .apply_batch(&scratch.feature_input, rows, &mut scratch.feature_hidden);
        silu_in_place(&mut scratch.feature_hidden);
        self.features_norm.apply_batch(
            &scratch.feature_hidden,
            rows,
            &mut scratch.normalized_features,
        );
        self.features_3
            .apply_batch(&scratch.normalized_features, rows, &mut scratch.x);
        silu_in_place(&mut scratch.x);

        for (module_id, module) in self.modules.iter().enumerate() {
            module.run_query_batch(&mut scratch.x, items, module_id, &mut scratch.module);
        }

        self.prehead_norm
            .apply_batch(&scratch.x, rows, &mut scratch.prehead);
        self.head_p_0
            .apply_batch(&scratch.prehead, rows, &mut scratch.head);
        relu_in_place(&mut scratch.head);
        self.p_linear
            .apply_batch(&scratch.head, rows, &mut scratch.logits);
        for (retrievability, logits) in retrievabilities
            .iter_mut()
            .zip(scratch.logits.chunks_exact(4))
        {
            let mut probabilities = [0.0; 4];
            softmax_into(logits, &mut probabilities);
            *retrievability = 1.0 - probabilities[0];
        }
    }

    /// The feature MLP shared by the per-review and bulk replay paths.
    fn feature_mlp(&self, features: &[f32]) -> Vec<f32> {
        let mut x = self.features_0.apply(features);
        silu_in_place(&mut x);
        x = self.features_norm.apply(&x);
        x = self.features_3.apply(&x);
        silu_in_place(&mut x);
        x
    }

    /// The recall-curve heads shared by the per-review and bulk replay paths.
    /// `prehead_x` must already have `prehead_norm` applied.
    fn curve_head(&self, prehead_x: &[f32]) -> ReviewCurve {
        let mut head_w = self.head_w_0.apply(prehead_x);
        relu_in_place(&mut head_w);
        head_w = self.head_w_norm.apply(&head_w);
        head_w = self.head_w_4.apply(&head_w);
        let weights = softmax(&self.w_linear.apply(&head_w));

        let mut ahead = self.head_ahead_0.apply(prehead_x);
        relu_in_place(&mut ahead);
        let ahead_logits = self.ahead_linear.apply(&ahead);

        ReviewCurve {
            ahead_logits,
            weights,
        }
    }

    /// The retrievability head shared by the per-review and bulk replay
    /// paths. `prehead_x` must already have `prehead_norm` applied.
    fn button_probabilities_head(&self, prehead_x: &[f32]) -> [f32; 4] {
        let mut head_p = self.head_p_0.apply(prehead_x);
        relu_in_place(&mut head_p);
        let logits = self.p_linear.apply(&head_p);
        let probabilities = softmax(&logits);
        let mut out = [0.0; 4];
        out.copy_from_slice(&probabilities);
        out
    }

    fn retrievability_head(&self, prehead_x: &[f32]) -> f32 {
        1.0 - self.button_probabilities_head(prehead_x)[0]
    }

    fn review_features(&self, features: &[f32], state: SrsStateRef<'_>) -> ReviewHeads {
        #[cfg(test)]
        let review_profile_started = rwkv_warmup_profile_start();
        #[cfg(test)]
        let feature_mlp_profile_started = rwkv_warmup_profile_start();

        let x = self.feature_mlp(features);

        #[cfg(test)]
        rwkv_warmup_profile_record(
            RwkvWarmupProfileBucket::FeatureMlp,
            feature_mlp_profile_started,
        );

        #[cfg(test)]
        let module_profile_started = rwkv_warmup_profile_start();
        let (x, card_state) = self.modules[0].run(&x, state.card);
        #[cfg(test)]
        rwkv_warmup_profile_record(RwkvWarmupProfileBucket::ModuleCard, module_profile_started);
        #[cfg(test)]
        let module_profile_started = rwkv_warmup_profile_start();
        let (x, deck_state) = self.modules[1].run(&x, state.deck);
        #[cfg(test)]
        rwkv_warmup_profile_record(RwkvWarmupProfileBucket::ModuleDeck, module_profile_started);
        #[cfg(test)]
        let module_profile_started = rwkv_warmup_profile_start();
        let (x, note_state) = self.modules[2].run(&x, state.note);
        #[cfg(test)]
        rwkv_warmup_profile_record(RwkvWarmupProfileBucket::ModuleNote, module_profile_started);
        #[cfg(test)]
        let module_profile_started = rwkv_warmup_profile_start();
        let (x, preset_state) = self.modules[3].run(&x, state.preset);
        #[cfg(test)]
        rwkv_warmup_profile_record(
            RwkvWarmupProfileBucket::ModulePreset,
            module_profile_started,
        );
        #[cfg(test)]
        let module_profile_started = rwkv_warmup_profile_start();
        let (x, global_state) = self.modules[4].run(&x, state.global);
        #[cfg(test)]
        rwkv_warmup_profile_record(
            RwkvWarmupProfileBucket::ModuleGlobal,
            module_profile_started,
        );

        #[cfg(test)]
        let heads_profile_started = rwkv_warmup_profile_start();
        let x = self.prehead_norm.apply(&x);
        let curve = self.curve_head(&x);
        let button_probabilities = self.button_probabilities_head(&x);
        let retrievability = 1.0 - button_probabilities[0];

        let next_state = SrsState {
            card: card_state,
            deck: deck_state,
            note: note_state,
            preset: preset_state,
            global: global_state,
        };

        let heads = ReviewHeads {
            retrievability,
            button_probabilities,
            curve,
            next_state,
        };
        #[cfg(test)]
        rwkv_warmup_profile_record(RwkvWarmupProfileBucket::Heads, heads_profile_started);
        #[cfg(test)]
        rwkv_warmup_profile_record(RwkvWarmupProfileBucket::ModelReview, review_profile_started);
        heads
    }

    #[cfg(any(not(target_os = "macos"), test))]
    fn review_retrievability_features(
        &self,
        features: &[f32],
        state: SrsStateRef<'_>,
        scratch: &mut ReviewRetrievabilityScratch,
    ) -> f32 {
        self.features_0
            .apply_into(features, &mut scratch.feature_hidden);
        silu_in_place(&mut scratch.feature_hidden);
        self.features_norm
            .apply_into(&scratch.feature_hidden, &mut scratch.normalized_features);
        let mut x = [0.0; D_MODEL];
        self.features_3
            .apply_into(&scratch.normalized_features, &mut x);
        silu_in_place(&mut x);

        let states = [
            state.card,
            state.deck,
            state.note,
            state.preset,
            state.global,
        ];
        for (module, state) in self.modules.iter().zip(states) {
            x = module.run_query(&x, state, &mut scratch.module);
        }

        self.prehead_norm.apply_into(&x, &mut scratch.prehead);
        self.head_p_0
            .apply_into(&scratch.prehead, &mut scratch.head);
        relu_in_place(&mut scratch.head);
        self.p_linear.apply_into(&scratch.head, &mut scratch.logits);
        softmax_into(&scratch.logits, &mut scratch.probabilities);
        1.0 - scratch.probabilities[0]
    }
}

struct ReviewHeads {
    retrievability: f32,
    button_probabilities: [f32; 4],
    curve: ReviewCurve,
    next_state: SrsState,
}

#[derive(Clone)]
struct ReviewCurve {
    ahead_logits: Vec<f32>,
    weights: Vec<f32>,
}

impl ReviewCurve {
    fn write_cache_state(&self, out: &mut Vec<u8>) {
        write_f32_slice(out, &self.ahead_logits);
        write_f32_slice(out, &self.weights);
    }

    fn read_cache_state(cursor: &mut Cursor<'_>) -> io::Result<Self> {
        Ok(Self {
            ahead_logits: cursor.f32_vec()?,
            weights: cursor.f32_vec()?,
        })
    }
}

#[derive(Clone, Copy)]
struct SrsStateRef<'a> {
    card: Option<&'a ModuleState>,
    deck: Option<&'a ModuleState>,
    note: Option<&'a ModuleState>,
    preset: Option<&'a ModuleState>,
    global: Option<&'a ModuleState>,
}

impl<'a> SrsStateRef<'a> {
    #[cfg(any(target_os = "macos", test))]
    fn module(self, module_id: usize) -> Option<&'a ModuleState> {
        match module_id {
            0 => self.card,
            1 => self.deck,
            2 => self.note,
            3 => self.preset,
            4 => self.global,
            _ => None,
        }
    }
}

struct SrsStateOwned {
    card: Option<ModuleState>,
    deck: Option<ModuleState>,
    note: Option<ModuleState>,
    preset: Option<ModuleState>,
    global: Option<ModuleState>,
}

impl SrsStateOwned {
    fn as_ref(&self) -> SrsStateRef<'_> {
        SrsStateRef {
            card: self.card.as_ref(),
            deck: self.deck.as_ref(),
            note: self.note.as_ref(),
            preset: self.preset.as_ref(),
            global: self.global.as_ref(),
        }
    }
}

struct SrsState {
    card: ModuleState,
    deck: ModuleState,
    note: ModuleState,
    preset: ModuleState,
    global: ModuleState,
}

#[derive(Default)]
struct ReviewStateMaps {
    card: HashMap<i64, ModuleState>,
    note: HashMap<i64, ModuleState>,
    deck: HashMap<i64, ModuleState>,
    preset: HashMap<i64, ModuleState>,
    global: Option<ModuleState>,
}

impl ReviewStateMaps {
    fn from_serialized(
        card_states: &[(i64, Vec<u8>)],
        note_states: &[(i64, Vec<u8>)],
        deck_states: &[(i64, Vec<u8>)],
        preset_states: &[(i64, Vec<u8>)],
        global_state: Option<&[u8]>,
    ) -> io::Result<Self> {
        Ok(Self {
            card: deserialize_state_map(card_states)?,
            note: deserialize_state_map(note_states)?,
            deck: deserialize_state_map(deck_states)?,
            preset: deserialize_state_map(preset_states)?,
            global: deserialize_module_state(global_state)?,
        })
    }

    fn state_ref(&self, input: &ReviewInput) -> SrsStateRef<'_> {
        SrsStateRef {
            card: self.card.get(&input.card_id),
            note: input.note_id.and_then(|id| self.note.get(&id)),
            deck: input.deck_id.and_then(|id| self.deck.get(&id)),
            preset: input.preset_id.and_then(|id| self.preset.get(&id)),
            global: self.global.as_ref(),
        }
    }

    fn state_owned(&self, input: &ReviewInput) -> SrsStateOwned {
        SrsStateOwned {
            card: self.card.get(&input.card_id).cloned(),
            note: input.note_id.and_then(|id| self.note.get(&id).cloned()),
            deck: input.deck_id.and_then(|id| self.deck.get(&id).cloned()),
            preset: input.preset_id.and_then(|id| self.preset.get(&id).cloned()),
            global: self.global.clone(),
        }
    }

    fn state_owned_after_review(
        &self,
        answer: &ReviewInput,
        query: &ReviewInput,
        next_state: &SrsState,
    ) -> SrsStateOwned {
        SrsStateOwned {
            card: if query.card_id == answer.card_id {
                Some(next_state.card.clone())
            } else {
                self.card.get(&query.card_id).cloned()
            },
            note: branched_optional_module_state(
                &self.note,
                query.note_id,
                answer.note_id,
                &next_state.note,
            ),
            deck: branched_optional_module_state(
                &self.deck,
                query.deck_id,
                answer.deck_id,
                &next_state.deck,
            ),
            preset: branched_optional_module_state(
                &self.preset,
                query.preset_id,
                answer.preset_id,
                &next_state.preset,
            ),
            global: Some(next_state.global.clone()),
        }
    }

    fn store(&mut self, input: &ReviewInput, state: SrsState) {
        self.card.insert(input.card_id, state.card);
        if let Some(note_id) = input.note_id {
            self.note.insert(note_id, state.note);
        }
        if let Some(deck_id) = input.deck_id {
            self.deck.insert(deck_id, state.deck);
        }
        if let Some(preset_id) = input.preset_id {
            self.preset.insert(preset_id, state.preset);
        }
        self.global = Some(state.global);
    }

    fn restore_serialized(
        &mut self,
        card_id: i64,
        note_id: Option<i64>,
        deck_id: Option<i64>,
        preset_id: Option<i64>,
        state: &ReviewStateOwned,
    ) -> io::Result<()> {
        restore_serialized_state(&mut self.card, card_id, state.card.as_deref())?;
        restore_optional_serialized_state(&mut self.note, note_id, state.note.as_deref())?;
        restore_optional_serialized_state(&mut self.deck, deck_id, state.deck.as_deref())?;
        restore_optional_serialized_state(&mut self.preset, preset_id, state.preset.as_deref())?;
        self.global = deserialize_module_state(state.global.as_deref())?;
        Ok(())
    }
}

fn restore_serialized_state(
    states: &mut HashMap<i64, ModuleState>,
    id: i64,
    state: Option<&[u8]>,
) -> io::Result<()> {
    if let Some(state) = deserialize_module_state(state)? {
        states.insert(id, state);
    } else {
        states.remove(&id);
    }
    Ok(())
}

fn restore_optional_serialized_state(
    states: &mut HashMap<i64, ModuleState>,
    id: Option<i64>,
    state: Option<&[u8]>,
) -> io::Result<()> {
    if let Some(id) = id {
        restore_serialized_state(states, id, state)?;
    }
    Ok(())
}

fn branched_optional_module_state(
    map: &HashMap<i64, ModuleState>,
    query_id: Option<i64>,
    answer_id: Option<i64>,
    next_state: &ModuleState,
) -> Option<ModuleState> {
    match query_id {
        Some(id) if Some(id) == answer_id => Some(next_state.clone()),
        Some(id) => map.get(&id).cloned(),
        None => None,
    }
}

fn deserialize_state_map(states: &[(i64, Vec<u8>)]) -> io::Result<HashMap<i64, ModuleState>> {
    let mut map = HashMap::with_capacity(states.len());
    for (key, state) in states {
        let Some(state) = deserialize_module_state(Some(state.as_slice()))? else {
            continue;
        };
        map.insert(*key, state);
    }
    Ok(map)
}

fn serialize_state_map(states: &HashMap<i64, ModuleState>) -> Vec<(i64, Vec<u8>)> {
    let mut states: Vec<_> = states
        .iter()
        .map(|(key, state)| (*key, serialize_module_state(state)))
        .collect();
    states.sort_by_key(|(key, _)| *key);
    states
}

fn serialize_module_state(state: &ModuleState) -> Vec<u8> {
    let mut out = Vec::new();
    out.extend_from_slice(b"ARWKVMODSTATE1");
    write_u32(&mut out, state.layers.len() as u32);
    for layer in &state.layers {
        write_f32_slice(
            &mut out,
            layer
                .time
                .as_ref()
                .map_or(&[][..], |time| time.x_shift.as_slice()),
        );
        write_f32_slice(
            &mut out,
            layer
                .time
                .as_ref()
                .map_or(&[][..], |time| time.matrix.as_slice()),
        );
        write_f32_slice(
            &mut out,
            layer
                .channel_shift
                .as_ref()
                .map_or(&[][..], |shift| shift.as_slice()),
        );
    }
    out
}

fn deserialize_module_state(bytes: Option<&[u8]>) -> io::Result<Option<ModuleState>> {
    let Some(bytes) = bytes else {
        return Ok(None);
    };
    let mut cursor = Cursor::new(bytes);
    cursor.expect_magic(b"ARWKVMODSTATE1")?;
    let layer_count = cursor.u32()? as usize;
    let mut layers = Vec::with_capacity(layer_count);
    for _ in 0..layer_count {
        let x_shift = cursor.f32_vec()?;
        let matrix = cursor.f32_vec()?;
        let channel_shift = cursor.f32_vec()?;
        layers.push(LayerState {
            time: Some(TimeState { x_shift, matrix }),
            channel_shift: Some(channel_shift),
        });
    }
    cursor.expect_end()?;
    Ok(Some(ModuleState { layers }))
}

fn write_u32(out: &mut Vec<u8>, value: u32) {
    out.extend_from_slice(&value.to_le_bytes());
}

fn write_i64(out: &mut Vec<u8>, value: i64) {
    out.extend_from_slice(&value.to_le_bytes());
}

fn write_option_i64(out: &mut Vec<u8>, value: Option<i64>) {
    match value {
        Some(value) => {
            out.push(1);
            write_i64(out, value);
        }
        None => out.push(0),
    }
}

fn write_i64_set(out: &mut Vec<u8>, values: impl Iterator<Item = i64>) {
    let mut values: Vec<_> = values.collect();
    values.sort_unstable();
    write_u32(out, values.len() as u32);
    for value in values {
        write_i64(out, value);
    }
}

fn read_i64_set(cursor: &mut Cursor<'_>) -> io::Result<HashMap<i64, ()>> {
    let count = cursor.u32()? as usize;
    let mut values = HashMap::with_capacity(count);
    for _ in 0..count {
        values.insert(cursor.i64()?, ());
    }
    Ok(values)
}

fn write_i64_map(out: &mut Vec<u8>, values: &HashMap<i64, i64>) {
    let mut values: Vec<_> = values.iter().collect();
    values.sort_by_key(|(key, _)| *key);
    write_u32(out, values.len() as u32);
    for (key, value) in values {
        write_i64(out, *key);
        write_i64(out, *value);
    }
}

fn read_i64_map(cursor: &mut Cursor<'_>) -> io::Result<HashMap<i64, i64>> {
    let count = cursor.u32()? as usize;
    let mut values = HashMap::with_capacity(count);
    for _ in 0..count {
        values.insert(cursor.i64()?, cursor.i64()?);
    }
    Ok(values)
}

fn write_f32_slice(out: &mut Vec<u8>, values: &[f32]) {
    write_u32(out, values.len() as u32);
    for value in values {
        out.extend_from_slice(&value.to_le_bytes());
    }
}

fn interval_for_curve(
    curve: &ReviewCurve,
    target_retention: f32,
    max_interval_days: u32,
) -> Option<u32> {
    if !(0.0..=1.0).contains(&target_retention) || max_interval_days < 1 {
        return None;
    }

    let days = interval_search_days(max_interval_days);
    let mut previous: Option<(u32, f32)> = None;
    for day in days {
        let retrievability = predict_curve(curve, day as f32 * SECONDS_PER_DAY as f32);
        if let Some((previous_day, previous_retrievability)) = previous {
            if retrievability > previous_retrievability + 1e-4 {
                return None;
            }
            if retrievability <= target_retention {
                let span = day - previous_day;
                let denominator = previous_retrievability - retrievability;
                let interpolated = if denominator <= 0.0 {
                    day as f32
                } else {
                    previous_day as f32
                        + span as f32 * (previous_retrievability - target_retention) / denominator
                };
                return Some(clamped_interval_days(interpolated, max_interval_days));
            }
        } else if retrievability <= target_retention {
            return Some(day.clamp(1, max_interval_days));
        }

        previous = Some((day, retrievability));
    }

    Some(max_interval_days)
}

fn interval_search_days(max_interval_days: u32) -> Vec<u32> {
    if max_interval_days <= 30 {
        return (1..=max_interval_days).collect();
    }

    let mut days = (1..=30).collect::<Vec<_>>();
    let mut day = 45;
    while day < max_interval_days {
        days.push(day);
        day = ((day as f32) * 1.5) as u32;
    }
    days.push(max_interval_days);
    days
}

fn clamped_interval_days(elapsed_days: f32, max_interval_days: u32) -> u32 {
    elapsed_days.ceil().clamp(1.0, max_interval_days as f32) as u32
}

fn predict_curve(curve: &ReviewCurve, elapsed_seconds: f32) -> f32 {
    let elapsed_seconds = elapsed_seconds.max(1.0);
    let mut raw_probability = 0.0;
    for (index, weight) in curve.weights.iter().enumerate() {
        let s_space_raw = linspace_exp(index, NUM_CURVES, 18.5);
        let s_space = 0.1 + (s_space_raw - 1.0) * (22.0_f32 - 18.5).exp();
        raw_probability += weight * 0.9_f32.powf(elapsed_seconds / s_space);
    }
    let curve_probability = 1e-5 + (1.0 - 2e-5) * raw_probability;
    let curve_logits = (curve_probability / (1.0 - curve_probability)).ln();
    sigmoid(curve_logits + interp_ahead_logits(&curve.ahead_logits, elapsed_seconds))
}

fn interp_ahead_logits(ahead_logits: &[f32], elapsed_seconds: f32) -> f32 {
    let point_count = ahead_logits.len();
    if point_count < 2 {
        return ahead_logits.first().copied().unwrap_or(0.0);
    }

    let point = |index| {
        let raw = linspace_exp(index, point_count, 18.5);
        0.5 + (raw - 1.0) * (21.0_f32 - 18.5).exp()
    };

    let mut right = 0;
    while right + 1 < point_count && point(right) < elapsed_seconds {
        right += 1;
    }
    let right = right.clamp(1, point_count - 1);
    let left = right - 1;
    let xl = point(left);
    let xr = point(right);
    let yl = ahead_logits[left];
    let yr = ahead_logits[right];
    1e-5 + (1.0 - 2e-5) * (yl + (yr - yl) * (elapsed_seconds - xl) / (xr - xl))
}

fn linspace_exp(index: usize, count: usize, point_spread: f32) -> f32 {
    let value = if count <= 1 {
        0.0
    } else {
        point_spread * index as f32 / (count - 1) as f32
    };
    value.exp()
}

struct RwkvModule {
    layers: Vec<RwkvLayer>,
}

#[cfg(any(not(target_os = "macos"), test))]
struct ModuleQueryScratch {
    current: [f32; D_MODEL],
    next: [f32; D_MODEL],
    v0: [f32; D_MODEL],
    layer: LayerQueryScratch,
}

#[cfg(any(target_os = "macos", test))]
#[derive(Default)]
struct ModuleQueryBatchScratch {
    next: Vec<f32>,
    v0: Vec<f32>,
    layer: LayerQueryBatchScratch,
}

#[cfg(any(not(target_os = "macos"), test))]
impl Default for ModuleQueryScratch {
    fn default() -> Self {
        Self {
            current: [0.0; D_MODEL],
            next: [0.0; D_MODEL],
            v0: [0.0; D_MODEL],
            layer: LayerQueryScratch::default(),
        }
    }
}

impl RwkvModule {
    fn load(weights: &WeightMap, module_id: usize, layer_count: usize) -> io::Result<Self> {
        let mut layers = Vec::with_capacity(layer_count);
        for layer_id in 0..layer_count {
            layers.push(RwkvLayer::load(weights, module_id, layer_id)?);
        }
        Ok(Self { layers })
    }

    fn run(&self, input: &[f32], state: Option<&ModuleState>) -> (Vec<f32>, ModuleState) {
        #[cfg(test)]
        let profile_started = rwkv_warmup_profile_start();

        let mut x = input.to_vec();
        let mut v0 = vec![0.0; D_MODEL];
        let mut next_layers = Vec::with_capacity(self.layers.len());

        for (layer_id, layer) in self.layers.iter().enumerate() {
            let layer_state = state.and_then(|state| state.layers.get(layer_id));
            let (next_x, next_v0, next_layer_state) = layer.run(&x, &v0, layer_state);
            x = next_x;
            v0 = next_v0;
            next_layers.push(next_layer_state);
        }

        let output = (
            x,
            ModuleState {
                layers: next_layers,
            },
        );
        #[cfg(test)]
        rwkv_warmup_profile_record(RwkvWarmupProfileBucket::ModuleRun, profile_started);
        output
    }

    #[cfg(any(not(target_os = "macos"), test))]
    fn run_query(
        &self,
        input: &[f32],
        state: Option<&ModuleState>,
        scratch: &mut ModuleQueryScratch,
    ) -> [f32; D_MODEL] {
        let ModuleQueryScratch {
            current,
            next,
            v0,
            layer: layer_scratch,
        } = scratch;
        current.copy_from_slice(input);
        v0.fill(0.0);
        let mut current = current;
        let mut next = next;

        for (layer_id, layer) in self.layers.iter().enumerate() {
            let layer_state = state.and_then(|state| state.layers.get(layer_id));
            layer.run_query_into(current, v0, layer_state, next, layer_scratch);
            std::mem::swap(&mut current, &mut next);
        }

        *current
    }

    #[cfg(any(target_os = "macos", test))]
    fn run_query_batch(
        &self,
        x: &mut Vec<f32>,
        items: &[ReviewPredictionQueryRef<'_>],
        module_id: usize,
        scratch: &mut ModuleQueryBatchScratch,
    ) {
        let rows = items.len();
        scratch.v0.resize(rows * D_MODEL, 0.0);
        scratch.v0.fill(0.0);
        let mut current = std::mem::take(x);
        let mut next = std::mem::take(&mut scratch.next);

        for (layer_id, layer) in self.layers.iter().enumerate() {
            layer.run_query_batch(
                &current,
                &mut scratch.v0,
                items,
                module_id,
                layer_id,
                &mut next,
                &mut scratch.layer,
            );
            std::mem::swap(&mut current, &mut next);
        }

        *x = current;
        scratch.next = next;
    }
}

#[derive(Clone)]
struct ModuleState {
    layers: Vec<LayerState>,
}

struct RwkvLayer {
    time_mixer: TimeMixer,
    channel_mixer: ChannelMixer,
}

#[cfg(any(not(target_os = "macos"), test))]
struct LayerQueryScratch {
    time_output: [f32; D_MODEL],
    time: TimeMixerQueryScratch,
    channel: ChannelMixerQueryScratch,
}

#[cfg(any(target_os = "macos", test))]
#[derive(Default)]
struct LayerQueryBatchScratch {
    time_output: Vec<f32>,
    time: TimeMixerQueryBatchScratch,
    channel: ChannelMixerQueryBatchScratch,
}

#[cfg(any(not(target_os = "macos"), test))]
impl Default for LayerQueryScratch {
    fn default() -> Self {
        Self {
            time_output: [0.0; D_MODEL],
            time: TimeMixerQueryScratch::default(),
            channel: ChannelMixerQueryScratch::default(),
        }
    }
}

impl RwkvLayer {
    fn load(weights: &WeightMap, module_id: usize, layer_id: usize) -> io::Result<Self> {
        Ok(Self {
            time_mixer: TimeMixer::load(weights, module_id, layer_id)?,
            channel_mixer: ChannelMixer::load(weights, module_id, layer_id)?,
        })
    }

    fn run(
        &self,
        input: &[f32],
        v0: &[f32],
        state: Option<&LayerState>,
    ) -> (Vec<f32>, Vec<f32>, LayerState) {
        let (x, v0, time_state) =
            self.time_mixer
                .run(input, v0, state.and_then(|state| state.time.as_ref()));
        let (x, channel_shift) = self
            .channel_mixer
            .run(&x, state.and_then(|state| state.channel_shift.as_ref()));
        (
            x,
            v0,
            LayerState {
                time: Some(time_state),
                channel_shift: Some(channel_shift),
            },
        )
    }

    #[cfg(any(not(target_os = "macos"), test))]
    fn run_query_into(
        &self,
        input: &[f32],
        v0: &mut [f32; D_MODEL],
        state: Option<&LayerState>,
        out: &mut [f32; D_MODEL],
        scratch: &mut LayerQueryScratch,
    ) {
        self.time_mixer.run_query_into(
            input,
            v0,
            state.and_then(|state| state.time.as_ref()),
            &mut scratch.time_output,
            &mut scratch.time,
        );
        self.channel_mixer.run_query_into(
            &scratch.time_output,
            state.and_then(|state| state.channel_shift.as_deref()),
            out,
            &mut scratch.channel,
        );
    }

    #[cfg(any(target_os = "macos", test))]
    #[allow(clippy::too_many_arguments)]
    fn run_query_batch(
        &self,
        input: &[f32],
        v0: &mut [f32],
        items: &[ReviewPredictionQueryRef<'_>],
        module_id: usize,
        layer_id: usize,
        out: &mut Vec<f32>,
        scratch: &mut LayerQueryBatchScratch,
    ) {
        self.time_mixer.run_query_batch(
            input,
            v0,
            items,
            module_id,
            layer_id,
            &mut scratch.time_output,
            &mut scratch.time,
        );
        self.channel_mixer.run_query_batch(
            &scratch.time_output,
            items,
            module_id,
            layer_id,
            out,
            &mut scratch.channel,
        );
    }
}

#[derive(Clone)]
struct LayerState {
    time: Option<TimeState>,
    channel_shift: Option<Vec<f32>>,
}

struct TimeMixer {
    #[cfg(test)]
    module_id: usize,
    layer_id: usize,
    layer_norm: Norm,
    rkvdag_lerp: Vec<f32>,
    bonus: Vec<f32>,
    w_r: Linear,
    w_k: Linear,
    w_v: Linear,
    w_o: Linear,
    k_scale_linear: Linear,
    v_scale_linear: Linear,
    v_lora: LoraSimple,
    a_lora: LoraSimple,
    d_lora: LoraSimple,
    lora_a_g: Linear,
    lora_b_g: Linear,
    out_group_norm: Norm,
}

#[cfg(any(not(target_os = "macos"), test))]
struct TimeMixerQueryScratch {
    x: [f32; D_MODEL],
    mixed: [[f32; D_MODEL]; 8],
    r: [f32; D_MODEL],
    k: [f32; D_MODEL],
    v: [f32; D_MODEL],
    k_scale: [f32; HEADS],
    v_scale: [f32; HEADS],
    a: [f32; D_MODEL],
    g: [f32; D_MODEL],
    w: [f32; D_MODEL],
    k_deformed: [f32; D_MODEL],
    timestep_out: [f32; D_MODEL],
    temp: [f32; D_MODEL],
    lora_hidden: [f32; MAX_LORA_RANK],
    next_row: [f32; HEAD_SIZE],
}

#[cfg(any(target_os = "macos", test))]
#[derive(Default)]
struct TimeMixerQueryBatchScratch {
    x: Vec<f32>,
    mixed: [Vec<f32>; 8],
    r: Vec<f32>,
    k: Vec<f32>,
    v: Vec<f32>,
    k_scale: Vec<f32>,
    v_scale: Vec<f32>,
    a: Vec<f32>,
    g: Vec<f32>,
    w: Vec<f32>,
    k_deformed: Vec<f32>,
    timestep_out: Vec<f32>,
    temp: Vec<f32>,
    lora_hidden: Vec<f32>,
}

#[cfg(any(not(target_os = "macos"), test))]
impl Default for TimeMixerQueryScratch {
    fn default() -> Self {
        Self {
            x: [0.0; D_MODEL],
            mixed: [[0.0; D_MODEL]; 8],
            r: [0.0; D_MODEL],
            k: [0.0; D_MODEL],
            v: [0.0; D_MODEL],
            k_scale: [0.0; HEADS],
            v_scale: [0.0; HEADS],
            a: [0.0; D_MODEL],
            g: [0.0; D_MODEL],
            w: [0.0; D_MODEL],
            k_deformed: [0.0; D_MODEL],
            timestep_out: [0.0; D_MODEL],
            temp: [0.0; D_MODEL],
            lora_hidden: [0.0; MAX_LORA_RANK],
            next_row: [0.0; HEAD_SIZE],
        }
    }
}

impl TimeMixer {
    fn load(weights: &WeightMap, module_id: usize, layer_id: usize) -> io::Result<Self> {
        let prefix = format!("rwkv_modules.{module_id}.blocks.{layer_id}.time_mixer");
        Ok(Self {
            #[cfg(test)]
            module_id,
            layer_id,
            layer_norm: weights.layer_norm(&format!("{prefix}.layer_norm"), D_MODEL, 1e-5)?,
            rkvdag_lerp: weights.values(&format!("{prefix}.rkvdag_lerp"))?,
            bonus: weights.values(&format!("{prefix}.bonus"))?,
            w_r: weights.linear(&format!("{prefix}.W_r"), D_MODEL, D_MODEL, false)?,
            w_k: weights.linear(&format!("{prefix}.W_k"), D_MODEL, D_MODEL, false)?,
            w_v: weights.linear(&format!("{prefix}.W_v"), D_MODEL, D_MODEL, false)?,
            w_o: weights.linear(&format!("{prefix}.W_o"), D_MODEL, D_MODEL, false)?,
            k_scale_linear: weights.linear(
                &format!("{prefix}.k_scale_linear"),
                D_MODEL,
                HEADS,
                true,
            )?,
            v_scale_linear: weights.linear(
                &format!("{prefix}.v_scale_linear"),
                D_MODEL,
                HEADS,
                true,
            )?,
            v_lora: LoraSimple::load(weights, &format!("{prefix}.v_lora_simple"), 8)?,
            a_lora: LoraSimple::load(weights, &format!("{prefix}.a_lora_simple"), 16)?,
            d_lora: LoraSimple::load(weights, &format!("{prefix}.d_lora_mlp"), 16)?,
            lora_a_g: weights.linear(&format!("{prefix}.lora_A_g"), D_MODEL, 16, false)?,
            lora_b_g: weights.linear(&format!("{prefix}.lora_B_g"), 16, D_MODEL, false)?,
            out_group_norm: weights.group_norm(
                &format!("{prefix}.out_group_norm"),
                HEADS,
                D_MODEL,
                64e-5,
            )?,
        })
    }

    fn run(
        &self,
        input: &[f32],
        v0: &[f32],
        state: Option<&TimeState>,
    ) -> (Vec<f32>, Vec<f32>, TimeState) {
        #[cfg(test)]
        let profile_started = rwkv_warmup_profile_start();

        let x = self.layer_norm.apply(input);
        let (x_shift, state_matrix) = match state {
            Some(state) => (state.x_shift.as_slice(), state.matrix.as_slice()),
            None => (x.as_slice(), &[0.0; HEADS * HEAD_SIZE * HEAD_SIZE][..]),
        };

        let parts = self.mix_parts(&x, x_shift, v0);

        #[cfg(test)]
        record_rwkv_scan_step(
            self.module_id,
            self.layer_id,
            &parts.r,
            &parts.k,
            &parts.v,
            &parts.w,
            &parts.a,
            &parts.k_deformed,
        );

        let (out, next_matrix) = single_timestep(
            &parts.r,
            &parts.k,
            &parts.v,
            &parts.w,
            &parts.a,
            &parts.k_deformed,
            state_matrix,
        );
        let out = self.mix_output(&parts, out, input);

        let output = (
            out,
            parts.next_v0,
            TimeState {
                x_shift: x,
                matrix: next_matrix,
            },
        );
        #[cfg(test)]
        rwkv_warmup_profile_record(RwkvWarmupProfileBucket::TimeMixer, profile_started);
        output
    }

    /// The pre-recurrence per-timestep math shared by the per-review and bulk
    /// replay paths: lerp mixes, projections, loras, activations, and head
    /// normalization. `x` must already be layer-normed; `x_shift` is the
    /// previous timestep's normed input for this stream (or `x` itself when
    /// the stream has no state yet).
    fn mix_parts(&self, x: &[f32], x_shift: &[f32], v0: &[f32]) -> TimeMixParts {
        let mut mixed = [0.0; D_MODEL];
        let fill_mixed = |mix_id: usize, mixed: &mut [f32; D_MODEL]| {
            let lerp_offset = mix_id * D_MODEL;
            for channel in 0..D_MODEL {
                mixed[channel] = lerp(
                    x[channel],
                    x_shift[channel],
                    self.rkvdag_lerp[lerp_offset + channel],
                );
            }
        };

        fill_mixed(0, &mut mixed);
        let r = self.w_r.apply(&mixed);
        fill_mixed(1, &mut mixed);
        let mut k = self.w_k.apply(&mixed);
        fill_mixed(6, &mut mixed);
        let mut k_scale = self.k_scale_linear.apply(&mixed);
        sigmoid_in_place(&mut k_scale);
        fill_mixed(7, &mut mixed);
        let mut v_scale = self.v_scale_linear.apply(&mixed);
        sigmoid_in_place(&mut v_scale);

        fill_mixed(2, &mut mixed);
        let (v, next_v0) = if self.layer_id == 0 {
            let v = self.w_v.apply(&mixed);
            (v.clone(), v)
        } else {
            let mut v_lerp = self.v_lora.apply_sigmoid(&mixed);
            let w_v = self.w_v.apply(&mixed);
            for channel in 0..D_MODEL {
                v_lerp[channel] = lerp(w_v[channel], v0[channel], v_lerp[channel]);
            }
            (v_lerp, v0.to_vec())
        };

        fill_mixed(4, &mut mixed);
        let a = self.a_lora.apply_sigmoid(&mixed);
        fill_mixed(5, &mut mixed);
        let mut g = self.lora_a_g.apply(&mixed);
        sigmoid_in_place(&mut g);
        g = self.lora_b_g.apply(&g);

        fill_mixed(3, &mut mixed);
        let mut w = self.d_lora.apply_tanh(&mixed);
        for value in &mut w {
            let d = -0.5 - softplus(-*value);
            *value = (-d.exp()).exp();
        }

        normalize_heads_in_place(&mut k);
        for head in 0..HEADS {
            for index in 0..HEAD_SIZE {
                k[head * HEAD_SIZE + index] *= k_scale[head];
            }
        }

        let mut v = v;
        normalize_heads_in_place(&mut v);
        for head in 0..HEADS {
            for index in 0..HEAD_SIZE {
                v[head * HEAD_SIZE + index] *= v_scale[head];
            }
        }

        let k_deformed = k.clone();
        for channel in 0..D_MODEL {
            k[channel] *= a[channel];
        }

        TimeMixParts {
            r,
            k,
            v,
            w,
            a,
            k_deformed,
            g,
            next_v0,
        }
    }

    /// The post-recurrence per-timestep math shared by the per-review and
    /// bulk replay paths: group norm, bonus, gate, output projection, and the
    /// residual add against the un-normed layer input.
    fn mix_output(
        &self,
        parts: &TimeMixParts,
        recurrence_out: Vec<f32>,
        input: &[f32],
    ) -> Vec<f32> {
        let mut out = self.out_group_norm.apply(&recurrence_out);

        let mut bonus = [0.0; D_MODEL];
        for head in 0..HEADS {
            let base = head * HEAD_SIZE;
            let mut bonus_scale = 0.0;
            for index in 0..HEAD_SIZE {
                bonus_scale +=
                    parts.r[base + index] * self.bonus[base + index] * parts.k[base + index];
            }
            for index in 0..HEAD_SIZE {
                bonus[base + index] = bonus_scale * parts.v[base + index];
            }
        }

        for channel in 0..D_MODEL {
            out[channel] = parts.g[channel] * (out[channel] + bonus[channel]);
        }
        let mut out = self.w_o.apply(&out);
        for channel in 0..D_MODEL {
            out[channel] += input[channel];
        }
        out
    }

    #[cfg(any(not(target_os = "macos"), test))]
    fn run_query_into(
        &self,
        input: &[f32],
        v0: &mut [f32; D_MODEL],
        state: Option<&TimeState>,
        out: &mut [f32; D_MODEL],
        scratch: &mut TimeMixerQueryScratch,
    ) {
        let TimeMixerQueryScratch {
            x,
            mixed,
            r,
            k,
            v,
            k_scale,
            v_scale,
            a,
            g,
            w,
            k_deformed,
            timestep_out,
            temp,
            lora_hidden,
            next_row,
        } = scratch;

        self.layer_norm.apply_into(input, x);
        let x_shift = state.map_or(x.as_slice(), |state| state.x_shift.as_slice());
        for (mix_id, mixed_row) in mixed.iter_mut().enumerate() {
            let lerp_offset = mix_id * D_MODEL;
            for channel in 0..D_MODEL {
                mixed_row[channel] = lerp(
                    x[channel],
                    x_shift[channel],
                    self.rkvdag_lerp[lerp_offset + channel],
                );
            }
        }

        self.w_r.apply_into(&mixed[0], r);
        self.w_k.apply_into(&mixed[1], k);
        self.k_scale_linear.apply_into(&mixed[6], k_scale);
        sigmoid_in_place(k_scale);
        self.v_scale_linear.apply_into(&mixed[7], v_scale);
        sigmoid_in_place(v_scale);

        if self.layer_id == 0 {
            self.w_v.apply_into(&mixed[2], v);
            v0.copy_from_slice(v);
        } else {
            self.v_lora.apply_sigmoid_into(&mixed[2], lora_hidden, v);
            self.w_v.apply_into(&mixed[2], temp);
            for channel in 0..D_MODEL {
                v[channel] = lerp(temp[channel], v0[channel], v[channel]);
            }
        }

        self.a_lora.apply_sigmoid_into(&mixed[4], lora_hidden, a);
        self.lora_a_g.apply_into(&mixed[5], lora_hidden);
        sigmoid_in_place(lora_hidden);
        self.lora_b_g.apply_into(lora_hidden, g);

        self.d_lora.apply_tanh_into(&mixed[3], lora_hidden, w);
        for value in w.iter_mut() {
            let decay = -0.5 - softplus(-*value);
            *value = (-decay.exp()).exp();
        }

        normalize_heads_in_place(k);
        for head in 0..HEADS {
            for index in 0..HEAD_SIZE {
                k[head * HEAD_SIZE + index] *= k_scale[head];
            }
        }

        normalize_heads_in_place(v);
        for head in 0..HEADS {
            for index in 0..HEAD_SIZE {
                v[head * HEAD_SIZE + index] *= v_scale[head];
            }
        }

        k_deformed.copy_from_slice(k);
        for channel in 0..D_MODEL {
            k[channel] *= a[channel];
        }

        single_timestep_query_into(
            r,
            k,
            v,
            w,
            a,
            k_deformed,
            state.map(|state| state.matrix.as_slice()),
            timestep_out,
            next_row,
        );
        self.out_group_norm.apply_into(timestep_out, temp);

        for head in 0..HEADS {
            let base = head * HEAD_SIZE;
            let mut bonus_scale = 0.0;
            for index in 0..HEAD_SIZE {
                bonus_scale += r[base + index] * self.bonus[base + index] * k[base + index];
            }
            for index in 0..HEAD_SIZE {
                let channel = base + index;
                temp[channel] = g[channel] * (temp[channel] + bonus_scale * v[channel]);
            }
        }

        self.w_o.apply_into(temp, out);
        for channel in 0..D_MODEL {
            out[channel] += input[channel];
        }
    }

    #[cfg(any(target_os = "macos", test))]
    #[allow(clippy::too_many_arguments)]
    fn run_query_batch(
        &self,
        input: &[f32],
        v0: &mut [f32],
        items: &[ReviewPredictionQueryRef<'_>],
        module_id: usize,
        layer_id: usize,
        out: &mut Vec<f32>,
        scratch: &mut TimeMixerQueryBatchScratch,
    ) {
        let rows = items.len();
        debug_assert_eq!(input.len(), rows * D_MODEL);
        debug_assert_eq!(v0.len(), rows * D_MODEL);

        self.layer_norm.apply_batch(input, rows, &mut scratch.x);
        for (mix_id, mixed) in scratch.mixed.iter_mut().enumerate() {
            mixed.resize(rows * D_MODEL, 0.0);
            let lerp_weights = &self.rkvdag_lerp[mix_id * D_MODEL..(mix_id + 1) * D_MODEL];
            mixed
                .chunks_mut(D_MODEL)
                .enumerate()
                .for_each(|(row, mixed)| {
                    let x = &scratch.x[row * D_MODEL..(row + 1) * D_MODEL];
                    let x_shift = items[row]
                        .layer_state(module_id, layer_id)
                        .and_then(|state| state.time.as_ref())
                        .map_or(x, |state| state.x_shift.as_slice());
                    for channel in 0..D_MODEL {
                        mixed[channel] = lerp(x[channel], x_shift[channel], lerp_weights[channel]);
                    }
                });
        }

        self.w_r
            .apply_batch(&scratch.mixed[0], rows, &mut scratch.r);
        self.w_k
            .apply_batch(&scratch.mixed[1], rows, &mut scratch.k);
        self.k_scale_linear
            .apply_batch(&scratch.mixed[6], rows, &mut scratch.k_scale);
        sigmoid_in_place(&mut scratch.k_scale);
        self.v_scale_linear
            .apply_batch(&scratch.mixed[7], rows, &mut scratch.v_scale);
        sigmoid_in_place(&mut scratch.v_scale);

        if self.layer_id == 0 {
            self.w_v
                .apply_batch(&scratch.mixed[2], rows, &mut scratch.v);
            v0.copy_from_slice(&scratch.v);
        } else {
            self.v_lora.apply_sigmoid_batch(
                &scratch.mixed[2],
                rows,
                &mut scratch.lora_hidden,
                &mut scratch.v,
            );
            self.w_v
                .apply_batch(&scratch.mixed[2], rows, &mut scratch.temp);
            scratch
                .v
                .chunks_mut(D_MODEL)
                .enumerate()
                .for_each(|(row, v)| {
                    let projected = &scratch.temp[row * D_MODEL..(row + 1) * D_MODEL];
                    let v0 = &v0[row * D_MODEL..(row + 1) * D_MODEL];
                    for channel in 0..D_MODEL {
                        v[channel] = lerp(projected[channel], v0[channel], v[channel]);
                    }
                });
        }

        self.a_lora.apply_sigmoid_batch(
            &scratch.mixed[4],
            rows,
            &mut scratch.lora_hidden,
            &mut scratch.a,
        );
        self.lora_a_g
            .apply_batch(&scratch.mixed[5], rows, &mut scratch.lora_hidden);
        sigmoid_in_place(&mut scratch.lora_hidden);
        self.lora_b_g
            .apply_batch(&scratch.lora_hidden, rows, &mut scratch.g);

        self.d_lora.apply_tanh_batch(
            &scratch.mixed[3],
            rows,
            &mut scratch.lora_hidden,
            &mut scratch.w,
        );
        scratch.w.iter_mut().for_each(|value| {
            let decay = -0.5 - softplus(-*value);
            *value = (-decay.exp()).exp();
        });

        scratch
            .k
            .chunks_mut(D_MODEL)
            .enumerate()
            .for_each(|(row, k)| {
                normalize_heads_in_place(k);
                let scales = &scratch.k_scale[row * HEADS..(row + 1) * HEADS];
                scale_heads_in_place(k, scales);
            });
        scratch
            .v
            .chunks_mut(D_MODEL)
            .enumerate()
            .for_each(|(row, v)| {
                normalize_heads_in_place(v);
                let scales = &scratch.v_scale[row * HEADS..(row + 1) * HEADS];
                scale_heads_in_place(v, scales);
            });

        scratch.k_deformed.resize(rows * D_MODEL, 0.0);
        scratch.k_deformed.copy_from_slice(&scratch.k);
        scratch
            .k
            .iter_mut()
            .zip(scratch.a.iter())
            .for_each(|(k, a)| *k *= *a);

        scratch.timestep_out.resize(rows * D_MODEL, 0.0);
        scratch
            .timestep_out
            .chunks_mut(D_MODEL)
            .enumerate()
            .for_each(|(row, timestep_out)| {
                let range = row * D_MODEL..(row + 1) * D_MODEL;
                let state = items[row]
                    .layer_state(module_id, layer_id)
                    .and_then(|state| state.time.as_ref())
                    .map(|state| state.matrix.as_slice());
                single_timestep_query_fast_into(
                    &scratch.r[range.clone()],
                    &scratch.k[range.clone()],
                    &scratch.v[range.clone()],
                    &scratch.w[range.clone()],
                    &scratch.a[range.clone()],
                    &scratch.k_deformed[range],
                    state,
                    timestep_out,
                );
            });
        self.out_group_norm
            .apply_batch(&scratch.timestep_out, rows, &mut scratch.temp);

        scratch
            .temp
            .chunks_mut(D_MODEL)
            .enumerate()
            .for_each(|(row, output)| {
                let base = row * D_MODEL;
                for head in 0..HEADS {
                    let head_base = head * HEAD_SIZE;
                    let mut bonus_scale = 0.0;
                    for index in 0..HEAD_SIZE {
                        let channel = base + head_base + index;
                        bonus_scale +=
                            scratch.r[channel] * self.bonus[head_base + index] * scratch.k[channel];
                    }
                    for index in 0..HEAD_SIZE {
                        let local_channel = head_base + index;
                        let channel = base + local_channel;
                        output[local_channel] = scratch.g[channel]
                            * (output[local_channel] + bonus_scale * scratch.v[channel]);
                    }
                }
            });

        self.w_o.apply_batch(&scratch.temp, rows, out);
        out.iter_mut()
            .zip(input.iter())
            .for_each(|(output, input)| *output += *input);
    }
}

/// Per-timestep time-mixer coefficients produced ahead of the recurrence.
struct TimeMixParts {
    r: Vec<f32>,
    k: Vec<f32>,
    v: Vec<f32>,
    w: Vec<f32>,
    a: Vec<f32>,
    k_deformed: Vec<f32>,
    g: Vec<f32>,
    next_v0: Vec<f32>,
}

#[derive(Clone)]
struct TimeState {
    x_shift: Vec<f32>,
    matrix: Vec<f32>,
}

struct ChannelMixer {
    layer_norm: Norm,
    lerp_k: Vec<f32>,
    w_k: Linear,
    w_v: Linear,
}

#[cfg(any(not(target_os = "macos"), test))]
struct ChannelMixerQueryScratch {
    x: [f32; D_MODEL],
    mixed: [f32; D_MODEL],
    hidden: [f32; MAX_CHANNEL_MIXER_DIM],
    projected: [f32; D_MODEL],
}

#[cfg(any(target_os = "macos", test))]
#[derive(Default)]
struct ChannelMixerQueryBatchScratch {
    x: Vec<f32>,
    mixed: Vec<f32>,
    hidden: Vec<f32>,
    projected: Vec<f32>,
}

#[cfg(any(not(target_os = "macos"), test))]
impl Default for ChannelMixerQueryScratch {
    fn default() -> Self {
        Self {
            x: [0.0; D_MODEL],
            mixed: [0.0; D_MODEL],
            hidden: [0.0; MAX_CHANNEL_MIXER_DIM],
            projected: [0.0; D_MODEL],
        }
    }
}

impl ChannelMixer {
    fn load(weights: &WeightMap, module_id: usize, layer_id: usize) -> io::Result<Self> {
        let channel_dim = CHANNEL_MIXER_DIMS[module_id];
        let prefix = format!("rwkv_modules.{module_id}.blocks.{layer_id}.channel_mixer");
        Ok(Self {
            layer_norm: weights.layer_norm(&format!("{prefix}.layer_norm"), D_MODEL, 1e-5)?,
            lerp_k: weights.values(&format!("{prefix}.lerp_k"))?,
            w_k: weights.linear(&format!("{prefix}.W_k"), D_MODEL, channel_dim, false)?,
            w_v: weights.linear(&format!("{prefix}.W_v"), channel_dim, D_MODEL, false)?,
        })
    }

    fn run(&self, input: &[f32], state: Option<&Vec<f32>>) -> (Vec<f32>, Vec<f32>) {
        #[cfg(test)]
        let profile_started = rwkv_warmup_profile_start();

        let x = self.layer_norm.apply(input);
        let x_shift = state.map_or(x.as_slice(), |state| state.as_slice());
        let out = self.mix_value(&x, x_shift, input);
        let output = (out, x);
        #[cfg(test)]
        rwkv_warmup_profile_record(RwkvWarmupProfileBucket::ChannelMixer, profile_started);
        output
    }

    /// The per-timestep channel-mixer math shared by the per-review and bulk
    /// replay paths. `x` must already be layer-normed; `x_shift` is the
    /// previous timestep's normed input for this stream (or `x` itself when
    /// the stream has no state yet); `input` is the un-normed layer input
    /// used for the residual add.
    fn mix_value(&self, x: &[f32], x_shift: &[f32], input: &[f32]) -> Vec<f32> {
        let mut mixed = [0.0; D_MODEL];
        for channel in 0..D_MODEL {
            mixed[channel] = lerp(x[channel], x_shift[channel], self.lerp_k[channel]);
        }

        let mut k = self.w_k.apply(&mixed);
        for value in &mut k {
            *value = value.max(0.0).powi(2);
        }
        let mut out = self.w_v.apply(&k);
        for channel in 0..D_MODEL {
            out[channel] += input[channel];
        }
        out
    }

    #[cfg(any(not(target_os = "macos"), test))]
    fn run_query_into(
        &self,
        input: &[f32],
        state: Option<&[f32]>,
        out: &mut [f32; D_MODEL],
        scratch: &mut ChannelMixerQueryScratch,
    ) {
        self.layer_norm.apply_into(input, &mut scratch.x);
        let x_shift = state.unwrap_or(&scratch.x);
        for (channel, mixed) in scratch.mixed.iter_mut().enumerate() {
            *mixed = lerp(scratch.x[channel], x_shift[channel], self.lerp_k[channel]);
        }

        let hidden = &mut scratch.hidden[..self.w_k.output];
        self.w_k.apply_into(&scratch.mixed, hidden);
        for value in hidden.iter_mut() {
            *value = value.max(0.0).powi(2);
        }
        self.w_v.apply_into(hidden, &mut scratch.projected);
        for channel in 0..D_MODEL {
            out[channel] = input[channel] + scratch.projected[channel];
        }
    }

    #[cfg(any(target_os = "macos", test))]
    fn run_query_batch(
        &self,
        input: &[f32],
        items: &[ReviewPredictionQueryRef<'_>],
        module_id: usize,
        layer_id: usize,
        out: &mut Vec<f32>,
        scratch: &mut ChannelMixerQueryBatchScratch,
    ) {
        let rows = items.len();
        self.layer_norm.apply_batch(input, rows, &mut scratch.x);
        scratch.mixed.resize(rows * D_MODEL, 0.0);
        scratch
            .mixed
            .chunks_mut(D_MODEL)
            .enumerate()
            .for_each(|(row, mixed)| {
                let x = &scratch.x[row * D_MODEL..(row + 1) * D_MODEL];
                let x_shift = items[row]
                    .layer_state(module_id, layer_id)
                    .and_then(|state| state.channel_shift.as_deref())
                    .unwrap_or(x);
                for channel in 0..D_MODEL {
                    mixed[channel] = lerp(x[channel], x_shift[channel], self.lerp_k[channel]);
                }
            });

        self.w_k
            .apply_batch(&scratch.mixed, rows, &mut scratch.hidden);
        scratch
            .hidden
            .iter_mut()
            .for_each(|value| *value = value.max(0.0).powi(2));
        self.w_v
            .apply_batch(&scratch.hidden, rows, &mut scratch.projected);
        out.resize(rows * D_MODEL, 0.0);
        out.iter_mut()
            .zip(input.iter())
            .zip(scratch.projected.iter())
            .for_each(|((output, input), projected)| *output = *input + *projected);
    }
}

struct LoraSimple {
    a: Linear,
    b: Linear,
}

impl LoraSimple {
    fn load(weights: &WeightMap, prefix: &str, rank: usize) -> io::Result<Self> {
        Ok(Self {
            a: weights.linear(&format!("{prefix}.A"), D_MODEL, rank, false)?,
            b: weights.linear(&format!("{prefix}.B_and_lamb"), rank, D_MODEL, true)?,
        })
    }

    fn apply_sigmoid(&self, input: &[f32]) -> Vec<f32> {
        let mut out = self.b.apply(&self.a.apply(input));
        sigmoid_in_place(&mut out);
        out
    }

    #[cfg(any(not(target_os = "macos"), test))]
    fn apply_sigmoid_into(&self, input: &[f32], hidden: &mut [f32], out: &mut [f32]) {
        let hidden = &mut hidden[..self.a.output];
        self.a.apply_into(input, hidden);
        self.b.apply_into(hidden, out);
        sigmoid_in_place(out);
    }

    #[cfg(any(target_os = "macos", test))]
    fn apply_sigmoid_batch(
        &self,
        input: &[f32],
        rows: usize,
        hidden: &mut Vec<f32>,
        out: &mut Vec<f32>,
    ) {
        self.a.apply_batch(input, rows, hidden);
        self.b.apply_batch(hidden, rows, out);
        sigmoid_in_place(out);
    }

    fn apply_tanh(&self, input: &[f32]) -> Vec<f32> {
        let mut hidden = self.a.apply(input);
        for value in &mut hidden {
            *value = value.tanh();
        }
        self.b.apply(&hidden)
    }

    /// Block variant of `apply_sigmoid`; bit-identical per row.
    fn apply_sigmoid_block_into(&self, inputs: &[f32], outs: &mut [f32], rows: usize) {
        let rank = self.a.output;
        debug_assert!(rank <= 16);
        debug_assert!(rows <= LINEAR_BLOCK_ROWS);
        let mut hidden = [0.0; 16 * LINEAR_BLOCK_ROWS];
        let hidden = &mut hidden[..rows * rank];
        self.a.apply_block_into(inputs, hidden, rows);
        self.b.apply_block_into(hidden, outs, rows);
        sigmoid_in_place(outs);
    }

    /// Block variant of `apply_tanh`; bit-identical per row.
    fn apply_tanh_block_into(&self, inputs: &[f32], outs: &mut [f32], rows: usize) {
        let rank = self.a.output;
        debug_assert!(rank <= 16);
        debug_assert!(rows <= LINEAR_BLOCK_ROWS);
        let mut hidden = [0.0; 16 * LINEAR_BLOCK_ROWS];
        let hidden = &mut hidden[..rows * rank];
        self.a.apply_block_into(inputs, hidden, rows);
        for value in hidden.iter_mut() {
            *value = value.tanh();
        }
        self.b.apply_block_into(hidden, outs, rows);
    }

    #[cfg(any(not(target_os = "macos"), test))]
    fn apply_tanh_into(&self, input: &[f32], hidden: &mut [f32], out: &mut [f32]) {
        let hidden = &mut hidden[..self.a.output];
        self.a.apply_into(input, hidden);
        for value in hidden.iter_mut() {
            *value = value.tanh();
        }
        self.b.apply_into(hidden, out);
    }

    #[cfg(any(target_os = "macos", test))]
    fn apply_tanh_batch(
        &self,
        input: &[f32],
        rows: usize,
        hidden: &mut Vec<f32>,
        out: &mut Vec<f32>,
    ) {
        self.a.apply_batch(input, rows, hidden);
        hidden.iter_mut().for_each(|value| *value = value.tanh());
        self.b.apply_batch(hidden, rows, out);
    }
}

struct Linear {
    input: usize,
    output: usize,
    weight_by_input: Vec<f32>,
    bias: Option<Vec<f32>>,
}

impl Linear {
    fn apply(&self, input: &[f32]) -> Vec<f32> {
        let mut out = vec![0.0; self.output];
        self.apply_into(input, &mut out);
        out
    }

    fn apply_into(&self, input: &[f32], out: &mut [f32]) {
        #[cfg(test)]
        let profile_started = rwkv_warmup_profile_start();

        debug_assert_eq!(input.len(), self.input);
        debug_assert_eq!(out.len(), self.output);
        if let Some(bias) = &self.bias {
            out.copy_from_slice(bias);
        } else {
            out.fill(0.0);
        }
        for (column, scale) in input.iter().copied().enumerate() {
            if scale == 0.0 {
                continue;
            }
            let weight_column =
                &self.weight_by_input[column * self.output..(column + 1) * self.output];
            add_scaled_in_place(out, weight_column, scale);
        }
        #[cfg(test)]
        rwkv_warmup_profile_record(RwkvWarmupProfileBucket::Linear, profile_started);
    }

    #[cfg(any(target_os = "macos", test))]
    fn apply_batch(&self, input: &[f32], rows: usize, out: &mut Vec<f32>) {
        let input_len = rows
            .checked_mul(self.input)
            .expect("linear batch input is too large");
        let output_len = rows
            .checked_mul(self.output)
            .expect("linear batch output is too large");
        assert_eq!(input.len(), input_len);
        out.resize(output_len, 0.0);
        if rows == 0 {
            return;
        }

        #[cfg(target_os = "macos")]
        matmul::matrix_times_matrix(
            input,
            &self.weight_by_input,
            rows,
            self.output,
            self.input,
            out,
        );

        #[cfg(not(target_os = "macos"))]
        out.par_chunks_mut(self.output)
            .zip(input.par_chunks(self.input))
            .for_each(|(output, input)| self.apply_into(input, output));

        #[cfg(target_os = "macos")]
        if let Some(bias) = &self.bias {
            out.chunks_mut(self.output).for_each(|output| {
                for (output, bias) in output.iter_mut().zip(bias) {
                    *output += bias;
                }
            });
        }
    }

    /// Applies the projection to `rows` packed input rows at once, streaming
    /// each weight column once per block instead of once per row. For every
    /// output element this performs the identical operation sequence as
    /// `apply_into` (bias init, then ascending-column fused accumulation with
    /// the same zero-column skip), so results are bit-identical to calling
    /// `apply_into` per row.
    fn apply_block_into(&self, inputs: &[f32], outs: &mut [f32], rows: usize) {
        #[cfg(test)]
        let profile_started = rwkv_warmup_profile_start();

        debug_assert_eq!(inputs.len(), rows * self.input);
        debug_assert_eq!(outs.len(), rows * self.output);
        for out in outs.chunks_exact_mut(self.output) {
            if let Some(bias) = &self.bias {
                out.copy_from_slice(bias);
            } else {
                out.fill(0.0);
            }
        }
        for column in 0..self.input {
            let weight_column =
                &self.weight_by_input[column * self.output..(column + 1) * self.output];
            for (row, out) in outs.chunks_exact_mut(self.output).enumerate() {
                let scale = inputs[row * self.input + column];
                if scale == 0.0 {
                    continue;
                }
                add_scaled_in_place(out, weight_column, scale);
            }
        }
        #[cfg(test)]
        rwkv_warmup_profile_record(RwkvWarmupProfileBucket::Linear, profile_started);
    }
}

#[inline(always)]
fn add_scaled_in_place(out: &mut [f32], weights: &[f32], scale: f32) {
    debug_assert_eq!(out.len(), weights.len());
    add_scaled_in_place_arch(out, weights, scale)
}

#[cfg(target_arch = "aarch64")]
#[inline(always)]
fn add_scaled_in_place_arch(out: &mut [f32], weights: &[f32], scale: f32) {
    // SAFETY: aarch64 guarantees NEON support, and the helper only uses
    // unaligned loads/stores within the bounds checked by its loop conditions.
    unsafe { add_scaled_in_place_neon(out, weights, scale) }
}

#[cfg(target_arch = "x86_64")]
#[inline(always)]
fn add_scaled_in_place_arch(out: &mut [f32], weights: &[f32], scale: f32) {
    if x86_avx2_fma_available() {
        // SAFETY: availability was checked above, and the helper only uses
        // unaligned loads/stores within the bounds checked by its loops.
        unsafe { add_scaled_in_place_avx2_fma(out, weights, scale) }
    } else {
        add_scaled_in_place_scalar(out, weights, scale)
    }
}

#[cfg(all(not(target_arch = "aarch64"), not(target_arch = "x86_64")))]
#[inline(always)]
fn add_scaled_in_place_arch(out: &mut [f32], weights: &[f32], scale: f32) {
    add_scaled_in_place_scalar(out, weights, scale)
}

#[cfg(target_arch = "aarch64")]
#[inline(always)]
unsafe fn add_scaled_in_place_neon(out: &mut [f32], weights: &[f32], scale: f32) {
    use std::arch::aarch64::*;

    let mut offset = 0;
    let len = out.len();
    let scale = vdupq_n_f32(scale);
    while offset + 16 <= len {
        let out_ptr = out.as_mut_ptr().add(offset);
        let weight_ptr = weights.as_ptr().add(offset);
        vst1q_f32(
            out_ptr,
            vfmaq_f32(vld1q_f32(out_ptr), vld1q_f32(weight_ptr), scale),
        );
        vst1q_f32(
            out_ptr.add(4),
            vfmaq_f32(
                vld1q_f32(out_ptr.add(4)),
                vld1q_f32(weight_ptr.add(4)),
                scale,
            ),
        );
        vst1q_f32(
            out_ptr.add(8),
            vfmaq_f32(
                vld1q_f32(out_ptr.add(8)),
                vld1q_f32(weight_ptr.add(8)),
                scale,
            ),
        );
        vst1q_f32(
            out_ptr.add(12),
            vfmaq_f32(
                vld1q_f32(out_ptr.add(12)),
                vld1q_f32(weight_ptr.add(12)),
                scale,
            ),
        );
        offset += 16;
    }
    while offset + 4 <= len {
        let out_ptr = out.as_mut_ptr().add(offset);
        let weight_ptr = weights.as_ptr().add(offset);
        vst1q_f32(
            out_ptr,
            vfmaq_f32(vld1q_f32(out_ptr), vld1q_f32(weight_ptr), scale),
        );
        offset += 4;
    }
    while offset < len {
        out[offset] += weights[offset] * vgetq_lane_f32(scale, 0);
        offset += 1;
    }
}

#[cfg(target_arch = "x86_64")]
#[inline(always)]
fn x86_avx2_fma_available() -> bool {
    use std::sync::atomic::AtomicU8;
    use std::sync::atomic::Ordering;

    static AVAILABLE: AtomicU8 = AtomicU8::new(0);

    match AVAILABLE.load(Ordering::Relaxed) {
        1 => false,
        2 => true,
        _ => {
            let available =
                std::is_x86_feature_detected!("avx2") && std::is_x86_feature_detected!("fma");
            AVAILABLE.store(if available { 2 } else { 1 }, Ordering::Relaxed);
            available
        }
    }
}

#[cfg(target_arch = "x86_64")]
#[target_feature(enable = "avx2,fma")]
unsafe fn add_scaled_in_place_avx2_fma(out: &mut [f32], weights: &[f32], scale: f32) {
    use std::arch::x86_64::*;

    let mut offset = 0;
    let len = out.len();
    let scale_vector = _mm256_set1_ps(scale);
    while offset + 32 <= len {
        let out_ptr = out.as_mut_ptr().add(offset);
        let weight_ptr = weights.as_ptr().add(offset);
        _mm256_storeu_ps(
            out_ptr,
            _mm256_fmadd_ps(
                _mm256_loadu_ps(weight_ptr),
                scale_vector,
                _mm256_loadu_ps(out_ptr),
            ),
        );
        _mm256_storeu_ps(
            out_ptr.add(8),
            _mm256_fmadd_ps(
                _mm256_loadu_ps(weight_ptr.add(8)),
                scale_vector,
                _mm256_loadu_ps(out_ptr.add(8)),
            ),
        );
        _mm256_storeu_ps(
            out_ptr.add(16),
            _mm256_fmadd_ps(
                _mm256_loadu_ps(weight_ptr.add(16)),
                scale_vector,
                _mm256_loadu_ps(out_ptr.add(16)),
            ),
        );
        _mm256_storeu_ps(
            out_ptr.add(24),
            _mm256_fmadd_ps(
                _mm256_loadu_ps(weight_ptr.add(24)),
                scale_vector,
                _mm256_loadu_ps(out_ptr.add(24)),
            ),
        );
        offset += 32;
    }
    while offset + 8 <= len {
        let out_ptr = out.as_mut_ptr().add(offset);
        let weight_ptr = weights.as_ptr().add(offset);
        _mm256_storeu_ps(
            out_ptr,
            _mm256_fmadd_ps(
                _mm256_loadu_ps(weight_ptr),
                scale_vector,
                _mm256_loadu_ps(out_ptr),
            ),
        );
        offset += 8;
    }
    while offset < len {
        out[offset] += weights[offset] * scale;
        offset += 1;
    }
}

#[cfg(not(target_arch = "aarch64"))]
#[inline(always)]
fn add_scaled_in_place_scalar(out: &mut [f32], weights: &[f32], scale: f32) {
    for (output, weight) in out.iter_mut().zip(weights) {
        *output += weight * scale;
    }
}

#[inline(always)]
fn dot_product(left: &[f32], right: &[f32]) -> f32 {
    debug_assert_eq!(left.len(), right.len());
    dot_product_arch(left, right)
}

#[cfg(target_arch = "aarch64")]
#[inline(always)]
fn dot_product_arch(left: &[f32], right: &[f32]) -> f32 {
    // SAFETY: aarch64 guarantees NEON support, and the helper only uses
    // unaligned loads within the bounds checked by its loop conditions.
    unsafe { dot_product_neon(left, right) }
}

#[cfg(target_arch = "x86_64")]
#[inline(always)]
fn dot_product_arch(left: &[f32], right: &[f32]) -> f32 {
    if x86_avx2_fma_available() {
        // SAFETY: availability was checked above, and the helper only uses
        // unaligned loads within the bounds checked by its loops.
        unsafe { dot_product_avx2_fma(left, right) }
    } else {
        dot_product_scalar(left, right)
    }
}

#[cfg(all(not(target_arch = "aarch64"), not(target_arch = "x86_64")))]
#[inline(always)]
fn dot_product_arch(left: &[f32], right: &[f32]) -> f32 {
    dot_product_scalar(left, right)
}

#[cfg(target_arch = "aarch64")]
#[inline(always)]
unsafe fn dot_product_neon(left: &[f32], right: &[f32]) -> f32 {
    use std::arch::aarch64::*;

    let mut offset = 0;
    let len = left.len();
    let mut acc0 = vdupq_n_f32(0.0);
    let mut acc1 = vdupq_n_f32(0.0);
    let mut acc2 = vdupq_n_f32(0.0);
    let mut acc3 = vdupq_n_f32(0.0);

    while offset + 16 <= len {
        let left_ptr = left.as_ptr().add(offset);
        let right_ptr = right.as_ptr().add(offset);
        acc0 = vfmaq_f32(acc0, vld1q_f32(left_ptr), vld1q_f32(right_ptr));
        acc1 = vfmaq_f32(
            acc1,
            vld1q_f32(left_ptr.add(4)),
            vld1q_f32(right_ptr.add(4)),
        );
        acc2 = vfmaq_f32(
            acc2,
            vld1q_f32(left_ptr.add(8)),
            vld1q_f32(right_ptr.add(8)),
        );
        acc3 = vfmaq_f32(
            acc3,
            vld1q_f32(left_ptr.add(12)),
            vld1q_f32(right_ptr.add(12)),
        );
        offset += 16;
    }

    let mut acc = vaddq_f32(vaddq_f32(acc0, acc1), vaddq_f32(acc2, acc3));
    while offset + 4 <= len {
        acc = vfmaq_f32(
            acc,
            vld1q_f32(left.as_ptr().add(offset)),
            vld1q_f32(right.as_ptr().add(offset)),
        );
        offset += 4;
    }

    let mut sum = vaddvq_f32(acc);
    while offset < len {
        sum += left[offset] * right[offset];
        offset += 1;
    }
    sum
}

#[cfg(target_arch = "x86_64")]
#[target_feature(enable = "avx2,fma")]
unsafe fn dot_product_avx2_fma(left: &[f32], right: &[f32]) -> f32 {
    use std::arch::x86_64::*;

    let mut offset = 0;
    let len = left.len();
    let mut acc0 = _mm256_setzero_ps();
    let mut acc1 = _mm256_setzero_ps();
    let mut acc2 = _mm256_setzero_ps();
    let mut acc3 = _mm256_setzero_ps();

    while offset + 32 <= len {
        let left_ptr = left.as_ptr().add(offset);
        let right_ptr = right.as_ptr().add(offset);
        acc0 = _mm256_fmadd_ps(_mm256_loadu_ps(left_ptr), _mm256_loadu_ps(right_ptr), acc0);
        acc1 = _mm256_fmadd_ps(
            _mm256_loadu_ps(left_ptr.add(8)),
            _mm256_loadu_ps(right_ptr.add(8)),
            acc1,
        );
        acc2 = _mm256_fmadd_ps(
            _mm256_loadu_ps(left_ptr.add(16)),
            _mm256_loadu_ps(right_ptr.add(16)),
            acc2,
        );
        acc3 = _mm256_fmadd_ps(
            _mm256_loadu_ps(left_ptr.add(24)),
            _mm256_loadu_ps(right_ptr.add(24)),
            acc3,
        );
        offset += 32;
    }

    let mut acc = _mm256_add_ps(_mm256_add_ps(acc0, acc1), _mm256_add_ps(acc2, acc3));
    while offset + 8 <= len {
        acc = _mm256_fmadd_ps(
            _mm256_loadu_ps(left.as_ptr().add(offset)),
            _mm256_loadu_ps(right.as_ptr().add(offset)),
            acc,
        );
        offset += 8;
    }

    let mut lanes = [0.0_f32; 8];
    _mm256_storeu_ps(lanes.as_mut_ptr(), acc);
    let mut sum = lanes.iter().copied().sum::<f32>();
    while offset < len {
        sum += left[offset] * right[offset];
        offset += 1;
    }
    sum
}

#[cfg(not(target_arch = "aarch64"))]
#[inline(always)]
fn dot_product_scalar(left: &[f32], right: &[f32]) -> f32 {
    left.iter()
        .zip(right)
        .map(|(left, right)| left * right)
        .sum()
}

struct Norm {
    groups: usize,
    dim: usize,
    eps: f32,
    weight: Vec<f32>,
    bias: Vec<f32>,
}

impl Norm {
    fn apply(&self, input: &[f32]) -> Vec<f32> {
        let mut out = vec![0.0; self.dim];
        self.apply_into(input, &mut out);
        out
    }

    fn apply_into(&self, input: &[f32], out: &mut [f32]) {
        #[cfg(test)]
        let profile_started = rwkv_warmup_profile_start();
        debug_assert_eq!(input.len(), self.dim);
        debug_assert_eq!(out.len(), self.dim);
        let group_size = self.dim / self.groups;

        for group in 0..self.groups {
            let start = group * group_size;
            let end = start + group_size;
            let values = &input[start..end];
            let mean = values.iter().sum::<f32>() / group_size as f32;
            let variance = values
                .iter()
                .map(|value| {
                    let diff = value - mean;
                    diff * diff
                })
                .sum::<f32>()
                / group_size as f32;
            let scale = (variance + self.eps).sqrt().recip();
            for index in start..end {
                out[index] = (input[index] - mean) * scale * self.weight[index] + self.bias[index];
            }
        }

        #[cfg(test)]
        rwkv_warmup_profile_record(RwkvWarmupProfileBucket::Norm, profile_started);
    }

    #[cfg(any(target_os = "macos", test))]
    fn apply_batch(&self, input: &[f32], rows: usize, out: &mut Vec<f32>) {
        debug_assert_eq!(input.len(), rows * self.dim);
        out.resize(rows * self.dim, 0.0);
        out.chunks_mut(self.dim)
            .zip(input.chunks(self.dim))
            .for_each(|(output, input)| self.apply_into(input, output));
    }
}

fn single_timestep(
    r: &[f32],
    k: &[f32],
    v: &[f32],
    w: &[f32],
    a: &[f32],
    k_deformed: &[f32],
    state: &[f32],
) -> (Vec<f32>, Vec<f32>) {
    #[cfg(test)]
    let profile_started =
        if RWKV_SINGLE_TIMESTEP_PROFILE_ENABLED.load(std::sync::atomic::Ordering::Relaxed) {
            Some(std::time::Instant::now())
        } else {
            None
        };
    #[cfg(test)]
    let warmup_profile_started = rwkv_warmup_profile_start();

    let mut next_state = vec![0.0; HEADS * HEAD_SIZE * HEAD_SIZE];
    let mut out = vec![0.0; D_MODEL];

    for head in 0..HEADS {
        let head_base = head * HEAD_SIZE;
        let matrix_base = head * HEAD_SIZE * HEAD_SIZE;
        let mut state_dot_k = [0.0_f32; HEAD_SIZE];
        let key_deformed = &k_deformed[head_base..head_base + HEAD_SIZE];
        let receptance = &r[head_base..head_base + HEAD_SIZE];

        for (row, value) in state_dot_k.iter_mut().enumerate() {
            let row_start = matrix_base + row * HEAD_SIZE;
            let state_row = &state[row_start..row_start + HEAD_SIZE];
            *value = dot_product(state_row, key_deformed);
        }

        for row in 0..HEAD_SIZE {
            for column in 0..HEAD_SIZE {
                let channel = head_base + column;
                let old = state[matrix_base + row * HEAD_SIZE + column];
                next_state[matrix_base + row * HEAD_SIZE + column] = old * w[channel]
                    - state_dot_k[row] * a[channel] * k_deformed[channel]
                    + v[head_base + row] * k[channel];
            }
        }

        for row in 0..HEAD_SIZE {
            let row_start = matrix_base + row * HEAD_SIZE;
            let state_row = &next_state[row_start..row_start + HEAD_SIZE];
            out[head_base + row] = dot_product(state_row, receptance);
        }
    }

    #[cfg(test)]
    if let Some(started) = profile_started {
        RWKV_SINGLE_TIMESTEP_PROFILE_CALLS.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        RWKV_SINGLE_TIMESTEP_PROFILE_NANOS.fetch_add(
            started.elapsed().as_nanos().min(u128::from(u64::MAX)) as u64,
            std::sync::atomic::Ordering::Relaxed,
        );
    }
    #[cfg(test)]
    rwkv_warmup_profile_record(
        RwkvWarmupProfileBucket::SingleTimestep,
        warmup_profile_started,
    );

    (out, next_state)
}

#[cfg(any(not(target_os = "macos"), test))]
#[allow(clippy::too_many_arguments)]
fn single_timestep_query_into(
    r: &[f32],
    k: &[f32],
    v: &[f32],
    w: &[f32],
    a: &[f32],
    k_deformed: &[f32],
    state: Option<&[f32]>,
    out: &mut [f32],
    next_row: &mut [f32],
) {
    debug_assert_eq!(out.len(), D_MODEL);
    debug_assert_eq!(next_row.len(), HEAD_SIZE);
    debug_assert!(
        state.is_none() || state.is_some_and(|state| state.len() == HEADS * HEAD_SIZE * HEAD_SIZE)
    );

    for head in 0..HEADS {
        let head_base = head * HEAD_SIZE;
        let matrix_base = head * HEAD_SIZE * HEAD_SIZE;
        let mut state_dot_k = [0.0_f32; HEAD_SIZE];
        let key_deformed = &k_deformed[head_base..head_base + HEAD_SIZE];
        let receptance = &r[head_base..head_base + HEAD_SIZE];

        if let Some(state) = state {
            for (row, value) in state_dot_k.iter_mut().enumerate() {
                let row_start = matrix_base + row * HEAD_SIZE;
                *value = dot_product(&state[row_start..row_start + HEAD_SIZE], key_deformed);
            }
        }

        for row in 0..HEAD_SIZE {
            for (column, next) in next_row.iter_mut().enumerate().take(HEAD_SIZE) {
                let channel = head_base + column;
                let matrix_index = matrix_base + row * HEAD_SIZE + column;
                let old = state.map_or(0.0, |state| state[matrix_index]);
                *next = old * w[channel] - state_dot_k[row] * a[channel] * k_deformed[channel]
                    + v[head_base + row] * k[channel];
            }
            out[head_base + row] = dot_product(next_row, receptance);
        }
    }
}

#[cfg(any(target_os = "macos", test))]
#[allow(clippy::too_many_arguments)]
fn single_timestep_query_fast_into(
    r: &[f32],
    k: &[f32],
    v: &[f32],
    w: &[f32],
    a: &[f32],
    k_deformed: &[f32],
    state: Option<&[f32]>,
    out: &mut [f32],
) {
    debug_assert_eq!(out.len(), D_MODEL);
    debug_assert!(
        state.is_none() || state.is_some_and(|state| state.len() == HEADS * HEAD_SIZE * HEAD_SIZE)
    );

    for head in 0..HEADS {
        let head_base = head * HEAD_SIZE;
        let matrix_base = head * HEAD_SIZE * HEAD_SIZE;
        let receptance = &r[head_base..head_base + HEAD_SIZE];
        let key = &k[head_base..head_base + HEAD_SIZE];
        let key_deformed = &k_deformed[head_base..head_base + HEAD_SIZE];
        let key_receptance = dot_product(key, receptance);

        let Some(state) = state else {
            for row in 0..HEAD_SIZE {
                out[head_base + row] = v[head_base + row] * key_receptance;
            }
            continue;
        };

        let mut decayed_receptance = [0.0; HEAD_SIZE];
        let mut adaptation_receptance = 0.0;
        for column in 0..HEAD_SIZE {
            let channel = head_base + column;
            decayed_receptance[column] = w[channel] * receptance[column];
            adaptation_receptance += a[channel] * key_deformed[column] * receptance[column];
        }

        for row in 0..HEAD_SIZE {
            let row_start = matrix_base + row * HEAD_SIZE;
            let state_row = &state[row_start..row_start + HEAD_SIZE];
            let state_dot_key = dot_product(state_row, key_deformed);
            let retained = dot_product(state_row, &decayed_receptance);
            out[head_base + row] = retained - state_dot_key * adaptation_receptance
                + v[head_base + row] * key_receptance;
        }
    }
}

fn normalize_heads_in_place(values: &mut [f32]) {
    for head in 0..HEADS {
        let start = head * HEAD_SIZE;
        let end = start + HEAD_SIZE;
        let norm = values[start..end]
            .iter()
            .map(|value| value * value)
            .sum::<f32>()
            .sqrt()
            .max(1e-12);
        for value in &mut values[start..end] {
            *value /= norm;
        }
    }
}

#[cfg(any(target_os = "macos", test))]
fn scale_heads_in_place(values: &mut [f32], scales: &[f32]) {
    debug_assert_eq!(values.len(), D_MODEL);
    debug_assert_eq!(scales.len(), HEADS);
    for head in 0..HEADS {
        for index in 0..HEAD_SIZE {
            values[head * HEAD_SIZE + index] *= scales[head];
        }
    }
}

fn softmax(input: &[f32]) -> Vec<f32> {
    let max = input
        .iter()
        .copied()
        .fold(f32::NEG_INFINITY, |a, b| a.max(b));
    let mut out = input
        .iter()
        .map(|value| (*value - max).exp())
        .collect::<Vec<_>>();
    let sum = out.iter().sum::<f32>();
    for value in &mut out {
        *value /= sum;
    }
    out
}

fn softmax_into(input: &[f32], out: &mut [f32]) {
    debug_assert_eq!(input.len(), out.len());
    let max = input
        .iter()
        .copied()
        .fold(f32::NEG_INFINITY, |a, b| a.max(b));
    for (output, value) in out.iter_mut().zip(input) {
        *output = (*value - max).exp();
    }
    let sum = out.iter().sum::<f32>();
    for value in out {
        *value /= sum;
    }
}

fn sigmoid_in_place(values: &mut [f32]) {
    for value in values {
        *value = sigmoid(*value);
    }
}

fn sigmoid(value: f32) -> f32 {
    if value >= 0.0 {
        1.0 / (1.0 + (-value).exp())
    } else {
        let exp = value.exp();
        exp / (1.0 + exp)
    }
}

fn softplus(value: f32) -> f32 {
    if value > 20.0 {
        value
    } else if value < -20.0 {
        value.exp()
    } else {
        value.exp().ln_1p()
    }
}

fn silu_in_place(values: &mut [f32]) {
    for value in values {
        *value *= sigmoid(*value);
    }
}

fn relu_in_place(values: &mut [f32]) {
    for value in values {
        *value = value.max(0.0);
    }
}

fn lerp(start: f32, end: f32, weight: f32) -> f32 {
    start + weight * (end - start)
}

#[cfg(test)]
#[derive(Clone, Copy)]
struct StateCompression {
    shift_bits: Option<u8>,
    matrix_rank: usize,
    matrix_bits: Option<u8>,
    power_iterations: usize,
}

#[cfg(test)]
fn compressed_srs_state(state: SrsState, compression: StateCompression) -> SrsState {
    SrsState {
        card: compressed_module_state(state.card, compression),
        deck: compressed_module_state(state.deck, compression),
        note: compressed_module_state(state.note, compression),
        preset: compressed_module_state(state.preset, compression),
        global: compressed_module_state(state.global, compression),
    }
}

#[cfg(test)]
fn compressed_module_state(mut state: ModuleState, compression: StateCompression) -> ModuleState {
    for layer in &mut state.layers {
        if let Some(time) = layer.time.as_mut() {
            if let Some(bits) = compression.shift_bits {
                quantize_dequantize_symmetric(&mut time.x_shift, bits);
            }
            time.matrix = low_rank_matrix_state(&time.matrix, compression);
        }
        if let (Some(bits), Some(channel_shift)) =
            (compression.shift_bits, layer.channel_shift.as_mut())
        {
            quantize_dequantize_symmetric(channel_shift, bits);
        }
    }
    state
}

#[cfg(test)]
fn low_rank_matrix_state(matrix: &[f32], compression: StateCompression) -> Vec<f32> {
    debug_assert_eq!(matrix.len(), HEADS * HEAD_SIZE * HEAD_SIZE);
    let mut out = vec![0.0; matrix.len()];
    for head in 0..HEADS {
        let start = head * HEAD_SIZE * HEAD_SIZE;
        let end = start + HEAD_SIZE * HEAD_SIZE;
        low_rank_head_matrix(
            &matrix[start..end],
            &mut out[start..end],
            compression.matrix_rank,
            compression.matrix_bits,
            compression.power_iterations,
        );
    }
    out
}

#[cfg(test)]
fn low_rank_head_matrix(
    matrix: &[f32],
    out: &mut [f32],
    rank: usize,
    bits: Option<u8>,
    power_iterations: usize,
) {
    debug_assert_eq!(matrix.len(), HEAD_SIZE * HEAD_SIZE);
    debug_assert_eq!(out.len(), HEAD_SIZE * HEAD_SIZE);
    if rank == 0 {
        return;
    }

    let mut residual = matrix.to_vec();
    let mut left = vec![0.0; HEAD_SIZE];
    let mut right = vec![0.0; HEAD_SIZE];
    let mut next_left = vec![0.0; HEAD_SIZE];

    for component in 0..rank {
        initialize_power_vector(&residual, &mut left, component);
        for _ in 0..power_iterations {
            multiply_transpose_matrix_vector(&residual, &left, &mut right);
            let right_norm = normalize_vector(&mut right);
            if right_norm <= 1e-12 {
                break;
            }
            multiply_matrix_vector(&residual, &right, &mut next_left);
            let left_norm = normalize_vector(&mut next_left);
            if left_norm <= 1e-12 {
                break;
            }
            left.copy_from_slice(&next_left);
        }

        multiply_transpose_matrix_vector(&residual, &left, &mut right);
        let sigma = normalize_vector(&mut right);
        if sigma <= 1e-12 || !sigma.is_finite() {
            break;
        }

        let root = sigma.sqrt();
        let mut left_factor = left.iter().map(|value| value * root).collect::<Vec<_>>();
        let mut right_factor = right.iter().map(|value| value * root).collect::<Vec<_>>();
        if let Some(bits) = bits {
            quantize_dequantize_symmetric(&mut left_factor, bits);
            quantize_dequantize_symmetric(&mut right_factor, bits);
        }

        for (row, &left) in left_factor.iter().enumerate() {
            for (column, &right) in right_factor.iter().enumerate() {
                let index = row * HEAD_SIZE + column;
                let value = left * right;
                out[index] += value;
                residual[index] -= value;
            }
        }
    }
}

#[cfg(test)]
fn initialize_power_vector(matrix: &[f32], out: &mut [f32], component: usize) {
    for (row, out) in out.iter_mut().enumerate().take(HEAD_SIZE) {
        let row_start = row * HEAD_SIZE;
        let row_sum = matrix[row_start..row_start + HEAD_SIZE]
            .iter()
            .copied()
            .sum::<f32>();
        *out = row_sum + 0.001 * ((row + component + 1) as f32);
    }
    if normalize_vector(out) <= 1e-12 {
        out.fill(0.0);
        out[component % HEAD_SIZE] = 1.0;
    }
}

#[cfg(test)]
fn multiply_matrix_vector(matrix: &[f32], vector: &[f32], out: &mut [f32]) {
    for (row, out) in out.iter_mut().enumerate().take(HEAD_SIZE) {
        let row_start = row * HEAD_SIZE;
        *out = matrix[row_start..row_start + HEAD_SIZE]
            .iter()
            .zip(vector)
            .map(|(left, right)| left * right)
            .sum();
    }
}

#[cfg(test)]
fn multiply_transpose_matrix_vector(matrix: &[f32], vector: &[f32], out: &mut [f32]) {
    out.fill(0.0);
    for (row, &value) in vector.iter().enumerate().take(HEAD_SIZE) {
        let row_start = row * HEAD_SIZE;
        for (column, out) in out.iter_mut().enumerate().take(HEAD_SIZE) {
            *out += matrix[row_start + column] * value;
        }
    }
}

#[cfg(test)]
fn normalize_vector(values: &mut [f32]) -> f32 {
    let norm = values.iter().map(|value| value * value).sum::<f32>().sqrt();
    if norm > 0.0 && norm.is_finite() {
        for value in values {
            *value /= norm;
        }
    }
    norm
}

#[cfg(test)]
fn quantize_dequantize_symmetric(values: &mut [f32], bits: u8) {
    let qmax = (1_i32 << (bits.saturating_sub(1) as u32)) - 1;
    if qmax <= 0 {
        values.fill(0.0);
        return;
    }
    let amax = values.iter().copied().map(f32::abs).fold(0.0, f32::max);
    if amax == 0.0 || !amax.is_finite() {
        values.fill(0.0);
        return;
    }
    let scale = amax / qmax as f32;
    for value in values {
        let quantized = (*value / scale).round().clamp(-(qmax as f32), qmax as f32);
        *value = quantized * scale;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::env;
    use std::path::PathBuf;
    use std::time::Instant;

    use rusqlite::Connection;

    struct TimeMixerAffineTransform {
        matrix: Vec<f32>,
        bias: Vec<f32>,
    }

    struct TimeMixerStateAffineTransform {
        matrix_by_head: Vec<f32>,
        bias: Vec<f32>,
    }

    fn deterministic_values(len: usize, seed: f32) -> Vec<f32> {
        (0..len)
            .map(|index| {
                let value = index as f32 + 1.0;
                (value * seed).sin() * 0.25 + (value * seed * 0.37).cos() * 0.1
            })
            .collect()
    }

    fn deterministic_step_vectors(step: usize) -> RwkvScanCapturedStep {
        let offset = step as f32 * 0.001;
        RwkvScanCapturedStep {
            r: deterministic_values(D_MODEL, 0.013 + offset),
            k: deterministic_values(D_MODEL, 0.017 + offset),
            v: deterministic_values(D_MODEL, 0.019 + offset),
            w: deterministic_values(D_MODEL, 0.023 + offset),
            a: deterministic_values(D_MODEL, 0.029 + offset),
            k_deformed: deterministic_values(D_MODEL, 0.031 + offset),
        }
    }

    fn time_mixer_transform_for_head_row(
        head: usize,
        row: usize,
        k: &[f32],
        v: &[f32],
        w: &[f32],
        a: &[f32],
        k_deformed: &[f32],
    ) -> TimeMixerAffineTransform {
        let head_base = head * HEAD_SIZE;
        let mut matrix = vec![0.0; HEAD_SIZE * HEAD_SIZE];
        let mut bias = vec![0.0; HEAD_SIZE];

        for input_column in 0..HEAD_SIZE {
            for output_column in 0..HEAD_SIZE {
                let output_channel = head_base + output_column;
                matrix[input_column * HEAD_SIZE + output_column] =
                    if input_column == output_column {
                        w[output_channel]
                    } else {
                        0.0
                    } - k_deformed[head_base + input_column]
                        * a[output_channel]
                        * k_deformed[output_channel];
            }
        }

        for output_column in 0..HEAD_SIZE {
            bias[output_column] = v[head_base + row] * k[head_base + output_column];
        }

        TimeMixerAffineTransform { matrix, bias }
    }

    fn time_mixer_state_transform(
        k: &[f32],
        v: &[f32],
        w: &[f32],
        a: &[f32],
        k_deformed: &[f32],
    ) -> TimeMixerStateAffineTransform {
        let mut matrix_by_head = vec![0.0; HEADS * HEAD_SIZE * HEAD_SIZE];
        let mut bias = vec![0.0; HEADS * HEAD_SIZE * HEAD_SIZE];

        for head in 0..HEADS {
            let head_base = head * HEAD_SIZE;
            let matrix_base = head * HEAD_SIZE * HEAD_SIZE;
            for input_column in 0..HEAD_SIZE {
                for output_column in 0..HEAD_SIZE {
                    let output_channel = head_base + output_column;
                    matrix_by_head[matrix_base + input_column * HEAD_SIZE + output_column] =
                        if input_column == output_column {
                            w[output_channel]
                        } else {
                            0.0
                        } - k_deformed[head_base + input_column]
                            * a[output_channel]
                            * k_deformed[output_channel];
                }
            }

            for row in 0..HEAD_SIZE {
                let row_start = matrix_base + row * HEAD_SIZE;
                for output_column in 0..HEAD_SIZE {
                    bias[row_start + output_column] =
                        v[head_base + row] * k[head_base + output_column];
                }
            }
        }

        TimeMixerStateAffineTransform {
            matrix_by_head,
            bias,
        }
    }

    fn compose_affine_transforms(
        first: &TimeMixerAffineTransform,
        second: &TimeMixerAffineTransform,
    ) -> TimeMixerAffineTransform {
        let mut matrix = vec![0.0; HEAD_SIZE * HEAD_SIZE];
        let mut bias = vec![0.0; HEAD_SIZE];

        for input_column in 0..HEAD_SIZE {
            for output_column in 0..HEAD_SIZE {
                matrix[input_column * HEAD_SIZE + output_column] = (0..HEAD_SIZE)
                    .map(|middle| {
                        first.matrix[input_column * HEAD_SIZE + middle]
                            * second.matrix[middle * HEAD_SIZE + output_column]
                    })
                    .sum();
            }
        }

        for (output_column, value) in bias.iter_mut().enumerate() {
            *value = second.bias[output_column]
                + (0..HEAD_SIZE)
                    .map(|middle| {
                        first.bias[middle] * second.matrix[middle * HEAD_SIZE + output_column]
                    })
                    .sum::<f32>();
        }

        TimeMixerAffineTransform { matrix, bias }
    }

    fn compose_time_mixer_state_transforms(
        first: &TimeMixerStateAffineTransform,
        second: &TimeMixerStateAffineTransform,
    ) -> TimeMixerStateAffineTransform {
        let mut matrix_by_head = vec![0.0; HEADS * HEAD_SIZE * HEAD_SIZE];
        let mut bias = vec![0.0; HEADS * HEAD_SIZE * HEAD_SIZE];

        for head in 0..HEADS {
            let matrix_base = head * HEAD_SIZE * HEAD_SIZE;
            for input_column in 0..HEAD_SIZE {
                for output_column in 0..HEAD_SIZE {
                    matrix_by_head[matrix_base + input_column * HEAD_SIZE + output_column] = (0
                        ..HEAD_SIZE)
                        .map(|middle| {
                            first.matrix_by_head[matrix_base + input_column * HEAD_SIZE + middle]
                                * second.matrix_by_head
                                    [matrix_base + middle * HEAD_SIZE + output_column]
                        })
                        .sum();
                }
            }

            for row in 0..HEAD_SIZE {
                let row_start = matrix_base + row * HEAD_SIZE;
                for output_column in 0..HEAD_SIZE {
                    bias[row_start + output_column] = second.bias[row_start + output_column]
                        + (0..HEAD_SIZE)
                            .map(|middle| {
                                first.bias[row_start + middle]
                                    * second.matrix_by_head
                                        [matrix_base + middle * HEAD_SIZE + output_column]
                            })
                            .sum::<f32>();
                }
            }
        }

        TimeMixerStateAffineTransform {
            matrix_by_head,
            bias,
        }
    }

    fn compose_time_mixer_step_range(
        steps: &[RwkvScanCapturedStep],
        start: usize,
        end: usize,
    ) -> TimeMixerStateAffineTransform {
        assert!(start < end);
        let first = &steps[start];
        let mut transform =
            time_mixer_state_transform(&first.k, &first.v, &first.w, &first.a, &first.k_deformed);
        for step in &steps[start + 1..end] {
            let next =
                time_mixer_state_transform(&step.k, &step.v, &step.w, &step.a, &step.k_deformed);
            transform = compose_time_mixer_state_transforms(&transform, &next);
        }

        transform
    }

    fn chunked_time_mixer_state_transform(
        steps: &[RwkvScanCapturedStep],
        chunk_size: usize,
    ) -> TimeMixerStateAffineTransform {
        assert!(!steps.is_empty());
        assert!(chunk_size > 0);
        let ranges = (0..steps.len())
            .step_by(chunk_size)
            .map(|start| {
                let end = (start + chunk_size).min(steps.len());
                (start, end)
            })
            .collect::<Vec<_>>();
        let chunk_transforms = std::thread::scope(|scope| {
            let handles = ranges
                .iter()
                .map(|&(start, end)| {
                    scope.spawn(move || compose_time_mixer_step_range(steps, start, end))
                })
                .collect::<Vec<_>>();

            handles
                .into_iter()
                .map(|handle| handle.join().unwrap())
                .collect::<Vec<_>>()
        });

        let mut transforms = chunk_transforms.into_iter();
        let mut transform = transforms.next().unwrap();
        for next in transforms {
            transform = compose_time_mixer_state_transforms(&transform, &next);
        }

        transform
    }

    fn apply_affine_transform(row_state: &[f32], transform: &TimeMixerAffineTransform) -> Vec<f32> {
        (0..HEAD_SIZE)
            .map(|output_column| {
                transform.bias[output_column]
                    + (0..HEAD_SIZE)
                        .map(|input_column| {
                            row_state[input_column]
                                * transform.matrix[input_column * HEAD_SIZE + output_column]
                        })
                        .sum::<f32>()
            })
            .collect()
    }

    fn apply_time_mixer_state_transform(
        state: &[f32],
        transform: &TimeMixerStateAffineTransform,
    ) -> Vec<f32> {
        let mut output = vec![0.0; HEADS * HEAD_SIZE * HEAD_SIZE];

        for head in 0..HEADS {
            let matrix_base = head * HEAD_SIZE * HEAD_SIZE;
            for row in 0..HEAD_SIZE {
                let row_start = matrix_base + row * HEAD_SIZE;
                for output_column in 0..HEAD_SIZE {
                    output[row_start + output_column] = transform.bias[row_start + output_column]
                        + (0..HEAD_SIZE)
                            .map(|input_column| {
                                state[row_start + input_column]
                                    * transform.matrix_by_head
                                        [matrix_base + input_column * HEAD_SIZE + output_column]
                            })
                            .sum::<f32>();
                }
            }
        }

        output
    }

    fn assert_close(left: &[f32], right: &[f32], tolerance: f32) {
        assert_eq!(left.len(), right.len());
        for (index, (left, right)) in left.iter().zip(right).enumerate() {
            let diff = (left - right).abs();
            assert!(
                diff <= tolerance,
                "values differ at {index}: left={left}, right={right}, diff={diff}, tolerance={tolerance}"
            );
        }
    }

    #[cfg(target_arch = "x86_64")]
    fn deterministic_simd_values(len: usize, period: usize, scale: f32) -> Vec<f32> {
        (0..len)
            .map(|index| {
                let centered = index as i32 % period as i32 - period as i32 / 2;
                centered as f32 * scale
            })
            .collect()
    }

    #[cfg(target_arch = "x86_64")]
    #[test]
    fn add_scaled_in_place_avx2_fma_matches_scalar() {
        if !x86_avx2_fma_available() {
            eprintln!("skipping: AVX2/FMA not available");
            return;
        }

        let mut scalar = deterministic_simd_values(101, 17, 0.03125);
        let mut simd = scalar.clone();
        let weights = deterministic_simd_values(101, 23, -0.015625);

        add_scaled_in_place_scalar(&mut scalar, &weights, 0.75);
        // SAFETY: the runtime feature check above confirms AVX2/FMA support.
        unsafe { add_scaled_in_place_avx2_fma(&mut simd, &weights, 0.75) };

        assert_close(&simd, &scalar, 1e-6);
    }

    #[cfg(target_arch = "x86_64")]
    #[test]
    fn dot_product_avx2_fma_matches_scalar() {
        if !x86_avx2_fma_available() {
            eprintln!("skipping: AVX2/FMA not available");
            return;
        }

        let left = deterministic_simd_values(101, 19, 0.046875);
        let right = deterministic_simd_values(101, 29, -0.0234375);

        let scalar = dot_product_scalar(&left, &right);
        // SAFETY: the runtime feature check above confirms AVX2/FMA support.
        let simd = unsafe { dot_product_avx2_fma(&left, &right) };

        assert!(
            (simd - scalar).abs() <= 1e-5,
            "simd={simd}, scalar={scalar}, diff={}",
            (simd - scalar).abs()
        );
    }

    fn sequential_time_mixer_state(steps: &[RwkvScanCapturedStep]) -> Vec<f32> {
        let mut state = vec![0.0; HEADS * HEAD_SIZE * HEAD_SIZE];
        for step in steps {
            let (_, next_state) = single_timestep(
                &step.r,
                &step.k,
                &step.v,
                &step.w,
                &step.a,
                &step.k_deformed,
                &state,
            );
            state = next_state;
        }

        state
    }

    fn scan_reduce_time_mixer_state(
        steps: &[RwkvScanCapturedStep],
        bulk_size: usize,
        leaf_size: usize,
    ) -> Vec<f32> {
        assert!(bulk_size > 0);
        assert!(leaf_size > 0);
        let mut state = vec![0.0; HEADS * HEAD_SIZE * HEAD_SIZE];

        for bulk in steps.chunks(bulk_size) {
            let leaf_transforms = bulk
                .par_chunks(leaf_size)
                .map(|chunk| compose_time_mixer_step_range(chunk, 0, chunk.len()))
                .collect::<Vec<_>>();
            let mut leaf_transforms = leaf_transforms.into_iter();
            let Some(mut bulk_transform) = leaf_transforms.next() else {
                continue;
            };
            for next in leaf_transforms {
                bulk_transform = compose_time_mixer_state_transforms(&bulk_transform, &next);
            }
            state = apply_time_mixer_state_transform(&state, &bulk_transform);
        }

        state
    }

    fn max_abs_diff(left: &[f32], right: &[f32]) -> f32 {
        left.iter()
            .zip(right)
            .map(|(left, right)| (left - right).abs())
            .fold(0.0, f32::max)
    }

    fn parse_scan_bench_sizes(value: &str) -> Vec<usize> {
        value
            .split(',')
            .filter_map(|value| value.trim().parse::<usize>().ok())
            .filter(|value| *value > 0)
            .collect()
    }

    fn auto_scan_bench_sizes(
        step_count: usize,
        memory_budget_bytes: u64,
        bytes_per_review_all_layers: usize,
    ) -> Vec<usize> {
        let max_by_memory = (memory_budget_bytes / bytes_per_review_all_layers as u64)
            .max(1)
            .try_into()
            .unwrap_or(usize::MAX);
        let max_size = step_count.min(max_by_memory).max(1);
        let mut sizes = Vec::new();
        let mut size = 64_usize;
        while size < max_size {
            sizes.push(size);
            size = size.saturating_mul(2);
        }
        sizes.push(max_size);
        sizes.sort_unstable();
        sizes.dedup();
        sizes
    }

    fn scan_bench_env_usize(name: &str, default: usize) -> usize {
        std::env::var(name)
            .ok()
            .and_then(|value| value.parse().ok())
            .unwrap_or(default)
    }

    fn scan_bench_env_u64(name: &str, default: u64) -> u64 {
        std::env::var(name)
            .ok()
            .and_then(|value| value.parse().ok())
            .unwrap_or(default)
    }

    #[derive(Clone, Copy)]
    struct ScanBenchTiming {
        days_elapsed: i64,
        next_day_at: i64,
    }

    fn scan_bench_timing() -> ScanBenchTiming {
        let now_secs = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs() as i64;
        let days_elapsed = now_secs / SECONDS_PER_DAY;
        ScanBenchTiming {
            days_elapsed,
            next_day_at: (days_elapsed + 1) * SECONDS_PER_DAY,
        }
    }

    struct ScanBenchHistoricalReviewRow {
        review_id: i64,
        card_id: i64,
        note_id: i64,
        deck_id: i64,
        ease: i64,
        duration_millis: i64,
        review_kind: i64,
    }

    fn collection_reviews_for_scan_bench(
        path: &std::path::Path,
        limit: usize,
        deck_id: Option<i64>,
    ) -> rusqlite::Result<Vec<ReviewInput>> {
        let uri = format!("file:{}?mode=ro&immutable=1", path.to_string_lossy());
        let db = rusqlite::Connection::open_with_flags(
            uri,
            rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY | rusqlite::OpenFlags::SQLITE_OPEN_URI,
        )?;
        let current_deck_id_sql = scan_bench_current_deck_id_sql(&db)?;
        let limit_clause = if limit == 0 {
            String::new()
        } else {
            format!(" limit {limit}")
        };
        let deck_clause = deck_id.map_or(String::new(), |deck_id| {
            format!(" and {current_deck_id_sql} = {deck_id}")
        });
        let sql = format!(
            "
select
  r.id,
  r.cid,
  c.nid,
  {current_deck_id_sql},
  r.ease,
  r.time,
  r.type
from revlog r
join cards c on c.id = r.cid
where r.ease between 1 and 4
  and (r.type in (0, 1, 2, 3) or r.type = 4)
  {deck_clause}
order by r.id, r.cid
{limit_clause}"
        );
        let timing = scan_bench_timing();
        let mut stmt = db.prepare(&sql)?;
        let rows = stmt.query_map([], |row| {
            Ok(ScanBenchHistoricalReviewRow {
                review_id: row.get(0)?,
                card_id: row.get(1)?,
                note_id: row.get(2)?,
                deck_id: row.get(3)?,
                ease: row.get(4)?,
                duration_millis: row.get(5)?,
                review_kind: row.get(6)?,
            })
        })?;

        let mut previous_review_id_by_card = HashMap::new();
        let mut reviews = Vec::new();
        for row in rows {
            let row = row?;
            let previous_review_id = previous_review_id_by_card.insert(row.card_id, row.review_id);
            let elapsed_seconds = previous_review_id
                .map_or(-1, |previous| ((row.review_id - previous) / 1000).max(0));
            let elapsed_days = if elapsed_seconds >= 0 {
                elapsed_seconds / SECONDS_PER_DAY
            } else {
                -1
            };
            reviews.push(ReviewInput {
                card_id: row.card_id,
                note_id: Some(row.note_id),
                deck_id: Some(row.deck_id),
                preset_id: Some(row.deck_id),
                is_query: false,
                ease: Some(row.ease as u8),
                duration_millis: Some(row.duration_millis),
                card_type: Some(scan_bench_historical_card_type(row.review_kind)),
                day_offset: Some(scan_bench_historical_day_offset(row.review_id, &timing)),
                current_elapsed_days: Some(elapsed_days),
                current_elapsed_seconds: Some(elapsed_seconds),
                target_retentions: [Some(0.9), Some(0.9), Some(0.9), Some(0.9)],
            });
        }

        Ok(reviews)
    }

    fn scan_bench_current_deck_id_sql(db: &rusqlite::Connection) -> rusqlite::Result<&'static str> {
        Ok(if scan_bench_table_has_column(db, "cards", "odid")? {
            "case when c.odid != 0 then c.odid else c.did end"
        } else {
            "c.did"
        })
    }

    fn scan_bench_table_has_column(
        db: &rusqlite::Connection,
        table: &str,
        column: &str,
    ) -> rusqlite::Result<bool> {
        let mut stmt = db.prepare(&format!("pragma table_info({table})"))?;
        let columns = stmt.query_map([], |row| row.get::<_, String>(1))?;
        for name in columns {
            if name? == column {
                return Ok(true);
            }
        }
        Ok(false)
    }

    fn scan_bench_historical_card_type(review_kind: i64) -> i64 {
        match review_kind {
            0 => 1,
            2 => 3,
            _ => 2,
        }
    }

    fn scan_bench_historical_day_offset(review_id: i64, timing: &ScanBenchTiming) -> i64 {
        let review_secs = review_id / 1000;
        let days_before_today = (timing.next_day_at - 1 - review_secs).max(0) / SECONDS_PER_DAY;
        (timing.days_elapsed - days_before_today).max(0)
    }

    fn scan_bench_global_layer_matrix(
        inference: &RwkvInference,
        layer_id: usize,
    ) -> Option<&[f32]> {
        inference
            .warm_up_states
            .global
            .as_ref()?
            .layers
            .get(layer_id)?
            .time
            .as_ref()
            .map(|state| state.matrix.as_slice())
    }

    fn write_scan_bench_capture(
        path: &std::path::Path,
        steps: &[RwkvScanCapturedStep],
        final_state: &[f32],
    ) -> std::io::Result<()> {
        let mut file = std::io::BufWriter::new(std::fs::File::create(path)?);
        std::io::Write::write_all(&mut file, b"ARWKVSCAN1")?;
        std::io::Write::write_all(&mut file, &(D_MODEL as u32).to_le_bytes())?;
        std::io::Write::write_all(&mut file, &(HEADS as u32).to_le_bytes())?;
        std::io::Write::write_all(&mut file, &(HEAD_SIZE as u32).to_le_bytes())?;
        std::io::Write::write_all(&mut file, &(steps.len() as u64).to_le_bytes())?;
        for step in steps {
            for values in [
                &step.r,
                &step.k,
                &step.v,
                &step.w,
                &step.a,
                &step.k_deformed,
            ] {
                for value in values {
                    std::io::Write::write_all(&mut file, &value.to_le_bytes())?;
                }
            }
        }
        std::io::Write::write_all(&mut file, &(final_state.len() as u32).to_le_bytes())?;
        for value in final_state {
            std::io::Write::write_all(&mut file, &value.to_le_bytes())?;
        }
        Ok(())
    }

    #[test]
    #[ignore]
    fn rwkv_bulk_collection_benchmark() {
        let Ok(collection_path) = std::env::var("ANKI_RWKV_BULK_BENCH_COLLECTION")
            .or_else(|_| std::env::var("ANKI_RWKV_SCAN_BENCH_COLLECTION"))
        else {
            eprintln!(
                "set ANKI_RWKV_BULK_BENCH_COLLECTION to a copied collection.anki2 path to run this benchmark"
            );
            return;
        };
        let weights_path = std::env::var("ANKI_RWKV_BULK_BENCH_MODEL")
            .or_else(|_| std::env::var("ANKI_RWKV_SCAN_BENCH_MODEL"))
            .unwrap_or_else(|_| "qt/aqt/rwkv_inference/RWKV_trained_on_5000_10000.bin".to_string());
        let review_limit = scan_bench_env_usize("ANKI_RWKV_BULK_BENCH_LIMIT", 0);
        let deck_id = std::env::var("ANKI_RWKV_BULK_BENCH_DECK_ID")
            .or_else(|_| std::env::var("ANKI_RWKV_SCAN_BENCH_DECK_ID"))
            .ok()
            .and_then(|value| value.parse().ok());
        let chunk_rows = std::env::var("ANKI_RWKV_BULK_BENCH_CHUNK_ROWS")
            .ok()
            .and_then(|value| value.parse::<usize>().ok());
        let record_predictions = std::env::var("ANKI_RWKV_BULK_BENCH_RECORD_PREDICTIONS")
            .is_ok_and(|value| matches!(value.as_str(), "1" | "true" | "TRUE" | "yes" | "YES"));
        let profile = std::env::var("ANKI_RWKV_BULK_BENCH_PROFILE").map_or(true, |value| {
            !matches!(value.as_str(), "0" | "false" | "FALSE" | "no" | "NO")
        });

        let collection_path = std::path::PathBuf::from(collection_path);
        let weights_path = std::path::PathBuf::from(weights_path);
        let load_reviews_started = std::time::Instant::now();
        let reviews = collection_reviews_for_scan_bench(&collection_path, review_limit, deck_id)
            .expect("collection review load failed");
        let load_reviews_ms = load_reviews_started.elapsed().as_secs_f64() * 1000.0;
        assert!(
            !reviews.is_empty(),
            "collection review load returned no rows"
        );

        let mut inference =
            RwkvInference::load(weights_path.clone(), 0.9, 36_500).expect("RWKV load failed");
        if profile {
            start_rwkv_single_timestep_profile();
            start_rwkv_warmup_profile();
        }
        let warmup_started = std::time::Instant::now();
        let predictions = if let Some(chunk_rows) = chunk_rows {
            bulk::warm_up_reviews_bulk_chunked(
                &mut inference,
                reviews.clone(),
                record_predictions,
                chunk_rows,
            )
        } else {
            inference.warm_up_reviews(reviews.clone(), record_predictions)
        }
        .expect("RWKV warm-up failed");
        let warmup_ms = warmup_started.elapsed().as_secs_f64() * 1000.0;
        let warmup_profile = profile.then(stop_rwkv_warmup_profile);
        let single_timestep_profile = profile.then(stop_rwkv_single_timestep_profile);

        println!("collection={}", collection_path.display());
        println!("weights={}", weights_path.display());
        println!("collection_reviews={}", reviews.len());
        println!("record_predictions={record_predictions}");
        println!("profile={profile}");
        if let Some(chunk_rows) = chunk_rows {
            println!("chunk_rows={chunk_rows}");
        }
        println!("predictions={}", predictions.len());
        println!("load_reviews_ms={load_reviews_ms:.3}");
        println!("warmup_ms={warmup_ms:.3}");
        if let Ok(threads) = std::env::var("RAYON_NUM_THREADS") {
            println!("rayon_num_threads={threads}");
        }
        if let Some(warmup_profile) = warmup_profile {
            for (name, calls, nanos) in warmup_profile {
                let ms = nanos as f64 / 1_000_000.0;
                println!("warmup_profile_{name}_calls={calls}");
                println!("warmup_profile_{name}_ms={ms:.3}");
                println!(
                    "warmup_profile_{name}_fraction={:.6}",
                    ms / warmup_ms.max(f64::MIN_POSITIVE)
                );
            }
        }
        if let Some((calls, nanos)) = single_timestep_profile {
            println!("single_timestep_calls={calls}");
            println!("single_timestep_ms={:.3}", nanos as f64 / 1_000_000.0);
            println!(
                "single_timestep_warmup_fraction={:.6}",
                (nanos as f64 / 1_000_000.0) / warmup_ms.max(f64::MIN_POSITIVE)
            );
        }
    }

    #[test]
    #[ignore]
    fn rwkv_scan_collection_benchmark() {
        let Ok(collection_path) = std::env::var("ANKI_RWKV_SCAN_BENCH_COLLECTION") else {
            eprintln!(
                "set ANKI_RWKV_SCAN_BENCH_COLLECTION to a copied collection.anki2 path to run this benchmark"
            );
            return;
        };
        let weights_path = std::env::var("ANKI_RWKV_SCAN_BENCH_MODEL")
            .unwrap_or_else(|_| "qt/aqt/rwkv_inference/RWKV_trained_on_5000_10000.bin".to_string());
        let review_limit = scan_bench_env_usize("ANKI_RWKV_SCAN_BENCH_LIMIT", 0);
        let leaf_size = scan_bench_env_usize("ANKI_RWKV_SCAN_BENCH_LEAF_SIZE", 64);
        let layer_id = scan_bench_env_usize("ANKI_RWKV_SCAN_BENCH_LAYER_ID", MODULE_LAYERS[4] - 1);
        let deck_id = std::env::var("ANKI_RWKV_SCAN_BENCH_DECK_ID")
            .ok()
            .and_then(|value| value.parse().ok());
        let memory_budget_bytes = scan_bench_env_u64(
            "ANKI_RWKV_SCAN_BENCH_MEMORY_BUDGET_BYTES",
            16 * 1024 * 1024 * 1024,
        );
        let extend_to_memory_budget = std::env::var("ANKI_RWKV_SCAN_BENCH_EXTEND_TO_MEMORY_BUDGET")
            .is_ok_and(|value| matches!(value.as_str(), "1" | "true" | "TRUE" | "yes" | "YES"));
        let profile_single_timestep = std::env::var("ANKI_RWKV_SCAN_BENCH_PROFILE_SINGLE_TIMESTEP")
            .is_ok_and(|value| matches!(value.as_str(), "1" | "true" | "TRUE" | "yes" | "YES"));
        let profile_warmup = std::env::var("ANKI_RWKV_SCAN_BENCH_PROFILE_WARMUP")
            .is_ok_and(|value| matches!(value.as_str(), "1" | "true" | "TRUE" | "yes" | "YES"));
        let selected_layer_step_bytes = 6 * D_MODEL * std::mem::size_of::<f32>();
        let all_layer_step_bytes = selected_layer_step_bytes * MODULE_LAYERS.iter().sum::<usize>();

        let collection_path = std::path::PathBuf::from(collection_path);
        let weights_path = std::path::PathBuf::from(weights_path);
        let load_reviews_started = std::time::Instant::now();
        let reviews = collection_reviews_for_scan_bench(&collection_path, review_limit, deck_id)
            .expect("collection review load failed");
        let load_reviews_ms = load_reviews_started.elapsed().as_secs_f64() * 1000.0;
        assert!(
            !reviews.is_empty(),
            "collection review load returned no rows"
        );
        assert!(
            layer_id < MODULE_LAYERS[4],
            "global layer id is out of range"
        );

        let mut inference =
            RwkvInference::load(weights_path.clone(), 0.9, 36_500).expect("RWKV load failed");
        start_rwkv_scan_capture(4, layer_id);
        if profile_single_timestep {
            start_rwkv_single_timestep_profile();
        }
        if profile_warmup {
            start_rwkv_warmup_profile();
        }
        let warmup_started = std::time::Instant::now();
        inference
            .warm_up_reviews(reviews.clone(), false)
            .expect("RWKV warm-up failed");
        let warmup_ms = warmup_started.elapsed().as_secs_f64() * 1000.0;
        let warmup_profile = if profile_warmup {
            Some(stop_rwkv_warmup_profile())
        } else {
            None
        };
        let single_timestep_profile = if profile_single_timestep {
            Some(stop_rwkv_single_timestep_profile())
        } else {
            None
        };
        let mut steps = take_rwkv_scan_capture();
        assert_eq!(steps.len(), reviews.len());
        let runtime_matrix = scan_bench_global_layer_matrix(&inference, layer_id)
            .expect("captured global layer state is missing")
            .to_vec();
        let captured_steps = steps.len();
        let capture_state = sequential_time_mixer_state(&steps);
        let capture_error = max_abs_diff(&capture_state, &runtime_matrix);
        if let Ok(capture_path) = std::env::var("ANKI_RWKV_SCAN_BENCH_CAPTURE_OUTPUT") {
            write_scan_bench_capture(std::path::Path::new(&capture_path), &steps, &capture_state)
                .expect("failed to write RWKV scan capture");
            println!("capture_output={capture_path}");
        }

        if extend_to_memory_budget {
            let target_steps = (memory_budget_bytes / all_layer_step_bytes as u64)
                .max(1)
                .try_into()
                .unwrap_or(usize::MAX);
            if steps.len() < target_steps {
                steps.reserve(target_steps - steps.len());
                for index in steps.len()..target_steps {
                    steps.push(steps[index % captured_steps].clone());
                }
            }
        }

        let sequential_started = std::time::Instant::now();
        let sequential_state = sequential_time_mixer_state(&steps);
        let sequential_ms = sequential_started.elapsed().as_secs_f64() * 1000.0;

        let bulk_sizes = std::env::var("ANKI_RWKV_SCAN_BENCH_BULK_SIZES")
            .ok()
            .map(|value| parse_scan_bench_sizes(&value))
            .filter(|sizes| !sizes.is_empty())
            .unwrap_or_else(|| {
                auto_scan_bench_sizes(steps.len(), memory_budget_bytes, all_layer_step_bytes)
            });

        let mut csv_rows = vec![
            "bulk_size,selected_layer_memory_mb,all_layer_memory_mb,scan_ms,speedup_vs_sequential,max_abs_error"
                .to_string(),
        ];

        println!("collection={}", collection_path.display());
        println!("weights={}", weights_path.display());
        println!("collection_reviews={captured_steps}");
        println!("benchmark_steps={}", steps.len());
        println!("extended_to_memory_budget={extend_to_memory_budget}");
        println!("captured_global_module_layer={layer_id}");
        println!("load_reviews_ms={load_reviews_ms:.3}");
        println!("warmup_ms={warmup_ms:.3}");
        if let Some(profile) = &warmup_profile {
            for (name, calls, nanos) in profile {
                let ms = *nanos as f64 / 1_000_000.0;
                println!("warmup_profile_{name}_calls={calls}");
                println!("warmup_profile_{name}_ms={ms:.3}");
                println!(
                    "warmup_profile_{name}_fraction={:.6}",
                    ms / warmup_ms.max(f64::MIN_POSITIVE)
                );
            }
        }
        if let Some((calls, nanos)) = single_timestep_profile {
            println!("single_timestep_calls={calls}");
            println!("single_timestep_ms={:.3}", nanos as f64 / 1_000_000.0);
            println!(
                "single_timestep_warmup_fraction={:.6}",
                (nanos as f64 / 1_000_000.0) / warmup_ms.max(f64::MIN_POSITIVE)
            );
        }
        println!("sequential_single_timestep_ms={sequential_ms:.3}");
        println!("capture_vs_runtime_max_abs_error={capture_error:.9}");
        println!("leaf_size={leaf_size}");
        println!("memory_budget_bytes={memory_budget_bytes}");
        println!("bulk_size,selected_layer_memory_mb,all_layer_memory_mb,scan_ms,speedup_vs_sequential,max_abs_error");

        for bulk_size in bulk_sizes {
            let started = std::time::Instant::now();
            let state = scan_reduce_time_mixer_state(&steps, bulk_size, leaf_size);
            let scan_ms = started.elapsed().as_secs_f64() * 1000.0;
            let error = max_abs_diff(&state, &sequential_state);
            let speedup = sequential_ms / scan_ms.max(f64::MIN_POSITIVE);
            let selected_layer_memory_mb =
                bulk_size as f64 * selected_layer_step_bytes as f64 / (1024.0 * 1024.0);
            let all_layer_memory_mb =
                bulk_size as f64 * all_layer_step_bytes as f64 / (1024.0 * 1024.0);
            let row = format!(
                "{bulk_size},{selected_layer_memory_mb:.3},{all_layer_memory_mb:.3},{scan_ms:.3},{speedup:.6},{error:.9}"
            );
            println!("{row}");
            csv_rows.push(row);
            std::hint::black_box(state);
        }

        if let Ok(output_path) = std::env::var("ANKI_RWKV_SCAN_BENCH_OUTPUT") {
            std::fs::write(&output_path, csv_rows.join("\n"))
                .expect("failed to write RWKV scan benchmark CSV");
            println!("output={output_path}");
        }
    }

    #[test]
    fn single_timestep_state_update_supports_affine_composition() {
        let r1 = deterministic_values(D_MODEL, 0.013);
        let k1 = deterministic_values(D_MODEL, 0.017);
        let v1 = deterministic_values(D_MODEL, 0.019);
        let w1 = deterministic_values(D_MODEL, 0.023);
        let a1 = deterministic_values(D_MODEL, 0.029);
        let k_deformed1 = deterministic_values(D_MODEL, 0.031);
        let r2 = deterministic_values(D_MODEL, 0.037);
        let k2 = deterministic_values(D_MODEL, 0.041);
        let v2 = deterministic_values(D_MODEL, 0.043);
        let w2 = deterministic_values(D_MODEL, 0.047);
        let a2 = deterministic_values(D_MODEL, 0.053);
        let k_deformed2 = deterministic_values(D_MODEL, 0.059);
        let state0 = deterministic_values(HEADS * HEAD_SIZE * HEAD_SIZE, 0.061);

        let (_, state1) = single_timestep(&r1, &k1, &v1, &w1, &a1, &k_deformed1, &state0);
        let (_, sequential_state) = single_timestep(&r2, &k2, &v2, &w2, &a2, &k_deformed2, &state1);

        let mut composed_state = vec![0.0; HEADS * HEAD_SIZE * HEAD_SIZE];
        for head in 0..HEADS {
            let matrix_base = head * HEAD_SIZE * HEAD_SIZE;
            for row in 0..HEAD_SIZE {
                let row_start = matrix_base + row * HEAD_SIZE;
                let first =
                    time_mixer_transform_for_head_row(head, row, &k1, &v1, &w1, &a1, &k_deformed1);
                let second =
                    time_mixer_transform_for_head_row(head, row, &k2, &v2, &w2, &a2, &k_deformed2);
                let composed = compose_affine_transforms(&first, &second);
                let row_state =
                    apply_affine_transform(&state0[row_start..row_start + HEAD_SIZE], &composed);
                composed_state[row_start..row_start + HEAD_SIZE].copy_from_slice(&row_state);
            }
        }

        assert_close(&composed_state, &sequential_state, 1e-5);
    }

    #[test]
    fn single_timestep_chunked_affine_reduction_matches_sequential() {
        let steps = (0..32).map(deterministic_step_vectors).collect::<Vec<_>>();
        let state0 = deterministic_values(HEADS * HEAD_SIZE * HEAD_SIZE, 0.061);

        let mut sequential_state = state0.clone();
        for step in &steps {
            let (_, next_state) = single_timestep(
                &step.r,
                &step.k,
                &step.v,
                &step.w,
                &step.a,
                &step.k_deformed,
                &sequential_state,
            );
            sequential_state = next_state;
        }

        let transform = chunked_time_mixer_state_transform(&steps, 8);
        let reduced_state = apply_time_mixer_state_transform(&state0, &transform);

        assert_close(&reduced_state, &sequential_state, 1e-4);
    }

    fn embedded_weights_path() -> Option<PathBuf> {
        let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../qt/aqt/rwkv_inference/RWKV_trained_on_5000_10000.bin");
        path.exists().then_some(path)
    }

    fn bulk_parity_reviews(count: usize) -> Vec<ReviewInput> {
        let mut state = 0x9e3779b97f4a7c15_u64;
        let mut next = move || {
            state = state
                .wrapping_mul(6364136223846793005)
                .wrapping_add(1442695040888963407);
            (state >> 33) as i64
        };
        (0..count)
            .map(|index| {
                let card_id = 100 + next().rem_euclid(23);
                ReviewInput {
                    card_id,
                    note_id: (index % 11 != 3).then_some(1000 + card_id / 2),
                    deck_id: (index % 13 != 5).then_some(2000 + next().rem_euclid(3)),
                    preset_id: (index % 17 != 7).then_some(3000 + next().rem_euclid(2)),
                    is_query: false,
                    ease: (index % 19 != 11).then_some((next().rem_euclid(4) + 1) as u8),
                    duration_millis: Some(1500 + next().rem_euclid(20_000)),
                    card_type: Some(next().rem_euclid(3)),
                    day_offset: Some(7300 + (index as i64) / 7),
                    current_elapsed_days: Some(next().rem_euclid(45) - 1),
                    current_elapsed_seconds: Some(next().rem_euclid(45 * 86_400) - 1),
                    target_retentions: [Some(0.9), Some(0.9), Some(0.9), Some(0.9)],
                }
            })
            .collect()
    }

    fn sorted_states(mut states: Vec<(i64, Vec<u8>)>) -> Vec<(i64, Vec<u8>)> {
        states.sort_by_key(|(id, _)| *id);
        states
    }

    fn sorted_curve_bytes(curves: &HashMap<i64, ReviewCurve>) -> Vec<(i64, Vec<u8>)> {
        let mut serialized: Vec<(i64, Vec<u8>)> = curves
            .iter()
            .map(|(id, curve)| {
                let mut bytes = Vec::new();
                curve.write_cache_state(&mut bytes);
                (*id, bytes)
            })
            .collect();
        serialized.sort_by_key(|(id, _)| *id);
        serialized
    }

    fn assert_warm_up_parity(sequential: &RwkvInference, bulk: &RwkvInference) {
        let sequential_snapshot = sequential.warm_up_snapshot();
        let bulk_snapshot = bulk.warm_up_snapshot();
        assert_eq!(
            sorted_states(sequential_snapshot.card_states),
            sorted_states(bulk_snapshot.card_states),
            "card states diverged"
        );
        assert_eq!(
            sorted_states(sequential_snapshot.note_states),
            sorted_states(bulk_snapshot.note_states),
            "note states diverged"
        );
        assert_eq!(
            sorted_states(sequential_snapshot.deck_states),
            sorted_states(bulk_snapshot.deck_states),
            "deck states diverged"
        );
        assert_eq!(
            sorted_states(sequential_snapshot.preset_states),
            sorted_states(bulk_snapshot.preset_states),
            "preset states diverged"
        );
        assert_eq!(
            sequential_snapshot.global_state, bulk_snapshot.global_state,
            "global state diverged"
        );
        assert_eq!(
            sorted_curve_bytes(&sequential.curves),
            sorted_curve_bytes(&bulk.curves),
            "curves diverged"
        );
    }

    fn assert_prediction_parity(sequential: &[(usize, f32)], bulk: &[(usize, f32)]) {
        assert_eq!(sequential.len(), bulk.len(), "prediction counts diverged");
        for ((sequential_index, sequential_value), (bulk_index, bulk_value)) in
            sequential.iter().zip(bulk)
        {
            assert_eq!(sequential_index, bulk_index, "prediction order diverged");
            assert_eq!(
                sequential_value.to_bits(),
                bulk_value.to_bits(),
                "prediction values diverged at review {sequential_index}"
            );
        }
    }

    #[test]
    fn bulk_warm_up_matches_sequential() {
        let Some(weights) = embedded_weights_path() else {
            eprintln!("skipping: embedded RWKV weights not found");
            return;
        };
        let reviews = bulk_parity_reviews(230);
        for record_predictions in [false, true] {
            let mut sequential = RwkvInference::load(weights.clone(), 0.9, 36_500).unwrap();
            let sequential_predictions = sequential
                .warm_up_reviews_sequential(reviews.clone(), record_predictions)
                .unwrap();

            let mut bulk = RwkvInference::load(weights.clone(), 0.9, 36_500).unwrap();
            let bulk_predictions =
                bulk::warm_up_reviews_bulk(&mut bulk, reviews.clone(), record_predictions).unwrap();

            assert_prediction_parity(&sequential_predictions, &bulk_predictions);
            assert_warm_up_parity(&sequential, &bulk);
            assert_eq!(
                sequential.cache_state().len(),
                bulk.cache_state().len(),
                "runtime cache size diverged (record={record_predictions})"
            );
        }
    }

    #[test]
    fn same_card_multi_answer_prediction_matches_individual_branches() {
        let Some(weights) = embedded_weights_path() else {
            eprintln!("skipping: embedded RWKV weights not found");
            return;
        };
        let reviews = bulk_parity_reviews(64);
        let mut inference = RwkvInference::load(weights, 0.9, 36_500).unwrap();
        inference.warm_up_reviews(reviews.clone(), false).unwrap();

        let mut query = reviews.last().unwrap().clone();
        query.is_query = true;
        query.ease = None;
        query.duration_millis = None;
        let answers = ANSWER_EASES
            .into_iter()
            .map(|ease| simulated_answer_input(&query, ease))
            .collect::<Vec<_>>();
        let snapshot = || {
            let states = inference.warm_up_snapshot();
            RwkvWorkloadSimulationSnapshot {
                card_states: states.card_states,
                note_states: states.note_states,
                deck_states: states.deck_states,
                preset_states: states.preset_states,
                global_state: states.global_state,
                runtime_state: Some(inference.cache_state()),
            }
        };

        let expected = answers
            .iter()
            .map(|answer| {
                inference
                    .predict_retrievability_many_after_review(
                        answer.clone(),
                        vec![query.clone()],
                        snapshot(),
                    )
                    .unwrap()[0]
            })
            .collect::<Vec<_>>();
        let actual = inference
            .predict_retrievability_many_after_reviews(answers, vec![query], snapshot())
            .unwrap()
            .into_iter()
            .map(|scores| scores[0])
            .collect::<Vec<_>>();

        assert_close(&actual, &expected, 1e-6);
    }

    #[test]
    fn bulk_warm_up_chunked_calls_match_sequential() {
        let Some(weights) = embedded_weights_path() else {
            eprintln!("skipping: embedded RWKV weights not found");
            return;
        };
        let reviews = bulk_parity_reviews(160);

        let mut sequential = RwkvInference::load(weights.clone(), 0.9, 36_500).unwrap();
        let sequential_predictions = sequential
            .warm_up_reviews_sequential(reviews.clone(), true)
            .unwrap();

        // Uneven bridge-style calls plus a tiny internal chunk size, so both
        // cross-call and cross-chunk stream continuity are exercised.
        let mut bulk = RwkvInference::load(weights, 0.9, 36_500).unwrap();
        let mut bulk_predictions = Vec::new();
        let mut offset = 0;
        for call in [37, 96, 27] {
            let calls: Vec<ReviewInput> = reviews[offset..offset + call].to_vec();
            let predictions =
                bulk::warm_up_reviews_bulk_chunked(&mut bulk, calls, true, 7).unwrap();
            bulk_predictions.extend(
                predictions
                    .into_iter()
                    .map(|(index, value)| (index + offset, value)),
            );
            offset += call;
        }

        assert_prediction_parity(&sequential_predictions, &bulk_predictions);
        assert_warm_up_parity(&sequential, &bulk);
    }

    #[test]
    fn clamped_interval_days_rounds_up_to_target_crossing() {
        assert_eq!(clamped_interval_days(1.0, 10), 1);
        assert_eq!(clamped_interval_days(1.1, 10), 2);
        assert_eq!(clamped_interval_days(12.0, 10), 10);
    }

    #[test]
    fn interval_for_curve_returns_max_when_target_not_reached() {
        let curve = ReviewCurve {
            ahead_logits: vec![20.0],
            weights: vec![1.0],
        };

        assert_eq!(interval_for_curve(&curve, 0.30, 365), Some(365));
    }

    #[test]
    fn simulated_answer_input_preserves_review_context() {
        let input = ReviewInput {
            card_id: 123,
            note_id: Some(456),
            deck_id: Some(789),
            preset_id: Some(10),
            is_query: true,
            ease: None,
            duration_millis: None,
            card_type: Some(2),
            day_offset: Some(42),
            current_elapsed_days: Some(3),
            current_elapsed_seconds: Some(259_200),
            target_retentions: [Some(0.81), Some(0.82), Some(0.83), Some(0.84)],
        };

        let good = simulated_answer_input(&input, 3);

        assert!(!good.is_query);
        assert_eq!(good.ease, Some(3));
        assert_eq!(good.card_id, input.card_id);
        assert_eq!(good.current_elapsed_days, input.current_elapsed_days);
        assert_eq!(good.target_retentions, input.target_retentions);
    }

    #[test]
    fn query_timestep_matches_stateful_output_without_retaining_state() {
        let values = (0..D_MODEL)
            .map(|index| (index as f32 - 64.0) / 128.0)
            .collect::<Vec<_>>();
        let r = values.iter().map(|value| value * 0.7).collect::<Vec<_>>();
        let k = values.iter().map(|value| value * -0.4).collect::<Vec<_>>();
        let v = values.iter().map(|value| value * 0.3).collect::<Vec<_>>();
        let w = values
            .iter()
            .map(|value| 0.85 + value * 0.05)
            .collect::<Vec<_>>();
        let a = values
            .iter()
            .map(|value| 0.5 + value * 0.1)
            .collect::<Vec<_>>();
        let k_deformed = values.iter().map(|value| value * -0.2).collect::<Vec<_>>();
        let state = (0..HEADS * HEAD_SIZE * HEAD_SIZE)
            .map(|index| (index % 31) as f32 * 0.002 - 0.03)
            .collect::<Vec<_>>();
        let zero_state = vec![0.0; state.len()];

        for query_state in [None, Some(state.as_slice())] {
            let stateful_state = query_state.unwrap_or(&zero_state);
            let (expected, _) = single_timestep(&r, &k, &v, &w, &a, &k_deformed, stateful_state);
            let mut actual = [0.0; D_MODEL];
            let mut next_row = [0.0; HEAD_SIZE];
            single_timestep_query_into(
                &r,
                &k,
                &v,
                &w,
                &a,
                &k_deformed,
                query_state,
                &mut actual,
                &mut next_row,
            );

            assert_eq!(actual.as_slice(), expected);

            let mut fast = [0.0; D_MODEL];
            single_timestep_query_fast_into(
                &r,
                &k,
                &v,
                &w,
                &a,
                &k_deformed,
                query_state,
                &mut fast,
            );
            let max_delta = fast
                .iter()
                .zip(&expected)
                .map(|(fast, expected)| (fast - expected).abs())
                .fold(0.0_f32, f32::max);
            assert!(max_delta <= 1e-6, "max fast timestep delta: {max_delta}");
        }
    }

    #[test]
    fn batched_retrievability_matches_scalar_query_path() {
        let model_path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../qt/aqt/rwkv_inference/RWKV_trained_on_5000_10000.bin");
        assert!(model_path.exists(), "RWKV parity model is missing");

        fn input(index: usize, is_query: bool) -> ReviewInput {
            ReviewInput {
                card_id: 10_000 + (index % 97) as i64,
                note_id: Some(20_000 + (index % 53) as i64),
                deck_id: Some(30_000 + (index % 7) as i64),
                preset_id: Some(40_000 + (index % 3) as i64),
                is_query,
                ease: (!is_query).then_some((index % 4 + 1) as u8),
                duration_millis: (!is_query).then_some(500 + index as i64 % 2_000),
                card_type: Some((index % 3) as i64),
                day_offset: Some((index / 11) as i64),
                current_elapsed_days: Some((index % 31) as i64),
                current_elapsed_seconds: Some((index % 31 * 86_400) as i64),
                target_retentions: [Some(0.9); 4],
            }
        }

        let mut inference = RwkvInference::load(model_path, 0.9, 36_500).unwrap();
        inference
            .warm_up_reviews((0..257).map(|index| input(index, false)).collect(), false)
            .unwrap();
        let inputs = (0..129)
            .map(|index| input(index + 257, true))
            .collect::<Vec<_>>();
        let features = inputs
            .iter()
            .map(|input| inference.features.features_for(input))
            .collect::<Vec<_>>();
        let items = inputs
            .iter()
            .zip(&features)
            .map(|(input, features)| ReviewPredictionQueryRef {
                features,
                state: inference.warm_up_states.state_ref(input),
            })
            .collect::<Vec<_>>();

        let scalar = inference
            .model
            .review_retrievability_query_refs_scalar(&items);
        assert!(inference
            .model
            .review_retrievability_query_refs_batched(&[])
            .is_empty());
        let batched = inference
            .model
            .review_retrievability_query_refs_batched(&items);
        let max_delta = scalar
            .iter()
            .zip(&batched)
            .map(|(scalar, batched)| (scalar - batched).abs())
            .fold(0.0_f32, f32::max);

        assert!(max_delta <= 1e-6, "max batch prediction delta: {max_delta}");
    }

    #[test]
    fn restore_serialized_warm_up_state_replaces_and_removes_scopes() {
        let serialized = serialize_module_state(&ModuleState {
            layers: vec![LayerState {
                time: Some(TimeState {
                    x_shift: vec![1.0, 2.0],
                    matrix: vec![3.0, 4.0],
                }),
                channel_shift: Some(vec![5.0, 6.0]),
            }],
        });
        let populated = ReviewStateOwned {
            card: Some(serialized.clone()),
            note: Some(serialized.clone()),
            deck: Some(serialized.clone()),
            preset: Some(serialized.clone()),
            global: Some(serialized.clone()),
        };
        let mut states = ReviewStateMaps::default();

        states
            .restore_serialized(1, Some(2), Some(3), Some(4), &populated)
            .unwrap();

        assert_eq!(
            serialize_module_state(states.card.get(&1).unwrap()),
            serialized
        );
        assert_eq!(
            serialize_module_state(states.note.get(&2).unwrap()),
            serialized
        );
        assert_eq!(
            serialize_module_state(states.deck.get(&3).unwrap()),
            serialized
        );
        assert_eq!(
            serialize_module_state(states.preset.get(&4).unwrap()),
            serialized
        );
        assert_eq!(
            serialize_module_state(states.global.as_ref().unwrap()),
            serialized
        );

        states
            .restore_serialized(
                1,
                Some(2),
                Some(3),
                Some(4),
                &ReviewStateOwned {
                    card: None,
                    note: None,
                    deck: None,
                    preset: None,
                    global: None,
                },
            )
            .unwrap();

        assert!(states.card.is_empty());
        assert!(states.note.is_empty());
        assert!(states.deck.is_empty());
        assert!(states.preset.is_empty());
        assert!(states.global.is_none());
    }

    #[test]
    fn workload_review_counts_are_monotonic_by_target_dr() {
        fn point(review_count: u32) -> RwkvWorkloadSimulationPoint {
            RwkvWorkloadSimulationPoint {
                memorized: 0.0,
                weighted_memorized: 0.0,
                cost: 0.0,
                review_count,
            }
        }

        let mut points = vec![
            (31, point(8)),
            (30, point(10)),
            (34, point(12)),
            (33, point(11)),
        ];

        enforce_monotonic_workload_review_counts(&mut points);

        assert_eq!(points[0].1.review_count, 10);
        assert_eq!(points[1].1.review_count, 10);
        assert_eq!(points[2].1.review_count, 12);
        assert_eq!(points[3].1.review_count, 11);
    }

    #[test]
    fn feature_state_scales_duration_from_milliseconds() {
        let mut features = FeatureState::default();
        let input = ReviewInput {
            card_id: 123,
            note_id: Some(456),
            deck_id: Some(789),
            preset_id: Some(10),
            is_query: false,
            ease: Some(3),
            duration_millis: Some(1234),
            card_type: Some(2),
            day_offset: Some(42),
            current_elapsed_days: Some(3),
            current_elapsed_seconds: Some(259_200),
            target_retentions: [None; 4],
        };

        let values = features.features_for(&input);

        assert_eq!(values[8], scale_duration(1234.0));

        let query_values = features.features_for(&ReviewInput {
            is_query: true,
            ease: None,
            duration_millis: None,
            ..input
        });
        assert_eq!(query_values[8], 0.0);
    }

    #[test]
    fn feature_state_normalizes_day_offset_to_first_raw_review_day() {
        let mut features = FeatureState::default();
        let first = ReviewInput {
            card_id: 123,
            note_id: Some(456),
            deck_id: Some(789),
            preset_id: Some(10),
            is_query: false,
            ease: Some(3),
            duration_millis: Some(1234),
            card_type: Some(2),
            day_offset: Some(100),
            current_elapsed_days: Some(3),
            current_elapsed_seconds: Some(259_200),
            target_retentions: [None; 4],
        };
        let second = ReviewInput {
            card_id: 124,
            note_id: Some(457),
            day_offset: Some(101),
            ..first.clone()
        };

        features.store_review(&first);
        let values = features.features_for(&second);

        assert_eq!(features.first_day_offset, Some(100));
        assert_eq!(values[17], -2.0 / 3.0);
    }

    #[test]
    fn feature_state_uses_card_specific_placeholder_for_missing_note_id() {
        let mut features = FeatureState::default();
        let input = ReviewInput {
            card_id: 123,
            note_id: None,
            deck_id: None,
            preset_id: None,
            is_query: false,
            ease: Some(3),
            duration_millis: Some(1234),
            card_type: Some(2),
            day_offset: Some(42),
            current_elapsed_days: Some(3),
            current_elapsed_seconds: Some(259_200),
            target_retentions: [None; 4],
        };

        features.features_for(&input);

        assert!(features
            .id_encodings
            .contains_key(&(IdKind::Note, ID_PLACEHOLDER + input.card_id)));
        assert!(!features
            .id_encodings
            .contains_key(&(IdKind::Note, ID_PLACEHOLDER)));
    }

    #[test]
    fn feature_state_uses_benchmark_id_encoding_stream() {
        let mut features = FeatureState::default();
        let input = ReviewInput {
            card_id: 123,
            note_id: Some(456),
            deck_id: Some(789),
            preset_id: Some(10),
            is_query: false,
            ease: Some(3),
            duration_millis: Some(1234),
            card_type: Some(2),
            day_offset: Some(42),
            current_elapsed_days: Some(3),
            current_elapsed_seconds: Some(259_200),
            target_retentions: [None; 4],
        };

        let values = features.features_for(&input);

        assert_eq!(
            &values[24..36],
            &[0.5, -1.5, 1.5, -0.5, -1.5, -1.5, 1.5, 1.5, 0.5, -0.5, 0.5, -0.5]
        );
        assert_eq!(
            &values[36..48],
            &[1.5, -0.5, 1.5, -0.5, -0.5, -1.5, -1.5, 0.5, 0.5, -0.5, 0.5, 1.5]
        );
        assert_eq!(
            &values[48..56],
            &[1.5, 1.5, -0.5, -1.5, -0.5, 1.5, -0.5, 1.5]
        );
        assert_eq!(
            &values[56..64],
            &[-0.5, -1.5, -0.5, 1.5, 1.5, 1.5, -1.5, -0.5]
        );
    }

    #[test]
    fn feature_state_initializes_today_like_benchmark() {
        let mut features = FeatureState::default();
        let input = ReviewInput {
            card_id: 123,
            note_id: Some(456),
            deck_id: Some(789),
            preset_id: Some(10),
            is_query: false,
            ease: Some(3),
            duration_millis: Some(1234),
            card_type: Some(2),
            day_offset: Some(0),
            current_elapsed_days: Some(3),
            current_elapsed_seconds: Some(259_200),
            target_retentions: [None; 4],
        };

        let values = features.features_for(&input);

        assert_eq!(features.today, -1);
        assert_eq!(values[21], scale_cum_reviews_today(0));
    }

    #[test]
    fn feature_state_cache_round_trips_id_encodings_and_rng() {
        let mut features = FeatureState::default();
        let input = ReviewInput {
            card_id: 123,
            note_id: Some(456),
            deck_id: Some(789),
            preset_id: Some(10),
            is_query: false,
            ease: Some(3),
            duration_millis: Some(1234),
            card_type: Some(2),
            day_offset: Some(42),
            current_elapsed_days: Some(3),
            current_elapsed_seconds: Some(259_200),
            target_retentions: [None; 4],
        };
        let first_values = features.features_for(&input);

        let mut cache = Vec::new();
        features.write_cache_state(&mut cache);
        let mut cursor = Cursor::new(&cache);
        let mut restored = FeatureState::read_cache_state(&mut cursor).unwrap();
        cursor.expect_end().unwrap();

        let restored_first_values = restored.features_for(&input);
        assert_eq!(&restored_first_values[24..64], &first_values[24..64]);

        let next_input = ReviewInput {
            card_id: 124,
            note_id: Some(457),
            deck_id: Some(790),
            preset_id: Some(11),
            ..input
        };
        let expected_next_values = features.features_for(&next_input);
        let restored_next_values = restored.features_for(&next_input);
        assert_eq!(&restored_next_values[24..64], &expected_next_values[24..64]);
    }

    #[test]
    fn feature_state_for_card_restores_review_mutations() {
        let mut features = FeatureState::default();
        let before = features.state_for_card(123);
        let input = ReviewInput {
            card_id: 123,
            note_id: Some(456),
            deck_id: Some(789),
            preset_id: Some(10),
            is_query: false,
            ease: Some(3),
            duration_millis: Some(1200),
            card_type: Some(2),
            day_offset: Some(42),
            current_elapsed_days: Some(3),
            current_elapsed_seconds: Some(259_200),
            target_retentions: [None; 4],
        };

        features.store_review(&input);
        assert_eq!(features.review_index, 1);
        assert!(features.card_set.contains_key(&123));
        assert_eq!(features.last_i.get(&123), Some(&0));
        assert_eq!(features.card_elapsed_days_cumulative.get(&123), Some(&3));
        assert_eq!(
            features.card_elapsed_seconds_cumulative.get(&123),
            Some(&259_200)
        );

        features.restore_state(&before);
        assert_eq!(features.review_index, 0);
        assert_eq!(features.today, -1);
        assert!(!features.card_set.contains_key(&123));
        assert!(!features.last_i.contains_key(&123));
        assert!(!features.last_new_cards.contains_key(&123));
        assert!(!features.card_first_day_offset.contains_key(&123));
        assert!(!features.card_elapsed_days_cumulative.contains_key(&123));
        assert!(!features.card_elapsed_seconds_cumulative.contains_key(&123));
        assert_eq!(features.previous_day_offset, None);
        assert_eq!(features.today_reviews, 0);
        assert_eq!(features.today_new_cards, 0);
    }

    #[test]
    #[ignore]
    fn rwkv_state_compression_metrics() {
        let Ok(collection_path) = env::var("ANKI_RWKV_STATE_COMPRESSION_COLLECTION") else {
            eprintln!("set ANKI_RWKV_STATE_COMPRESSION_COLLECTION to a copied collection.anki2");
            return;
        };
        let weights_path = env::var("ANKI_RWKV_STATE_COMPRESSION_MODEL")
            .unwrap_or_else(|_| "qt/aqt/rwkv_inference/RWKV_trained_on_5000_10000.bin".into());
        let limit = env_usize("ANKI_RWKV_STATE_COMPRESSION_LIMIT", 5_000);
        let power_iterations = env_usize("ANKI_RWKV_STATE_COMPRESSION_POWER_ITERS", 8);
        let selected_configs = env::var("ANKI_RWKV_STATE_COMPRESSION_CONFIGS")
            .ok()
            .map(|configs| {
                configs
                    .split(',')
                    .map(str::trim)
                    .filter(|config| !config.is_empty())
                    .map(str::to_string)
                    .collect::<Vec<_>>()
            })
            .filter(|configs| !configs.is_empty());

        let load_start = Instant::now();
        let (reviews, outcomes) =
            load_metric_reviews(&collection_path, limit).expect("failed to load metric reviews");
        assert!(!reviews.is_empty(), "no eligible reviews loaded");
        eprintln!(
            "rwkv_state_compression_metric_inputs reviews={} limit={} load_ms={:.1}",
            reviews.len(),
            limit,
            load_start.elapsed().as_secs_f64() * 1000.0
        );
        if let Some(configs) = &selected_configs {
            eprintln!(
                "rwkv_state_compression_metric_configs configs={}",
                configs.join(",")
            );
        }

        let configs = [
            ("raw", None),
            (
                "shift-int4",
                Some(StateCompression {
                    shift_bits: Some(4),
                    matrix_rank: HEAD_SIZE,
                    matrix_bits: None,
                    power_iterations,
                }),
            ),
            (
                "rank4-int8-shift-int4",
                Some(StateCompression {
                    shift_bits: Some(4),
                    matrix_rank: 4,
                    matrix_bits: Some(8),
                    power_iterations,
                }),
            ),
            (
                "rank4-int8",
                Some(StateCompression {
                    shift_bits: None,
                    matrix_rank: 4,
                    matrix_bits: Some(8),
                    power_iterations,
                }),
            ),
            (
                "rank4-int4-shift-int4",
                Some(StateCompression {
                    shift_bits: Some(4),
                    matrix_rank: 4,
                    matrix_bits: Some(4),
                    power_iterations,
                }),
            ),
            (
                "rank2-int8-shift-int4",
                Some(StateCompression {
                    shift_bits: Some(4),
                    matrix_rank: 2,
                    matrix_bits: Some(8),
                    power_iterations,
                }),
            ),
            (
                "rank2-int8",
                Some(StateCompression {
                    shift_bits: None,
                    matrix_rank: 2,
                    matrix_bits: Some(8),
                    power_iterations,
                }),
            ),
            (
                "rank2-int4-shift-int4",
                Some(StateCompression {
                    shift_bits: Some(4),
                    matrix_rank: 2,
                    matrix_bits: Some(4),
                    power_iterations,
                }),
            ),
            (
                "rank1-int4-shift-int4",
                Some(StateCompression {
                    shift_bits: Some(4),
                    matrix_rank: 1,
                    matrix_bits: Some(4),
                    power_iterations,
                }),
            ),
        ];

        let mut baseline = None;
        println!(
            "config,reviews,log_loss,delta_log_loss,rmse_bins,delta_rmse_bins,rmse_bins_weighted,delta_rmse_bins_weighted,elapsed_ms"
        );
        for (label, compression) in configs {
            if let Some(configs) = &selected_configs {
                if !configs.iter().any(|config| config == label) {
                    continue;
                }
            }
            let start = Instant::now();
            let mut inference = RwkvInference::load(PathBuf::from(&weights_path), 0.9, 36_500)
                .expect("RWKV load failed");
            let predictions =
                inference.warm_up_reviews_with_state_compression(&reviews, compression);
            assert_eq!(predictions.len(), outcomes.len());
            let metrics = MetricAccumulator::from_predictions(&predictions, &outcomes);
            let baseline_metrics = baseline.get_or_insert(metrics);
            println!(
                "{label},{},{:.9},{:.9},{:.9},{:.9},{:.9},{:.9},{:.1}",
                outcomes.len(),
                metrics.log_loss,
                metrics.log_loss - baseline_metrics.log_loss,
                metrics.rmse_bins,
                metrics.rmse_bins - baseline_metrics.rmse_bins,
                metrics.rmse_bins_weighted,
                metrics.rmse_bins_weighted - baseline_metrics.rmse_bins_weighted,
                start.elapsed().as_secs_f64() * 1000.0,
            );
        }
    }

    fn env_usize(name: &str, default: usize) -> usize {
        env::var(name)
            .ok()
            .and_then(|value| value.parse().ok())
            .unwrap_or(default)
    }

    fn load_metric_reviews(
        collection_path: &str,
        limit: usize,
    ) -> rusqlite::Result<(Vec<ReviewInput>, Vec<bool>)> {
        let connection = Connection::open_with_flags(
            collection_path,
            rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
        )?;
        let created_secs: i64 =
            connection.query_row("select crt from col limit 1", [], |row| row.get(0))?;
        let preset_by_deck = deck_config_ids_by_deck(&connection)?;
        let mut previous_review_id_by_card = HashMap::new();
        let mut reviews = Vec::new();
        let mut outcomes = Vec::new();
        let sql = format!(
            "\
select r.id, r.cid, c.nid, c.did, r.ease, r.time, r.type
from revlog r
join cards c on c.id = r.cid
where r.ease between 1 and 4
  and (r.type in (0, 1, 2, 3) or r.type = 4)
order by r.id, r.cid
{}",
            if limit > 0 { "limit ?" } else { "" }
        );
        let mut statement = connection.prepare(&sql)?;
        let mut rows = if limit > 0 {
            statement.query([limit as i64])?
        } else {
            statement.query([])?
        };
        while let Some(row) = rows.next()? {
            let review_id: i64 = row.get(0)?;
            let card_id: i64 = row.get(1)?;
            let note_id: i64 = row.get(2)?;
            let deck_id: i64 = row.get(3)?;
            let ease: u8 = row.get(4)?;
            let duration_millis: i64 = row.get(5)?;
            let review_kind: i64 = row.get(6)?;
            let previous_review_id = previous_review_id_by_card.insert(card_id, review_id);
            let elapsed_seconds = previous_review_id
                .map(|previous| ((review_id - previous) / 1000).max(0))
                .unwrap_or(-1);
            let elapsed_days = if elapsed_seconds >= 0 {
                elapsed_seconds / SECONDS_PER_DAY
            } else {
                -1
            };
            let review_secs = review_id / 1000;
            let day_offset = ((review_secs - created_secs).max(0)) / SECONDS_PER_DAY;
            let preset_id = preset_by_deck
                .get(&deck_id)
                .copied()
                .flatten()
                .or(Some(deck_id));
            outcomes.push(ease != 1);
            reviews.push(ReviewInput {
                card_id,
                note_id: Some(note_id),
                deck_id: Some(deck_id),
                preset_id,
                is_query: false,
                ease: Some(ease),
                duration_millis: Some(duration_millis),
                card_type: Some(historical_card_type(review_kind)),
                day_offset: Some(day_offset),
                current_elapsed_days: Some(elapsed_days),
                current_elapsed_seconds: Some(elapsed_seconds),
                target_retentions: [None; 4],
            });
        }
        Ok((reviews, outcomes))
    }

    fn deck_config_ids_by_deck(
        connection: &Connection,
    ) -> rusqlite::Result<HashMap<i64, Option<i64>>> {
        let decks_json: String =
            connection.query_row("select decks from col limit 1", [], |row| row.get(0))?;
        let decks: serde_json::Value = serde_json::from_str(&decks_json).unwrap_or_default();
        let mut by_deck = HashMap::new();
        if let Some(decks) = decks.as_object() {
            for (id, deck) in decks {
                if let Ok(deck_id) = id.parse::<i64>() {
                    let config_id = deck.get("conf").and_then(|value| value.as_i64());
                    by_deck.insert(deck_id, config_id);
                }
            }
        }
        Ok(by_deck)
    }

    fn historical_card_type(review_kind: i64) -> i64 {
        match review_kind {
            0 => 1,
            2 => 3,
            _ => 2,
        }
    }

    #[derive(Clone, Copy)]
    struct MetricSummary {
        log_loss: f64,
        rmse_bins: f64,
        rmse_bins_weighted: f64,
    }

    struct MetricAccumulator {
        log_loss_sum: f64,
        bins: [MetricBin; 20],
    }

    #[derive(Clone, Copy, Default)]
    struct MetricBin {
        count: usize,
        prediction_sum: f64,
        outcome_sum: f64,
    }

    impl MetricAccumulator {
        fn from_predictions(predictions: &[f32], outcomes: &[bool]) -> MetricSummary {
            let mut accumulator = Self {
                log_loss_sum: 0.0,
                bins: [MetricBin::default(); 20],
            };
            for (&prediction, &outcome) in predictions.iter().zip(outcomes) {
                accumulator.record(prediction, outcome);
            }
            accumulator.finish(predictions.len())
        }

        fn record(&mut self, prediction: f32, outcome: bool) {
            let prediction = valid_probability(prediction as f64);
            let outcome_value = if outcome { 1.0 } else { 0.0 };
            self.log_loss_sum += -(outcome_value * prediction.ln()
                + (1.0 - outcome_value) * (1.0 - prediction).ln());
            let bin =
                ((prediction * self.bins.len() as f64).floor() as usize).min(self.bins.len() - 1);
            let metric_bin = &mut self.bins[bin];
            metric_bin.count += 1;
            metric_bin.prediction_sum += prediction;
            metric_bin.outcome_sum += outcome_value;
        }

        fn finish(self, count: usize) -> MetricSummary {
            let mut unweighted_sum = 0.0;
            let mut weighted_sum = 0.0;
            let mut bin_count = 0;
            for bin in self.bins {
                if bin.count == 0 {
                    continue;
                }
                let predicted = bin.prediction_sum / bin.count as f64;
                let observed = bin.outcome_sum / bin.count as f64;
                let squared = (predicted - observed).powi(2);
                unweighted_sum += squared;
                weighted_sum += squared * bin.count as f64;
                bin_count += 1;
            }
            MetricSummary {
                log_loss: self.log_loss_sum / count as f64,
                rmse_bins: (unweighted_sum / bin_count as f64).sqrt(),
                rmse_bins_weighted: (weighted_sum / count as f64).sqrt(),
            }
        }
    }

    fn valid_probability(value: f64) -> f64 {
        if value.is_finite() {
            value.clamp(1e-6, 1.0 - 1e-6)
        } else {
            0.5
        }
    }
}

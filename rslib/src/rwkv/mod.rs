// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

use std::collections::HashMap;
use std::fs;
use std::io;
use std::path::PathBuf;
use std::sync::Arc;

use rayon::prelude::*;

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

        Ok(ReviewOutput {
            retrievability: heads.retrievability,
            current_interval,
            current_s90,
            intervals,
            s90s,
            card_state: serialize_module_state(&heads.next_state.card),
            deck_state: serialize_module_state(&heads.next_state.deck),
            note_state: serialize_module_state(&heads.next_state.note),
            preset_state: serialize_module_state(&heads.next_state.preset),
            global_state: serialize_module_state(&heads.next_state.global),
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

    pub fn warm_up_reviews(
        &mut self,
        reviews: Vec<ReviewInput>,
        record_predictions: bool,
    ) -> io::Result<Vec<(usize, f32)>> {
        let mut predictions = vec![];
        for (index, input) in reviews.into_iter().enumerate() {
            if input.ease.is_none() {
                continue;
            }

            if record_predictions {
                let mut query_input = input.clone();
                query_input.is_query = true;
                query_input.ease = None;
                query_input.duration_millis = None;
                let features = self.features.features_for(&query_input);
                let query_heads = self
                    .model
                    .review(&features, self.warm_up_states.state_ref(&query_input));
                predictions.push((index, query_heads.retrievability));
            }

            let features = self.features.features_for(&input);
            let heads = self
                .model
                .review(&features, self.warm_up_states.state_ref(&input));
            self.features.store_review(&input);
            self.curves.insert(input.card_id, heads.curve.clone());
            self.warm_up_states.store(&input, heads.next_state);
        }

        Ok(predictions)
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
        features
    }

    fn store_review(&mut self, input: &ReviewInput) {
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
        let weight = self.tensor(&weight_name, &[output, input])?.values.clone();
        let bias = if bias {
            Some(self.tensor(&bias_name, &[output])?.values.clone())
        } else {
            None
        };
        Ok(Linear {
            input,
            output,
            weight,
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
        items
            .par_iter()
            .map(|item| self.review_retrievability_features(&item.features, item.state.as_ref()))
            .collect()
    }

    fn review_features(&self, features: &[f32], state: SrsStateRef<'_>) -> ReviewHeads {
        let mut x = self.features_0.apply(features);
        silu_in_place(&mut x);
        x = self.features_norm.apply(&x);
        x = self.features_3.apply(&x);
        silu_in_place(&mut x);

        let (x, card_state) = self.modules[0].run(&x, state.card);
        let (x, deck_state) = self.modules[1].run(&x, state.deck);
        let (x, note_state) = self.modules[2].run(&x, state.note);
        let (x, preset_state) = self.modules[3].run(&x, state.preset);
        let (x, global_state) = self.modules[4].run(&x, state.global);

        let x = self.prehead_norm.apply(&x);

        let mut head_w = self.head_w_0.apply(&x);
        relu_in_place(&mut head_w);
        head_w = self.head_w_norm.apply(&head_w);
        head_w = self.head_w_4.apply(&head_w);
        let weights = softmax(&self.w_linear.apply(&head_w));

        let mut ahead = self.head_ahead_0.apply(&x);
        relu_in_place(&mut ahead);
        let ahead_logits = self.ahead_linear.apply(&ahead);

        let mut head_p = self.head_p_0.apply(&x);
        relu_in_place(&mut head_p);
        let logits = self.p_linear.apply(&head_p);
        let probabilities = softmax(&logits);

        let next_state = SrsState {
            card: card_state,
            deck: deck_state,
            note: note_state,
            preset: preset_state,
            global: global_state,
        };

        ReviewHeads {
            retrievability: 1.0 - probabilities[0],
            curve: ReviewCurve {
                ahead_logits,
                weights,
            },
            next_state,
        }
    }

    fn review_retrievability_features(&self, features: &[f32], state: SrsStateRef<'_>) -> f32 {
        let mut x = self.features_0.apply(features);
        silu_in_place(&mut x);
        x = self.features_norm.apply(&x);
        x = self.features_3.apply(&x);
        silu_in_place(&mut x);

        let (x, _) = self.modules[0].run(&x, state.card);
        let (x, _) = self.modules[1].run(&x, state.deck);
        let (x, _) = self.modules[2].run(&x, state.note);
        let (x, _) = self.modules[3].run(&x, state.preset);
        let (x, _) = self.modules[4].run(&x, state.global);

        let x = self.prehead_norm.apply(&x);
        let mut head_p = self.head_p_0.apply(&x);
        relu_in_place(&mut head_p);
        let logits = self.p_linear.apply(&head_p);
        let probabilities = softmax(&logits);
        1.0 - probabilities[0]
    }
}

struct ReviewHeads {
    retrievability: f32,
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

    None
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

impl RwkvModule {
    fn load(weights: &WeightMap, module_id: usize, layer_count: usize) -> io::Result<Self> {
        let mut layers = Vec::with_capacity(layer_count);
        for layer_id in 0..layer_count {
            layers.push(RwkvLayer::load(weights, module_id, layer_id)?);
        }
        Ok(Self { layers })
    }

    fn run(&self, input: &[f32], state: Option<&ModuleState>) -> (Vec<f32>, ModuleState) {
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

        (
            x,
            ModuleState {
                layers: next_layers,
            },
        )
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
}

#[derive(Clone)]
struct LayerState {
    time: Option<TimeState>,
    channel_shift: Option<Vec<f32>>,
}

struct TimeMixer {
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

impl TimeMixer {
    fn load(weights: &WeightMap, module_id: usize, layer_id: usize) -> io::Result<Self> {
        let prefix = format!("rwkv_modules.{module_id}.blocks.{layer_id}.time_mixer");
        Ok(Self {
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
        let x = self.layer_norm.apply(input);
        let (x_shift, state_matrix) = match state {
            Some(state) => (state.x_shift.as_slice(), state.matrix.as_slice()),
            None => (x.as_slice(), &[0.0; HEADS * HEAD_SIZE * HEAD_SIZE][..]),
        };

        let mut mixed = vec![vec![0.0; D_MODEL]; 8];
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

        let r = self.w_r.apply(&mixed[0]);
        let mut k = self.w_k.apply(&mixed[1]);
        let mut k_scale = self.k_scale_linear.apply(&mixed[6]);
        sigmoid_in_place(&mut k_scale);
        let mut v_scale = self.v_scale_linear.apply(&mixed[7]);
        sigmoid_in_place(&mut v_scale);

        let (v, next_v0) = if self.layer_id == 0 {
            let v = self.w_v.apply(&mixed[2]);
            (v.clone(), v)
        } else {
            let mut v_lerp = self.v_lora.apply_sigmoid(&mixed[2]);
            let w_v = self.w_v.apply(&mixed[2]);
            for channel in 0..D_MODEL {
                v_lerp[channel] = lerp(w_v[channel], v0[channel], v_lerp[channel]);
            }
            (v_lerp, v0.to_vec())
        };

        let a = self.a_lora.apply_sigmoid(&mixed[4]);
        let mut g = self.lora_a_g.apply(&mixed[5]);
        sigmoid_in_place(&mut g);
        g = self.lora_b_g.apply(&g);

        let mut d = self.d_lora.apply_tanh(&mixed[3]);
        for value in &mut d {
            *value = -0.5 - softplus(-*value);
        }
        let w = d
            .iter()
            .map(|value| (-value.exp()).exp())
            .collect::<Vec<_>>();

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

        let (mut out, next_matrix) = single_timestep(&r, &k, &v, &w, &a, &k_deformed, state_matrix);
        out = self.out_group_norm.apply(&out);

        let mut bonus = vec![0.0; D_MODEL];
        for head in 0..HEADS {
            let base = head * HEAD_SIZE;
            let mut bonus_scale = 0.0;
            for index in 0..HEAD_SIZE {
                bonus_scale += r[base + index] * self.bonus[base + index] * k[base + index];
            }
            for index in 0..HEAD_SIZE {
                bonus[base + index] = bonus_scale * v[base + index];
            }
        }

        for channel in 0..D_MODEL {
            out[channel] = g[channel] * (out[channel] + bonus[channel]);
        }
        let out = self.w_o.apply(&out);
        let mut next = vec![0.0; D_MODEL];
        for channel in 0..D_MODEL {
            next[channel] = input[channel] + out[channel];
        }

        (
            next,
            next_v0,
            TimeState {
                x_shift: x,
                matrix: next_matrix,
            },
        )
    }
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
        let x = self.layer_norm.apply(input);
        let x_shift = state.map_or(x.as_slice(), |state| state.as_slice());
        let mut mixed = vec![0.0; D_MODEL];
        for channel in 0..D_MODEL {
            mixed[channel] = lerp(x[channel], x_shift[channel], self.lerp_k[channel]);
        }

        let mut k = self.w_k.apply(&mixed);
        for value in &mut k {
            *value = value.max(0.0).powi(2);
        }
        let out = self.w_v.apply(&k);
        let mut next = vec![0.0; D_MODEL];
        for channel in 0..D_MODEL {
            next[channel] = input[channel] + out[channel];
        }
        (next, x)
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

    fn apply_tanh(&self, input: &[f32]) -> Vec<f32> {
        let mut hidden = self.a.apply(input);
        for value in &mut hidden {
            *value = value.tanh();
        }
        self.b.apply(&hidden)
    }
}

struct Linear {
    input: usize,
    output: usize,
    weight: Vec<f32>,
    bias: Option<Vec<f32>>,
}

impl Linear {
    fn apply(&self, input: &[f32]) -> Vec<f32> {
        debug_assert_eq!(input.len(), self.input);
        let mut out = vec![0.0; self.output];
        for (row, output) in out.iter_mut().enumerate() {
            let weight_row = &self.weight[row * self.input..(row + 1) * self.input];
            let mut sum = dot_product(input, weight_row);
            sum += self.bias.as_ref().map_or(0.0, |bias| bias[row]);
            *output = sum;
        }
        out
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

#[cfg(not(target_arch = "aarch64"))]
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
        debug_assert_eq!(input.len(), self.dim);
        let group_size = self.dim / self.groups;
        let mut out = vec![0.0; self.dim];

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

        out
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

    (out, next_state)
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
mod tests {
    use super::*;

    #[test]
    fn clamped_interval_days_rounds_up_to_target_crossing() {
        assert_eq!(clamped_interval_days(1.0, 10), 1);
        assert_eq!(clamped_interval_days(1.1, 10), 2);
        assert_eq!(clamped_interval_days(12.0, 10), 10);
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
}

// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

const CBLAS_ROW_MAJOR: i32 = 101;
const CBLAS_NO_TRANS: i32 = 111;

#[link(name = "Accelerate", kind = "framework")]
extern "C" {
    fn cblas_sgemm(
        order: i32,
        trans_a: i32,
        trans_b: i32,
        m: i32,
        n: i32,
        k: i32,
        alpha: f32,
        a: *const f32,
        lda: i32,
        b: *const f32,
        ldb: i32,
        beta: f32,
        c: *mut f32,
        ldc: i32,
    );
}

pub(super) fn matrix_times_matrix(
    left: &[f32],
    right: &[f32],
    rows: usize,
    columns: usize,
    shared: usize,
    out: &mut [f32],
) {
    let left_len = rows.checked_mul(shared).expect("left matrix is too large");
    let right_len = columns
        .checked_mul(shared)
        .expect("right matrix is too large");
    let output_len = rows
        .checked_mul(columns)
        .expect("output matrix is too large");
    assert_eq!(left.len(), left_len);
    assert_eq!(right.len(), right_len);
    assert_eq!(out.len(), output_len);
    let rows = i32::try_from(rows).expect("row count exceeds CBLAS limits");
    let columns = i32::try_from(columns).expect("column count exceeds CBLAS limits");
    let shared = i32::try_from(shared).expect("shared dimension exceeds CBLAS limits");

    // SAFETY: all matrices are contiguous row-major f32 slices with the
    // dimensions and leading strides provided below.
    unsafe {
        cblas_sgemm(
            CBLAS_ROW_MAJOR,
            CBLAS_NO_TRANS,
            CBLAS_NO_TRANS,
            rows,
            columns,
            shared,
            1.0,
            left.as_ptr(),
            shared,
            right.as_ptr(),
            shared,
            0.0,
            out.as_mut_ptr(),
            columns,
        );
    }
}

//! Template matching for the `locate` GUI action.
//!
//! pyautogui's `locateOnScreen(confidence=c)` scores every placement with
//! OpenCV's normalized correlation coefficient (`TM_CCOEFF_NORMED`) and
//! returns the first placement, in row-major order, scoring at least `c`.
//! This computes the same score on grayscale images: the correlation with the
//! zero-mean template through FFTs over the screen size (placements that fit
//! never wrap around) and the window statistics through integral images.

use image::GrayImage;
use rustfft::num_complex::Complex;
use rustfft::{FftDirection, FftPlanner};

/// Top-left corner of the first placement scoring at least `confidence`.
pub fn first_match(
    haystack: &GrayImage,
    needle: &GrayImage,
    confidence: f64,
) -> Result<Option<(u32, u32)>, String> {
    let (cols, scores) = scores(haystack, needle)?;
    Ok(scores
        .iter()
        .position(|score| *score >= confidence)
        .map(|index| ((index % cols) as u32, (index / cols) as u32)))
}

/// `TM_CCOEFF_NORMED` of every placement, row-major, with its row length.
fn scores(haystack: &GrayImage, needle: &GrayImage) -> Result<(usize, Vec<f64>), String> {
    let (w, h) = (haystack.width() as usize, haystack.height() as usize);
    let (tw, th) = (needle.width() as usize, needle.height() as usize);
    if tw == 0 || th == 0 {
        return Err("the image to locate is empty".into());
    }
    if tw > w || th > h {
        return Err("needle dimension(s) exceed the haystack image or region dimensions".into());
    }
    let n = (tw * th) as f64;
    let template: Vec<f64> = needle.pixels().map(|p| f64::from(p.0[0])).collect();
    let mean = template.iter().sum::<f64>() / n;
    let template_norm = template
        .iter()
        .map(|t| (t - mean).powi(2))
        .sum::<f64>()
        .sqrt();

    let mut planner = FftPlanner::new();
    let mut screen: Vec<Complex<f64>> = haystack
        .pixels()
        .map(|p| Complex::new(f64::from(p.0[0]), 0.0))
        .collect();
    let mut kernel = vec![Complex::new(0.0, 0.0); w * h];
    for y in 0..th {
        for x in 0..tw {
            kernel[y * w + x].re = template[y * tw + x] - mean;
        }
    }
    fft2(&mut screen, w, h, &mut planner, FftDirection::Forward);
    fft2(&mut kernel, w, h, &mut planner, FftDirection::Forward);
    for (s, k) in screen.iter_mut().zip(&kernel) {
        *s *= k.conj();
    }
    fft2(&mut screen, w, h, &mut planner, FftDirection::Inverse);
    let normalize = (w * h) as f64;

    let stride = w + 1;
    let mut sum = vec![0.0f64; stride * (h + 1)];
    let mut sum_sq = vec![0.0f64; stride * (h + 1)];
    for y in 0..h {
        let (mut row, mut row_sq) = (0.0, 0.0);
        for x in 0..w {
            let v = f64::from(haystack.get_pixel(x as u32, y as u32).0[0]);
            row += v;
            row_sq += v * v;
            sum[(y + 1) * stride + x + 1] = sum[y * stride + x + 1] + row;
            sum_sq[(y + 1) * stride + x + 1] = sum_sq[y * stride + x + 1] + row_sq;
        }
    }
    let window = |table: &[f64], x: usize, y: usize| {
        table[(y + th) * stride + x + tw]
            - table[y * stride + x + tw]
            - table[(y + th) * stride + x]
            + table[y * stride + x]
    };

    let (cols, rows) = (w - tw + 1, h - th + 1);
    let mut out = Vec::with_capacity(cols * rows);
    for y in 0..rows {
        for x in 0..cols {
            let num = screen[y * w + x].re / normalize;
            let s = window(&sum, x, y);
            let variance = (window(&sum_sq, x, y) - s * s / n).max(0.0);
            out.push(normalized(num, variance.sqrt() * template_norm));
        }
    }
    Ok((cols, out))
}

/// OpenCV's guard for the normalization: rounding may push |num| just past
/// the denominator (a perfect match), and a flat window or template scores 0.
fn normalized(num: f64, denominator: f64) -> f64 {
    if num.abs() < denominator {
        num / denominator
    } else if num.abs() < denominator * 1.125 {
        num.signum()
    } else {
        0.0
    }
}

fn fft2(
    data: &mut [Complex<f64>],
    w: usize,
    h: usize,
    planner: &mut FftPlanner<f64>,
    direction: FftDirection,
) {
    planner.plan_fft(w, direction).process(data);
    let mut columns = transpose(data, w, h);
    planner.plan_fft(h, direction).process(&mut columns);
    data.copy_from_slice(&transpose(&columns, h, w));
}

fn transpose(data: &[Complex<f64>], w: usize, h: usize) -> Vec<Complex<f64>> {
    let mut out = vec![Complex::new(0.0, 0.0); w * h];
    for y in 0..h {
        for x in 0..w {
            out[x * h + y] = data[y * w + x];
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn noise(w: u32, h: u32, seed: u64) -> GrayImage {
        let mut state = seed;
        GrayImage::from_fn(w, h, |_, _| {
            state = state
                .wrapping_mul(6364136223846793005)
                .wrapping_add(1442695040888963407);
            image::Luma([(state >> 56) as u8])
        })
    }

    fn naive(haystack: &GrayImage, needle: &GrayImage, x: u32, y: u32) -> f64 {
        let n = f64::from(needle.width() * needle.height());
        let t: Vec<f64> = needle.pixels().map(|p| f64::from(p.0[0])).collect();
        let i: Vec<f64> = (0..needle.height())
            .flat_map(|dy| (0..needle.width()).map(move |dx| (dx, dy)))
            .map(|(dx, dy)| f64::from(haystack.get_pixel(x + dx, y + dy).0[0]))
            .collect();
        let (tm, im) = (t.iter().sum::<f64>() / n, i.iter().sum::<f64>() / n);
        let num: f64 = t.iter().zip(&i).map(|(a, b)| (a - tm) * (b - im)).sum();
        let den = (t.iter().map(|a| (a - tm).powi(2)).sum::<f64>()
            * i.iter().map(|b| (b - im).powi(2)).sum::<f64>())
        .sqrt();
        num / den
    }

    #[test]
    fn scores_equal_the_direct_correlation_coefficient() {
        let haystack = noise(41, 29, 7);
        let needle = noise(7, 5, 11);
        let (cols, scores) = scores(&haystack, &needle).unwrap();
        assert_eq!((cols, scores.len()), (35, 35 * 25));
        for (index, score) in scores.iter().enumerate() {
            let (x, y) = ((index % cols) as u32, (index / cols) as u32);
            assert!(
                (score - naive(&haystack, &needle, x, y)).abs() < 1e-9,
                "at {x},{y}"
            );
        }
    }

    #[test]
    fn finds_an_exact_crop_and_respects_the_confidence_threshold() {
        let haystack = noise(200, 150, 3);
        let needle = image::imageops::crop_imm(&haystack, 37, 81, 20, 15).to_image();
        assert_eq!(
            first_match(&haystack, &needle, 0.999).unwrap(),
            Some((37, 81))
        );
        let mut noisy = needle.clone();
        for (i, p) in noisy.pixels_mut().enumerate() {
            p.0[0] = p.0[0].saturating_add(if i % 3 == 0 { 40 } else { 0 });
        }
        assert_eq!(first_match(&haystack, &noisy, 0.8).unwrap(), Some((37, 81)));
        assert_eq!(first_match(&haystack, &noisy, 0.999).unwrap(), None);
    }

    #[test]
    fn first_match_is_row_major() {
        let mut haystack = GrayImage::new(60, 40);
        let patch = noise(6, 6, 5);
        for (ox, oy) in [(40, 5), (3, 20), (10, 5)] {
            image::imageops::replace(&mut haystack, &patch, ox, oy);
        }
        assert_eq!(first_match(&haystack, &patch, 0.99).unwrap(), Some((10, 5)));
    }

    #[test]
    fn flat_templates_never_match_and_oversized_ones_are_refused() {
        let haystack = GrayImage::from_pixel(30, 30, image::Luma([9]));
        let flat = GrayImage::from_pixel(5, 5, image::Luma([9]));
        assert_eq!(first_match(&haystack, &flat, 0.5).unwrap(), None);
        let big = GrayImage::new(31, 2);
        assert_eq!(
            first_match(&haystack, &big, 0.5).unwrap_err(),
            "needle dimension(s) exceed the haystack image or region dimensions"
        );
    }
}

//! Screenshot downscaling and coordinate remapping: the port of
//! `lib/desktop_agent/_scaling.py`.
//!
//! Vision models ground clicks best on an XGA-sized image (Anthropic Computer
//! Use guidance), so the screenshot is shrunk to fit 1024x768 and the model's
//! coordinates are mapped back to real pixels. Both directions derive the
//! factor from the real screen size alone, so screenshot and action share no
//! state. `TOFU_DESKTOP_TARGET_W` / `TOFU_DESKTOP_TARGET_H` override the
//! target exactly as the Python node does. Rounding is Python's `round`
//! (half to even).

const DEFAULT_TARGET_W: i64 = 1024;
const DEFAULT_TARGET_H: i64 = 768;

/// Python's `round(x)` for a float: nearest integer, ties to even.
pub fn py_round(x: f64) -> i64 {
    x.round_ties_even() as i64
}

/// The fit box from the two environment values; a value that is not a
/// positive integer keeps the default.
fn target_size_from(width: Option<String>, height: Option<String>) -> (i64, i64) {
    let parse = |raw: Option<String>, default: i64| {
        raw.and_then(|raw| raw.trim().parse::<i64>().ok())
            .filter(|v| *v > 0)
            .unwrap_or(default)
    };
    (
        parse(width, DEFAULT_TARGET_W),
        parse(height, DEFAULT_TARGET_H),
    )
}

pub fn target_size() -> (i64, i64) {
    target_size_from(
        std::env::var("TOFU_DESKTOP_TARGET_W").ok(),
        std::env::var("TOFU_DESKTOP_TARGET_H").ok(),
    )
}

/// Downscale factor in (0, 1] fitting the screen in the target box; never
/// upscales and is 1.0 for degenerate sizes.
fn compute_scale_in(real_w: i64, real_h: i64, (target_w, target_h): (i64, i64)) -> f64 {
    if real_w <= 0 || real_h <= 0 {
        return 1.0;
    }
    let scale = (target_w as f64 / real_w as f64).min(target_h as f64 / real_h as f64);
    if scale < 1.0 {
        scale
    } else {
        1.0
    }
}

fn scaled_dimensions_in(real_w: i64, real_h: i64, target: (i64, i64)) -> (i64, i64, f64) {
    let scale = compute_scale_in(real_w, real_h, target);
    (
        py_round(real_w as f64 * scale).max(1),
        py_round(real_h as f64 * scale).max(1),
        scale,
    )
}

/// `(scaled_w, scaled_h, scale)`: the image size the model sees.
pub fn scaled_dimensions(real_w: i64, real_h: i64) -> (i64, i64, f64) {
    scaled_dimensions_in(real_w, real_h, target_size())
}

fn usable(scale: f64) -> f64 {
    if scale > 0.0 {
        scale
    } else {
        1.0
    }
}

/// A coordinate the model produced (scaled space) in real pixels.
pub fn api_to_real(x: f64, y: f64, scale: f64) -> (i64, i64) {
    let scale = usable(scale);
    (py_round(x / scale), py_round(y / scale))
}

/// A real-screen coordinate in the scaled space the model sees.
pub fn real_to_api(x: f64, y: f64, scale: f64) -> (i64, i64) {
    let scale = usable(scale);
    (py_round(x * scale), py_round(y * scale))
}

#[cfg(test)]
mod tests {
    use super::*;

    const XGA: (i64, i64) = (DEFAULT_TARGET_W, DEFAULT_TARGET_H);

    #[test]
    fn compute_scale_downscales_to_xga() {
        assert!((compute_scale_in(1920, 1080, XGA) - 1024.0 / 1920.0).abs() < 1e-9);
    }

    #[test]
    fn compute_scale_never_upscales_small_screens() {
        assert_eq!(compute_scale_in(800, 600, XGA), 1.0);
        assert_eq!(compute_scale_in(1024, 768, XGA), 1.0);
    }

    #[test]
    fn compute_scale_degenerate_inputs() {
        assert_eq!(compute_scale_in(0, 0, XGA), 1.0);
        assert_eq!(compute_scale_in(-5, 100, XGA), 1.0);
    }

    #[test]
    fn scaled_dimensions_1920_to_xga() {
        let (w, h, _) = scaled_dimensions_in(1920, 1080, XGA);
        assert_eq!((w, h), (1024, 576));
    }

    #[test]
    fn api_to_real_remaps_model_click_back_to_real_pixels() {
        let scale = compute_scale_in(1920, 1080, XGA);
        assert_eq!(api_to_real(512.0, 288.0, scale), (960, 540));
    }

    #[test]
    fn real_to_api_is_inverse_of_api_to_real() {
        let scale = compute_scale_in(2560, 1440, XGA);
        let (ax, ay) = real_to_api(1280.0, 720.0, scale);
        let (rx, ry) = api_to_real(ax as f64, ay as f64, scale);
        assert!((rx - 1280).abs() <= 1 && (ry - 720).abs() <= 1);
    }

    #[test]
    fn remap_is_identity_when_screen_below_xga() {
        let scale = compute_scale_in(800, 600, XGA);
        assert_eq!(api_to_real(400.0, 300.0, scale), (400, 300));
    }

    #[test]
    fn api_to_real_guards_zero_scale() {
        assert_eq!(api_to_real(100.0, 50.0, 0.0), (100, 50));
        assert_eq!(api_to_real(100.0, 50.0, -2.0), (100, 50));
    }

    #[test]
    fn rounding_is_half_to_even() {
        assert_eq!(api_to_real(5.0, 7.0, 2.0), (2, 4));
        assert_eq!(py_round(-0.5), 0);
    }

    #[test]
    fn target_size_env_override() {
        assert_eq!(
            target_size_from(Some("1280".into()), Some("800".into())),
            (1280, 800)
        );
    }

    #[test]
    fn target_size_bad_env_keeps_default() {
        assert_eq!(target_size_from(Some("not-an-int".into()), None), XGA);
        assert_eq!(target_size_from(Some("-3".into()), Some("0".into())), XGA);
    }
}

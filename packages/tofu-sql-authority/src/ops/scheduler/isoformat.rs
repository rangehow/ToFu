//! Byte-for-byte port of CPython 3.12 `datetime.fromisoformat`
//! (`Modules/_datetimemodule.c`) as used by `_scheduler_claim_due`: the
//! accepted grammar and the instant it denotes (UTC-normalized when aware).

/// A parsed datetime: microseconds since 0001-01-01 local time, plus the UTC
/// offset in microseconds when the value is aware.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct IsoDateTime {
    pub local_micros: i128,
    pub offset_micros: Option<i128>,
}

impl IsoDateTime {
    fn instant(&self) -> i128 {
        self.local_micros - self.offset_micros.unwrap_or(0)
    }
}

/// `(left - right).total_seconds()` as microseconds; `None` is the
/// naive/aware `TypeError`.
pub fn difference_micros(left: &IsoDateTime, right: &IsoDateTime) -> Option<i128> {
    if left.offset_micros.is_some() != right.offset_micros.is_some() {
        return None;
    }
    Some(left.instant() - right.instant())
}

fn at(bytes: &[u8], index: usize) -> u8 {
    bytes.get(index).copied().unwrap_or(0)
}

fn parse_digits(bytes: &[u8], position: usize, count: usize) -> Option<(usize, i64)> {
    let mut value = 0i64;
    for offset in 0..count {
        let byte = at(bytes, position + offset);
        if !byte.is_ascii_digit() {
            return None;
        }
        value = value * 10 + i64::from(byte - b'0');
    }
    Some((position + count, value))
}

fn find_separator(bytes: &[u8]) -> usize {
    let len = bytes.len();
    if len == 7 {
        return 7;
    }
    if at(bytes, 4) == b'-' {
        if at(bytes, 5) == b'W' {
            if len < 8 {
                return usize::MAX;
            }
            if len > 8 && at(bytes, 8) == b'-' {
                if len == 9 {
                    return usize::MAX;
                }
                if len > 10 && at(bytes, 10).is_ascii_digit() {
                    return 8;
                }
                return 10;
            }
            return 8;
        }
        return 10;
    }
    if at(bytes, 4) == b'W' {
        let mut index = 7;
        while index < len && at(bytes, index).is_ascii_digit() {
            index += 1;
        }
        if index < 9 {
            return index;
        }
        return if index % 2 == 0 { 7 } else { 8 };
    }
    8
}

fn is_leap(year: i64) -> bool {
    year % 4 == 0 && (year % 100 != 0 || year % 400 == 0)
}

fn days_before_year(year: i64) -> i64 {
    let y = year - 1;
    y * 365 + y.div_euclid(4) - y.div_euclid(100) + y.div_euclid(400)
}

fn days_in_month(year: i64, month: i64) -> i64 {
    match month {
        2 if is_leap(year) => 29,
        2 => 28,
        4 | 6 | 9 | 11 => 30,
        _ => 31,
    }
}

fn days_before_month(year: i64, month: i64) -> i64 {
    (1..month).map(|m| days_in_month(year, m)).sum()
}

/// `ymd_to_ord`: proleptic Gregorian ordinal, 0001-01-01 is day 1.
fn ymd_to_ord(year: i64, month: i64, day: i64) -> i64 {
    days_before_year(year) + days_before_month(year, month) + day
}

fn ord_to_ymd(ordinal: i64) -> (i64, i64, i64) {
    let mut year = (ordinal * 400).div_euclid(146_097) + 1;
    while days_before_year(year) >= ordinal {
        year -= 1;
    }
    while days_before_year(year + 1) < ordinal {
        year += 1;
    }
    let mut remaining = ordinal - days_before_year(year);
    let mut month = 1;
    while remaining > days_in_month(year, month) {
        remaining -= days_in_month(year, month);
        month += 1;
    }
    (year, month, remaining)
}

fn iso_week1_monday(year: i64) -> i64 {
    let first_day = ymd_to_ord(year, 1, 1);
    let first_weekday = (first_day + 6).rem_euclid(7);
    let mut week1_monday = first_day - first_weekday;
    if first_weekday > 3 {
        week1_monday += 7;
    }
    week1_monday
}

fn iso_to_ymd(year: i64, week: i64, day: i64) -> Option<(i64, i64, i64)> {
    if week <= 0 || week >= 53 {
        let mut out_of_range = true;
        if week == 53 {
            let first_weekday = (ymd_to_ord(year, 1, 1) + 6).rem_euclid(7);
            if first_weekday == 3 || (first_weekday == 2 && is_leap(year)) {
                out_of_range = false;
            }
        }
        if out_of_range {
            return None;
        }
    }
    if day <= 0 || day >= 8 {
        return None;
    }
    let ordinal = iso_week1_monday(year) + (week - 1) * 7 + day - 1;
    Some(ord_to_ymd(ordinal))
}

fn parse_date(bytes: &[u8], len: usize) -> Option<(i64, i64, i64)> {
    let (mut position, year) = parse_digits(bytes, 0, 4)?;
    let uses_separator = at(bytes, position) == b'-';
    if uses_separator {
        position += 1;
    }
    if at(bytes, position) == b'W' {
        position += 1;
        let (next, week) = parse_digits(bytes, position, 2)?;
        position = next;
        let day = if position < len {
            if uses_separator {
                let separator = at(bytes, position);
                position += 1;
                if separator != b'-' {
                    return None;
                }
            }
            parse_digits(bytes, position, 1)?.1
        } else {
            1
        };
        return iso_to_ymd(year, week, day);
    }
    let (next, month) = parse_digits(bytes, position, 2)?;
    position = next;
    if uses_separator {
        let separator = at(bytes, position);
        position += 1;
        if separator != b'-' {
            return None;
        }
    }
    let (_, day) = parse_digits(bytes, position, 2)?;
    Some((year, month, day))
}

/// `parse_hh_mm_ss_ff` over `bytes[start..end]`, NUL-terminated at the
/// end of the whole string: `Some((has_trailing, [h, m, s, us]))`.
fn parse_hh_mm_ss_ff(bytes: &[u8], start: usize, end: usize) -> Option<(bool, [i64; 4])> {
    let mut values = [0i64; 4];
    let mut position = start;
    let mut has_separator = true;
    for (index, slot) in values.iter_mut().take(3).enumerate() {
        let (next, value) = parse_digits(bytes, position, 2)?;
        *slot = value;
        position = next;
        let character = at(bytes, position);
        position += 1;
        if index == 0 {
            has_separator = character == b':';
        }
        if position >= end {
            return Some((character != 0, values));
        } else if has_separator && character == b':' {
            continue;
        } else if character == b'.' || character == b',' {
            break;
        } else if !has_separator {
            position -= 1;
        } else {
            return None;
        }
    }
    let remains = end - position;
    let to_parse = remains.min(6);
    let (mut position, mut micros) = parse_digits(bytes, position, to_parse)?;
    if to_parse < 6 {
        micros *= [100_000, 10_000, 1_000, 100, 10][to_parse - 1];
    }
    values[3] = micros;
    while at(bytes, position).is_ascii_digit() {
        position += 1;
    }
    Some((at(bytes, position) != 0, values))
}

/// `[hour, minute, second, microsecond]` and the optional
/// `(offset_seconds, offset_microseconds)`.
type ParsedTime = ([i64; 4], Option<(i64, i64)>);

/// `parse_isoformat_time`.
fn parse_time(bytes: &[u8], start: usize) -> Option<ParsedTime> {
    let end = bytes.len();
    let mut tz_position = start;
    loop {
        let byte = at(bytes, tz_position);
        if byte == b'Z' || byte == b'+' || byte == b'-' {
            break;
        }
        tz_position += 1;
        if tz_position >= end {
            break;
        }
    }
    let (trailing, clock) = parse_hh_mm_ss_ff(bytes, start, tz_position)?;
    if tz_position == end {
        return if trailing { None } else { Some((clock, None)) };
    }
    if at(bytes, tz_position) == b'Z' {
        return if at(bytes, tz_position + 1) != 0 { None } else { Some((clock, Some((0, 0)))) };
    }
    let sign = if at(bytes, tz_position) == b'-' { -1 } else { 1 };
    let (trailing, zone) = parse_hh_mm_ss_ff(bytes, tz_position + 1, end)?;
    if trailing {
        return None;
    }
    Some((clock, Some((sign * (zone[0] * 3600 + zone[1] * 60 + zone[2]), sign * zone[3]))))
}

/// `datetime.fromisoformat(text)`; `None` is the `ValueError`.
pub fn fromisoformat(text: &str) -> Option<IsoDateTime> {
    let bytes = text.as_bytes();
    let len = bytes.len();
    let separator = find_separator(bytes);
    if separator == usize::MAX {
        return None;
    }
    let (year, month, day) = parse_date(bytes, separator)?;
    let (clock, zone) = if len > separator { parse_time(bytes, separator + 1)? } else { ([0; 4], None) };
    if !(1..=9999).contains(&year) || !(1..=12).contains(&month) || day < 1 || day > days_in_month(year, month) {
        return None;
    }
    let [hour, minute, second, micros] = clock;
    if !(0..=23).contains(&hour) || !(0..=59).contains(&minute) || !(0..=59).contains(&second) {
        return None;
    }
    let offset_micros = match zone {
        None => None,
        Some((seconds, micros)) => {
            let total = i128::from(seconds) * 1_000_000 + i128::from(micros);
            if total.abs() >= 86_400 * 1_000_000 {
                return None;
            }
            Some(total)
        }
    };
    let days = i128::from(ymd_to_ord(year, month, day));
    let seconds = days * 86_400 + i128::from(hour * 3600 + minute * 60 + second);
    Some(IsoDateTime { local_micros: seconds * 1_000_000 + i128::from(micros), offset_micros })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn micros(text: &str) -> Option<i128> {
        fromisoformat(text).map(|value| value.instant())
    }

    #[test]
    fn grammar_matches_cpython() {
        let base = micros("2026-09-05T10:00:00").unwrap();
        for accepted in [
            "20260905T100000",
            "2026-09-05 10:00",
            "2026-09-05T10",
            "2026-09-05x10:00",
            "2026-09-05T1000",
            "2026-09-05T10:00:00Z",
            "2026-09-05T10:00:00-00:00",
        ] {
            assert_eq!(micros(accepted), Some(base), "{accepted}");
        }
        assert_eq!(micros("2026-09-05T10:00:00,5"), Some(base + 500_000));
        assert_eq!(micros("2026-09-05T10:00:00.1234567"), Some(base + 123_456));
        assert_eq!(micros("2026-W36-1"), micros("2026-08-31"));
        assert_eq!(micros("2026W361"), micros("2026-08-31"));
        assert_eq!(micros("2026-09-05T10:00:00+0530"), Some(base - 19_800_000_000));
        assert_eq!(micros("2026-09-05T10:00:00 +05:00"), Some(base - 18_000_000_000));
        for rejected in
            ["2026-09-05T24:00:00", " 2026-09-05", "2026-9-05", "2026-09-05T10:00:00.", "2026-02-30", "", "2026-09-05T"]
        {
            assert_eq!(fromisoformat(rejected), None, "{rejected}");
        }
        let naive = fromisoformat("2026-09-05").unwrap();
        assert_eq!(fromisoformat("2026-09-05Z"), None);
        assert_eq!(micros("2026-09-05T10:00:00:5"), Some(base + 500_000));
        assert_eq!(micros("2026-09-05T10:00:00\0"), Some(base));
        assert_eq!(micros("20260905T10:00"), Some(base));
        assert_eq!(micros("2026-W53-1"), micros("2026-12-28"));
        assert_eq!(micros("2020-W53-7"), micros("2021-01-03"));
        assert_eq!(micros("2026-W36"), micros("2026-08-31"));
        assert!(micros("2026-09-05T10:00:00+23:59:59.999999").is_some());
        assert_eq!(fromisoformat("2026-09-05T10:00:00+24:00"), None);
        assert_eq!(fromisoformat("2026W3"), None);
        let aware = fromisoformat("2026-09-05T00Z").unwrap();
        assert_eq!(difference_micros(&naive, &aware), None);
    }
}

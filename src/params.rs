//! Query-parameter parsing and limits shared by every data endpoint.

use std::collections::HashMap;

use chrono::{DateTime, Utc};

use crate::error::ApiError;

/// Longest time window one request may cover.
pub const MAX_WINDOW_DAYS: i64 = 31;
/// Most bins one time_bins request may return per lane and direction.
pub const MAX_BINS: i64 = 5000;
/// Matches the range traffic.time_bins accepts.
pub const MAX_BIN_SECONDS: i64 = 86_400;
const MAX_CAMERA_ID_LEN: usize = 200;

/// A camera and a half-open time window: start <= t < end.
#[derive(Debug, Clone, PartialEq)]
pub struct Window {
    pub camera_id: String,
    pub start: DateTime<Utc>,
    pub end: DateTime<Utc>,
}

pub fn window(params: &HashMap<String, String>) -> Result<Window, ApiError> {
    let camera_id = required(params, "camera_id")?.trim().to_owned();
    if camera_id.is_empty() {
        return Err(ApiError::bad_request("camera_id must not be empty"));
    }
    if camera_id.len() > MAX_CAMERA_ID_LEN {
        return Err(ApiError::bad_request("camera_id is too long"));
    }
    let start = timestamp(params, "start")?;
    let end = timestamp(params, "end")?;
    if start >= end {
        return Err(ApiError::bad_request("start must be before end"));
    }
    if end - start > chrono::Duration::days(MAX_WINDOW_DAYS) {
        return Err(ApiError::bad_request(format!(
            "the time window can be at most {MAX_WINDOW_DAYS} days"
        )));
    }
    Ok(Window {
        camera_id,
        start,
        end,
    })
}

/// Bin size in seconds, checked against the window so one request can't ask for
/// millions of bins.
pub fn bin_seconds(params: &HashMap<String, String>, window: &Window) -> Result<i32, ApiError> {
    let raw = required(params, "bin_seconds")?;
    let seconds: i64 = raw
        .trim()
        .parse()
        .map_err(|_| ApiError::bad_request("bin_seconds must be a whole number"))?;
    if !(1..=MAX_BIN_SECONDS).contains(&seconds) {
        return Err(ApiError::bad_request(format!(
            "bin_seconds must be between 1 and {MAX_BIN_SECONDS}"
        )));
    }
    let window_seconds = (window.end - window.start).num_seconds();
    if (window_seconds + seconds - 1) / seconds > MAX_BINS {
        return Err(ApiError::bad_request(format!(
            "that window and bin size give more than {MAX_BINS} bins; use a larger bin_seconds"
        )));
    }
    Ok(seconds as i32)
}

fn required<'a>(params: &'a HashMap<String, String>, name: &str) -> Result<&'a str, ApiError> {
    params
        .get(name)
        .map(String::as_str)
        .ok_or_else(|| ApiError::bad_request(format!("{name} is required")))
}

/// RFC 3339 with an explicit offset or Z. A timestamp without one is rejected rather
/// than guessed, the same rule the CSV importer follows.
fn timestamp(params: &HashMap<String, String>, name: &str) -> Result<DateTime<Utc>, ApiError> {
    let raw = required(params, name)?;
    DateTime::parse_from_rfc3339(raw.trim())
        .map(|t| t.with_timezone(&Utc))
        .map_err(|_| {
            ApiError::bad_request(format!(
                "{name} must be an RFC 3339 time with an offset, e.g. 2026-10-09T08:00:00Z"
            ))
        })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn params(pairs: &[(&str, &str)]) -> HashMap<String, String> {
        pairs
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect()
    }

    fn message(err: ApiError) -> String {
        match err {
            ApiError::BadRequest(m) => m,
            other => panic!("expected BadRequest, got {other:?}"),
        }
    }

    const MINUTE: [(&str, &str); 3] = [
        ("camera_id", "sample-camera"),
        ("start", "2026-10-09T08:00:00Z"),
        ("end", "2026-10-09T08:01:00Z"),
    ];

    #[test]
    fn valid_window_is_normalized_to_utc() {
        let w = window(&params(&[
            ("camera_id", " cam-1 "),
            ("start", "2026-10-09T04:00:00-04:00"),
            ("end", "2026-10-09T08:01:00Z"),
        ]))
        .unwrap();
        assert_eq!(w.camera_id, "cam-1");
        assert_eq!(w.start.to_rfc3339(), "2026-10-09T08:00:00+00:00");
    }

    #[test]
    fn missing_params_are_named() {
        assert_eq!(
            message(window(&params(&[])).unwrap_err()),
            "camera_id is required"
        );
        let no_end = params(&MINUTE[..2]);
        assert_eq!(message(window(&no_end).unwrap_err()), "end is required");
    }

    #[test]
    fn timestamps_need_an_offset() {
        let naive = params(&[
            ("camera_id", "c"),
            ("start", "2026-10-09T08:00:00"),
            ("end", "2026-10-09T08:01:00Z"),
        ]);
        assert!(message(window(&naive).unwrap_err()).contains("RFC 3339"));
    }

    #[test]
    fn start_must_be_before_end() {
        let backwards = params(&[
            ("camera_id", "c"),
            ("start", "2026-10-09T08:01:00Z"),
            ("end", "2026-10-09T08:01:00Z"),
        ]);
        assert_eq!(
            message(window(&backwards).unwrap_err()),
            "start must be before end"
        );
    }

    #[test]
    fn window_is_capped() {
        let year = params(&[
            ("camera_id", "c"),
            ("start", "2026-01-01T00:00:00Z"),
            ("end", "2026-12-31T00:00:00Z"),
        ]);
        assert!(message(window(&year).unwrap_err()).contains("31 days"));
    }

    #[test]
    fn blank_camera_is_rejected() {
        let blank = params(&[("camera_id", "  "), MINUTE[1], MINUTE[2]]);
        assert_eq!(
            message(window(&blank).unwrap_err()),
            "camera_id must not be empty"
        );
    }

    #[test]
    fn bin_seconds_range_and_bin_count() {
        let w = window(&params(&MINUTE)).unwrap();
        let with = |b: &str| {
            let mut p = params(&MINUTE);
            p.insert("bin_seconds".into(), b.into());
            bin_seconds(&p, &w)
        };
        assert_eq!(with("10").unwrap(), 10);
        assert!(message(with("0").unwrap_err()).contains("between 1 and"));
        assert!(message(with("86401").unwrap_err()).contains("between 1 and"));
        assert!(message(with("ten").unwrap_err()).contains("whole number"));

        let month = window(&params(&[
            ("camera_id", "c"),
            ("start", "2026-10-01T00:00:00Z"),
            ("end", "2026-10-31T00:00:00Z"),
        ]))
        .unwrap();
        let mut p = params(&[]);
        p.insert("bin_seconds".into(), "60".into());
        assert!(message(bin_seconds(&p, &month).unwrap_err()).contains("5000 bins"));
        p.insert("bin_seconds".into(), "3600".into());
        assert_eq!(bin_seconds(&p, &month).unwrap(), 3600);
    }
}

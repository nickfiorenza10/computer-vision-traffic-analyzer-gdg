//! Florida FL511 camera list, public.
//!
//! FDOT first authorized analysis of public FL511 footage for academic research from
//! 2026-10-28 through 2026-12-01. On 2026-10-29 FDOT confirmed in writing that the project
//! may use FL511 with no end date, public display included (the email is on file with
//! Michael), and Michael made Florida public the same day. The endpoint:
//! - needs no token;
//! - refuses every request after `AUTHORIZED_UNTIL`, if an end date is ever set again;
//! - is cached by Vercel's edge for 5 minutes, so public visitors don't multiply the
//!   requests FL511 sees.
//!
//! It reads camera metadata from the same public FeatureServer layer, with the same
//! Tampa-area filter, as ingestion/fl511.py.

use std::collections::HashMap;
use std::sync::LazyLock;
use std::time::Duration;

use axum::Json;
use axum::extract::{Query, State};
use axum::http::{HeaderValue, header};
use axum::response::{IntoResponse, Response};
use chrono::{DateTime, Utc};
use regex::Regex;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use crate::error::ApiError;

/// End of the FDOT authorization as (instant, date shown to people), or None for no end.
/// None since FDOT's written confirmation of 2026-10-29. A constant on purpose: it changes
/// only with a new authorization, not by configuration.
pub const AUTHORIZED_UNTIL: Option<(&str, &str)> = None;

pub const DEFAULT_LAYER_URL: &str = "https://services.arcgis.com/3wFbqsFPLeKqOlIK/arcgis/rest/services/FL511_Traffic_Cameras/FeatureServer/0";
/// west, south, east, north. Matches TAMPA_BBOX in ingestion/fl511.py.
const TAMPA_BBOX: [f64; 4] = [-82.60, 27.80, -82.30, 28.10];
const FIELDS: &str = "ID,DESCRIPT,COUNTY,HIGHWAY,DIRECTION,LATITUDE,LONGITUDE,IMAGE";
const PAGE_SIZE: usize = 2000;
const MAX_PAGES: usize = 5;
/// Shared caches may keep the list for 5 minutes; cameras rarely change.
const PUBLIC_CACHE: &str = "public, s-maxage=300, stale-while-revalidate=600";

/// Same rules as is_limited_access in ingestion/fl511.py.
static LIMITED_ACCESS: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?i)(^|\b)(i[- ]?\d+|interstate|expressway|turnpike|veterans|selmon)(\b|$)")
        .unwrap()
});
static RAMP_OR_MILE_MARKER: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?i)\b(ramp|interchange|mm\s*\d)").unwrap());

#[derive(Clone)]
pub struct Config {
    pub layer_url: String,
    /// None: no end date.
    pub authorized_until: Option<(DateTime<Utc>, String)>,
    pub client: reqwest::Client,
}

impl Config {
    pub fn new(layer_url: Option<String>) -> Self {
        Self {
            layer_url: layer_url.unwrap_or_else(|| DEFAULT_LAYER_URL.to_owned()),
            authorized_until: AUTHORIZED_UNTIL.map(|(until, through)| {
                (until.parse().expect("valid constant"), through.to_owned())
            }),
            client: reqwest::Client::builder()
                .timeout(Duration::from_secs(10))
                .user_agent("TrafficAnalyzer/1.0")
                .build()
                .expect("HTTP client"),
        }
    }

    /// Refuses once the authorization has ended, if it ever has an end date.
    pub fn authorize(&self, now: DateTime<Utc>) -> Result<(), ApiError> {
        if let Some((until, through)) = &self.authorized_until
            && now >= *until
        {
            return Err(ApiError::Forbidden(format!(
                "the FDOT authorization for FL511 cameras ended {through}"
            )));
        }
        Ok(())
    }
}

pub fn is_limited_access(road: &str, description: &str) -> bool {
    let text = format!("{road} {description}");
    LIMITED_ACCESS.is_match(&text) || RAMP_OR_MILE_MARKER.is_match(&text)
}

fn parse_bbox(raw: Option<&String>) -> Result<[f64; 4], ApiError> {
    let Some(raw) = raw else {
        return Ok(TAMPA_BBOX);
    };
    let parts: Vec<f64> = raw
        .split(',')
        .map(|p| p.trim().parse::<f64>())
        .collect::<Result<_, _>>()
        .map_err(|_| ApiError::bad_request("bbox must be west,south,east,north numbers"))?;
    let [west, south, east, north] = parts[..] else {
        return Err(ApiError::bad_request(
            "bbox must be west,south,east,north numbers",
        ));
    };
    let lon_ok = |v: f64| (-180.0..=180.0).contains(&v);
    let lat_ok = |v: f64| (-90.0..=90.0).contains(&v);
    if !(lon_ok(west) && lon_ok(east) && lat_ok(south) && lat_ok(north)) {
        return Err(ApiError::bad_request("bbox coordinates are out of range"));
    }
    if west >= east || south >= north {
        return Err(ApiError::bad_request(
            "bbox must have west < east and south < north",
        ));
    }
    Ok([west, south, east, north])
}

#[derive(Serialize)]
pub struct Camera {
    pub camera_id: String,
    pub description: String,
    pub road: String,
    pub direction: String,
    pub latitude: f64,
    pub longitude: f64,
    pub image_url: String,
}

#[derive(Deserialize)]
struct LayerPage {
    features: Option<Vec<Feature>>,
    #[serde(rename = "exceededTransferLimit", default)]
    exceeded_transfer_limit: bool,
    error: Option<Value>,
}

#[derive(Deserialize)]
struct Feature {
    attributes: HashMap<String, Value>,
}

fn text(attributes: &HashMap<String, Value>, key: &str) -> String {
    match attributes.get(key) {
        Some(Value::String(s)) => s.trim().to_owned(),
        Some(Value::Number(n)) => n.to_string(),
        _ => String::new(),
    }
}

fn number(attributes: &HashMap<String, Value>, key: &str) -> Option<f64> {
    match attributes.get(key) {
        Some(Value::Number(n)) => n.as_f64(),
        Some(Value::String(s)) => s.trim().parse().ok(),
        _ => None,
    }
}

/// Records without an ID, an HTTPS image or valid coordinates are skipped.
fn camera_from(attributes: &HashMap<String, Value>) -> Option<Camera> {
    let camera_id = text(attributes, "ID");
    let image_url = text(attributes, "IMAGE");
    if camera_id.is_empty() || !image_url.starts_with("https://") {
        return None;
    }
    Some(Camera {
        camera_id,
        description: text(attributes, "DESCRIPT"),
        road: text(attributes, "HIGHWAY"),
        direction: text(attributes, "DIRECTION"),
        latitude: number(attributes, "LATITUDE")?,
        longitude: number(attributes, "LONGITUDE")?,
        image_url,
    })
}

pub async fn fetch_cameras(
    config: &Config,
    bbox: [f64; 4],
    include_highways: bool,
) -> Result<Vec<Camera>, ApiError> {
    let [west, south, east, north] = bbox;
    let geometry = format!("{west},{south},{east},{north}");
    let mut cameras = Vec::new();
    for page in 0..MAX_PAGES {
        let offset = (page * PAGE_SIZE).to_string();
        let page_size = PAGE_SIZE.to_string();
        let response = config
            .client
            .get(format!("{}/query", config.layer_url))
            .query(&[
                ("where", "COUNTY='Hillsborough'"),
                ("geometry", geometry.as_str()),
                ("geometryType", "esriGeometryEnvelope"),
                ("inSR", "4326"),
                ("spatialRel", "esriSpatialRelIntersects"),
                ("outFields", FIELDS),
                ("returnGeometry", "false"),
                ("resultRecordCount", page_size.as_str()),
                ("resultOffset", offset.as_str()),
                ("f", "json"),
            ])
            .send()
            .await
            .and_then(|r| r.error_for_status())
            .map_err(|e| ApiError::Upstream(format!("FL511 request failed: {e}")))?;
        let body: LayerPage = response
            .json()
            .await
            .map_err(|e| ApiError::Upstream(format!("FL511 response was not valid JSON: {e}")))?;
        if let Some(error) = body.error {
            return Err(ApiError::Upstream(format!(
                "FL511 returned an error: {error}"
            )));
        }
        let features = body
            .features
            .ok_or_else(|| ApiError::Upstream("FL511 response had no features list".to_owned()))?;
        let count = features.len();
        cameras.extend(features.iter().filter_map(|f| camera_from(&f.attributes)));
        if !body.exceeded_transfer_limit || count == 0 {
            break;
        }
    }
    if !include_highways {
        cameras.retain(|c| !is_limited_access(&c.road, &c.description));
    }
    cameras.sort_by(|a, b| {
        (
            a.road.to_lowercase(),
            a.description.to_lowercase(),
            &a.camera_id,
        )
            .cmp(&(
                b.road.to_lowercase(),
                b.description.to_lowercase(),
                &b.camera_id,
            ))
    });
    Ok(cameras)
}

pub async fn cameras(
    State(config): State<Config>,
    Query(query): Query<HashMap<String, String>>,
) -> Result<Response, ApiError> {
    config.authorize(Utc::now())?;
    let bbox = parse_bbox(query.get("bbox"))?;
    let include_highways = query.get("include_highways").is_some_and(|v| v == "true");
    let cameras = fetch_cameras(&config, bbox, include_highways).await?;

    let mut response = Json(json!({
        "source": "FL511",
        "authorized_through": config.authorized_until.as_ref().map(|(_, through)| through),
        "bbox": bbox,
        "cameras": cameras,
    }))
    .into_response();
    response.headers_mut().insert(
        header::CACHE_CONTROL,
        HeaderValue::from_static(PUBLIC_CACHE),
    );
    Ok(response)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn status(result: Result<(), ApiError>) -> &'static str {
        match result {
            Ok(()) => "ok",
            Err(ApiError::Forbidden(_)) => "forbidden",
            Err(_) => "other",
        }
    }

    #[test]
    fn open_with_no_end_date_since_the_2026_10_29_confirmation() {
        let config = Config::new(None);
        assert!(config.authorized_until.is_none());
        let years_later: DateTime<Utc> = "2030-01-01T00:00:00Z".parse().unwrap();
        assert_eq!(status(config.authorize(years_later)), "ok");
    }

    #[test]
    fn refuses_after_an_end_date_if_one_is_set() {
        let mut config = Config::new(None);
        config.authorized_until = Some((
            "2026-12-02T05:00:00Z".parse().unwrap(),
            "2026-12-01".to_owned(),
        ));
        let last_minute: DateTime<Utc> = "2026-12-02T04:59:59Z".parse().unwrap();
        let ended: DateTime<Utc> = "2026-12-02T05:00:00Z".parse().unwrap();
        assert_eq!(status(config.authorize(last_minute)), "ok");
        assert_eq!(status(config.authorize(ended)), "forbidden");
    }

    #[test]
    fn limited_access_matches_ingestion_rules() {
        for (road, description) in [
            ("I-275", "at downtown"),
            ("Veterans Expressway", "at Linebaugh Ave"),
            ("SR 60", "I-275 interchange"),
            ("SR 589", "MM 4.3"),
        ] {
            assert!(is_limited_access(road, description), "{road} {description}");
        }
        assert!(!is_limited_access("Himes Avenue", "at Lemon St"));
        assert!(!is_limited_access(
            "for City of Tampa cameras",
            "Dale Mabry at Hillsborough Ave"
        ));
    }

    #[test]
    fn bbox_parsing() {
        assert_eq!(parse_bbox(None).unwrap(), TAMPA_BBOX);
        let custom = "-82.5,27.9,-82.4,28.0".to_owned();
        assert_eq!(
            parse_bbox(Some(&custom)).unwrap(),
            [-82.5, 27.9, -82.4, 28.0]
        );
        for bad in [
            "-82.3,27.8,-82.6,28.1",
            "1,2,3",
            "a,b,c,d",
            "-200,27.8,-82.3,28.1",
        ] {
            assert!(parse_bbox(Some(&bad.to_owned())).is_err(), "{bad}");
        }
    }

    #[test]
    fn records_without_https_image_or_coordinates_are_skipped() {
        let record = |image: &str, lat: Value| -> HashMap<String, Value> {
            [
                ("ID", json!(1043)),
                ("DESCRIPT", json!("Dale Mabry at Hillsborough Ave")),
                ("HIGHWAY", json!("for City of Tampa cameras")),
                ("DIRECTION", json!("N")),
                ("LATITUDE", lat),
                ("LONGITUDE", json!(-82.505)),
                ("IMAGE", json!(image)),
            ]
            .into_iter()
            .map(|(k, v)| (k.to_owned(), v))
            .collect()
        };
        let ok = camera_from(&record("https://example.test/1043.jpg", json!(27.99))).unwrap();
        assert_eq!(ok.camera_id, "1043");
        assert!(camera_from(&record("http://example.test/1043.jpg", json!(27.99))).is_none());
        assert!(camera_from(&record("https://example.test/1043.jpg", json!("n/a"))).is_none());
    }
}

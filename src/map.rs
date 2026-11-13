//! The live traffic map: `GET /api/map/cameras` (GeoJSON) and the live-analysis proxy.
//!
//! Camera locations and the latest 30-minute sweep come from the database
//! (`traffic.camera_map()`, database/map_schema.sql). Live analysis runs on the worker
//! (worker/), which this API reaches over a tunnel at `WORKER_URL`, sending `WORKER_TOKEN`
//! itself so the token never reaches browsers. When live cannot go ahead, calls answer 503
//! with an `error_code` naming the part that failed (`worker_unreachable`, `worker_timeout`,
//! `worker_error`, ... or the worker's own code), and the map still works from stored sweeps.

use std::time::Duration;

use axum::body::Body;
use axum::extract::{Path, State};
use axum::http::{HeaderMap, HeaderValue, StatusCode, header};
use axum::response::{IntoResponse, Response};
use axum::{Json, http};
use chrono::{DateTime, Utc};
use regex::Regex;
use serde::Deserialize;
use serde_json::{Value, json};
use sqlx::PgPool;
use std::sync::LazyLock;

use crate::error::ApiError;

const MAP_CACHE: &str = "public, s-maxage=60, stale-while-revalidate=120";
/// Starting a session looks the camera up and probes its stream first, and Caltrans can
/// take about 9 s to answer 404 for a dead stream, so start gets more than the 8 s default.
const START_TIMEOUT: Duration = Duration::from_secs(30);
/// A Caltrans camera ("caltrans-D3-41", sent to the worker as "D3-41") or a Florida one by its
/// FL511 map id ("fl511-2849", sent as it is).
static CAMERA_ID: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^(?:caltrans-(D(?:[1-9]|1[0-2])-\d{1,5})|(fl511-\d{1,7}))$").unwrap()
});
static SESSION_ID: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[A-Za-z0-9_-]{1,64}$").unwrap());

#[derive(sqlx::FromRow)]
struct MapRow {
    camera_id: String,
    name: String,
    county: String,
    route: String,
    direction: String,
    latitude: f64,
    longitude: f64,
    has_stream: bool,
    image_url: Option<String>,
    swept: bool,
    level: Option<String>,
    vehicles_mean: Option<f32>,
    swept_at: Option<DateTime<Utc>>,
}

pub async fn cameras(State(pool): State<PgPool>) -> Result<Response, ApiError> {
    let rows: Vec<MapRow> = sqlx::query_as("SELECT * FROM traffic.camera_map()")
        .fetch_all(&pool)
        .await?;
    let features: Vec<Value> = rows
        .into_iter()
        .map(|r| {
            json!({
                "type": "Feature",
                "geometry": { "type": "Point", "coordinates": [r.longitude, r.latitude] },
                "properties": {
                    "camera_id": r.camera_id,
                    "name": r.name,
                    "county": r.county,
                    "route": r.route,
                    "direction": r.direction,
                    "has_stream": r.has_stream,
                    "image_url": r.image_url,
                    "swept": r.swept,
                    "level": r.level,
                    "vehicles_mean": r.vehicles_mean,
                    "swept_at": r.swept_at,
                },
            })
        })
        .collect();
    let mut response =
        Json(json!({ "type": "FeatureCollection", "features": features })).into_response();
    response
        .headers_mut()
        .insert(header::CACHE_CONTROL, HeaderValue::from_static(MAP_CACHE));
    Ok(response)
}

/// Where the worker is and how to reach it. Without `url` and `token`, live is offline.
#[derive(Clone)]
pub struct WorkerConfig {
    pub url: Option<String>,
    pub token: Option<String>,
    /// When set, starting a live session needs `Authorization: Bearer <access code>`.
    pub access_code: Option<String>,
    pub client: reqwest::Client,
}

impl WorkerConfig {
    pub fn new(url: Option<String>, token: Option<String>, access_code: Option<String>) -> Self {
        let non_empty = |v: Option<String>| v.filter(|s| !s.trim().is_empty());
        Self {
            url: non_empty(url).map(|u| u.trim_end_matches('/').to_owned()),
            token: non_empty(token),
            access_code: non_empty(access_code),
            client: reqwest::Client::builder()
                .timeout(Duration::from_secs(8))
                .user_agent("TrafficAnalyzer-API/1.0")
                .build()
                .expect("HTTP client"),
        }
    }

    fn endpoint(&self) -> Result<(&str, &str), ApiError> {
        match (&self.url, &self.token) {
            (Some(url), Some(token)) => Ok((url, token)),
            _ => Err(live_down(
                "worker_not_configured",
                "Live analysis isn't set up on this deployment: it has no worker address or token.",
            )),
        }
    }

    fn check_access(&self, headers: &HeaderMap) -> Result<(), ApiError> {
        let Some(expected) = &self.access_code else {
            return Ok(());
        };
        let given = headers
            .get(header::AUTHORIZATION)
            .and_then(|v| v.to_str().ok())
            .and_then(|v| v.strip_prefix("Bearer "))
            .unwrap_or("");
        if constant_time_eq(given.as_bytes(), expected.as_bytes()) {
            Ok(())
        } else {
            Err(ApiError::Unauthorized)
        }
    }
}

/// Compares without stopping at the first difference, so timing doesn't reveal the code.
fn constant_time_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

fn live_down(code: &'static str, message: impl Into<String>) -> ApiError {
    ApiError::LiveDown {
        code,
        message: message.into(),
    }
}

/// Why a request to the worker never got an answer.
fn unreachable_worker(error: &reqwest::Error) -> ApiError {
    if error.is_timeout() {
        live_down(
            "worker_timeout",
            "The live worker took too long to answer. It may be busy opening a slow stream.",
        )
    } else {
        live_down(
            "worker_unreachable",
            "The live worker can't be reached. The GPU PC may be off or asleep, or its tunnel is down.",
        )
    }
}

fn bad_reply() -> ApiError {
    live_down(
        "worker_bad_reply",
        "The live worker sent a reply the site couldn't read.",
    )
}

fn session_path(id: &str) -> Result<&str, ApiError> {
    if SESSION_ID.is_match(id) {
        Ok(id)
    } else {
        Err(ApiError::bad_request("invalid session id"))
    }
}

/// Pass the worker's JSON and status through; every failure says which part failed.
///
/// A 503 that carries an `error_code` is the worker refusing this camera (no video, stream
/// down) and is passed through unchanged. Any other server error keeps the worker's own
/// words when it sent some, under `worker_error`.
async fn relay_json(request: reqwest::RequestBuilder) -> Result<Response, ApiError> {
    let reply = request.send().await.map_err(|e| unreachable_worker(&e))?;
    let status = StatusCode::from_u16(reply.status().as_u16()).unwrap_or(StatusCode::BAD_GATEWAY);
    // The tunnel answers 502/504 itself when nothing listens behind it: that is the GPU PC
    // or its worker being down, not the worker failing.
    if matches!(
        status,
        StatusCode::BAD_GATEWAY | StatusCode::GATEWAY_TIMEOUT
    ) {
        return Err(live_down(
            "worker_unreachable",
            "The live worker can't be reached. The GPU PC may be off or asleep, or the worker isn't running.",
        ));
    }
    let body: Value = if status.is_server_error() {
        let body: Value = reply.json().await.unwrap_or(Value::Null);
        let refused = status == StatusCode::SERVICE_UNAVAILABLE
            && body.get("error_code").and_then(Value::as_str).is_some();
        if !refused {
            let said = body.get("error").and_then(Value::as_str);
            return Err(live_down(
                "worker_error",
                match said {
                    Some(text) => format!("The live worker failed: {text}"),
                    None => format!("The live worker answered with an error (HTTP {status})."),
                },
            ));
        }
        body
    } else {
        reply.json().await.map_err(|_| bad_reply())?
    };
    let mut response = (status, Json(body)).into_response();
    response
        .headers_mut()
        .insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
    Ok(response)
}

#[derive(Deserialize)]
pub struct StartRequest {
    camera_id: String,
}

pub async fn start(
    State(worker): State<WorkerConfig>,
    headers: HeaderMap,
    Json(request): Json<StartRequest>,
) -> Result<Response, ApiError> {
    worker.check_access(&headers)?;
    let worker_id = CAMERA_ID
        .captures(request.camera_id.trim())
        .and_then(|c| c.get(1).or_else(|| c.get(2)).map(|m| m.as_str().to_owned()))
        .ok_or_else(|| {
            ApiError::bad_request("camera_id must look like caltrans-D3-41 or fl511-2849")
        })?;
    let (url, token) = worker.endpoint()?;
    relay_json(
        worker
            .client
            .post(format!("{url}/live/start"))
            .timeout(START_TIMEOUT)
            .bearer_auth(token)
            .json(&json!({ "camera_id": worker_id })),
    )
    .await
}

pub async fn status(
    State(worker): State<WorkerConfig>,
    Path(id): Path<String>,
) -> Result<Response, ApiError> {
    let id = session_path(&id)?;
    let (url, token) = worker.endpoint()?;
    relay_json(
        worker
            .client
            .get(format!("{url}/live/{id}"))
            .bearer_auth(token),
    )
    .await
}

pub async fn stop(
    State(worker): State<WorkerConfig>,
    Path(id): Path<String>,
) -> Result<Response, ApiError> {
    let id = session_path(&id)?;
    let (url, token) = worker.endpoint()?;
    relay_json(
        worker
            .client
            .delete(format!("{url}/live/{id}"))
            .bearer_auth(token),
    )
    .await
}

pub async fn frame(
    State(worker): State<WorkerConfig>,
    Path(id): Path<String>,
) -> Result<Response, ApiError> {
    let id = session_path(&id)?;
    let (url, token) = worker.endpoint()?;
    let reply = worker
        .client
        .get(format!("{url}/live/{id}/frame.jpg"))
        .bearer_auth(token)
        .send()
        .await
        .map_err(|e| unreachable_worker(&e))?;
    if reply.status() == reqwest::StatusCode::NOT_FOUND {
        return Err(ApiError::NotFound("no frame yet".to_owned()));
    }
    if !reply.status().is_success() {
        return Err(live_down(
            "worker_error",
            format!(
                "The live worker answered with an error (HTTP {}).",
                reply.status()
            ),
        ));
    }
    let is_jpeg = reply
        .headers()
        .get(reqwest::header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .is_some_and(|v| v.starts_with("image/jpeg"));
    if !is_jpeg {
        return Err(bad_reply());
    }
    let bytes = reply.bytes().await.map_err(|_| bad_reply())?;
    Ok(http::Response::builder()
        .header(header::CONTENT_TYPE, "image/jpeg")
        .header(header::CACHE_CONTROL, "no-store")
        .body(Body::from(bytes))
        .expect("response"))
}

//! Read-only JSON API over Sebastian's `traffic` schema (database/schema.sql).
//!
//! Every handler calls the database's own functions (`traffic.lane_totals`,
//! `traffic.time_bins`) or a plain aggregate, so counting rules live in one place.
//! Missing bins are left out, not filled with zeros: no row means no data.

pub mod error;
pub mod fl511;
pub mod map;
pub mod params;

use std::collections::HashMap;
use std::time::Duration;

use axum::extract::{FromRef, Query, State};
use axum::http::{HeaderValue, StatusCode, header};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use chrono::{DateTime, Utc};
use serde::Serialize;
use serde_json::{Value, json};
use sqlx::PgPool;
use sqlx::postgres::PgPoolOptions;

use crate::error::ApiError;
use crate::params::Window;

/// Short shared caching for data responses: counts for past windows rarely change.
const DATA_CACHE: &str = "public, s-maxage=30, stale-while-revalidate=60";

/// A small, lazily connected pool. Warm function instances reuse it between requests.
pub fn pool(database_url: &str) -> Result<PgPool, sqlx::Error> {
    PgPoolOptions::new()
        .max_connections(3)
        .acquire_timeout(Duration::from_secs(5))
        .after_connect(|conn, _| {
            Box::pin(async move {
                sqlx::query("SET statement_timeout = '5s'")
                    .execute(&mut *conn)
                    .await?;
                Ok(())
            })
        })
        .connect_lazy(database_url)
}

/// Everything the handlers share. Handlers take just the part they need
/// (`State<PgPool>` or `State<fl511::Config>`).
#[derive(Clone)]
pub struct AppState {
    pub pool: PgPool,
    pub fl511: fl511::Config,
    pub worker: map::WorkerConfig,
}

impl AppState {
    /// Reads DATABASE_URL_READONLY (required), FL511_LAYER_URL (optional override,
    /// used by tests), and for live analysis WORKER_URL + WORKER_TOKEN (optional; without
    /// them live is "offline") and LIVE_ACCESS_CODE (optional; when set, starting a live
    /// session needs it).
    pub fn from_env() -> Result<Self, Box<dyn std::error::Error + Send + Sync>> {
        let url = std::env::var("DATABASE_URL_READONLY")
            .map_err(|_| "DATABASE_URL_READONLY is not set")?;
        Ok(Self {
            pool: pool(&url)?,
            fl511: fl511::Config::new(std::env::var("FL511_LAYER_URL").ok()),
            worker: map::WorkerConfig::new(
                std::env::var("WORKER_URL").ok(),
                std::env::var("WORKER_TOKEN").ok(),
                std::env::var("LIVE_ACCESS_CODE").ok(),
            ),
        })
    }
}

impl FromRef<AppState> for PgPool {
    fn from_ref(state: &AppState) -> Self {
        state.pool.clone()
    }
}

impl FromRef<AppState> for map::WorkerConfig {
    fn from_ref(state: &AppState) -> Self {
        state.worker.clone()
    }
}

impl FromRef<AppState> for fl511::Config {
    fn from_ref(state: &AppState) -> Self {
        state.fl511.clone()
    }
}

pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/api/health", get(health))
        .route("/api/cameras", get(cameras))
        .route("/api/lane_totals", get(lane_totals))
        .route("/api/time_bins", get(time_bins))
        .route("/api/classes", get(classes))
        .route("/api/fl511/cameras", get(fl511::cameras))
        .route("/api/map/cameras", get(map::cameras))
        .route("/api/live", post(map::start))
        .route("/api/live/{id}", get(map::status).delete(map::stop))
        .route("/api/live/{id}/frame.jpg", get(map::frame))
        .fallback(not_found)
        .with_state(state)
}

fn cached(body: Value) -> Response {
    let mut response = Json(body).into_response();
    response
        .headers_mut()
        .insert(header::CACHE_CONTROL, HeaderValue::from_static(DATA_CACHE));
    response
}

fn window_json(w: &Window) -> Value {
    json!({ "camera_id": w.camera_id, "start": w.start, "end": w.end })
}

async fn health(State(pool): State<PgPool>) -> Response {
    let database_ok = sqlx::query("SELECT 1").execute(&pool).await.is_ok();
    let (status, database) = if database_ok {
        (StatusCode::OK, "ok")
    } else {
        (StatusCode::SERVICE_UNAVAILABLE, "error")
    };
    let mut response = (
        status,
        Json(json!({ "status": "ok", "database": database })),
    )
        .into_response();
    response
        .headers_mut()
        .insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
    response
}

#[derive(Serialize, sqlx::FromRow)]
struct CameraRow {
    camera_id: String,
    sources: i64,
    events: i64,
    first_event: Option<DateTime<Utc>>,
    last_event: Option<DateTime<Utc>>,
}

async fn cameras(State(pool): State<PgPool>) -> Result<Response, ApiError> {
    let rows: Vec<CameraRow> = sqlx::query_as(
        "SELECT s.camera_id,
                count(DISTINCT s.source_id) AS sources,
                count(e.source_id) AS events,
                min(e.occurred_at) AS first_event,
                max(e.occurred_at) AS last_event
         FROM traffic.sources s
         LEFT JOIN traffic.events e ON e.source_id = s.source_id
         GROUP BY s.camera_id
         ORDER BY s.camera_id",
    )
    .fetch_all(&pool)
    .await?;
    Ok(cached(json!({ "cameras": rows })))
}

#[derive(Serialize, sqlx::FromRow)]
struct TotalRow {
    lane: i32,
    direction: String,
    vehicle_count: i64,
}

async fn lane_totals(
    State(pool): State<PgPool>,
    Query(query): Query<HashMap<String, String>>,
) -> Result<Response, ApiError> {
    let w = params::window(&query)?;
    let totals: Vec<TotalRow> = sqlx::query_as(
        "SELECT lane, direction, vehicle_count FROM traffic.lane_totals($1, $2, $3)",
    )
    .bind(&w.camera_id)
    .bind(w.start)
    .bind(w.end)
    .fetch_all(&pool)
    .await?;
    let mut body = window_json(&w);
    body["totals"] = json!(totals);
    Ok(cached(body))
}

#[derive(Serialize, sqlx::FromRow)]
struct BinRow {
    bin_start: DateTime<Utc>,
    lane: i32,
    direction: String,
    vehicle_count: i64,
}

async fn time_bins(
    State(pool): State<PgPool>,
    Query(query): Query<HashMap<String, String>>,
) -> Result<Response, ApiError> {
    let w = params::window(&query)?;
    let bin_seconds = params::bin_seconds(&query, &w)?;
    let bins: Vec<BinRow> = sqlx::query_as(
        "SELECT bin_start, lane, direction, vehicle_count
         FROM traffic.time_bins($1, $2, $3, $4)",
    )
    .bind(&w.camera_id)
    .bind(w.start)
    .bind(w.end)
    .bind(bin_seconds)
    .fetch_all(&pool)
    .await?;
    let mut body = window_json(&w);
    body["bin_seconds"] = json!(bin_seconds);
    body["bins"] = json!(bins);
    Ok(cached(body))
}

#[derive(Serialize, sqlx::FromRow)]
struct ClassRow {
    lane: i32,
    direction: String,
    vehicle_class: String,
    vehicle_count: i64,
}

/// Same query as the class breakdown in database/queries.sql.
async fn classes(
    State(pool): State<PgPool>,
    Query(query): Query<HashMap<String, String>>,
) -> Result<Response, ApiError> {
    let w = params::window(&query)?;
    let classes: Vec<ClassRow> = sqlx::query_as(
        "SELECT e.lane, e.direction, e.vehicle_class, count(*) AS vehicle_count
         FROM traffic.events e
         JOIN traffic.sources s ON s.source_id = e.source_id
         WHERE s.camera_id = $1 AND e.occurred_at >= $2 AND e.occurred_at < $3
         GROUP BY e.lane, e.direction, e.vehicle_class
         ORDER BY e.lane, e.direction, e.vehicle_class",
    )
    .bind(&w.camera_id)
    .bind(w.start)
    .bind(w.end)
    .fetch_all(&pool)
    .await?;
    let mut body = window_json(&w);
    body["classes"] = json!(classes);
    Ok(cached(body))
}

async fn not_found() -> Response {
    (StatusCode::NOT_FOUND, Json(json!({ "error": "not found" }))).into_response()
}

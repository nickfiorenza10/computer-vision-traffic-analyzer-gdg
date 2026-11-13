//! Integration test against a disposable PostgreSQL database, like database/tests.
//! Skipped unless TEST_DATABASE_URL is set. It applies database/schema.sql, loads
//! data/sample_events.csv under its own camera ID, checks every endpoint against
//! counts computed independently from the CSV, and removes only its own rows.

use std::collections::BTreeMap;

use axum::Router;
use axum::body::Body;
use axum::http::{Request, StatusCode, header};
use http_body_util::BodyExt;
use serde_json::Value;
use sqlx::PgPool;
use tower::ServiceExt;

const SCHEMA: &str = include_str!("../database/schema.sql");
const SAMPLE: &str = include_str!("../data/sample_events.csv");
const START: &str = "2026-10-09T08:00:00Z";
const END: &str = "2026-10-09T08:01:00Z";

struct Event {
    timestamp: String,
    frame: i64,
    track_id: i64,
    class: String,
    lane: i32,
    direction: String,
}

fn sample_events() -> Vec<Event> {
    SAMPLE
        .lines()
        .skip(1)
        .filter(|line| !line.trim().is_empty())
        .map(|line| {
            let f: Vec<&str> = line.trim().split(',').collect();
            Event {
                timestamp: f[0].to_owned(),
                frame: f[1].parse().unwrap(),
                track_id: f[2].parse().unwrap(),
                class: f[3].to_owned(),
                lane: f[4].parse().unwrap(),
                direction: f[5].to_owned(),
            }
        })
        .collect()
}

async fn load(pool: &PgPool, source_id: &str, camera_id: &str, events: &[Event]) {
    sqlx::raw_sql(SCHEMA)
        .execute(pool)
        .await
        .expect("apply schema");
    let mut tx = pool.begin().await.unwrap();
    sqlx::query(
        "INSERT INTO traffic.sources
             (source_id, camera_id, csv_filename, csv_sha256, naive_offset, event_count)
         VALUES ($1, $2, 'sample_events.csv', repeat('0', 64), '+00:00', $3)",
    )
    .bind(source_id)
    .bind(camera_id)
    .bind(events.len() as i64)
    .execute(&mut *tx)
    .await
    .unwrap();
    for e in events {
        // The sample's timestamps are naive; the README imports them as UTC.
        sqlx::query(
            "INSERT INTO traffic.events
                 (source_id, occurred_at, frame, track_id, vehicle_class, lane, direction)
             VALUES ($1, $2::timestamp AT TIME ZONE 'UTC', $3, $4, $5, $6, $7)",
        )
        .bind(source_id)
        .bind(&e.timestamp)
        .bind(e.frame)
        .bind(e.track_id)
        .bind(&e.class)
        .bind(e.lane)
        .bind(&e.direction)
        .execute(&mut *tx)
        .await
        .unwrap();
    }
    tx.commit().await.unwrap();
}

async fn cleanup(pool: &PgPool, source_id: &str) {
    sqlx::query("DELETE FROM traffic.events WHERE source_id = $1")
        .bind(source_id)
        .execute(pool)
        .await
        .unwrap();
    sqlx::query("DELETE FROM traffic.sources WHERE source_id = $1")
        .bind(source_id)
        .execute(pool)
        .await
        .unwrap();
}

async fn get(app: &Router, uri: &str) -> (StatusCode, Option<String>, Value) {
    let response = app
        .clone()
        .oneshot(Request::get(uri).body(Body::empty()).unwrap())
        .await
        .unwrap();
    let status = response.status();
    let cache = response
        .headers()
        .get(header::CACHE_CONTROL)
        .map(|v| v.to_str().unwrap().to_owned());
    let bytes = response.into_body().collect().await.unwrap().to_bytes();
    (status, cache, serde_json::from_slice(&bytes).unwrap())
}

fn window(path: &str, camera: &str, extra: &str) -> String {
    format!("/api/{path}?camera_id={camera}&start={START}&end={END}{extra}")
}

#[tokio::test]
async fn endpoints_match_the_sample_csv() {
    let Ok(url) = std::env::var("TEST_DATABASE_URL") else {
        eprintln!("skipped: set TEST_DATABASE_URL for the PostgreSQL integration test");
        return;
    };
    let pool = traffic_api::pool(&url).unwrap();
    let suffix = format!(
        "{}-{}",
        std::process::id(),
        chrono::Utc::now().timestamp_micros()
    );
    let source_id = format!("rust-api-test-{suffix}");
    let camera = format!("rust-api-test-{suffix}");
    let events = sample_events();
    assert_eq!(
        events.len(),
        787,
        "sample_events.csv changed; update this test"
    );

    load(&pool, &source_id, &camera, &events).await;
    let app = traffic_api::router(traffic_api::AppState {
        pool: pool.clone(),
        fl511: traffic_api::fl511::Config::new(None),
        worker: traffic_api::map::WorkerConfig::new(None, None, None),
    });
    // Run the checks in their own task so a failed assertion still reaches cleanup.
    let outcome = tokio::spawn(check_endpoints(app, camera, events)).await;
    cleanup(&pool, &source_id).await;
    if let Err(err) = outcome {
        std::panic::resume_unwind(err.into_panic());
    }
}

async fn check_endpoints(app: Router, camera: String, events: Vec<Event>) {
    let (app, camera, events) = (&app, camera.as_str(), events.as_slice());
    // health
    let (status, cache, body) = get(app, "/api/health").await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["database"], "ok");
    assert_eq!(cache.as_deref(), Some("no-store"));

    // cameras
    let (status, cache, body) = get(app, "/api/cameras").await;
    assert_eq!(status, StatusCode::OK);
    assert!(cache.unwrap().contains("s-maxage=30"));
    let ours = body["cameras"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["camera_id"] == camera)
        .expect("test camera listed");
    assert_eq!(ours["sources"], 1);
    assert_eq!(ours["events"], 787);
    let first = events.iter().map(|e| e.timestamp.as_str()).min().unwrap();
    assert_eq!(ours["first_event"], format!("{first}Z"));

    // lane_totals
    let mut expected: BTreeMap<(i64, String), i64> = BTreeMap::new();
    for e in events {
        *expected
            .entry((e.lane as i64, e.direction.clone()))
            .or_default() += 1;
    }
    let (status, _, body) = get(app, &window("lane_totals", camera, "")).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["start"], START);
    let totals: BTreeMap<(i64, String), i64> = body["totals"]
        .as_array()
        .unwrap()
        .iter()
        .map(|t| {
            let key = (
                t["lane"].as_i64().unwrap(),
                t["direction"].as_str().unwrap().to_owned(),
            );
            (key, t["vehicle_count"].as_i64().unwrap())
        })
        .collect();
    assert_eq!(totals, expected);
    assert_eq!(totals.values().sum::<i64>(), 787);

    // time_bins: 10-second bins, keyed on the CSV's seconds field
    let mut expected_bins: BTreeMap<(String, i64, String), i64> = BTreeMap::new();
    for e in events {
        let second: u32 = e.timestamp[17..19].parse().unwrap();
        let bin = format!("2026-10-09T08:00:{:02}Z", second / 10 * 10);
        *expected_bins
            .entry((bin, e.lane as i64, e.direction.clone()))
            .or_default() += 1;
    }
    let (status, _, body) = get(app, &window("time_bins", camera, "&bin_seconds=10")).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["bin_seconds"], 10);
    let bins: BTreeMap<(String, i64, String), i64> = body["bins"]
        .as_array()
        .unwrap()
        .iter()
        .map(|b| {
            let key = (
                b["bin_start"].as_str().unwrap().to_owned(),
                b["lane"].as_i64().unwrap(),
                b["direction"].as_str().unwrap().to_owned(),
            );
            (key, b["vehicle_count"].as_i64().unwrap())
        })
        .collect();
    assert_eq!(bins, expected_bins);

    // classes
    let mut expected_classes: BTreeMap<(i64, String, String), i64> = BTreeMap::new();
    for e in events {
        *expected_classes
            .entry((e.lane as i64, e.direction.clone(), e.class.clone()))
            .or_default() += 1;
    }
    let (status, _, body) = get(app, &window("classes", camera, "")).await;
    assert_eq!(status, StatusCode::OK);
    let classes: BTreeMap<(i64, String, String), i64> = body["classes"]
        .as_array()
        .unwrap()
        .iter()
        .map(|c| {
            let key = (
                c["lane"].as_i64().unwrap(),
                c["direction"].as_str().unwrap().to_owned(),
                c["vehicle_class"].as_str().unwrap().to_owned(),
            );
            (key, c["vehicle_count"].as_i64().unwrap())
        })
        .collect();
    assert_eq!(classes, expected_classes);

    // A window with no data returns empty arrays, not zeros or an error.
    let (status, _, body) = get(
        app,
        &format!("/api/lane_totals?camera_id={camera}&start=2026-10-10T08:00:00Z&end=2026-10-10T09:00:00Z"),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["totals"], serde_json::json!([]));

    // Bad input is a 400 with a message and is never cached.
    let (status, cache, body) = get(
        app,
        &format!("/api/lane_totals?camera_id={camera}&start={START}"),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["error"], "end is required");
    assert_eq!(cache.as_deref(), Some("no-store"));

    let (status, _, _) = get(app, "/api/nope").await;
    assert_eq!(status, StatusCode::NOT_FOUND);
}

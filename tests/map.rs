//! The live traffic map endpoints.
//! - `GET /api/map/cameras` against a disposable PostgreSQL (skipped without TEST_DATABASE_URL).
//! - The live-analysis proxy against a fake worker on localhost (no database needed).

use std::sync::{Arc, Mutex};

use axum::Router;
use axum::body::Body;
use axum::extract::{Path, State};
use axum::http::{HeaderMap, Request, StatusCode, header};
use axum::response::IntoResponse;
use axum::routing::{get, post};
use http_body_util::BodyExt;
use serde_json::{Value, json};
use tower::ServiceExt;
use traffic_api::map::WorkerConfig;
use traffic_api::{AppState, fl511};

const SCHEMA: &str = include_str!("../database/schema.sql");
const MAP_SCHEMA: &str = include_str!("../database/map_schema.sql");
const WORKER_TOKEN: &str = "worker-test-token";

fn state(pool: sqlx::PgPool, worker: WorkerConfig) -> AppState {
    AppState {
        pool,
        fl511: fl511::Config::new(None),
        worker,
    }
}

fn unused_pool() -> sqlx::PgPool {
    traffic_api::pool("postgresql://unused:unused@127.0.0.1:1/unused").unwrap()
}

async fn call(app: &Router, request: Request<Body>) -> (StatusCode, HeaderMap, Vec<u8>) {
    let response = app.clone().oneshot(request).await.unwrap();
    let status = response.status();
    let headers = response.headers().clone();
    let bytes = response
        .into_body()
        .collect()
        .await
        .unwrap()
        .to_bytes()
        .to_vec();
    (status, headers, bytes)
}

fn json_of(bytes: &[u8]) -> Value {
    serde_json::from_slice(bytes).unwrap()
}

#[tokio::test]
async fn map_cameras_returns_geojson_with_latest_sweep() {
    let Ok(url) = std::env::var("TEST_DATABASE_URL") else {
        eprintln!("skipped: set TEST_DATABASE_URL for the PostgreSQL map test");
        return;
    };
    let pool = traffic_api::pool(&url).unwrap();
    sqlx::raw_sql(SCHEMA).execute(&pool).await.unwrap();
    sqlx::raw_sql(MAP_SCHEMA).execute(&pool).await.unwrap();

    // Unique ids so parallel or repeated runs don't collide; removed at the end.
    let n = (std::process::id() % 90_000) + 10_000;
    let swept = format!("caltrans-D12-{n}");
    let unswept = format!("caltrans-D11-{n}");
    sqlx::query(
        "INSERT INTO traffic.cameras (camera_id, district, name, county, route, direction, latitude, longitude, has_stream, image_url)
         VALUES ($1, 12, 'Test Swept', 'Orange', 'I-5', 'North', 33.7, -117.8, true, 'https://example.test/a.jpg'),
                ($2, 11, 'Test Unswept', 'San Diego', 'I-8', '', 32.7, -117.1, false, NULL)",
    )
    .bind(&swept)
    .bind(&unswept)
    .execute(&pool)
    .await
    .unwrap();
    sqlx::query(
        "INSERT INTO traffic.camera_sweeps (camera_id, swept_at, frames, vehicles_mean, level)
         VALUES ($1, '2026-10-29T18:30:00Z', 8, 9.5, 'moderate'), ($1, '2026-10-29T19:00:00Z', 8, 14.25, 'heavy')",
    )
    .bind(&swept)
    .execute(&pool)
    .await
    .unwrap();

    let app = traffic_api::router(state(pool.clone(), WorkerConfig::new(None, None, None)));
    let (status, headers, bytes) = call(
        &app,
        Request::get("/api/map/cameras")
            .body(Body::empty())
            .unwrap(),
    )
    .await;
    sqlx::query("DELETE FROM traffic.cameras WHERE camera_id = ANY($1)")
        .bind(vec![swept.clone(), unswept.clone()])
        .execute(&pool)
        .await
        .unwrap();

    assert_eq!(status, StatusCode::OK);
    assert!(
        headers[header::CACHE_CONTROL]
            .to_str()
            .unwrap()
            .contains("s-maxage=60")
    );
    let body = json_of(&bytes);
    assert_eq!(body["type"], "FeatureCollection");
    let find = |id: &str| {
        body["features"]
            .as_array()
            .unwrap()
            .iter()
            .find(|f| f["properties"]["camera_id"] == id)
            .cloned()
            .expect("camera in map")
    };
    let a = find(&swept);
    assert_eq!(a["geometry"]["coordinates"], json!([-117.8, 33.7]));
    assert_eq!(a["properties"]["swept"], true);
    assert_eq!(a["properties"]["level"], "heavy"); // the latest of the two sweeps
    assert_eq!(a["properties"]["vehicles_mean"], 14.25);
    assert_eq!(a["properties"]["swept_at"], "2026-10-29T19:00:00Z");
    assert_eq!(a["properties"]["has_stream"], true);
    let b = find(&unswept);
    assert_eq!(b["properties"]["swept"], false);
    assert_eq!(b["properties"]["level"], Value::Null);
    assert_eq!(b["properties"]["image_url"], Value::Null);
}

/// (method, path, JSON body) of one request the fake worker received.
type Seen = (String, String, Option<Value>);

/// A fake worker that records what it was asked and answers like the real one.
#[derive(Clone, Default)]
struct Fake {
    seen: Arc<Mutex<Vec<Seen>>>,
    busy: bool,
    /// Answer every start with 503 and this body, as the worker does when it refuses a camera.
    refuse: Option<Value>,
    /// Answer every start with 502 and an HTML page, as the tunnel does with no worker behind it.
    tunnel_down: bool,
}

async fn fake_worker(fake: Fake) -> String {
    fn authorized(headers: &HeaderMap) -> bool {
        headers
            .get(header::AUTHORIZATION)
            .and_then(|v| v.to_str().ok())
            == Some(&format!("Bearer {WORKER_TOKEN}"))
    }
    async fn start(
        State(f): State<Fake>,
        headers: HeaderMap,
        axum::Json(body): axum::Json<Value>,
    ) -> axum::response::Response {
        f.seen
            .lock()
            .unwrap()
            .push(("POST".into(), "/live/start".into(), Some(body)));
        if !authorized(&headers) {
            return StatusCode::UNAUTHORIZED.into_response();
        }
        if f.busy {
            return (
                StatusCode::CONFLICT,
                axum::Json(
                    json!({"error": "another live analysis is running", "retry_after_s": 75}),
                ),
            )
                .into_response();
        }
        if f.tunnel_down {
            return (StatusCode::BAD_GATEWAY, "<html>Bad gateway</html>").into_response();
        }
        if let Some(body) = f.refuse {
            return (StatusCode::SERVICE_UNAVAILABLE, axum::Json(body)).into_response();
        }
        (
            StatusCode::ACCEPTED,
            axum::Json(json!({"session_id": "s-1", "state": "starting"})),
        )
            .into_response()
    }
    async fn status(State(f): State<Fake>, Path(id): Path<String>) -> axum::response::Response {
        f.seen
            .lock()
            .unwrap()
            .push(("GET".into(), format!("/live/{id}"), None));
        axum::Json(json!({"state": "running", "camera_id": "D3-41", "elapsed_s": 12, "level": "moderate",
            "flow_per_min": 31.5, "density": 7.2, "speed": 0.8, "detector": "yolo", "frame_seq": 120, "error": null}))
            .into_response()
    }
    async fn stop(State(f): State<Fake>, Path(id): Path<String>) -> axum::response::Response {
        f.seen
            .lock()
            .unwrap()
            .push(("DELETE".into(), format!("/live/{id}"), None));
        axum::Json(json!({"state": "done"})).into_response()
    }
    async fn frame(Path(id): Path<String>) -> axum::response::Response {
        if id == "none" {
            return StatusCode::NOT_FOUND.into_response();
        }
        (
            [(header::CONTENT_TYPE, "image/jpeg")],
            vec![0xFF, 0xD8, 0xFF, 0xE0, 1, 2, 3],
        )
            .into_response()
    }
    let app = Router::new()
        .route("/live/start", post(start))
        .route("/live/{id}", get(status).delete(stop))
        .route("/live/{id}/frame.jpg", get(frame))
        .with_state(fake);
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    format!("http://{addr}")
}

fn post_live(camera_id: &str, code: Option<&str>) -> Request<Body> {
    let mut builder = Request::post("/api/live").header(header::CONTENT_TYPE, "application/json");
    if let Some(c) = code {
        builder = builder.header(header::AUTHORIZATION, format!("Bearer {c}"));
    }
    builder
        .body(Body::from(json!({ "camera_id": camera_id }).to_string()))
        .unwrap()
}

#[tokio::test]
async fn live_proxy_forwards_to_the_worker() {
    let fake = Fake::default();
    let base = fake_worker(fake.clone()).await;
    let app = traffic_api::router(state(
        unused_pool(),
        WorkerConfig::new(Some(format!("{base}/")), Some(WORKER_TOKEN.into()), None),
    ));

    let (status, headers, bytes) = call(&app, post_live("caltrans-D3-41", None)).await;
    assert_eq!(status, StatusCode::ACCEPTED);
    assert_eq!(headers[header::CACHE_CONTROL], "no-store");
    assert_eq!(json_of(&bytes)["session_id"], "s-1");
    // The worker gets the bare Caltrans id and the server-side token.
    assert_eq!(
        fake.seen.lock().unwrap()[0].2,
        Some(json!({"camera_id": "D3-41"}))
    );
    // A Florida camera goes through by its FL511 map id, unchanged.
    let (status, _, _) = call(&app, post_live("fl511-2849", None)).await;
    assert_eq!(status, StatusCode::ACCEPTED);
    assert_eq!(
        fake.seen.lock().unwrap()[1].2,
        Some(json!({"camera_id": "fl511-2849"}))
    );

    let (status, _, bytes) = call(
        &app,
        Request::get("/api/live/s-1").body(Body::empty()).unwrap(),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(json_of(&bytes)["level"], "moderate");

    let (status, headers, bytes) = call(
        &app,
        Request::get("/api/live/s-1/frame.jpg")
            .body(Body::empty())
            .unwrap(),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(headers[header::CONTENT_TYPE], "image/jpeg");
    assert_eq!(headers[header::CACHE_CONTROL], "no-store");
    assert_eq!(&bytes[..3], &[0xFF, 0xD8, 0xFF]);

    let (status, _, _) = call(
        &app,
        Request::get("/api/live/none/frame.jpg")
            .body(Body::empty())
            .unwrap(),
    )
    .await;
    assert_eq!(status, StatusCode::NOT_FOUND);

    let (status, _, _) = call(
        &app,
        Request::delete("/api/live/s-1")
            .body(Body::empty())
            .unwrap(),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert!(
        fake.seen
            .lock()
            .unwrap()
            .iter()
            .any(|(m, p, _)| m == "DELETE" && p == "/live/s-1")
    );
}

#[tokio::test]
async fn live_proxy_passes_busy_and_rejects_bad_input() {
    let base = fake_worker(Fake {
        busy: true,
        ..Fake::default()
    })
    .await;
    let app = traffic_api::router(state(
        unused_pool(),
        WorkerConfig::new(Some(base), Some(WORKER_TOKEN.into()), None),
    ));
    let (status, _, bytes) = call(&app, post_live("caltrans-D3-41", None)).await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(json_of(&bytes)["retry_after_s"], 75);

    for bad in [
        "D3-41",
        "caltrans-D13-1",
        "caltrans-D3-41/../x",
        "",
        "fl511-",
        "fl511-12a",
        "fl511-2849/../x",
        "FL511-2849",
    ] {
        let (status, _, _) = call(&app, post_live(bad, None)).await;
        assert_eq!(status, StatusCode::BAD_REQUEST, "{bad}");
    }
    let (status, _, _) = call(
        &app,
        Request::get("/api/live/a%2Fb").body(Body::empty()).unwrap(),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
}

#[tokio::test]
async fn live_proxy_passes_the_reason_a_camera_is_refused() {
    // FL511 has only a still for this camera: the panel must say so, not "offline".
    let reason = json!({
        "error": "FL511 has no live video for this camera; only its still.",
        "error_code": "no_video",
    });
    let base = fake_worker(Fake {
        refuse: Some(reason.clone()),
        ..Fake::default()
    })
    .await;
    let app = traffic_api::router(state(
        unused_pool(),
        WorkerConfig::new(Some(base), Some(WORKER_TOKEN.into()), None),
    ));
    let (status, _, bytes) = call(&app, post_live("fl511-1043", None)).await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
    assert_eq!(json_of(&bytes), reason);

    // A 503 without a code is the worker itself in trouble: its words survive, labelled.
    let base = fake_worker(Fake {
        refuse: Some(json!({"error": "model failed to load"})),
        ..Fake::default()
    })
    .await;
    let app = traffic_api::router(state(
        unused_pool(),
        WorkerConfig::new(Some(base), Some(WORKER_TOKEN.into()), None),
    ));
    let (status, _, bytes) = call(&app, post_live("fl511-1043", None)).await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
    let body = json_of(&bytes);
    assert_eq!(body["error_code"], "worker_error");
    assert_eq!(
        body["error"],
        "The live worker failed: model failed to load"
    );
}

#[tokio::test]
async fn a_tunnel_with_no_worker_behind_it_reads_as_unreachable() {
    let base = fake_worker(Fake {
        tunnel_down: true,
        ..Fake::default()
    })
    .await;
    let app = traffic_api::router(state(
        unused_pool(),
        WorkerConfig::new(Some(base), Some(WORKER_TOKEN.into()), None),
    ));
    let (status, _, bytes) = call(&app, post_live("fl511-2849", None)).await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
    assert_eq!(json_of(&bytes)["error_code"], "worker_unreachable");
}

#[tokio::test]
async fn live_is_offline_without_a_reachable_worker() {
    // Not configured
    let app = traffic_api::router(state(unused_pool(), WorkerConfig::new(None, None, None)));
    let (status, _, bytes) = call(&app, post_live("caltrans-D3-41", None)).await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
    assert_eq!(json_of(&bytes)["error_code"], "worker_not_configured");
    // Configured but nothing listening (port 1)
    let app = traffic_api::router(state(
        unused_pool(),
        WorkerConfig::new(
            Some("http://127.0.0.1:1".into()),
            Some(WORKER_TOKEN.into()),
            None,
        ),
    ));
    let (status, _, bytes) = call(
        &app,
        Request::get("/api/live/s-1").body(Body::empty()).unwrap(),
    )
    .await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
    let body = json_of(&bytes);
    assert_eq!(body["error_code"], "worker_unreachable");
    assert!(body["error"].as_str().unwrap().contains("GPU PC"));
}

#[tokio::test]
async fn live_access_code_is_enforced_when_set() {
    let base = fake_worker(Fake::default()).await;
    let app = traffic_api::router(state(
        unused_pool(),
        WorkerConfig::new(
            Some(base),
            Some(WORKER_TOKEN.into()),
            Some("demo-code".into()),
        ),
    ));
    let (status, _, _) = call(&app, post_live("caltrans-D3-41", None)).await;
    assert_eq!(status, StatusCode::UNAUTHORIZED);
    let (status, _, _) = call(&app, post_live("caltrans-D3-41", Some("wrong"))).await;
    assert_eq!(status, StatusCode::UNAUTHORIZED);
    let (status, _, _) = call(&app, post_live("caltrans-D3-41", Some("demo-code"))).await;
    assert_eq!(status, StatusCode::ACCEPTED);
}

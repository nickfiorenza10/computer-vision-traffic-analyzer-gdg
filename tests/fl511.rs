//! The public FL511 endpoint, end to end through the router, against a fake
//! FL511 layer on localhost. Never contacts FL511 and needs no database.

use axum::body::Body;
use axum::extract::Query;
use axum::http::{Request, StatusCode, header};
use axum::routing::get;
use axum::{Json, Router};
use http_body_util::BodyExt;
use serde_json::{Value, json};
use std::collections::HashMap;
use tower::ServiceExt;
use traffic_api::{AppState, fl511};

fn record(id: i64, road: &str, description: &str) -> Value {
    json!({ "attributes": {
        "ID": id, "DESCRIPT": description, "COUNTY": "Hillsborough", "HIGHWAY": road,
        "DIRECTION": "N", "LATITUDE": 27.99, "LONGITUDE": -82.5,
        "IMAGE": format!("https://images.example.test/{id}.jpg"),
    }})
}

/// A fake FeatureServer layer: /good/query returns cameras, /broken/query an ArcGIS error.
async fn fake_fl511() -> String {
    async fn good(Query(q): Query<HashMap<String, String>>) -> Json<Value> {
        assert_eq!(q["where"], "COUNTY='Hillsborough'");
        assert_eq!(q["geometry"], "-82.6,27.8,-82.3,28.1");
        Json(json!({ "features": [
            record(1043, "for City of Tampa cameras", "Dale Mabry at Hillsborough Ave"),
            record(2001, "I-275", "at downtown"),
            record(838, "Himes Avenue", "at Lemon St"),
            { "attributes": { "ID": 9, "IMAGE": "http://insecure.example.test/9.jpg",
                              "LATITUDE": 27.9, "LONGITUDE": -82.4 } },
        ]}))
    }
    async fn broken() -> Json<Value> {
        Json(json!({ "error": { "code": 400, "message": "Invalid query" } }))
    }
    let app = Router::new()
        .route("/good/query", get(good))
        .route("/broken/query", get(broken));
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    format!("http://{addr}")
}

fn app(layer_url: String) -> Router {
    // The database pool is never used by these routes; connect_lazy doesn't connect.
    let pool = traffic_api::pool("postgresql://unused:unused@127.0.0.1:1/unused").unwrap();
    traffic_api::router(AppState {
        pool,
        fl511: fl511::Config::new(Some(layer_url)),
        worker: traffic_api::map::WorkerConfig::new(None, None, None),
    })
}

async fn get_json(app: &Router, uri: &str) -> (StatusCode, header::HeaderMap, Value) {
    let response = app
        .clone()
        .oneshot(Request::get(uri).body(Body::empty()).unwrap())
        .await
        .unwrap();
    let status = response.status();
    let headers = response.headers().clone();
    let bytes = response.into_body().collect().await.unwrap().to_bytes();
    (status, headers, serde_json::from_slice(&bytes).unwrap())
}

fn ids(body: &Value) -> Vec<String> {
    body["cameras"]
        .as_array()
        .unwrap()
        .iter()
        .map(|c| c["camera_id"].as_str().unwrap().to_owned())
        .collect()
}

#[tokio::test]
async fn lists_cameras_to_anyone() {
    let base = fake_fl511().await;
    let app = app(format!("{base}/good"));

    let (status, headers, body) = get_json(&app, "/api/fl511/cameras").await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        headers[header::CACHE_CONTROL],
        "public, s-maxage=300, stale-while-revalidate=600"
    );
    assert!(headers.get(header::VARY).is_none());
    // No end date since FDOT's written confirmation of 2026-10-29.
    assert!(body["authorized_through"].is_null());
    // Interstate filtered out, insecure image skipped, sorted by road then description
    assert_eq!(ids(&body), vec!["1043", "838"]);
    assert_eq!(
        body["cameras"][0]["image_url"],
        "https://images.example.test/1043.jpg"
    );

    let (_, _, all) = get_json(&app, "/api/fl511/cameras?include_highways=true").await;
    assert_eq!(ids(&all), vec!["1043", "838", "2001"]);
}

#[tokio::test]
async fn refuses_after_an_end_date_if_one_is_set() {
    let base = fake_fl511().await;
    let pool = traffic_api::pool("postgresql://unused:unused@127.0.0.1:1/unused").unwrap();
    let mut config = fl511::Config::new(Some(format!("{base}/good")));
    config.authorized_until = Some((
        "2020-01-01T00:00:00Z".parse().unwrap(),
        "2026-12-01".to_owned(),
    ));
    let app = traffic_api::router(AppState {
        pool,
        fl511: config,
        worker: traffic_api::map::WorkerConfig::new(None, None, None),
    });
    let (status, _, body) = get_json(&app, "/api/fl511/cameras").await;
    assert_eq!(status, StatusCode::FORBIDDEN);
    assert!(body["error"].as_str().unwrap().contains("2026-12-01"));
}

#[tokio::test]
async fn upstream_errors_are_hidden_and_bad_bbox_is_400() {
    let base = fake_fl511().await;
    let broken = app(format!("{base}/broken"));
    let (status, _, body) = get_json(&broken, "/api/fl511/cameras").await;
    assert_eq!(status, StatusCode::BAD_GATEWAY);
    assert_eq!(body["error"], "the camera data source is not responding");

    let good = app(format!("{base}/good"));
    let (status, _, body) = get_json(&good, "/api/fl511/cameras?bbox=1,2,3").await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert!(body["error"].as_str().unwrap().contains("bbox"));
}

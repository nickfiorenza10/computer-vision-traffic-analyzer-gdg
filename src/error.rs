//! One error type for every endpoint, rendered as `{"error": "..."}`.

use axum::Json;
use axum::http::{HeaderValue, StatusCode, header};
use axum::response::{IntoResponse, Response};
use serde_json::json;

/// SQLSTATE raised by traffic.lane_totals / traffic.time_bins for bad arguments.
const INVALID_PARAMETER_VALUE: &str = "22023";

#[derive(Debug)]
pub enum ApiError {
    /// The caller's fault; the message is safe to show.
    BadRequest(String),
    /// Missing or wrong access token.
    Unauthorized,
    /// Allowed in principle, refused now (e.g. an authorization has expired).
    Forbidden(String),
    /// A third-party service failed. The detail is logged, never returned.
    Upstream(String),
    /// The feature isn't configured on this deployment, or its backend is unreachable.
    Unavailable(String),
    /// Live analysis could not go ahead, with a code saying which part failed so the panel
    /// can tell a person what happened instead of a bare "offline".
    LiveDown { code: &'static str, message: String },
    /// Nothing to return yet (e.g. no live frame).
    NotFound(String),
    /// Anything else. The detail is logged, never returned.
    Internal(String),
}

impl ApiError {
    pub fn bad_request(message: impl Into<String>) -> Self {
        Self::BadRequest(message.into())
    }
}

impl From<sqlx::Error> for ApiError {
    fn from(err: sqlx::Error) -> Self {
        if let Some(db) = err.as_database_error()
            && db.code().as_deref() == Some(INVALID_PARAMETER_VALUE)
        {
            return Self::BadRequest(db.message().to_owned());
        }
        Self::Internal(err.to_string())
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        if let Self::LiveDown { code, message } = self {
            let mut response = (
                StatusCode::SERVICE_UNAVAILABLE,
                Json(json!({ "error": message, "error_code": code })),
            )
                .into_response();
            response
                .headers_mut()
                .insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
            return response;
        }
        let (status, message) = match self {
            Self::LiveDown { .. } => unreachable!("handled above"),
            Self::BadRequest(message) => (StatusCode::BAD_REQUEST, message),
            Self::Unauthorized => (
                StatusCode::UNAUTHORIZED,
                "a valid research access token is required".to_owned(),
            ),
            Self::Forbidden(message) => (StatusCode::FORBIDDEN, message),
            Self::Upstream(detail) => {
                eprintln!("upstream error: {detail}");
                (
                    StatusCode::BAD_GATEWAY,
                    "the camera data source is not responding".to_owned(),
                )
            }
            Self::Unavailable(message) => (StatusCode::SERVICE_UNAVAILABLE, message),
            Self::NotFound(message) => (StatusCode::NOT_FOUND, message),
            Self::Internal(detail) => {
                eprintln!("internal error: {detail}");
                (
                    StatusCode::INTERNAL_SERVER_ERROR,
                    "internal server error".to_owned(),
                )
            }
        };
        let unauthorized = status == StatusCode::UNAUTHORIZED;
        let mut response = (status, Json(json!({ "error": message }))).into_response();
        let headers = response.headers_mut();
        headers.insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
        if unauthorized {
            headers.insert(header::WWW_AUTHENTICATE, HeaderValue::from_static("Bearer"));
        }
        response
    }
}

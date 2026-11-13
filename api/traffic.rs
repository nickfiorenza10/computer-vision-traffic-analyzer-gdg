//! The Vercel function. vercel.json rewrites every /api/* request here, and the
//! router in src/lib.rs dispatches on the original path.

use tower::ServiceBuilder;
use vercel_runtime::Error;
use vercel_runtime::axum::VercelLayer;

#[tokio::main]
async fn main() -> Result<(), Error> {
    let state = traffic_api::AppState::from_env()?;
    let app = ServiceBuilder::new()
        .layer(VercelLayer::new())
        .service(traffic_api::router(state));
    vercel_runtime::run(app).await
}

//! Local development server: the same router as the Vercel function, on a plain port.
//! Not deployed. Run it with `cargo run --example dev_server` (see api/README.md).

use std::net::SocketAddr;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let state = traffic_api::AppState::from_env()?;
    let port: u16 = std::env::var("PORT")
        .ok()
        .and_then(|p| p.parse().ok())
        .unwrap_or(3001);
    let app = traffic_api::router(state);
    let addr = SocketAddr::from(([0, 0, 0, 0], port));
    println!("traffic API on http://localhost:{port}/api/health");
    axum::serve(tokio::net::TcpListener::bind(addr).await?, app).await?;
    Ok(())
}

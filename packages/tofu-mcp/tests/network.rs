//! `network.rs`: route plan order, connect-only failover, reports and no
//! replay, against loopback sockets and a scripted binding.

use async_trait::async_trait;
use serde_json::{json, Value};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use tofu_mcp::callback::{BindingCalls, NoBinding};
use tofu_mcp::errors::CallError;
use tofu_mcp::network::{NetworkClient, NetworkError};
use tokio::io::{AsyncReadExt, AsyncWriteExt};

struct Plan {
    routes: Value,
    reports: Mutex<Vec<(String, bool, bool)>>,
}

#[async_trait]
impl BindingCalls for Plan {
    async fn ask(&self, method: &str, params: Value) -> Result<Value, CallError> {
        match method {
            "network.route_candidates" => Ok(self.routes.clone()),
            "network.report" => {
                self.reports.lock().unwrap().push((
                    params["route_id"].as_str().unwrap_or_default().into(),
                    params["ok"] == json!(true),
                    params["latency_ms"].as_f64().is_some_and(|l| l >= 0.0),
                ));
                Ok(Value::Null)
            }
            _ => Err(CallError::Unavailable(method.into())),
        }
    }
}

/// Answers every request with `status` and counts requests.
async fn server(status: u16) -> (String, Arc<AtomicUsize>) {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}/x", listener.local_addr().unwrap());
    let hits = Arc::new(AtomicUsize::new(0));
    let counter = hits.clone();
    tokio::spawn(async move {
        while let Ok((mut stream, _)) = listener.accept().await {
            counter.fetch_add(1, Ordering::SeqCst);
            let mut buffer = [0u8; 4096];
            let _ = stream.read(&mut buffer).await;
            let response =
                format!("HTTP/1.1 {status} X\r\ncontent-length: 2\r\nconnection: close\r\n\r\nok");
            let _ = stream.write_all(response.as_bytes()).await;
        }
    });
    (url, hits)
}

/// A closed port: the listener is dropped, so connections are refused.
async fn closed_port() -> String {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    drop(listener);
    url
}

fn plan(routes: Value) -> Arc<Plan> {
    Arc::new(Plan {
        routes,
        reports: Mutex::new(Vec::new()),
    })
}

#[tokio::test]
async fn a_refused_route_fails_over_in_plan_order_and_both_outcomes_are_reported() {
    let (url, hits) = server(200).await;
    // The proxy route is refused; the direct route reaches the server.
    let binding = plan(json!([
        {"route_id": "pool:refused", "proxy_url": closed_port().await},
        {"route_id": "direct", "proxy_url": null},
    ]));
    let network = NetworkClient::new(binding.clone(), None);
    let response = network.send(&url, |c| c.get(&url)).await.unwrap();
    assert_eq!(response.status().as_u16(), 200);
    assert_eq!(hits.load(Ordering::SeqCst), 1);
    assert_eq!(
        *binding.reports.lock().unwrap(),
        vec![
            ("pool:refused".to_string(), false, false),
            ("direct".to_string(), true, true)
        ]
    );
}

#[tokio::test]
async fn an_error_status_is_reported_ok_and_never_replayed() {
    let (url, hits) = server(503).await;
    let binding = plan(json!([
        {"route_id": "direct", "proxy_url": null},
        {"route_id": "direct:again", "proxy_url": null},
    ]));
    let network = NetworkClient::new(binding.clone(), None);
    let response = network.send(&url, |c| c.get(&url)).await.unwrap();
    assert_eq!(response.status().as_u16(), 503);
    assert_eq!(hits.load(Ordering::SeqCst), 1);
    assert_eq!(
        *binding.reports.lock().unwrap(),
        vec![("direct".to_string(), true, true)]
    );
}

#[tokio::test]
async fn exhausted_routes_are_a_typed_connect_failure() {
    let target = format!("{}/x", closed_port().await);
    let binding = plan(json!([
        {"route_id": "pool:a", "proxy_url": closed_port().await},
        {"route_id": "pool:b", "proxy_url": closed_port().await},
    ]));
    let network = NetworkClient::new(binding.clone(), None);
    let error = network.send(&target, |c| c.get(&target)).await.unwrap_err();
    assert!(matches!(error, NetworkError::RoutesExhausted));
    assert!(error.is_connect());
    assert_eq!(error.to_string(), "No configured network route connected");
    let reports = binding.reports.lock().unwrap();
    assert_eq!(
        reports
            .iter()
            .map(|r| (r.0.as_str(), r.1))
            .collect::<Vec<_>>(),
        vec![("pool:a", false), ("pool:b", false)]
    );
}

#[tokio::test]
async fn a_dropped_request_is_neither_replayed_nor_reported() {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}/x", listener.local_addr().unwrap());
    let hits = Arc::new(AtomicUsize::new(0));
    let counter = hits.clone();
    tokio::spawn(async move {
        while let Ok((mut stream, _)) = listener.accept().await {
            counter.fetch_add(1, Ordering::SeqCst);
            let mut buffer = [0u8; 4096];
            let _ = stream.read(&mut buffer).await;
            drop(stream);
        }
    });
    let binding = plan(json!([
        {"route_id": "direct", "proxy_url": null},
        {"route_id": "direct:again", "proxy_url": null},
    ]));
    let network = NetworkClient::new(binding.clone(), None);
    let error = network.send(&url, |c| c.get(&url)).await.unwrap_err();
    assert!(!error.is_connect());
    assert_eq!(hits.load(Ordering::SeqCst), 1);
    assert!(binding.reports.lock().unwrap().is_empty());
}

#[tokio::test]
async fn without_a_binding_the_client_connects_direct() {
    let (url, hits) = server(200).await;
    let network = NetworkClient::new(Arc::new(NoBinding), None);
    let response = network.send(&url, |c| c.get(&url)).await.unwrap();
    assert_eq!(response.status().as_u16(), 200);
    assert_eq!(hits.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn a_failing_authority_fails_closed() {
    struct Broken;
    #[async_trait]
    impl BindingCalls for Broken {
        async fn ask(&self, _: &str, _: Value) -> Result<Value, CallError> {
            Err(CallError::Error("route table unreadable".into()))
        }
    }
    let (url, hits) = server(200).await;
    let network = NetworkClient::new(Arc::new(Broken), None);
    let error = network.send(&url, |c| c.get(&url)).await.unwrap_err();
    assert!(matches!(error, NetworkError::Authority(_)));
    assert_eq!(hits.load(Ordering::SeqCst), 0);
}

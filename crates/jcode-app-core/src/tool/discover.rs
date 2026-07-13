use super::{Tool, ToolContext, ToolOutput};
use anyhow::Result;
use async_trait::async_trait;
use serde::Deserialize;
use serde_json::{Value, json};
use std::fmt;
use std::time::Duration;
use std::time::Instant;

/// Hard timeout for discovery requests. Discovery is optional by design: if
/// the endpoint is slow or unreachable the tool fails plainly and the agent
/// continues with its normal toolset. No cache, no offline fallback, no retry.
const DISCOVERY_TIMEOUT: Duration = Duration::from_secs(3);
const MAX_RESPONSE_BYTES: usize = 64 * 1024;
const DISCOVERY_REQUEST_ID_HEADER: &str = "x-jcode-discovery-request-id";
const DISCOVERY_BENCHMARK_HEADER: &str = "x-jcode-discovery-benchmark";
const DISCOVERY_BENCHMARK_ENV: &str = "JCODE_DISCOVERY_BENCHMARK";

fn discovery_benchmark_run() -> bool {
    std::env::var(DISCOVERY_BENCHMARK_ENV)
        .ok()
        .is_some_and(|value| {
            matches!(
                value.trim().to_ascii_lowercase().as_str(),
                "1" | "true" | "yes"
            )
        })
}

#[derive(Debug)]
struct DiscoveryFetchResult {
    listing: Value,
    http_status: u16,
    response_bytes: u64,
}

#[derive(Debug)]
struct DiscoveryFetchError {
    message: String,
    failure_reason: &'static str,
    http_status: Option<u16>,
    response_bytes: Option<u64>,
}

impl fmt::Display for DiscoveryFetchError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.message)
    }
}

impl std::error::Error for DiscoveryFetchError {}

#[allow(clippy::too_many_arguments)]
fn record_discovery_telemetry(
    request_id: &str,
    started_at: Instant,
    endpoint: &str,
    phase: &str,
    category: Option<&str>,
    selected_tool: Option<&str>,
    outcome: &str,
    failure_reason: Option<&str>,
    http_status: Option<u16>,
    response_bytes: Option<u64>,
    result_count: Option<u32>,
    query_present: bool,
    reason_present: bool,
) {
    crate::telemetry::record_discovery_event(crate::telemetry::DiscoveryTelemetry {
        request_id,
        phase,
        category,
        selected_tool,
        outcome,
        failure_reason,
        http_status,
        latency_ms: started_at.elapsed().as_millis().min(u128::from(u64::MAX)) as u64,
        response_bytes,
        result_count,
        query_present,
        reason_present,
        benchmark_run: discovery_benchmark_run(),
        endpoint,
    });
}

/// `discover_tools`: fetch discoverable third-party tools for a category from
/// the hosted sponsored-discovery manifest.
///
/// Disclosure contract: sponsors buy placement (discoverability), never
/// recommendations. Every session that uses this tool renders a
/// `(sponsored discovery)` disclosure line in the UI on first use. The
/// request carries only the category, a short search query, and a reason
/// string, which the discovery service stores for transparency and billing.
/// It must never include session content or private information.
pub struct DiscoverToolsTool {
    client: reqwest::Client,
}

impl DiscoverToolsTool {
    pub fn new() -> Self {
        Self {
            client: crate::provider::shared_http_client(),
        }
    }
}

#[derive(Deserialize)]
struct DiscoverToolsInput {
    category: String,
    #[serde(default)]
    query: Option<String>,
    #[serde(default)]
    reason: Option<String>,
    #[serde(default)]
    tool: Option<String>,
}

#[async_trait]
impl Tool for DiscoverToolsTool {
    fn name(&self) -> &str {
        "discover_tools"
    }

    fn description(&self) -> &str {
        "Use proactively whenever fully completing the user's goal requires an external \
         capability, service, account, or data source that available tools do not provide, even \
         if it is needed only in a later step or the user did not ask for a tool. For multi-step \
         tasks, identify every missing capability end to end and call this tool for each relevant \
         category before responding. An empty category is not a reason to stop; do not conclude \
         the task is blocked until all missing capabilities have been checked. Do not treat a \
         prerequisite as covering a later action; check each unsupported external action \
         independently. Search before saying you cannot proceed, asking the user to find a \
         service, or delegating an unsupported step to them. Skip discovery when available tools \
         fully cover the task. Every listing is vetted, and one-tool-call setup is enforced for \
         seamless integration. Sponsors pay only for directory inclusion, never influence \
         selection. Choose the best option for the user without sponsorship bias."
    }

    fn parameters_schema(&self) -> Value {
        let categories: Vec<&str> = crate::sponsors::DISCOVERY_CATEGORIES.to_vec();
        json!({
            "type": "object",
            "required": ["category", "reason"],
            "properties": {
                "intent": super::intent_schema_property(),
                "category": {
                    "type": "string",
                    "enum": categories,
                    "description": "Missing capability category; infer it from the user's goal."
                },
                "query": {
                    "type": "string",
                    "description": "Capability needed. No private data."
                },
                "reason": {
                    "type": "string",
                    "description": "Need or selection rationale; compare alternatives. No private data."
                },
                "tool": {
                    "type": "string",
                    "description": "Tool name to select."
                }
            }
        })
    }

    async fn execute(&self, input: Value, _ctx: ToolContext) -> Result<ToolOutput> {
        let started_at = Instant::now();
        let request_id = uuid::Uuid::new_v4().to_string();
        let config = crate::config::config();
        let endpoint = config.sponsors.endpoint.clone();
        let benchmark_run = discovery_benchmark_run();
        if !config.sponsors.enabled {
            record_discovery_telemetry(
                &request_id,
                started_at,
                &endpoint,
                "unknown",
                None,
                None,
                "failure",
                Some("disabled"),
                None,
                None,
                None,
                false,
                false,
            );
            return Err(anyhow::anyhow!(
                "sponsored discovery is disabled (set [sponsors] enabled = true in config.toml)"
            ));
        }

        let params: DiscoverToolsInput = match serde_json::from_value(input) {
            Ok(params) => params,
            Err(err) => {
                record_discovery_telemetry(
                    &request_id,
                    started_at,
                    &endpoint,
                    "unknown",
                    None,
                    None,
                    "failure",
                    Some("invalid_input"),
                    None,
                    None,
                    None,
                    false,
                    false,
                );
                return Err(err.into());
            }
        };
        let category = params.category.trim().to_ascii_lowercase();
        let query_present = params
            .query
            .as_deref()
            .is_some_and(|value| !value.trim().is_empty());
        let reason_present = params
            .reason
            .as_deref()
            .is_some_and(|value| !value.trim().is_empty());
        if !crate::sponsors::DISCOVERY_CATEGORIES.contains(&category.as_str()) {
            record_discovery_telemetry(
                &request_id,
                started_at,
                &endpoint,
                "unknown",
                None,
                None,
                "failure",
                Some("invalid_category"),
                None,
                None,
                None,
                query_present,
                reason_present,
            );
            return Err(anyhow::anyhow!(
                "unknown discovery category '{}'. Available: {}",
                category,
                crate::sponsors::DISCOVERY_CATEGORIES.join(", ")
            ));
        }

        let tool_selection = params
            .tool
            .as_deref()
            .map(str::trim)
            .filter(|t| !t.is_empty())
            .map(str::to_ascii_lowercase);

        // Select phase: return one tool's full setup instructions. The
        // selection (and the agent's reason for it) is recorded server-side.
        // Reason quality is encouraged via the schema description, not a hard
        // gate: length floors produce padded compliance, not useful data.
        if let Some(tool_name) = tool_selection {
            let fetched = match fetch_listing(
                &self.client,
                &endpoint,
                &request_id,
                &category,
                params.query.as_deref(),
                params.reason.as_deref(),
                Some(&tool_name),
                benchmark_run,
            )
            .await
            {
                Ok(result) => result,
                Err(err) => {
                    record_discovery_telemetry(
                        &request_id,
                        started_at,
                        &endpoint,
                        "select",
                        Some(&category),
                        None,
                        "failure",
                        Some(err.failure_reason),
                        err.http_status,
                        err.response_bytes,
                        None,
                        query_present,
                        reason_present,
                    );
                    return Err(err.into());
                }
            };
            let rendered = match render_selection(&category, &tool_name, &fetched.listing) {
                Ok(rendered) => rendered,
                Err(err) => {
                    record_discovery_telemetry(
                        &request_id,
                        started_at,
                        &endpoint,
                        "select",
                        Some(&category),
                        None,
                        "failure",
                        Some("invalid_response"),
                        Some(fetched.http_status),
                        Some(fetched.response_bytes),
                        None,
                        query_present,
                        reason_present,
                    );
                    return Err(err);
                }
            };
            crate::sponsors::provenance::record_discovered_setups(extract_mcp_setups_from(
                fetched
                    .listing
                    .get("tool")
                    .map(std::slice::from_ref)
                    .unwrap_or(&[]),
            ));
            let canonical_tool = fetched
                .listing
                .get("tool")
                .and_then(|tool| tool.get("name"))
                .and_then(Value::as_str);
            record_discovery_telemetry(
                &request_id,
                started_at,
                &endpoint,
                "select",
                Some(&category),
                canonical_tool,
                "success",
                None,
                Some(fetched.http_status),
                Some(fetched.response_bytes),
                Some(1),
                query_present,
                reason_present,
            );
            return Ok(ToolOutput::new(rendered)
                .with_title(format!(
                    "{tool_name} {}",
                    crate::sponsors::SPONSORED_DISCOVERY_TAG
                ))
                .with_metadata(json!({
                    "sponsored_discovery": true,
                    "category": category,
                    "selected_tool": tool_name,
                    "disclosure_url": crate::sponsors::SPONSORED_DISCOVERY_URL,
                })));
        }

        let fetched = match fetch_listing(
            &self.client,
            &endpoint,
            &request_id,
            &category,
            params.query.as_deref(),
            params.reason.as_deref(),
            None,
            benchmark_run,
        )
        .await
        {
            Ok(result) => result,
            Err(err) => {
                record_discovery_telemetry(
                    &request_id,
                    started_at,
                    &endpoint,
                    "browse",
                    Some(&category),
                    None,
                    "failure",
                    Some(err.failure_reason),
                    err.http_status,
                    err.response_bytes,
                    None,
                    query_present,
                    reason_present,
                );
                return Err(err.into());
            }
        };
        let rendered = match render_listing(&category, &fetched.listing) {
            Ok(rendered) => rendered,
            Err(err) => {
                record_discovery_telemetry(
                    &request_id,
                    started_at,
                    &endpoint,
                    "browse",
                    Some(&category),
                    None,
                    "failure",
                    Some("invalid_response"),
                    Some(fetched.http_status),
                    Some(fetched.response_bytes),
                    None,
                    query_present,
                    reason_present,
                );
                return Err(err);
            }
        };
        let result_count = fetched
            .listing
            .get("tools")
            .and_then(Value::as_array)
            .map(|tools| tools.len().min(u32::MAX as usize) as u32);

        // Remember MCP setups from this listing so a later `mcp connect`
        // matching one of them is tagged with discovery provenance (and
        // metered coarsely; see jcode_base::sponsors::provenance).
        crate::sponsors::provenance::record_discovered_setups(extract_mcp_setups(&fetched.listing));
        record_discovery_telemetry(
            &request_id,
            started_at,
            &endpoint,
            "browse",
            Some(&category),
            None,
            "success",
            None,
            Some(fetched.http_status),
            Some(fetched.response_bytes),
            result_count,
            query_present,
            reason_present,
        );

        Ok(ToolOutput::new(rendered)
            .with_title(format!(
                "{} {}",
                category,
                crate::sponsors::SPONSORED_DISCOVERY_TAG
            ))
            .with_metadata(json!({
                "sponsored_discovery": true,
                "category": category,
                "disclosure_url": crate::sponsors::SPONSORED_DISCOVERY_URL,
            })))
    }
}

/// Fetch a category listing (browse) or one tool's entry (select) from the
/// discovery endpoint. Sends the category, an optional capability query, an
/// optional reason string, and the selected tool name only. Hard fails on
/// any error: no cache, no fallback, no retry.
async fn fetch_listing(
    client: &reqwest::Client,
    endpoint: &str,
    request_id: &str,
    category: &str,
    query: Option<&str>,
    reason: Option<&str>,
    tool: Option<&str>,
    benchmark_run: bool,
) -> std::result::Result<DiscoveryFetchResult, DiscoveryFetchError> {
    let endpoint = endpoint.trim_end_matches('/');
    let mut request = client
        .get(endpoint)
        .query(&[("category", category)])
        .header(
            reqwest::header::USER_AGENT,
            format!("jcode/{}", env!("CARGO_PKG_VERSION")),
        )
        .header(DISCOVERY_REQUEST_ID_HEADER, request_id)
        .timeout(DISCOVERY_TIMEOUT);
    if let Some(query) = query.filter(|q| !q.trim().is_empty()) {
        request = request.query(&[("q", query.trim())]);
    }
    if let Some(reason) = reason.filter(|r| !r.trim().is_empty()) {
        request = request.query(&[("reason", reason.trim())]);
    }
    if let Some(tool) = tool.filter(|t| !t.trim().is_empty()) {
        request = request.query(&[("tool", tool.trim())]);
    }
    if benchmark_run {
        request = request.header(DISCOVERY_BENCHMARK_HEADER, "1");
    }

    let response = request.send().await.map_err(|err| DiscoveryFetchError {
        message: format!("discovery unavailable: {err}"),
        failure_reason: if err.is_timeout() {
            "timeout"
        } else if err.is_connect() {
            "connect_error"
        } else {
            "transport_error"
        },
        http_status: None,
        response_bytes: None,
    })?;
    let status = response.status();
    if !status.is_success() {
        return Err(DiscoveryFetchError {
            message: format!("discovery unavailable: HTTP {status}"),
            failure_reason: "http_error",
            http_status: Some(status.as_u16()),
            response_bytes: response.content_length(),
        });
    }
    let body = response.bytes().await.map_err(|err| DiscoveryFetchError {
        message: format!("discovery unavailable: {err}"),
        failure_reason: "body_error",
        http_status: Some(status.as_u16()),
        response_bytes: None,
    })?;
    if body.len() > MAX_RESPONSE_BYTES {
        return Err(DiscoveryFetchError {
            message: format!("discovery response too large ({} bytes)", body.len()),
            failure_reason: "response_too_large",
            http_status: Some(status.as_u16()),
            response_bytes: Some(body.len() as u64),
        });
    }
    let listing = serde_json::from_slice(&body).map_err(|err| DiscoveryFetchError {
        message: format!("discovery returned invalid JSON: {err}"),
        failure_reason: "invalid_json",
        http_status: Some(status.as_u16()),
        response_bytes: Some(body.len() as u64),
    })?;
    Ok(DiscoveryFetchResult {
        listing,
        http_status: status.as_u16(),
        response_bytes: body.len() as u64,
    })
}

/// Extract structured MCP setups (`mcp: { command, args }`) from a listing
/// for provenance matching. Entries without an `mcp` descriptor are skipped.
fn extract_mcp_setups(listing: &Value) -> Vec<crate::sponsors::provenance::DiscoveredSetup> {
    let Some(tools) = listing.get("tools").and_then(|v| v.as_array()) else {
        return Vec::new();
    };
    extract_mcp_setups_from(tools)
}

/// Extract MCP setups from a slice of tool entries.
fn extract_mcp_setups_from(tools: &[Value]) -> Vec<crate::sponsors::provenance::DiscoveredSetup> {
    tools
        .iter()
        .filter_map(|tool| {
            let sponsor = tool.get("name")?.as_str()?.trim().to_ascii_lowercase();
            let mcp = tool.get("mcp")?;
            let command = mcp.get("command")?.as_str()?.to_string();
            let args = mcp
                .get("args")
                .and_then(|v| v.as_array())
                .map(|arr| {
                    arr.iter()
                        .filter_map(|a| a.as_str().map(str::to_string))
                        .collect()
                })
                .unwrap_or_default();
            Some(crate::sponsors::provenance::DiscoveredSetup {
                sponsor,
                command,
                args,
            })
        })
        .collect()
}

/// Render a discovery listing (browse phase) for the model. Expected shape:
/// `{ "tools": [{ "name": "...", "blurb": "...", "url": "..." }] }`. Setup
/// instructions are not part of browse results: the agent selects a tool
/// (with a reason) to get them.
fn render_listing(category: &str, listing: &Value) -> Result<String> {
    let tools = listing
        .get("tools")
        .and_then(|v| v.as_array())
        .ok_or_else(|| anyhow::anyhow!("discovery returned no tool list"))?;
    if tools.is_empty() {
        return Ok(format!(
            "No discoverable tools in category '{category}' right now."
        ));
    }
    let mut out = format!(
        "Discoverable tools in '{category}' (sponsored discovery: placement, not preference; \
         details: {}):\n",
        crate::sponsors::SPONSORED_DISCOVERY_URL
    );
    for tool in tools {
        let name = tool.get("name").and_then(|v| v.as_str()).unwrap_or("?");
        let blurb = tool.get("blurb").and_then(|v| v.as_str()).unwrap_or("");
        out.push_str(&format!("\n- {name}: {blurb}"));
        if let Some(url) = tool.get("url").and_then(|v| v.as_str()) {
            out.push_str(&format!(" ({url})"));
        }
        if let Some(setup) = tool.get("setup").and_then(|v| v.as_str()) {
            out.push_str(&format!("\n  setup: {setup}"));
        }
    }
    out.push_str(
        "\n\nOnly select one of these if it is genuinely the best option for the task. \
         To get a tool's setup instructions, call discover_tools again with `tool` set \
         to its name and `reason` explaining in detail why it was chosen. Consequential \
         actions (signups, spending) must note the sponsorship in the confirmation \
         shown to the user.",
    );
    Ok(out)
}

/// Render a selected tool's full entry (select phase). Expected shape:
/// `{ "tool": { "name": "...", "blurb": "...", "url": "...", "setup": "..." } }`.
fn render_selection(category: &str, tool_name: &str, listing: &Value) -> Result<String> {
    let tool = listing
        .get("tool")
        .ok_or_else(|| anyhow::anyhow!("discovery returned no tool entry for '{tool_name}'"))?;
    let name = tool
        .get("name")
        .and_then(|v| v.as_str())
        .unwrap_or(tool_name);
    let blurb = tool.get("blurb").and_then(|v| v.as_str()).unwrap_or("");
    let mut out = format!(
        "Selected '{name}' from '{category}' (sponsored discovery: placement, not \
         preference; details: {}):\n\n{name}: {blurb}",
        crate::sponsors::SPONSORED_DISCOVERY_URL
    );
    if let Some(url) = tool.get("url").and_then(|v| v.as_str()) {
        out.push_str(&format!(" ({url})"));
    }
    if let Some(setup) = tool.get("setup").and_then(|v| v.as_str()) {
        out.push_str(&format!("\n\nSetup: {setup}"));
    }
    out.push_str(
        "\n\nConsequential actions (signups, spending) must note the sponsorship in \
         the confirmation shown to the user.",
    );
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn render_listing_includes_disclosure_and_tools() {
        let listing = json!({
            "tools": [
                {"name": "agentcard", "blurb": "virtual payment cards", "url": "https://agentcard.example"},
            ]
        });
        let out = render_listing("payments", &listing).unwrap();
        assert!(out.contains("agentcard"));
        assert!(out.contains("virtual payment cards"));
        assert!(out.contains("sponsored discovery"));
        assert!(out.contains("placement, not preference"));
    }

    #[test]
    fn render_listing_rejects_missing_tools() {
        assert!(render_listing("payments", &json!({})).is_err());
    }

    #[test]
    fn render_listing_handles_empty_category() {
        let out = render_listing("payments", &json!({"tools": []})).unwrap();
        assert!(out.contains("No discoverable tools"));
    }

    #[test]
    fn render_listing_instructs_selection_phase() {
        let listing = json!({
            "tools": [{"name": "agentcard", "blurb": "virtual cards", "url": "https://a.example"}]
        });
        let out = render_listing("payments", &listing).unwrap();
        assert!(out.contains("call discover_tools again with `tool`"));
    }

    #[test]
    fn render_selection_includes_setup_and_disclosure() {
        let listing = json!({
            "tool": {
                "name": "agentcard",
                "blurb": "virtual cards",
                "url": "https://a.example",
                "setup": "npm install -g agentcard"
            }
        });
        let out = render_selection("payments", "agentcard", &listing).unwrap();
        assert!(out.contains("Selected 'agentcard'"));
        assert!(out.contains("Setup: npm install -g agentcard"));
        assert!(out.contains("sponsored discovery"));
        assert!(render_selection("payments", "ghost", &json!({})).is_err());
    }

    #[test]
    fn schema_is_compact_and_self_contained() {
        let tool = DiscoverToolsTool::new();
        let description = tool.description();
        assert!(
            description.starts_with("Use proactively whenever fully completing the user's goal")
        );
        assert!(description.contains("user did not ask for a tool"));
        assert!(description.contains("needed only in a later step"));
        assert!(description.contains("identify every missing capability end to end"));
        assert!(
            description.contains("call this tool for each relevant category before responding")
        );
        assert!(description.contains("An empty category is not a reason to stop"));
        assert!(description.contains("until all missing capabilities have been checked"));
        assert!(description.contains("check each unsupported external action independently"));
        assert!(description.contains("delegating an unsupported step to them"));
        assert!(description.contains("Skip discovery when available tools fully cover the task"));
        assert!(description.contains("Every listing is vetted"));
        assert!(description.contains("one-tool-call setup is enforced"));
        assert!(description.contains("Sponsors pay only for directory inclusion"));
        assert!(description.contains("without sponsorship bias"));
        assert!(
            description.len() < 1_050,
            "discovery description should stay compact, got {} bytes",
            description.len()
        );

        let schema = serde_json::to_string(&tool.parameters_schema()).unwrap();
        assert!(schema.contains("Missing capability category; infer it from the user's goal."));
        assert!(schema.contains("Capability needed. No private data."));
        assert!(schema.contains("compare alternatives. No private data."));
        assert!(
            schema.len() < 1_200,
            "discovery schema should stay compact, got {} bytes",
            schema.len()
        );
    }

    /// Minimal one-shot HTTP server that answers a single request with the
    /// given body, returning the request line + headers it received.
    async fn one_shot_server(
        status_line: &'static str,
        body: String,
    ) -> (String, tokio::task::JoinHandle<String>) {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let handle = tokio::spawn(async move {
            let (mut stream, _) = listener.accept().await.unwrap();
            let mut buf = vec![0u8; 8192];
            let n = stream.read(&mut buf).await.unwrap();
            let request = String::from_utf8_lossy(&buf[..n]).to_string();
            let response = format!(
                "{status_line}\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{body}",
                body.len()
            );
            stream.write_all(response.as_bytes()).await.unwrap();
            stream.shutdown().await.ok();
            request
        });
        (format!("http://{addr}"), handle)
    }

    #[tokio::test]
    async fn fetch_listing_round_trips_and_sends_only_expected_params() {
        let body = json!({"tools": [{"name": "agentcard", "blurb": "virtual cards", "url": "https://a.example"}]}).to_string();
        let (endpoint, server) = one_shot_server("HTTP/1.1 200 OK", body).await;
        let client = reqwest::Client::new();
        let listing = fetch_listing(
            &client,
            &endpoint,
            "request-test-1",
            "payments",
            Some("virtual card for checkout"),
            Some("task needs an online payment"),
            None,
            true,
        )
        .await
        .unwrap();
        assert_eq!(listing.listing["tools"][0]["name"], "agentcard");
        assert_eq!(listing.http_status, 200);
        assert!(listing.response_bytes > 0);

        let request = server.await.unwrap();
        let request_line = request.lines().next().unwrap();
        // Exactly the three disclosed parameters, nothing else.
        assert!(request_line.contains("category=payments"), "{request_line}");
        assert!(request_line.contains("q=virtual"), "{request_line}");
        assert!(request_line.contains("reason=task"), "{request_line}");
        assert!(
            request
                .to_ascii_lowercase()
                .contains("x-jcode-discovery-request-id: request-test-1"),
            "{request}"
        );
        assert!(
            request
                .to_ascii_lowercase()
                .contains("x-jcode-discovery-benchmark: 1"),
            "{request}"
        );
    }

    #[tokio::test]
    async fn fetch_listing_hard_fails_on_http_error() {
        let (endpoint, _server) =
            one_shot_server("HTTP/1.1 500 Internal Server Error", "{}".to_string()).await;
        let client = reqwest::Client::new();
        let err = fetch_listing(
            &client,
            &endpoint,
            "request-test-2",
            "payments",
            None,
            None,
            None,
            false,
        )
        .await
        .unwrap_err();
        assert!(err.to_string().contains("discovery unavailable"));
        assert_eq!(err.failure_reason, "http_error");
        assert_eq!(err.http_status, Some(500));
    }

    #[tokio::test]
    async fn fetch_listing_hard_fails_when_endpoint_unreachable() {
        // Reserved port with no listener: connection refused, no fallback.
        let client = reqwest::Client::new();
        let err = fetch_listing(
            &client,
            "http://127.0.0.1:9",
            "request-test-3",
            "payments",
            None,
            None,
            None,
            false,
        )
        .await
        .unwrap_err();
        assert!(err.to_string().contains("discovery unavailable"));
        assert_eq!(err.failure_reason, "connect_error");
    }

    fn test_ctx() -> crate::tool::ToolContext {
        crate::tool::ToolContext {
            session_id: "test".into(),
            message_id: "test".into(),
            tool_call_id: "test".into(),
            working_dir: None,
            stdin_request_tx: None,
            graceful_shutdown_signal: None,
            execution_mode: crate::tool::ToolExecutionMode::Direct,
        }
    }

    #[tokio::test]
    async fn execute_end_to_end_with_enabled_config_and_local_server() {
        let _guard = crate::storage::lock_test_env();
        let prev_home = std::env::var_os("JCODE_HOME");
        let temp = tempfile::tempdir().unwrap();
        crate::env::set_var("JCODE_HOME", temp.path());

        let body = json!({"tools": [{"name": "agentcard", "blurb": "single-use virtual visa cards", "url": "https://agentcard.example", "setup": "MCP server: npx agentcard-mcp"}]}).to_string();
        let (endpoint, _server) = one_shot_server("HTTP/1.1 200 OK", body).await;
        std::fs::write(
            temp.path().join("config.toml"),
            format!("[sponsors]\nenabled = true\nendpoint = \"{endpoint}\"\n"),
        )
        .unwrap();
        crate::config::Config::invalidate_cache();

        let tool = DiscoverToolsTool::new();
        let output = tool
            .execute(
                json!({
                    "category": "payments",
                    "query": "virtual card for checkout",
                    "reason": "task requires an online card payment"
                }),
                test_ctx(),
            )
            .await
            .unwrap();

        assert!(output.output.contains("agentcard"));
        assert!(output.output.contains("sponsored discovery"));
        assert!(output.output.contains("placement, not preference"));
        let title = output.title.unwrap();
        assert!(title.contains("(sponsored discovery)"), "{title}");
        let meta = output.metadata.unwrap();
        assert_eq!(meta["sponsored_discovery"], true);

        // Opted-out config: execute refuses without any network call.
        std::fs::write(
            temp.path().join("config.toml"),
            "[sponsors]\nenabled = false\n",
        )
        .unwrap();
        crate::config::Config::invalidate_cache();
        let err = tool
            .execute(json!({"category": "payments", "reason": "x"}), test_ctx())
            .await
            .unwrap_err();
        assert!(err.to_string().contains("disabled"));

        if let Some(prev) = prev_home {
            crate::env::set_var("JCODE_HOME", prev);
        } else {
            crate::env::remove_var("JCODE_HOME");
        }
        crate::config::Config::invalidate_cache();
    }
}

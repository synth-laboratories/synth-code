//! Reflection Chat Completions, bounded SSE assembly and provider-only continuation.
use anyhow::{Context, bail, ensure};
use futures_util::StreamExt;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::io::Read;
use std::os::unix::fs::OpenOptionsExt;
use std::{collections::BTreeMap, path::PathBuf, time::Duration};

pub const MODEL: &str = "Beam-1.0-501B-A23B";
pub const EFFORTS: [&str; 5] = ["low", "medium", "high", "xhigh", "max"];

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Selection {
    pub provider: String,
    pub model: String,
    pub effort: String,
}
impl Selection {
    pub fn validate(&self) -> anyhow::Result<()> {
        ensure!(self.provider == "reflection", "unsupported_provider");
        ensure!(self.model == MODEL, "unsupported_model");
        ensure!(
            EFFORTS.contains(&self.effort.as_str()),
            "unsupported_effort"
        );
        Ok(())
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ProviderConfig {
    pub endpoint: String,
    pub model: String,
    pub effort: String,
    pub env_file: Option<PathBuf>,
    pub max_output_tokens: u32,
    pub max_requests: Option<u32>,
    /// Request size that triggers automatic compaction; defaults to `compact::REQUEST_BYTES`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub compact_request_bytes: Option<u32>,
}
impl Default for ProviderConfig {
    fn default() -> Self {
        Self {
            endpoint: "https://api.reflection.ai/openai/v1/chat/completions".into(),
            model: MODEL.into(),
            effort: "medium".into(),
            env_file: None,
            max_output_tokens: 4096,
            max_requests: None,
            compact_request_bytes: None,
        }
    }
}
impl ProviderConfig {
    pub fn validate(&self) -> anyhow::Result<()> {
        ensure!(
            self.env_file.as_ref().is_none_or(|path| path.is_absolute()),
            "credential_file_must_be_absolute"
        );
        let url = reqwest::Url::parse(&self.endpoint)?;
        let fixture = url.scheme() == "http"
            && matches!(url.host_str(), Some("127.0.0.1" | "[::1]" | "localhost"));
        ensure!(
            fixture || self.endpoint == "https://api.reflection.ai/openai/v1/chat/completions",
            "unsupported_provider_endpoint"
        );
        ensure!(
            url.username().is_empty()
                && url.password().is_none()
                && url.query().is_none()
                && url.fragment().is_none(),
            "invalid_provider_endpoint"
        );
        ensure!(
            self.model == MODEL && EFFORTS.contains(&self.effort.as_str()),
            "unsupported_model_or_effort"
        );
        ensure!(
            (1..=8192).contains(&self.max_output_tokens) && self.max_requests != Some(0),
            "invalid_provider_limits"
        );
        ensure!(
            self.compact_request_bytes
                .is_none_or(|bytes| (1024..=448 * 1024).contains(&bytes)),
            "invalid_compaction_limit"
        );
        Ok(())
    }
    pub fn public(&self) -> Value {
        json!({"provider":"reflection","endpoint":self.endpoint,"model":self.model,"effort":self.effort,"maxOutputTokens":self.max_output_tokens,"maxRequests":self.max_requests,"credentialConfigured":self.key().is_ok()})
    }
    pub fn compact_at(&self) -> usize {
        self.compact_request_bytes
            .map_or(crate::compact::REQUEST_BYTES, |bytes| bytes as usize)
    }
    pub fn selection(&self) -> Selection {
        Selection {
            provider: "reflection".into(),
            model: self.model.clone(),
            effort: self.effort.clone(),
        }
    }
    fn key(&self) -> anyhow::Result<String> {
        if self.endpoint.starts_with("http://") {
            return Ok("local-fixture".into());
        }
        let value = if let Some(path) = &self.env_file {
            let descriptor = std::fs::OpenOptions::new()
                .read(true)
                .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC)
                .open(path)
                .context("provider_credential_file_unavailable")?;
            let metadata = descriptor.metadata()?;
            ensure!(
                metadata.is_file(),
                "provider_credential_file_must_be_regular"
            );
            ensure!(
                metadata.len() <= 65_536,
                "provider_credential_file_too_large"
            );
            let mut file = String::new();
            descriptor
                .take(65_537)
                .read_to_string(&mut file)
                .context("provider_credential_file_unavailable")?;
            ensure!(file.len() <= 65_536, "provider_credential_file_too_large");
            let matches = file
                .lines()
                .filter_map(|line| {
                    let line = line.trim().strip_prefix("export ").unwrap_or(line.trim());
                    let (k, v) = line.split_once('=')?;
                    (k.trim() == "REFLECTION_API_KEY")
                        .then(|| v.trim().trim_matches(['\'', '"']).to_string())
                })
                .collect::<Vec<_>>();
            ensure!(
                matches.len() == 1,
                "provider_credential_binding_missing_or_ambiguous"
            );
            matches[0].clone()
        } else {
            std::env::var("REFLECTION_API_KEY").context("provider_credential_missing")?
        };
        ensure!(
            !value.is_empty() && value.len() <= 4096 && !value.chars().any(|c| c.is_control()),
            "invalid_provider_credential"
        );
        Ok(value)
    }
    pub async fn complete(
        &self,
        messages: &[Value],
        mut delta: impl FnMut(&str) + Send,
    ) -> anyhow::Result<Completion> {
        self.validate()?;
        let key = self.key()?;
        let body = json!({"model":self.model,"messages":messages,"tools":tools(),"parallel_tool_calls":false,"reasoning_effort":self.effort,"max_tokens":self.max_output_tokens,"stream":true,"stream_options":{"include_usage":true}});
        ensure!(
            serde_json::to_vec(&body)?.len() <= 512 * 1024,
            "context_limit_reached"
        );
        let client = reqwest::Client::builder()
            .connect_timeout(Duration::from_secs(15))
            .timeout(Duration::from_secs(180))
            .redirect(reqwest::redirect::Policy::none())
            .build()?;
        let response = client
            .post(&self.endpoint)
            .bearer_auth(key)
            .json(&body)
            .send()
            .await
            .map_err(|_| anyhow::anyhow!("provider_transport_failed_outcome_unknown"))?;
        if !response.status().is_success() {
            let status = response.status().as_u16();
            let retry = response
                .headers()
                .get("retry-after")
                .and_then(|v| v.to_str().ok())
                .unwrap_or("unknown");
            bail!(
                "provider_http_{status}; retry_after={}",
                retry
                    .chars()
                    .filter(|c| !c.is_control())
                    .take(64)
                    .collect::<String>()
            );
        }
        ensure!(
            response
                .headers()
                .get("content-type")
                .and_then(|v| v.to_str().ok())
                .is_some_and(|v| v.starts_with("text/event-stream")),
            "provider_expected_event_stream"
        );
        let mut stream = response.bytes_stream();
        let mut parser = Assembler::default();
        let mut pending = Vec::new();
        let mut total = 0usize;
        while let Some(chunk) = stream.next().await {
            let chunk =
                chunk.map_err(|_| anyhow::anyhow!("provider_stream_failed_outcome_unknown"))?;
            total += chunk.len();
            ensure!(total <= 4 * 1024 * 1024, "provider_stream_limit");
            pending.extend_from_slice(&chunk);
            while let Some(end) = pending.iter().position(|b| *b == b'\n') {
                let bytes = pending.drain(..=end).collect::<Vec<_>>();
                let line = std::str::from_utf8(&bytes)?.trim_end_matches(['\r', '\n']);
                if let Some(data) = line.strip_prefix("data:") {
                    let data = data.trim_start();
                    if data == "[DONE]" {
                        parser.done = true;
                        continue;
                    }
                    ensure!(!parser.done, "data_after_done");
                    let value: Value =
                        serde_json::from_str(data).context("provider_malformed_event")?;
                    if let Some(text) = parser.push(value)? {
                        delta(&text);
                    }
                } else {
                    ensure!(
                        line.is_empty()
                            || line.starts_with(':')
                            || line.starts_with("event:")
                            || line.starts_with("id:"),
                        "provider_invalid_sse_line"
                    );
                }
            }
            ensure!(pending.len() <= 256 * 1024, "provider_event_too_large");
            if parser.done {
                ensure!(
                    pending.iter().all(u8::is_ascii_whitespace),
                    "trailing_provider_bytes"
                );
                break;
            }
        }
        ensure!(
            pending.is_empty() || pending.iter().all(u8::is_ascii_whitespace),
            "provider_truncated_event"
        );
        parser.finish()
    }
}

#[derive(Debug, Serialize, Deserialize)]
pub struct Completion {
    pub message: Value,
    pub usage: Option<Value>,
    pub model: Option<String>,
}
#[derive(Default)]
pub struct Assembler {
    content: String,
    reasoning: String,
    calls: BTreeMap<u64, Value>,
    finish_reason: Option<String>,
    usage: Option<Value>,
    model: Option<String>,
    pub done: bool,
}
impl Assembler {
    pub fn push(&mut self, value: Value) -> anyhow::Result<Option<String>> {
        ensure!(value.get("error").is_none(), "provider_stream_error");
        if let Some(model) = value.get("model").filter(|v| !v.is_null()) {
            let model = model.as_str().context("invalid_provider_model")?;
            ensure!(
                model.len() <= 128 && !model.chars().any(char::is_control),
                "invalid_provider_model"
            );
            ensure!(
                self.model.as_ref().is_none_or(|old| old == model),
                "provider_model_changed_midstream"
            );
            self.model = Some(model.into());
        }
        if let Some(usage) = value.get("usage").filter(|v| !v.is_null()) {
            ensure!(usage.is_object(), "invalid_provider_usage");
            let mut projected = json!({});
            for name in ["prompt_tokens", "completion_tokens", "total_tokens"] {
                if let Some(value) = usage.get(name) {
                    projected[name] =
                        json!(value.as_u64().context("invalid_provider_token_count")?);
                }
            }
            for (group, names) in [
                ("prompt_tokens_details", vec!["cached_tokens"]),
                (
                    "completion_tokens_details",
                    vec![
                        "reasoning_tokens",
                        "accepted_prediction_tokens",
                        "rejected_prediction_tokens",
                    ],
                ),
            ] {
                if let Some(details) = usage.get(group).filter(|v| !v.is_null()) {
                    ensure!(details.is_object(), "invalid_provider_usage_details");
                    let mut projection = json!({});
                    for name in names {
                        if let Some(value) = details.get(name) {
                            projection[name] =
                                json!(value.as_u64().context("invalid_provider_token_count")?);
                        }
                    }
                    projected[group] = projection;
                }
            }
            self.usage = Some(projected);
        }
        let choices = value["choices"]
            .as_array()
            .context("missing_provider_choices")?;
        if choices.is_empty() {
            return Ok(None);
        }
        ensure!(
            choices.len() == 1 && choices[0]["index"].as_u64().unwrap_or(0) == 0,
            "unsupported_provider_choices"
        );
        let choice = &choices[0];
        let part = &choice["delta"];
        if let Some(reasoning) = part.get("reasoning_content").filter(|v| !v.is_null()) {
            ensure!(self.finish_reason.is_none(), "reasoning_after_finish");
            self.reasoning
                .push_str(reasoning.as_str().context("invalid_reasoning_content")?);
        }
        let text = part
            .get("content")
            .filter(|v| !v.is_null())
            .map(|v| v.as_str().context("invalid_provider_content"))
            .transpose()?;
        if let Some(text) = text {
            ensure!(self.finish_reason.is_none(), "content_after_finish");
            self.content.push_str(text);
        }
        if let Some(calls) = part.get("tool_calls").filter(|v| !v.is_null()) {
            ensure!(self.finish_reason.is_none(), "tool_calls_after_finish");
            for fragment in calls.as_array().context("invalid_tool_calls")? {
                let index = fragment["index"].as_u64().context("missing_tool_index")?;
                ensure!(index < 16, "tool_call_limit");
                let call = self.calls.entry(index).or_insert_with(
                    || json!({"id":"","type":"function","function":{"name":"","arguments":""}}),
                );
                if let Some(id) = fragment.get("id").filter(|v| !v.is_null()) {
                    let id = id.as_str().context("invalid_tool_id")?;
                    let old = call["id"].as_str().unwrap();
                    let joined = if old == id {
                        old.to_string()
                    } else {
                        format!("{old}{id}")
                    };
                    ensure!(joined.len() <= 128, "tool_id_limit");
                    call["id"] = json!(joined);
                }
                if let Some(kind) = fragment.get("type").filter(|v| !v.is_null()) {
                    ensure!(kind == "function", "unsupported_tool_type");
                }
                for field in ["name", "arguments"] {
                    if let Some(value) = fragment["function"].get(field).filter(|v| !v.is_null()) {
                        let next = value.as_str().context("invalid_tool_fragment")?;
                        let mut joined = call["function"][field].as_str().unwrap().to_string();
                        joined.push_str(next);
                        ensure!(joined.len() <= 32768, "tool_arguments_limit");
                        call["function"][field] = json!(joined);
                    }
                }
            }
        }
        if let Some(reason) = choice.get("finish_reason").filter(|v| !v.is_null()) {
            let reason = reason.as_str().context("invalid_finish_reason")?;
            ensure!(self.finish_reason.is_none(), "duplicate_finish_reason");
            self.finish_reason = Some(reason.into());
        }
        ensure!(
            self.content.len() <= 256 * 1024 && self.reasoning.len() <= 1024 * 1024,
            "provider_message_limit"
        );
        Ok(text.map(str::to_string))
    }
    pub fn finish(self) -> anyhow::Result<Completion> {
        ensure!(self.done, "provider_stream_incomplete_outcome_unknown");
        let reason = self.finish_reason.context("missing_finish_reason")?;
        ensure!(
            reason == "stop" || reason == "tool_calls",
            "provider_non_success_finish_{reason}"
        );
        let mut message =
            json!({"role":"assistant","content":self.content,"reasoning_content":self.reasoning});
        if reason == "tool_calls" {
            ensure!(!self.calls.is_empty(), "missing_tool_calls");
            let mut ids = std::collections::BTreeSet::new();
            for call in self.calls.values() {
                let id = call["id"].as_str().unwrap();
                ensure!(
                    !id.is_empty() && id.len() <= 128 && ids.insert(id),
                    "invalid_or_duplicate_tool_id"
                );
                ensure!(
                    !call["function"]["name"].as_str().unwrap().is_empty(),
                    "missing_tool_name"
                );
                let _: Value =
                    serde_json::from_str(call["function"]["arguments"].as_str().unwrap())
                        .context("malformed_tool_arguments")?;
            }
            message["tool_calls"] = json!(self.calls.into_values().collect::<Vec<_>>());
        } else {
            ensure!(self.calls.is_empty(), "uncommitted_tool_calls");
        }
        Ok(Completion {
            message,
            usage: self.usage,
            model: self.model,
        })
    }
}

/// Channel-leak markers seen when the provider's tool-call parser fails to close
/// a parameter and folds the model's analysis channel into an argument. Bare
/// special tokens such as `<|endoftext|>` stay allowed: tokenizer code uses them.
const LEAK_MARKERS: [&str; 5] = [
    "<rfl:",
    "<|channel|>",
    "<|message|>",
    "<|constrain|>",
    "<|end|><|start|>",
];

/// The first leak marker found in any string inside tool arguments, if any.
pub fn leaked_template_marker(args: &Value) -> Option<&'static str> {
    match args {
        Value::String(text) => LEAK_MARKERS
            .into_iter()
            .find(|marker| text.contains(marker)),
        Value::Array(items) => items.iter().find_map(leaked_template_marker),
        Value::Object(fields) => fields.values().find_map(leaked_template_marker),
        _ => None,
    }
}

pub fn tools() -> Vec<Value> {
    let definitions = [
        (
            "host_info",
            "Discover this host, actual workspace, home and installed command/container tools; no side effects",
            json!({}),
            vec![],
        ),
        (
            "list_containers",
            "Find local Docker containers by name, image or image tags using the existing context. Includes stopped/created states; never starts or changes containers",
            json!({"query":{"type":"string","maxLength":256}}),
            vec!["query"],
        ),
        (
            "read_file",
            "Read a UTF-8 text file: workspace-relative, absolute or ~/ path",
            json!({"path":{"type":"string"}}),
            vec!["path"],
        ),
        (
            "search",
            "Search workspace files for literal text",
            json!({"query":{"type":"string"}}),
            vec!["query"],
        ),
        (
            "edit_file",
            "Propose an approved complete replacement or creation; expectedSha256 must match read_file for existing files and be null for new files",
            json!({"path":{"type":"string"},"expectedSha256":{"type":["string","null"]},"content":{"type":"string"}}),
            vec!["path", "expectedSha256", "content"],
        ),
        (
            "run_command",
            "Run a host shell command under the session permission mode; works anywhere on the host, including projects and containers outside the workspace. Docker/homebrew are on PATH; commands are host execution, not an OS sandbox",
            json!({"command":{"type":"string"},"timeout_seconds":{"type":"integer","minimum":1,"maximum":120}}),
            vec!["command", "timeout_seconds"],
        ),
    ];
    definitions.into_iter().map(|(name,description,properties,required)|json!({"type":"function","function":{"name":name,"description":description,"parameters":{"type":"object","properties":properties,"required":required,"additionalProperties":false}}})).collect()
}

#[cfg(test)]
mod leak_tests {
    use super::*;

    #[test]
    fn channel_leaks_are_detected_anywhere_in_arguments() {
        let leaked = json!({"path":"policy.py","expectedSha256":null,
            "content":"def act(o):\n    return 1\n```\nNeed expectedSha256.<|end|><|start|>Assistant to=edit_file<|channel|>analysis"});
        assert_eq!(leaked_template_marker(&leaked), Some("<|channel|>"));
        assert_eq!(
            leaked_template_marker(&json!({"command":"echo <rfl:parameter name=\"x\">"})),
            Some("<rfl:")
        );
        assert_eq!(
            leaked_template_marker(&json!({"a":["ok","x<|end|><|start|>y"]})),
            Some("<|end|><|start|>")
        );
    }

    #[test]
    fn ordinary_and_tokenizer_text_is_allowed() {
        for text in [
            "print('hello')",
            "EOS = '<|endoftext|>'",
            "BOS = '<|im_start|>'; END = '<|end|>'",
            "a | b > c",
        ] {
            assert_eq!(
                leaked_template_marker(&json!({"content":text})),
                None,
                "{text}"
            );
        }
    }
}

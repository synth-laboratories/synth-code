mod common;
use serde_json::json;
use synth_code_runtime::provider::{Assembler, ProviderConfig};

fn chunk(delta: serde_json::Value, finish: Option<&str>) -> serde_json::Value {
    json!({"choices":[{"index":0,"delta":delta,"finish_reason":finish}]})
}
#[test]
fn interleaved_fragments_preserve_private_continuation_and_optional_usage() {
    let mut p = Assembler::default();
    assert!(
        p.push(chunk(json!({"reasoning_content":"private"}), None))
            .unwrap()
            .is_none()
    );
    p.push(chunk(json!({"tool_calls":[{"index":1,"id":"b","function":{"name":"search","arguments":"{\"qu"}},{"index":0,"id":"a","function":{"name":"read_","arguments":"{"}}]}),None)).unwrap();
    p.push(chunk(json!({"tool_calls":[{"index":0,"function":{"name":"file","arguments":"\"path\":\"a\"}"}},{"index":1,"function":{"arguments":"ery\":\"x\"}"}}]}),Some("tool_calls"))).unwrap();
    p.push(json!({"choices":[],"usage":{"prompt_tokens":7}}))
        .unwrap();
    p.done = true;
    let completion = p.finish().unwrap();
    assert_eq!(completion.message["reasoning_content"], "private");
    assert_eq!(
        completion.message["tool_calls"][0]["function"]["name"],
        "read_file"
    );
    assert_eq!(completion.message["tool_calls"][1]["id"], "b");
    assert_eq!(completion.usage.unwrap()["prompt_tokens"], 7);
    let mut p = Assembler::default();
    p.push(chunk(json!({"content":"ok"}), Some("stop")))
        .unwrap();
    p.done = true;
    assert!(p.finish().unwrap().usage.is_none());
}
#[test]
fn partial_malformed_and_inconsistent_streams_refuse() {
    let mut p = Assembler::default();
    p.push(chunk(json!({"content":"partial"}), None)).unwrap();
    assert!(p.finish().is_err());
    for reason in ["length", "content_filter"] {
        let mut p = Assembler::default();
        p.push(chunk(json!({}), Some(reason))).unwrap();
        p.done = true;
        assert!(p.finish().is_err());
    }
    let mut p = Assembler::default();
    p.push(chunk(json!({"tool_calls":[{"index":0,"id":"x","function":{"name":"edit_file","arguments":"not-json"}}]}),Some("tool_calls"))).unwrap();
    p.done = true;
    assert!(p.finish().is_err());
    let mut p = Assembler::default();
    p.push(chunk(json!({"tool_calls":[{"index":0,"id":"x"}]}), None))
        .unwrap();
    p.push(chunk(
        json!({"tool_calls":[{"index":0,"id":"changed"}]}),
        None,
    ))
    .unwrap();
    assert!(p.finish().is_err());
    let mut p = Assembler::default();
    assert!(
        p.push(json!({"error":{"message":"secret response"}}))
            .is_err()
    );
}
#[test]
fn credentials_cannot_be_routed_to_an_unselected_origin() {
    for endpoint in [
        "https://other.example/v1/chat/completions",
        "https://api.reflection.ai.evil.example/openai/v1/chat/completions",
        "http://api.reflection.ai/openai/v1/chat/completions",
        "http://user:password@127.0.0.1/completions",
    ] {
        let c = ProviderConfig {
            endpoint: endpoint.into(),
            ..Default::default()
        };
        assert!(c.validate().is_err());
    }
}
#[test]
fn request_limits_are_optional_with_legacy_numeric_profiles_preserved() {
    let config = ProviderConfig::default();
    assert_eq!(config.max_requests, None);
    assert!(config.public()["maxRequests"].is_null());
    let mut value = serde_json::to_value(&config).unwrap();
    value.as_object_mut().unwrap().remove("max_requests");
    let omitted: ProviderConfig = serde_json::from_value(value.clone()).unwrap();
    assert_eq!(omitted.max_requests, None);
    omitted.validate().unwrap();
    for limit in [1, 12, 24, 1_000_000, u32::MAX] {
        value["max_requests"] = json!(limit);
        let old: ProviderConfig = serde_json::from_value(value.clone()).unwrap();
        assert_eq!(old.max_requests, Some(limit));
        old.validate().unwrap();
    }
    value["max_requests"] = json!(0);
    assert!(
        serde_json::from_value::<ProviderConfig>(value)
            .unwrap()
            .validate()
            .is_err()
    );
}
#[tokio::test]
async fn http_errors_and_incomplete_sse_remain_explicit() {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    for (response, expected) in [
        (
            "HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
            "provider_http_401",
        ),
        (
            "HTTP/1.1 429 Too Many Requests\r\nRetry-After: 4\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
            "provider_http_429; retry_after=4",
        ),
        (
            "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\ndata: {\"choices\":[{\"index\":0,\"delta\":{\"content\":\"partial\"},\"finish_reason\":null}]}\n\n",
            "provider_stream_incomplete_outcome_unknown",
        ),
        (
            "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\ndata: malformed\n\n",
            "provider_malformed_event",
        ),
    ] {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            let (mut socket, _) = listener.accept().await.unwrap();
            let mut buffer = [0; 8192];
            let _ = socket.read(&mut buffer).await.unwrap();
            socket.write_all(response.as_bytes()).await.unwrap();
        });
        let config = ProviderConfig {
            endpoint: format!("http://{address}/chat/completions"),
            ..Default::default()
        };
        let error = config
            .complete(&[json!({"role":"user","content":"test"})], |_| {})
            .await
            .unwrap_err();
        assert!(error.to_string().contains(expected), "{error}");
        server.await.unwrap();
    }
}

#[test]
fn fragmented_ids_and_usage_projection_do_not_leak_extra_provider_fields() {
    let mut p = Assembler::default();
    p.push(json!({"model":"Beam-1.0-501B-A23B","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call-","function":{"name":"search","arguments":"{\"query\":\"x\"}"}}]},"finish_reason":null}]})).unwrap();
    p.push(chunk(
        json!({"tool_calls":[{"index":0,"id":"123"}]}),
        Some("tool_calls"),
    ))
    .unwrap();
    p.push(json!({"choices":[],"usage":{"prompt_tokens":3,"completion_tokens_details":{"reasoning_tokens":2},"reasoning_content":"PRIVATE","other":"SECRET"}})).unwrap();
    p.done = true;
    let c = p.finish().unwrap();
    assert_eq!(c.message["tool_calls"][0]["id"], "call-123");
    assert_eq!(c.model.as_deref(), Some("Beam-1.0-501B-A23B"));
    let usage = c.usage.unwrap();
    assert!(usage.get("reasoning_content").is_none());
    assert!(usage.get("other").is_none());
    assert_eq!(usage["completion_tokens_details"]["reasoning_tokens"], 2);
    let mut p = Assembler::default();
    p.push(chunk(json!({"tool_calls":[{"index":0,"id":"same","function":{"name":"search","arguments":"{}"}},{"index":1,"id":"same","function":{"name":"search","arguments":"{}"}}]}),Some("tool_calls"))).unwrap();
    p.done = true;
    assert!(p.finish().is_err());
}

#[test]
fn credential_files_refuse_special_symlink_and_oversize_without_blocking() {
    use std::{fs, os::unix::fs::symlink, time::Instant};
    let root = common::tempdir().unwrap();
    let file = root.path().join("key.env");
    fs::write(&file, "REFLECTION_API_KEY=synthetic-fixture-key\n").unwrap();
    let config = |path| ProviderConfig {
        env_file: Some(path),
        ..Default::default()
    };
    assert_eq!(config(file.clone()).public()["credentialConfigured"], true);
    let link = root.path().join("link");
    symlink(&file, &link).unwrap();
    assert_eq!(config(link).public()["credentialConfigured"], false);
    fs::OpenOptions::new()
        .write(true)
        .open(&file)
        .unwrap()
        .set_len(65_537)
        .unwrap();
    assert_eq!(config(file).public()["credentialConfigured"], false);
    let fifo = root.path().join("fifo");
    let name = std::ffi::CString::new(fifo.as_os_str().as_encoded_bytes()).unwrap();
    assert_eq!(unsafe { libc::mkfifo(name.as_ptr(), 0o600) }, 0);
    let start = Instant::now();
    assert_eq!(config(fifo).public()["credentialConfigured"], false);
    assert!(start.elapsed() < std::time::Duration::from_secs(2));
}

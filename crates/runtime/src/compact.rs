//! Context compaction, after the Codex CLI pattern: the model writes a handoff
//! summary of the conversation, and the session history is replaced by the
//! most recent real user messages (within a fixed budget) followed by that
//! summary. Assistant messages and tool traffic are not carried forward.
use serde_json::{Value, json};

/// Instruction appended to visible transcript records in a compaction request.
pub const SUMMARY_PROMPT: &str = "Synth Code context checkpoint: the conversation is about to be compacted. Write a handoff summary for another model that will continue this work without seeing the conversation. Include: the user's task and any stopping rule, with progress against it (for example \"3 of 6 iterations done\"); key decisions, results and measurements; files changed and their current state (committed or not); constraints and user preferences; and the exact next step. Be concise and structured. Return the summary through submit_summary; it records text and executes no actions. Do not request other tools.";
/// Starts the user message that carries the summary into the new history.
pub const SUMMARY_PREFIX: &str = "Synth Code compacted the earlier conversation. Another model worked on this task and wrote the summary below; the workspace and tool state it describes are real. Build on it and do not repeat work it reports as done. Summary:";
/// Default automatic trigger: a request body this large compacts first (the provider refuses 512 KiB).
pub const REQUEST_BYTES: usize = 384 * 1024;
/// Automatic trigger on the provider's own count for the previous request.
pub const PROMPT_TOKENS: u64 = 200_000;
/// Automatic trigger before the session history reaches the 2048-entry turn admission cap.
pub const HISTORY_ENTRIES: usize = 1536;
/// Budget for carried-forward user messages: about 20k tokens, as in Codex CLI.
pub const USER_MESSAGE_BYTES: usize = 80_000;
/// Carried-forward user messages at most; with the summary this stays within the
/// 256-entry limit of a `Compact` command.
pub const USER_MESSAGES: usize = 255;
/// Whether the next request should compact first.
pub fn needed(request_bytes: usize, last_prompt_tokens: u64, history: usize, limit: usize) -> bool {
    request_bytes >= limit || last_prompt_tokens >= PROMPT_TOKENS || history >= HISTORY_ENTRIES
}

/// A message the user wrote, as opposed to runtime checks or an earlier summary.
pub fn user_written(message: &Value, runtime_prefixes: &[&str]) -> Option<String> {
    let text = message["content"].as_str()?;
    (message["role"] == "user"
        && !text.starts_with(SUMMARY_PREFIX)
        && !runtime_prefixes.iter().any(|p| text.starts_with(p)))
    .then(|| text.to_owned())
}

/// The newest user messages that fit the budget, in chronological order. The
/// first one that does not fit is cut to the remaining budget and selection stops.
pub fn retained(history: &[Value], runtime_prefixes: &[&str], budget: usize) -> Vec<Value> {
    let mut kept = Vec::new();
    let mut left = budget;
    for text in history
        .iter()
        .rev()
        .filter_map(|m| user_written(m, runtime_prefixes))
    {
        if kept.len() == USER_MESSAGES {
            break;
        }
        if text.len() <= left {
            left -= text.len();
            kept.push(text);
        } else {
            if left > 0 {
                let mut end = left;
                while !text.is_char_boundary(end) {
                    end -= 1;
                }
                kept.push(format!("{}… [truncated by compaction]", &text[..end]));
            }
            break;
        }
    }
    kept.into_iter()
        .rev()
        .map(|text| json!({"role":"user","content":text}))
        .collect()
}

pub fn summary_message(summary: &str) -> Value {
    let summary = summary.trim();
    let summary = if summary.is_empty() {
        "(no summary available)"
    } else {
        summary
    };
    json!({"role":"user","content":format!("{SUMMARY_PREFIX}\n{summary}")})
}

/// Accept exactly one named carrier with a nonempty, leak-free summary.
pub fn extract_summary(message: &Value) -> Option<String> {
    let calls = message.get("tool_calls")?.as_array()?;
    if calls.len() != 1
        || calls[0]["type"] != "function"
        || calls[0]["function"]["name"] != "submit_summary"
    {
        return None;
    }
    #[derive(serde::Deserialize)]
    #[serde(deny_unknown_fields)]
    struct Carrier {
        summary: String,
    }
    let carrier: Carrier =
        serde_json::from_str(calls[0]["function"]["arguments"].as_str()?).ok()?;
    let text = carrier.summary.trim();
    if text.is_empty() || text.to_ascii_lowercase().contains("<rfl:") || text.contains("<|") {
        return None;
    }
    Some(text.to_owned())
}

/// Present the complete visible conversation as JSON records in one user
/// message. The coding system instructions and private reasoning are not part
/// of the handoff; tool calls and results remain available as visible records.
pub fn summary_request(messages: Vec<Value>) -> Vec<Value> {
    let records: Vec<String> = messages
        .into_iter()
        .skip(1)
        .map(|message| {
            let mut visible = serde_json::Map::new();
            for field in ["role", "content", "tool_calls", "tool_call_id"] {
                if let Some(value) = message.get(field) {
                    visible.insert(field.into(), value.clone());
                }
            }
            Value::Object(visible).to_string()
        })
        .collect();
    vec![
        json!({"role":"user","content":format!("Conversation to summarize (JSON records):\n{}\n\n{SUMMARY_PROMPT}", records.join("\n"))}),
    ]
}

#[cfg(test)]
mod tests {
    use super::*;
    const CHECK: &str = "Synth Code check before finishing";

    fn user(text: &str) -> Value {
        json!({"role":"user","content":text})
    }

    #[test]
    fn only_user_written_messages_are_kept_newest_first_within_budget() {
        let history = vec![
            user("first task"),
            json!({"role":"assistant","content":"ok","tool_calls":[]}),
            json!({"role":"tool","tool_call_id":"a","content":"x"}),
            user(&format!("{CHECK}: re-read the task")),
            summary_message("older summary"),
            user("second"),
            user("third"),
        ];
        let kept = retained(&history, &[CHECK], 1000);
        let texts: Vec<_> = kept
            .iter()
            .map(|m| m["content"].as_str().unwrap())
            .collect();
        assert_eq!(texts, ["first task", "second", "third"]);
        // A tight budget keeps the newest whole and cuts the next one.
        let kept = retained(&history, &[CHECK], 8);
        let texts: Vec<_> = kept
            .iter()
            .map(|m| m["content"].as_str().unwrap())
            .collect();
        assert_eq!(texts, ["sec… [truncated by compaction]", "third"]);
    }

    #[test]
    fn many_short_messages_stay_within_the_command_limit() {
        let history: Vec<_> = (0..400).map(|i| user(&i.to_string())).collect();
        let kept = retained(&history, &[], USER_MESSAGE_BYTES);
        assert_eq!(kept.len(), USER_MESSAGES);
        assert_eq!(kept.last().unwrap()["content"], "399");
    }

    #[test]
    fn truncation_respects_utf8_boundaries() {
        let kept = retained(&[user("ΩΩΩ")], &[], 3);
        assert_eq!(kept[0]["content"], "Ω… [truncated by compaction]");
    }

    #[test]
    fn summaries_are_prefixed_and_never_empty() {
        assert_eq!(
            summary_message("  ")["content"],
            format!("{SUMMARY_PREFIX}\n(no summary available)")
        );
        assert!(user_written(&summary_message("x"), &[]).is_none());
    }

    #[test]
    fn summary_request_preserves_visible_history_without_private_reasoning_or_system() {
        let messages = vec![
            json!({"role":"system","content":"SYSTEM_SENTINEL"}),
            user(&"x".repeat(500 * 1024)),
            json!({"role":"assistant","content":"done","reasoning_content":"PRIVATE_SENTINEL","tool_calls":[{"id":"a"}]}),
            json!({"role":"tool","tool_call_id":"a","content":"result"}),
        ];
        let request = summary_request(messages);
        assert_eq!(request.len(), 1);
        let text = request[0]["content"].as_str().unwrap();
        assert!(
            text.contains(&"x".repeat(500 * 1024))
                && text.contains("result")
                && text.contains("tool_calls")
        );
        assert!(!text.contains("PRIVATE_SENTINEL") && !text.contains("SYSTEM_SENTINEL"));
        assert!(text.ends_with(SUMMARY_PROMPT));
    }

    #[test]
    fn summary_carrier_rejects_invalid_and_leaked_payloads() {
        let call = |args: Value| json!({"tool_calls":[{"type":"function","function":{"name":"submit_summary","arguments":args.to_string()}}]});
        assert_eq!(
            extract_summary(&call(json!({"summary":"  useful  "}))),
            Some("useful".into())
        );
        for args in [
            json!({"summary":""}),
            json!({"summary":"<|channel|>analysis"}),
            json!({"summary":"<|start|>"}),
            json!({"summary":"<|end|>"}),
            json!({"summary":"ok","extra":1}),
            json!({"summary":3}),
        ] {
            assert!(extract_summary(&call(args)).is_none());
        }
        assert!(extract_summary(&json!({"content":"ordinary text"})).is_none());
        assert!(extract_summary(&json!({"tool_calls":[{"type":"function","function":{"name":"submit_summary","arguments":"{\"summary\":\"one\",\"summary\":\"two\"}"}}]})).is_none());
    }

    #[test]
    fn triggers() {
        assert!(!needed(1000, 1000, 10, REQUEST_BYTES));
        assert!(needed(REQUEST_BYTES, 0, 0, REQUEST_BYTES));
        assert!(needed(0, PROMPT_TOKENS, 0, REQUEST_BYTES));
        assert!(needed(0, 0, HISTORY_ENTRIES, REQUEST_BYTES));
    }
}

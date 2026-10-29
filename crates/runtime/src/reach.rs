//! Which paths a proposed action names outside its session workspace.
//! Display only: the agent has full file access, so this never refuses
//! anything. It tells the person approving (or reading the transcript) that
//! an action reaches beyond the workspace.
use std::path::{Component, Path, PathBuf};

const MAX_REPORTED: usize = 5;
// Paths that are part of running a command, not files the user cares about.
const ROUTINE: [&str; 4] = ["/dev/null", "/dev/stdin", "/dev/stdout", "/dev/stderr"];
const ROUTINE_PREFIXES: [&str; 5] = [
    "/bin/",
    "/usr/bin/",
    "/usr/local/bin/",
    "/opt/homebrew/bin/",
    "/dev/fd/",
];

/// Paths named by a shell command that resolve outside `workspace`, as written.
pub fn command(command: &str, workspace: &str, home: Option<&str>) -> Vec<String> {
    let mut found = Vec::new();
    for word in words(command) {
        if word.contains("://") {
            continue;
        }
        // `PYTHONPATH=/a:/b`, `--out=/x`, `2>/tmp/log` all name paths after a separator.
        for part in word.split(['=', ':']) {
            let part = part.trim_start_matches(|c: char| {
                c.is_ascii_digit() || matches!(c, '<' | '>' | '&' | '(')
            });
            if outside(part, workspace, home) && !found.iter().any(|f| f == part) {
                found.push(part.to_string());
            }
        }
    }
    found.truncate(MAX_REPORTED);
    found
}

/// An edit path outside `workspace`, if it is one (absolute or `~/` paths only).
pub fn edit(path: &str, workspace: &str, home: Option<&str>) -> Vec<String> {
    if (path.starts_with('/') || path.starts_with('~')) && outside(path, workspace, home) {
        vec![path.to_string()]
    } else {
        Vec::new()
    }
}

fn outside(token: &str, workspace: &str, home: Option<&str>) -> bool {
    if token.is_empty()
        || ROUTINE.contains(&token)
        || ROUTINE_PREFIXES.iter().any(|p| token.starts_with(p))
    {
        return false;
    }
    let workspace = Path::new(workspace);
    let path = if let Some(rest) = token
        .strip_prefix("$HOME")
        .or_else(|| token.strip_prefix("${HOME}"))
    {
        if !(rest.is_empty() || rest.starts_with('/')) {
            return false;
        }
        match home {
            Some(home) => PathBuf::from(format!("{home}{rest}")),
            None => return true,
        }
    } else if token == "~" || token.starts_with("~/") {
        match home {
            Some(home) => PathBuf::from(format!("{home}{}", &token[1..])),
            None => return true,
        }
    } else if token.starts_with('~') {
        // `~other/...` is another user's home.
        return true;
    } else if token.starts_with('/') {
        PathBuf::from(token)
    } else if token == ".."
        || token.starts_with("../")
        || token.contains("/../")
        || token.ends_with("/..")
    {
        workspace.join(token)
    } else {
        return false;
    };
    !normalize(&path).starts_with(normalize(workspace))
}

fn normalize(path: &Path) -> PathBuf {
    let mut out = PathBuf::new();
    for component in path.components() {
        match component {
            Component::ParentDir => {
                out.pop();
            }
            Component::CurDir => {}
            other => out.push(other),
        }
    }
    out
}

/// Shell words with quotes removed; operators split words apart. Rough on
/// purpose: a missed path only means a missing hint, never a refusal.
fn words(command: &str) -> Vec<String> {
    let mut words = Vec::new();
    let mut word = String::new();
    let mut quote = None;
    for c in command.chars() {
        match (quote, c) {
            (Some(q), c) if c == q => quote = None,
            (Some(_), c) => word.push(c),
            (None, '\'' | '"') => quote = Some(c),
            (None, c) if c.is_whitespace() || matches!(c, ';' | '|' | '&' | '(' | ')' | '`') => {
                if !word.is_empty() {
                    words.push(std::mem::take(&mut word));
                }
            }
            (None, c) => word.push(c),
        }
    }
    if !word.is_empty() {
        words.push(word);
    }
    words
}

#[cfg(test)]
mod tests {
    use super::*;
    const WS: &str = "/Users/me/GitHub/project";
    const HOME: Option<&str> = Some("/Users/me");

    #[test]
    fn workspace_local_commands_name_nothing() {
        for c in [
            "./measure.sh",
            "git commit -am 'keep: 0.1'",
            "python3 policy.py > out.txt 2>/dev/null",
            "ls src/../tests",
            "/usr/bin/env python3 x.py",
            "cat /Users/me/GitHub/project/policy.py",
            "curl https://example.com/a/b",
            "git diff HEAD~1",
            "cd ~/GitHub/project && ls",
        ] {
            assert_eq!(command(c, WS, HOME), Vec::<String>::new(), "{c}");
        }
    }

    #[test]
    fn outside_paths_are_reported_as_written() {
        let cases: [(&str, &[&str]); 8] = [
            ("find / -name '*.md'", &["/"]),
            (
                "ls ~ && cat ~/GitHub/handoffs/plan.md",
                &["~", "~/GitHub/handoffs/plan.md"],
            ),
            (
                "PYTHONPATH=/Users/me/GitHub/bench python3 x.py",
                &["/Users/me/GitHub/bench"],
            ),
            ("cat > /tmp/insp.py", &["/tmp/insp.py"]),
            ("grep -rn foo ../other", &["../other"]),
            ("echo $HOME/.zshrc", &["$HOME/.zshrc"]),
            ("python3 x.py 2>/tmp/err.log", &["/tmp/err.log"]),
            (
                "cat \"/Users/me/My Docs/a.txt\"",
                &["/Users/me/My Docs/a.txt"],
            ),
        ];
        for (c, want) in cases {
            assert_eq!(command(c, WS, HOME), want, "{c}");
        }
    }

    #[test]
    fn edits_outside_the_workspace_are_reported() {
        assert_eq!(edit("policy.py", WS, HOME), Vec::<String>::new());
        assert_eq!(
            edit("/Users/me/GitHub/project/a.py", WS, HOME),
            Vec::<String>::new()
        );
        assert_eq!(edit("~/notes.md", WS, HOME), vec!["~/notes.md"]);
        assert_eq!(edit("/etc/hosts", WS, HOME), vec!["/etc/hosts"]);
    }

    #[test]
    fn reports_are_bounded() {
        let c = (0..20)
            .map(|i| format!("/tmp/{i}"))
            .collect::<Vec<_>>()
            .join(" ");
        assert_eq!(command(&c, WS, HOME).len(), MAX_REPORTED);
    }
}

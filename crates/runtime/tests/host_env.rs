use synth_code_runtime::host_env;

#[test]
fn opaque_image_ids_can_be_found_by_tags_without_claiming_created_is_running() {
    let rows = "abc123\tpool-runtime\tsha256:deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef\tcreated\tCreated\nother\tweb\tnginx:latest\trunning\tUp 2 hours\n";
    let images = "sha256:deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef\tcraftax-runtime:qualified\n";
    let result = host_env::inventory(rows, images, "CRAFTAX");
    let items = result["containers"].as_array().unwrap();
    assert_eq!(items.len(), 1);
    assert_eq!(items[0]["name"], "pool-runtime");
    assert_eq!(items[0]["running"], false);
    assert_eq!(items[0]["state"], "created");
    assert_eq!(items[0]["imageTags"][0], "craftax-runtime:qualified");
    assert!(
        host_env::inventory(rows, images, "missing")["containers"]
            .as_array()
            .unwrap()
            .is_empty()
    );
}
#[test]
fn discovery_is_bounded_and_does_not_infer_container_ownership() {
    let rows = (0..100)
        .map(|i| format!("id{i}\tcraftax-{i}\timage\trunning\tUp\n"))
        .collect::<String>();
    let result = host_env::inventory(&rows, "", "craftax");
    assert_eq!(result["containers"].as_array().unwrap().len(), 64);
    assert_eq!(result["truncated"], true);
    assert!(serde_json::to_vec(&result).unwrap().len() < 60_000);
    assert!(result["containers"][0].get("owner").is_none());
    let rows = format!("id\t{}\timage\trunning\tUp\n", "x".repeat(60000));
    let result = host_env::inventory(&rows, "", "");
    assert_eq!(result["truncated"], true);
    assert!(result["containers"].as_array().unwrap().is_empty());
}
#[test]
fn host_info_distinguishes_the_workspace_and_command_environment() {
    let info = host_env::info("/selected/workspace");
    assert_eq!(info["workspace"], "/selected/workspace");
    assert_eq!(info["hostCommands"], true);
    assert!(info["homeDirectory"].as_str().unwrap().starts_with('/'));
    assert_eq!(info["commandPath"], host_env::COMMAND_PATH);
    assert!(info["executables"].get("docker").is_some());
    assert!(info.get("credentials").is_none());
}

#[test]
fn matching_tags_beyond_the_display_cap_remain_discoverable() {
    let id = "a".repeat(64);
    let images = (0..12)
        .map(|i| {
            format!(
                "sha256:{id}\t{}:{i}\n",
                if i == 11 { "craftax" } else { "generic" }
            )
        })
        .collect::<String>();
    let rows = format!("container\tpool\tsha256:{id}\tcreated\tCreated\n");
    let result = host_env::inventory(&rows, &images, "craftax");
    assert_eq!(result["containers"].as_array().unwrap().len(), 1);
    assert_eq!(result["containers"][0]["imageTags"][0], "craftax:11");
    assert_eq!(result["containers"][0]["imageTagsTruncated"], true);
}

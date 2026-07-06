// events.gen.rs -- DO NOT HAND-EDIT
// Generated from desktop/src/types/events.schema.json via npm run gen:events.

#[derive(Debug, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub(crate) enum KimEvent {
    Status {
        message: String,
    },
    Plan {
        steps: Vec<serde_json::Value>,
    },
    Step {
        n: usize,
        data: serde_json::Value,
    },
    Done {
        n: usize,
    },
    Context {
        cumulative_input: u64,
        budget: u64,
        phase: String,
        percent: u32,
        last_input: u64,
        last_output: u64,
        source: String,
        estimate: bool,
    },
    Stats {
        input: u64,
        output: u64,
        total: u64,
    },
    UiScreenshotFlash,
    UiShow,
    RunDone {
        termination: String,
        success: bool,
    },
    RunFailed {
        reason: String,
        recoverable: bool,
        suggestion: String,
    },
    ProviderError {
        code: String,
        retryable: bool,
    },
    RateLimited {
        delay: f64,
        attempt: u32,
        max_retries: u32,
    },
    HitlApprovalRequest {
        tool: String,
        risk: String,
        reason: String,
        #[serde(default)]
        preview: String,
        #[serde(default)]
        id: String,
    },
    HitlApprovalResult {
        tool: String,
        approved: bool,
    },
    Tool {
        name: String,
        args: serde_json::Value,
    },
    Answer {
        text: String,
    },
    Diff {
        path: String,
        added: usize,
        removed: usize,
    },
    Activity {
        kind: String,
        text: String,
    },
    CommandApprovalRequest {
        id: String,
        command: String,
        cwd: String,
        #[serde(default)]
        reason: String,
        #[serde(default)]
        risk: String,
        #[serde(default)]
        network: bool,
        #[serde(default)]
        amendment: Vec<serde_json::Value>,
    },
    FileChangeApprovalRequest {
        id: String,
        files: Vec<serde_json::Value>,
        #[serde(default)]
        reason: String,
    },
    CommandOutput {
        item_id: String,
        chunk: String,
    },
    AssistantDelta {
        chunk: String,
    },
    ReasoningDelta {
        chunk: String,
    },
    PlanUpdate {
        steps: Vec<serde_json::Value>,
    },
    DiffUpdate {
        unified_diff: String,
    },
    TokenUsage {
        input: u64,
        output: u64,
        total: u64,
    },
    ItemLifecycle {
        item_id: String,
        kind: String,
        phase: String,
        #[serde(default)]
        title: String,
    },
    TurnLifecycle {
        phase: String,
        #[serde(default)]
        turn_id: String,
    },
}

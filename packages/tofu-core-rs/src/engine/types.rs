//! Value types shared by the engine's ports and policy modules.

use serde_json::Value;

/// One tool call requested by the model, as the provider stream finished it.
#[derive(Debug, Clone, PartialEq)]
pub struct ToolCall {
    pub id: String,
    pub name: String,
    /// The argument document exactly as the provider sent it (a JSON text).
    pub arguments: String,
}

/// One message of the model-visible history. `content` is the text body;
/// assistant tool calls and tool results carry their call identities.
#[derive(Debug, Clone, PartialEq)]
pub struct Message {
    pub role: Role,
    pub content: String,
    pub tool_calls: Vec<ToolCall>,
    pub tool_call_id: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Role {
    System,
    User,
    Assistant,
    Tool,
}

impl Role {
    pub fn as_str(self) -> &'static str {
        match self {
            Role::System => "system",
            Role::User => "user",
            Role::Assistant => "assistant",
            Role::Tool => "tool",
        }
    }
}

impl Message {
    pub fn text(role: Role, content: impl Into<String>) -> Self {
        Self {
            role,
            content: content.into(),
            tool_calls: Vec::new(),
            tool_call_id: None,
        }
    }

    pub fn tool_result(call_id: &str, content: impl Into<String>) -> Self {
        Self {
            role: Role::Tool,
            content: content.into(),
            tool_calls: Vec::new(),
            tool_call_id: Some(call_id.to_string()),
        }
    }
}

/// How one provider request ended. Mirrors the terminal states of
/// `lib/llm/stream_result.py`: only `Completed` with a provider finish proves
/// completion; every other variant is a failure class the loop must decide.
#[derive(Debug, Clone, PartialEq)]
pub enum ProviderOutcome {
    /// The provider finished the response (`stop`, `tool_calls`, `length`).
    Completed {
        text: String,
        tool_calls: Vec<ToolCall>,
        finish_reason: String,
        usage: Value,
    },
    /// The provider refused (`content_filter` finish or a refusal error).
    /// `partial_text` is what streamed before the refusal; it is discarded.
    ContentFilter {
        partial_text: String,
        detail: String,
    },
    /// An HTTP error before or inside the stream (an in-stream 429 inside a
    /// 200 SSE body is reported here with `status = 429`).
    HttpError {
        status: u16,
        detail: String,
        partial_text: String,
    },
    /// The connection ended without a provider finish (EOF, reset, timeout).
    Dropped {
        partial_text: String,
        detail: String,
    },
    /// The request was cancelled while streaming.
    Cancelled { partial_text: String },
    /// An exception outside the provider failure taxonomy (a defect or an
    /// exhausted script): the loop settles it as an `internal` error.
    Internal { detail: String },
}

/// The result of running one tool call, as the tool port reports it.
#[derive(Debug, Clone, PartialEq)]
pub enum ToolRun {
    /// The call ran to completion; `is_error` marks a tool-level failure
    /// (the tool ran and reported an error to the model).
    Finished { output: String, is_error: bool },
    /// A typed refusal before the effect could start (admission, policy,
    /// schema). The only failure that settles as `not_started`.
    RefusedBeforeEntry { reason: String },
    /// The call entered its effect and then failed without a reliable result
    /// (panic, lost executor, transport loss). Settles as `unknown`.
    LostAfterEntry { detail: String },
    /// Cancelled; `entered` says whether the effect had started.
    Cancelled { entered: bool },
    /// The handler raised after entry. `type_name` and `detail` are the
    /// caught exception's class and text; `class` decides the effect.
    Raised {
        type_name: String,
        detail: String,
        class: RaisedClass,
    },
}

/// The recovery-relevant class of a raised handler exception.
#[derive(Debug, Clone, PartialEq)]
pub enum RaisedClass {
    /// A typed pre-execution admission refusal (`CommandAdmissionRefusal`):
    /// the effect never started.
    AdmissionRefusal,
    /// A storage error from the handler's own writes.
    Storage {
        code: String,
        transient: bool,
    },
    Other,
}

/// The side-effect class a tool declares (`docs/contracts/TOOL_EFFECT_RECOVERY.md`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EffectClass {
    /// Pure reads pay nothing: no intent, repeatable after a restart.
    ReadOnly,
    /// Any other call commits an intent with an exact storage ack before entry.
    Mutating,
}

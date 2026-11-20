//! `lib/llm/_sse_framer.py` `SSEFramer` over one complete body.

use super::errors::{unsupported, Flow};

const MAX_SSE_EVENT_BYTES: usize = 1 << 20;

/// One framing result in wire order.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum Frame {
    /// A complete event's joined `data:` payload.
    Event(String),
    /// One framing issue (an oversized event).
    Issue,
}

#[derive(Default)]
struct Framer {
    line: String,
    line_has_content: bool,
    data_lines: Vec<String>,
    saw_data_field: bool,
    event_bytes: usize,
    discard_event: bool,
    frames: Vec<Frame>,
}

impl Framer {
    fn reset_event(&mut self) {
        self.line.clear();
        self.line_has_content = false;
        self.data_lines.clear();
        self.saw_data_field = false;
        self.event_bytes = 0;
        self.discard_event = false;
    }

    fn mark_oversized(&mut self) {
        if self.discard_event {
            return;
        }
        self.frames.push(Frame::Issue);
        self.discard_event = true;
        self.line.clear();
        self.data_lines.clear();
        self.saw_data_field = false;
    }

    fn append(&mut self, text: &str) {
        if text.is_empty() {
            return;
        }
        self.line_has_content = true;
        if self.discard_event {
            return;
        }
        self.event_bytes += text.len();
        if self.event_bytes > MAX_SSE_EVENT_BYTES {
            self.mark_oversized();
            return;
        }
        self.line.push_str(text);
    }

    fn parse_field(&mut self, line: &str) {
        if line.is_empty() || line.starts_with(':') {
            return;
        }
        let (field, value) = match line.split_once(':') {
            Some((field, value)) => (field, value.strip_prefix(' ').unwrap_or(value)),
            None => (line, ""),
        };
        if field == "data" {
            self.saw_data_field = true;
            self.data_lines.push(value.to_owned());
        }
    }

    fn finish_line(&mut self) {
        let line_was_empty = !self.line_has_content;
        let line = std::mem::take(&mut self.line);
        self.line_has_content = false;
        if !line_was_empty {
            if !self.discard_event {
                self.event_bytes += 1;
                if self.event_bytes > MAX_SSE_EVENT_BYTES {
                    self.mark_oversized();
                } else {
                    self.parse_field(&line);
                }
            }
            return;
        }
        if self.discard_event {
            self.reset_event();
            return;
        }
        if self.saw_data_field {
            let data = self.data_lines.join("\n");
            self.frames.push(Frame::Event(data));
        }
        self.reset_event();
    }
}

/// Frame `body`; the flag reports EOF inside unfinished event data
/// (`SSEFramer.finalize`'s `truncated_event`), relevant only when the consumer
/// never stopped.
pub(crate) fn frame(body: &[u8]) -> Flow<(Vec<Frame>, bool)> {
    let Ok(text) = std::str::from_utf8(body) else {
        return unsupported("invalid UTF-8 framing recovery");
    };
    let text = text.strip_prefix('\u{feff}').unwrap_or(text);
    let mut framer = Framer::default();
    let mut segment_start = 0;
    let mut swallow_lf = false;
    for (index, ch) in text.char_indices() {
        if swallow_lf {
            swallow_lf = false;
            if ch == '\n' {
                segment_start = index + 1;
                continue;
            }
        }
        if ch != '\r' && ch != '\n' {
            continue;
        }
        framer.append(&text[segment_start..index]);
        framer.finish_line();
        segment_start = index + 1;
        swallow_lf = ch == '\r';
    }
    framer.append(&text[segment_start..]);
    let pending_field = framer.line.split(':').next().unwrap_or("");
    let truncated = framer.discard_event || framer.saw_data_field || pending_field == "data";
    Ok((framer.frames, truncated))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn crlf_comments_and_multiline_data() {
        let (frames, truncated) =
            frame(b": c\r\n\r\ndata: a\r\ndata:b\r\rdata: c\n\ndata").unwrap();
        assert_eq!(
            frames,
            vec![Frame::Event("a\nb".into()), Frame::Event("c".into())]
        );
        assert!(truncated);
    }
}

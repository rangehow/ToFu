//! Private port of `lib/storage/turn_projection_pages.py`: the frame-bounded
//! `turn.projection.page` plan over one public Turn.

use serde_json::{json, Value};

use super::core::Obj;

const TURN_PROJECTION_PAGE_CONTRACT: &str = "tofu.turn-projection-page/v1";
/// `generated_v2_semantics.MAX_FRAME_PAYLOAD_BYTES`.
const MAX_FRAME_PAYLOAD_BYTES: i64 = 8_384_512;
const PAGED_SEQUENCE_MEMBERS: &[&str] = &["toolRounds", "segments", "apiRounds"];
const PAGED_TEXT_MEMBERS: &[&str] = &["content", "thinking"];

pub fn stale_turn_projection_page(revision: i64) -> Value {
    json!({"contract": TURN_PROJECTION_PAGE_CONTRACT, "stale": true, "projectionRevision": revision})
}

fn encoded_bytes(value: &Value) -> i64 {
    crate::json::dumps(value).len() as i64
}

enum Member {
    Items(Vec<Value>),
    Text(Vec<char>),
}

impl Member {
    fn len(&self) -> usize {
        match self {
            Member::Items(items) => items.len(),
            Member::Text(chars) => chars.len(),
        }
    }
}

fn split_head(turn: &Obj) -> (Obj, Vec<(String, Member)>) {
    let mut head = turn.clone();
    let Some(Value::Object(projection)) = turn.get("projection") else { return (head, vec![]) };
    let mut head_projection = projection.clone();
    let mut members = Vec::new();
    for name in PAGED_SEQUENCE_MEMBERS {
        if let Some(Value::Array(_)) = head_projection.get(*name) {
            let Some(Value::Array(items)) = head_projection.shift_remove(*name) else { unreachable!() };
            members.push((name.to_string(), Member::Items(items)));
        }
    }
    for name in PAGED_TEXT_MEMBERS {
        if let Some(Value::String(_)) = head_projection.get(*name) {
            let Some(Value::String(text)) = head_projection.shift_remove(*name) else { unreachable!() };
            members.push((name.to_string(), Member::Text(text.chars().collect())));
        }
    }
    head.insert("projection".into(), Value::Object(head_projection));
    (head, members)
}

fn envelope(revision: i64, page: i64, page_count: i64) -> Obj {
    let mut map = Obj::new();
    map.insert("contract".into(), json!(TURN_PROJECTION_PAGE_CONTRACT));
    map.insert("stale".into(), json!(false));
    map.insert("projectionRevision".into(), json!(revision));
    map.insert("page".into(), json!(page));
    map.insert("pageCount".into(), json!(page_count));
    map
}

fn slice_opening_bytes(name: &str, start: usize, text: bool) -> i64 {
    let body = if text { json!({"start": start, "text": ""}) } else { json!({"start": start, "items": []}) };
    let mut map = Obj::new();
    map.insert(name.to_owned(), body);
    encoded_bytes(&Value::Object(map)) - 2
}

fn text_bytes(chars: &[char]) -> i64 {
    encoded_bytes(&Value::String(chars.iter().collect())) - 2
}

struct Planner {
    page_bytes: i64,
    pages: Vec<Vec<(String, usize, usize)>>,
    slices: Vec<(String, usize, usize)>,
    used: i64,
    units: usize,
}

impl Planner {
    fn fits(&self, cost: i64) -> bool {
        self.used + cost <= MAX_FRAME_PAYLOAD_BYTES
    }

    fn must_close(&self) -> bool {
        self.units > 0 || self.pages.is_empty()
    }

    fn close(&mut self) {
        self.pages.push(std::mem::take(&mut self.slices));
        self.used = self.page_bytes;
        self.units = 0;
    }

    fn opening(&self, name: &str, start: usize, text: bool) -> i64 {
        slice_opening_bytes(name, start, text) + if self.slices.is_empty() { 0 } else { 1 }
    }

    fn continues(&self, name: &str) -> bool {
        self.slices.last().is_some_and(|slice| slice.0 == name)
    }

    fn add_item(&mut self, name: &str, index: usize, item_bytes: i64) {
        let mut cost =
            if self.continues(name) { item_bytes + 1 } else { self.opening(name, index, false) + item_bytes };
        if !self.fits(cost) && self.must_close() {
            self.close();
            cost = self.opening(name, index, false) + item_bytes;
        }
        if self.continues(name) {
            self.slices.last_mut().expect("slice").2 = index + 1;
        } else {
            self.slices.push((name.to_owned(), index, index + 1));
        }
        self.used += cost;
        self.units += 1;
    }

    fn add_text(&mut self, name: &str, text: &[char]) {
        let mut start = 0;
        while start < text.len() {
            let opening = self.opening(name, start, true);
            let end = largest_fitting_end(text, start, MAX_FRAME_PAYLOAD_BYTES - self.used - opening);
            if end == start && self.must_close() {
                self.close();
                continue;
            }
            let end = end.max(start + 1);
            self.slices.push((name.to_owned(), start, end));
            self.used += opening + text_bytes(&text[start..end]);
            self.units += end - start;
            if end < text.len() {
                self.close();
            }
            start = end;
        }
    }

    fn finish(mut self) -> Vec<Vec<(String, usize, usize)>> {
        self.pages.push(self.slices);
        self.pages
    }
}

fn largest_fitting_end(text: &[char], start: usize, budget: i64) -> usize {
    let mut low = start;
    let mut high = text.len().min(start + budget.max(0) as usize);
    while low < high {
        let middle = (low + high).div_ceil(2);
        if text_bytes(&text[start..middle]) <= budget {
            low = middle;
        } else {
            high = middle - 1;
        }
    }
    low
}

/// `turn_projection_page(turn, page)`.
pub fn turn_projection_page(turn: &Obj, page: i64) -> Option<Value> {
    let revision = turn.get("projectionRevision").and_then(Value::as_i64).unwrap_or(0);
    let (head, members) = split_head(turn);
    let mut lengths = Obj::new();
    for (name, member) in &members {
        lengths.insert(name.clone(), json!(member.len()));
    }
    let page_bound = members.iter().map(|(_, member)| member.len() as i64).sum::<i64>() + 1;
    let mut later = envelope(revision, page_bound, page_bound);
    later.insert("members".into(), json!({}));
    let mut first = envelope(revision, page_bound, page_bound);
    first.insert("turn".into(), Value::Object(head.clone()));
    first.insert("pagedMembers".into(), Value::Object(lengths.clone()));
    first.insert("members".into(), json!({}));
    let mut planner = Planner {
        page_bytes: encoded_bytes(&Value::Object(later)),
        pages: vec![],
        slices: vec![],
        used: encoded_bytes(&Value::Object(first)),
        units: 0,
    };
    for (name, member) in &members {
        match member {
            Member::Text(chars) => planner.add_text(name, chars),
            Member::Items(items) => {
                for (index, item) in items.iter().enumerate() {
                    planner.add_item(name, index, encoded_bytes(item));
                }
            }
        }
    }
    let pages = planner.finish();
    if page < 0 || page as usize >= pages.len() {
        return None;
    }
    let mut response = envelope(revision, page, pages.len() as i64);
    if page == 0 {
        response.insert("turn".into(), Value::Object(head));
        response.insert("pagedMembers".into(), Value::Object(lengths));
    }
    let mut slices = Obj::new();
    for (name, start, end) in &pages[page as usize] {
        let member = &members.iter().find(|(member, _)| member == name).expect("member").1;
        let body = match member {
            Member::Text(chars) => json!({"start": start, "text": chars[*start..*end].iter().collect::<String>()}),
            Member::Items(items) => json!({"start": start, "items": items[*start..*end].to_vec()}),
        };
        slices.insert(name.clone(), body);
    }
    response.insert("members".into(), Value::Object(slices));
    Some(Value::Object(response))
}

//! Frame-bounded pages of one public Turn projection (`turn.projection.page`,
//! docs/STORAGE.md "Paged Turn storage"). Mirrors the SQL authorities'
//! `lib/storage/turn_projection_pages.py`: page 0 carries the Turn without its
//! paged members plus their lengths; every page carries contiguous member
//! slices in member order. Each encoded page fits one storage.v2 frame payload
//! unless it holds a single sequence item that alone exceeds it.
use super::*;
use crate::generated_storage_v2::MAX_PAYLOAD_BYTES;

const CONTRACT: &str = "tofu.turn-projection-page/v1";
/// Members that grow with tool rounds; every other projection field is bounded
/// by its own schema and stays in the page-0 head.
const SEQUENCE_MEMBERS: [&str; 3] = ["toolRounds", "segments", "apiRounds"];
const TEXT_MEMBERS: [&str; 2] = ["content", "thinking"];

struct ByteCount(usize);

impl io::Write for ByteCount {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        self.0 = self.0.saturating_add(bytes.len());
        Ok(bytes.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

fn encoded_bytes(value: &impl Serialize) -> io::Result<usize> {
    let mut count = ByteCount(0);
    serde_json::to_writer(&mut count, value)
        .map_err(|_| invalid_data("Turn projection page cannot be encoded"))?;
    Ok(count.0)
}

/// Bytes of one character inside a JSON string, matching serde_json escaping.
fn escaped_bytes(character: char) -> usize {
    match character {
        '"' | '\\' | '\u{08}' | '\u{0c}' | '\n' | '\r' | '\t' => 2,
        character if (character as u32) < 0x20 => 6,
        character => character.len_utf8(),
    }
}

enum Member {
    Sequence(Vec<Value>),
    Text(String),
}

impl Member {
    fn length(&self) -> usize {
        match self {
            Self::Sequence(items) => items.len(),
            Self::Text(text) => text.chars().count(),
        }
    }
}

/// One slice of `members[member]` on a page; text bounds count Unicode scalar values.
struct Slice {
    member: usize,
    start: usize,
    end: usize,
}

/// Greedy page boundaries with the SQL authorities' exact accounting.
struct Planner {
    limit: usize,
    page_bytes: usize,
    pages: Vec<Vec<Slice>>,
    slices: Vec<Slice>,
    used: usize,
    units: usize,
}

impl Planner {
    fn fits(&self, cost: usize) -> bool {
        self.used.saturating_add(cost) <= self.limit
    }

    /// A later page always admits one unit; only page 0 may hold just the head.
    fn must_close(&self) -> bool {
        self.units > 0 || self.pages.is_empty()
    }

    fn close(&mut self) {
        self.pages.push(std::mem::take(&mut self.slices));
        self.used = self.page_bytes;
        self.units = 0;
    }

    /// `"name":{"start":S,"items":[]}` or its text form, plus a separator.
    fn opening(&self, name: &str, start: usize, text: bool) -> io::Result<usize> {
        let mut body = Map::new();
        body.insert("start".to_owned(), Value::from(start));
        body.insert(
            if text { "text" } else { "items" }.to_owned(),
            if text {
                Value::String(String::new())
            } else {
                Value::Array(Vec::new())
            },
        );
        let mut slice = Map::new();
        slice.insert(name.to_owned(), Value::Object(body));
        Ok(encoded_bytes(&slice)? - 2 + usize::from(!self.slices.is_empty()))
    }

    fn add_item(
        &mut self,
        member: usize,
        name: &str,
        index: usize,
        item_bytes: usize,
    ) -> io::Result<()> {
        let open = self
            .slices
            .last()
            .is_some_and(|slice| slice.member == member);
        let mut cost = if open {
            item_bytes + 1
        } else {
            self.opening(name, index, false)? + item_bytes
        };
        if !self.fits(cost) && self.must_close() {
            self.close();
            cost = self.opening(name, index, false)? + item_bytes;
        }
        match self.slices.last_mut() {
            Some(slice) if slice.member == member => slice.end = index + 1,
            _ => self.slices.push(Slice {
                member,
                start: index,
                end: index + 1,
            }),
        }
        self.used = self.used.saturating_add(cost);
        self.units += 1;
        Ok(())
    }

    fn add_text(&mut self, member: usize, name: &str, text: &str) -> io::Result<()> {
        let mut characters = text.chars().peekable();
        let mut start = 0;
        while characters.peek().is_some() {
            let opening = self.opening(name, start, true)?;
            let budget = self.limit.saturating_sub(self.used.saturating_add(opening));
            let (mut end, mut body) = (start, 0);
            while let Some(&character) = characters.peek() {
                let bytes = escaped_bytes(character);
                if body + bytes > budget {
                    break;
                }
                body += bytes;
                end += 1;
                characters.next();
            }
            if end == start && self.must_close() {
                self.close();
                continue;
            }
            if end == start {
                if let Some(character) = characters.next() {
                    body += escaped_bytes(character);
                    end += 1;
                }
            }
            self.slices.push(Slice { member, start, end });
            self.used = self.used.saturating_add(opening + body);
            self.units += end - start;
            if characters.peek().is_some() {
                self.close();
            }
            start = end;
        }
        Ok(())
    }

    fn finish(mut self) -> Vec<Vec<Slice>> {
        self.pages.push(self.slices);
        self.pages
    }
}

fn envelope(revision: u64, page: usize, page_count: usize) -> Value {
    json!({
        "contract": CONTRACT,
        "stale": false,
        "projectionRevision": revision,
        "page": page,
        "pageCount": page_count,
        "members": {},
    })
}

/// Move the paged members out of the projection, in member order.
fn split_head(turn: &mut Value) -> Vec<(&'static str, Member)> {
    let Some(projection) = turn.get_mut("projection").and_then(Value::as_object_mut) else {
        return Vec::new();
    };
    let mut members = Vec::new();
    for name in SEQUENCE_MEMBERS {
        if projection.get(name).is_some_and(Value::is_array) {
            if let Some(Value::Array(items)) = projection.remove(name) {
                members.push((name, Member::Sequence(items)));
            }
        }
    }
    for name in TEXT_MEMBERS {
        if projection.get(name).is_some_and(Value::is_string) {
            if let Some(Value::String(text)) = projection.remove(name) {
                members.push((name, Member::Text(text)));
            }
        }
    }
    members
}

fn plan(
    revision: u64,
    head: &Value,
    lengths: &Map<String, Value>,
    members: &[(&'static str, Member)],
) -> io::Result<Vec<Vec<Slice>>> {
    plan_within(revision, head, lengths, members, MAX_PAYLOAD_BYTES)
}

fn plan_within(
    revision: u64,
    head: &Value,
    lengths: &Map<String, Value>,
    members: &[(&'static str, Member)],
    limit: usize,
) -> io::Result<Vec<Vec<Slice>>> {
    #[cfg(test)]
    PLANS.with(|plans| plans.set(plans.get() + 1));
    // Page numbers never exceed the unit count, so this bounds their digits.
    let page_bound = members
        .iter()
        .map(|(_, member)| member.length())
        .sum::<usize>()
        + 1;
    let later = encoded_bytes(&envelope(revision, page_bound, page_bound))?;
    // Page 0 adds `,"turn":<head>` and `,"pagedMembers":<lengths>`.
    let first = later
        + ",\"turn\":".len()
        + encoded_bytes(head)?
        + ",\"pagedMembers\":".len()
        + encoded_bytes(lengths)?;
    let mut planner = Planner {
        limit,
        page_bytes: later,
        pages: Vec::new(),
        slices: Vec::new(),
        used: first,
        units: 0,
    };
    for (position, (name, member)) in members.iter().enumerate() {
        match member {
            Member::Sequence(items) => {
                for (index, item) in items.iter().enumerate() {
                    planner.add_item(position, name, index, encoded_bytes(item)?)?;
                }
            }
            Member::Text(text) => planner.add_text(position, name, text)?,
        }
    }
    Ok(planner.finish())
}

fn text_slice(text: &str, start: usize, end: usize) -> &str {
    let mut offsets = text
        .char_indices()
        .map(|(offset, _)| offset)
        .chain(std::iter::once(text.len()));
    let first = offsets.nth(start).unwrap_or(text.len());
    let last = offsets.nth(end - start - 1).unwrap_or(text.len());
    &text[first..last]
}

/// One page of the public Turn at the expected revision; `None` when the Turn
/// is absent and a stale answer when the revision moved.
pub(crate) fn read(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    turn_id: &str,
    expected_revision: Option<u64>,
    page: u64,
) -> io::Result<Option<Vec<u8>>> {
    let Some(mut turn) = get_public_value(database, transaction, conversation_id, turn_id)? else {
        return Ok(None);
    };
    let revision = turn
        .get("projectionRevision")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("turn projection revision is malformed"))?;
    if expected_revision.is_some_and(|expected| expected != revision) {
        return encode_response(&json!({
            "contract": CONTRACT,
            "stale": true,
            "projectionRevision": revision,
        }))
        .map(Some);
    }
    let members = split_head(&mut turn);
    let lengths: Map<String, Value> = members
        .iter()
        .map(|(name, member)| ((*name).to_owned(), Value::from(member.length())))
        .collect();
    let pages = plan(revision, &turn, &lengths, &members)?;
    let page = usize::try_from(page)
        .ok()
        .filter(|page| *page < pages.len())
        .ok_or_else(|| invalid_input("Turn projection page is outside its revision"))?;
    let mut response = envelope(revision, page, pages.len());
    let mut slices = Map::new();
    for slice in &pages[page] {
        let (name, member) = members
            .get(slice.member)
            .ok_or_else(|| invalid_data("Turn projection page names an unknown member"))?;
        let value = match member {
            Member::Sequence(items) => json!({
                "start": slice.start,
                "items": &items[slice.start..slice.end],
            }),
            Member::Text(text) => json!({
                "start": slice.start,
                "text": text_slice(text, slice.start, slice.end),
            }),
        };
        slices.insert((*name).to_owned(), value);
    }
    response["members"] = Value::Object(slices);
    if page == 0 {
        response["turn"] = turn;
        response["pagedMembers"] = Value::Object(lengths);
    }
    encode_response(&response).map(Some)
}

#[cfg(test)]
thread_local! { pub(crate) static PLANS: std::cell::Cell<usize> = const { std::cell::Cell::new(0) }; }

/// Whether `plan` keeps the whole Turn on page 0, from the Turn's exact encoded
/// length instead of encoding every member item again. Greedy planning closes
/// a page only when the next unit does not fit, so the Turn has one page
/// exactly when page 0 holding every member fits. That page costs the Turn's
/// own encoding, plus the page envelope, the member lengths and one slice
/// opening per non-empty member, minus the member keys, brackets and
/// separators the head no longer carries.
pub(crate) fn fits_one_page(turn: &Value, encoded_bytes_of_turn: usize) -> io::Result<bool> {
    fits_one_page_within(turn, encoded_bytes_of_turn, MAX_PAYLOAD_BYTES)
}

fn fits_one_page_within(
    turn: &Value,
    encoded_bytes_of_turn: usize,
    limit: usize,
) -> io::Result<bool> {
    let revision = turn
        .get("projectionRevision")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("turn projection revision is malformed"))?;
    let empty = Map::new();
    let projection = turn
        .get("projection")
        .and_then(Value::as_object)
        .unwrap_or(&empty);
    // The members `split_head` moves, in member order, with their unit counts.
    let mut members: Vec<(&'static str, usize, bool)> = Vec::new();
    for name in SEQUENCE_MEMBERS {
        if let Some(Value::Array(items)) = projection.get(name) {
            members.push((name, items.len(), false));
        }
    }
    for name in TEXT_MEMBERS {
        if let Some(Value::String(text)) = projection.get(name) {
            members.push((name, text.chars().count(), true));
        }
    }
    let lengths: Map<String, Value> = members
        .iter()
        .map(|(name, length, _)| ((*name).to_owned(), Value::from(*length)))
        .collect();
    let page_bound = members.iter().map(|(_, length, _)| length).sum::<usize>() + 1;
    if page_bound == 1 {
        // No unit to place: the head alone is page 0 whatever its size.
        return Ok(true);
    }
    let mut cost = encoded_bytes(&envelope(revision, page_bound, page_bound))?
        + ",\"turn\":".len()
        + ",\"pagedMembers\":".len()
        + encoded_bytes(&lengths)?
        + encoded_bytes_of_turn;
    let mut removed = if projection.len() > members.len() {
        members.len()
    } else {
        members.len().saturating_sub(1)
    };
    let mut slices = 0_usize;
    for (name, length, text) in members {
        // `"name":` plus the member's brackets or quotes leave the head.
        removed += name.len() + 3 + 2;
        if length > 0 {
            let mut body = Map::new();
            body.insert("start".to_owned(), Value::from(0));
            body.insert(
                if text { "text" } else { "items" }.to_owned(),
                if text {
                    Value::String(String::new())
                } else {
                    Value::Array(Vec::new())
                },
            );
            let mut slice = Map::new();
            slice.insert(name.to_owned(), Value::Object(body));
            cost += encoded_bytes(&slice)? - 2 + usize::from(slices > 0);
            slices += 1;
        }
    }
    Ok(cost.checked_sub(removed).is_some_and(|cost| cost <= limit))
}

/// The public Turn as `turn.context.read` delivers it (docs/STORAGE.md, "Paged
/// Turn storage", Decision 6): its page-0 head and `true` when its pages are
/// more than one, otherwise the unchanged Turn and `false`.
pub(crate) fn paged_head(mut turn: Value) -> io::Result<(Value, bool)> {
    let revision = turn
        .get("projectionRevision")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("turn projection revision is malformed"))?;
    let members = split_head(&mut turn);
    let lengths: Map<String, Value> = members
        .iter()
        .map(|(name, member)| ((*name).to_owned(), Value::from(member.length())))
        .collect();
    if plan(revision, &turn, &lengths, &members)?.len() > 1 {
        return Ok((turn, true));
    }
    if let Some(projection) = turn.get_mut("projection").and_then(Value::as_object_mut) {
        for (name, member) in members {
            let value = match member {
                Member::Sequence(items) => Value::Array(items),
                Member::Text(text) => Value::String(text),
            };
            projection.insert(name.to_owned(), value);
        }
    }
    Ok((turn, false))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn escaped_bytes_match_the_serde_json_string_encoder() {
        let sample = "Résumé \"quoted\" \\ path\n\t\r\u{08}\u{0c}\u{01}\u{1f}\u{7f}\u{2028} 🚀";
        let expected = serde_json::to_vec(sample).unwrap().len() - 2;
        assert_eq!(sample.chars().map(escaped_bytes).sum::<usize>(), expected);
    }

    #[test]
    fn text_slices_follow_code_points() {
        let text = "a🚀é\"z";
        assert_eq!(text_slice(text, 0, 2), "a🚀");
        assert_eq!(text_slice(text, 2, 5), "é\"z");
        assert_eq!(text_slice(text, 4, 5), "z");
    }

    /// The exact single-page test agrees with the planner on both sides of
    /// every Turn's boundary, across member shapes, escapes and empty members.
    #[test]
    fn one_page_from_the_encoded_turn_matches_the_planner_at_every_boundary() {
        let mut state = 0x2545_f491_4f6c_dd1d_u64;
        let mut next = move |bound: u64| {
            state ^= state << 13;
            state ^= state >> 7;
            state ^= state << 17;
            state % bound
        };
        let alphabet = [
            "a", "\"", "\\", "\n", "\u{01}", "é", "🚀", "\u{2028}", " ", "z",
        ];
        for case in 0..400 {
            let text = |next: &mut dyn FnMut(u64) -> u64, units: u64| -> String {
                (0..next(units))
                    .map(|_| alphabet[next(alphabet.len() as u64) as usize])
                    .collect()
            };
            let mut projection = Map::new();
            for name in SEQUENCE_MEMBERS {
                match next(4) {
                    0 => {}
                    1 => {
                        projection.insert(name.to_owned(), json!([]));
                    }
                    2 => {
                        projection.insert(name.to_owned(), Value::Null);
                    }
                    _ => {
                        let items: Vec<Value> = (0..1 + next(5))
                            .map(
                                |round| json!({"round": round, "toolContent": text(&mut next, 60)}),
                            )
                            .collect();
                        projection.insert(name.to_owned(), Value::Array(items));
                    }
                }
            }
            for name in TEXT_MEMBERS {
                match next(3) {
                    0 => {}
                    1 => {
                        projection.insert(name.to_owned(), json!(""));
                    }
                    _ => {
                        projection.insert(name.to_owned(), Value::String(text(&mut next, 200)));
                    }
                }
            }
            if next(2) == 0 {
                projection.insert("status".to_owned(), json!("running"));
            }
            let mut turn =
                json!({"turnId": format!("t{case}"), "projectionRevision": next(1 << 40)});
            if next(8) != 0 {
                turn["projection"] = Value::Object(projection);
            }
            let encoded = serde_json::to_vec(&turn).unwrap().len();
            let mut head = turn.clone();
            let members = split_head(&mut head);
            let lengths: Map<String, Value> = members
                .iter()
                .map(|(name, member)| ((*name).to_owned(), Value::from(member.length())))
                .collect();
            let revision = turn["projectionRevision"].as_u64().unwrap();
            let pages = |limit| {
                plan_within(revision, &head, &lengths, &members, limit)
                    .unwrap()
                    .len()
            };
            // The smallest payload that keeps every member on page 0.
            let (mut low, mut high) = (0, encoded + 4096);
            assert_eq!(pages(high), 1, "case {case}");
            while low < high {
                let middle = (low + high) / 2;
                if pages(middle) == 1 {
                    high = middle;
                } else {
                    low = middle + 1;
                }
            }
            for limit in [
                low.saturating_sub(2),
                low.saturating_sub(1),
                low,
                low + 1,
                encoded + 4096,
            ] {
                assert_eq!(
                    fits_one_page_within(&turn, encoded, limit).unwrap(),
                    pages(limit) == 1,
                    "case {case} limit {limit} boundary {low}: {turn}"
                );
            }
        }
    }

    #[test]
    fn pages_partition_members_within_the_frame_payload() {
        let unit = "Résumé \"quoted\" \\ path\n\t\u{01} 🚀 ".repeat(200_000);
        let item = json!({"toolContent": "x".repeat(1_000_000)});
        let mut turn = json!({
            "turnId": "t",
            "projectionRevision": 7,
            "projection": {
                "content": unit,
                "toolRounds": vec![item; 20],
                "status": "done",
            },
        });
        let members = split_head(&mut turn);
        let lengths: Map<String, Value> = members
            .iter()
            .map(|(name, member)| ((*name).to_owned(), Value::from(member.length())))
            .collect();
        let pages = plan(7, &turn, &lengths, &members).unwrap();
        assert!(pages.len() >= 4);
        let mut covered: BTreeMap<&str, usize> = BTreeMap::new();
        for page in &pages {
            for slice in page {
                let next = covered.entry(members[slice.member].0).or_default();
                assert_eq!(slice.start, *next);
                assert!(slice.end > slice.start);
                *next = slice.end;
            }
        }
        assert_eq!(covered["toolRounds"], 20);
        assert_eq!(
            covered["content"],
            lengths["content"].as_u64().unwrap() as usize
        );
        assert_eq!(turn["projection"], json!({"status": "done"}));
    }
}

//! Immutable binary page tree shared by both persistence engines. Updates copy
//! one leaf and its ancestor path; unchanged subtrees share Arc allocations.
use serde::{Deserialize, Serialize};
use std::{
    collections::{BTreeMap, BTreeSet},
    io,
    sync::Arc,
};
use tofu_db::block::BlockId;

pub type Hash = [u8; 32];
pub type Pages = BTreeMap<Hash, Vec<u8>>;

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
enum EncodedPage {
    Leaf {
        slot: u32,
        value: i64,
        evidence: Vec<u8>,
    },
    Branch {
        start: u32,
        end: u32,
        left: Hash,
        right: Hash,
    },
}

#[derive(Debug)]
pub struct Page {
    pub hash: Hash,
    pub start: u32,
    pub end: u32,
    body: Body,
}

#[derive(Debug)]
enum Body {
    Leaf { value: i64, evidence: Arc<Vec<u8>> },
    Branch(Arc<Page>, Arc<Page>),
}

fn invalid(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

impl Page {
    fn leaf(
        slot: u32,
        value: i64,
        evidence: Arc<Vec<u8>>,
        pages: &mut Pages,
    ) -> io::Result<Arc<Self>> {
        let bytes = serde_json::to_vec(&EncodedPage::Leaf {
            slot,
            value,
            evidence: evidence.as_ref().clone(),
        })?;
        let hash = BlockId::for_payload(&bytes).0;
        pages.insert(hash, bytes);
        Ok(Arc::new(Self {
            hash,
            start: slot,
            end: slot + 1,
            body: Body::Leaf { value, evidence },
        }))
    }

    fn branch(left: Arc<Self>, right: Arc<Self>, pages: &mut Pages) -> io::Result<Arc<Self>> {
        if left.end != right.start {
            return Err(invalid("noncontiguous page children"));
        }
        let (start, end) = (left.start, right.end);
        let bytes = serde_json::to_vec(&EncodedPage::Branch {
            start,
            end,
            left: left.hash,
            right: right.hash,
        })?;
        let hash = BlockId::for_payload(&bytes).0;
        pages.insert(hash, bytes);
        Ok(Arc::new(Self {
            hash,
            start,
            end,
            body: Body::Branch(left, right),
        }))
    }

    pub fn build(
        values: &[i64],
        evidence_bytes: usize,
        salt: Hash,
        pages: &mut Pages,
    ) -> io::Result<Arc<Self>> {
        fn build_at(
            values: &[i64],
            start: u32,
            evidence_bytes: usize,
            salt: Hash,
            pages: &mut Pages,
        ) -> io::Result<Arc<Page>> {
            if values.len() == 1 {
                // Stable nonconstant evidence. Both engines receive identical bytes.
                let evidence = (0..evidence_bytes)
                    .map(|i| {
                        ((i.wrapping_mul(73) + start as usize * 31 + salt[i % 32] as usize) % 251)
                            as u8
                    })
                    .collect();
                return Page::leaf(start, values[0], Arc::new(evidence), pages);
            }
            let mid = values.len() / 2;
            let left = build_at(&values[..mid], start, evidence_bytes, salt, pages)?;
            let right = build_at(
                &values[mid..],
                start + mid as u32,
                evidence_bytes,
                salt,
                pages,
            )?;
            Page::branch(left, right, pages)
        }
        if values.is_empty() || !values.len().is_power_of_two() {
            return Err(invalid("slot count must be a nonzero power of two"));
        }
        build_at(values, 0, evidence_bytes, salt, pages)
    }

    pub fn value(&self, slot: u32) -> io::Result<i64> {
        if slot < self.start || slot >= self.end {
            return Err(invalid("slot outside domain"));
        }
        match &self.body {
            Body::Leaf { value, .. } => Ok(*value),
            Body::Branch(left, right) => {
                if slot < left.end {
                    left.value(slot)
                } else {
                    right.value(slot)
                }
            }
        }
    }

    pub fn add(&self, slot: u32, delta: i64, pages: &mut Pages) -> io::Result<Arc<Self>> {
        if slot < self.start || slot >= self.end {
            return Err(invalid("slot outside domain"));
        }
        match &self.body {
            Body::Leaf { value, evidence } => Self::leaf(
                slot,
                value
                    .checked_add(delta)
                    .ok_or_else(|| invalid("account overflow"))?,
                evidence.clone(),
                pages,
            ),
            Body::Branch(left, right) => {
                if slot < left.end {
                    Self::branch(left.add(slot, delta, pages)?, right.clone(), pages)
                } else {
                    Self::branch(left.clone(), right.add(slot, delta, pages)?, pages)
                }
            }
        }
    }

    pub fn values(&self, out: &mut Vec<i64>) {
        match &self.body {
            Body::Leaf { value, .. } => out.push(*value),
            Body::Branch(left, right) => {
                left.values(out);
                right.values(out);
            }
        }
    }

    /// Intermediate paths from several writes in one request are private
    /// garbage. Persist only final paths; old durable children need no restage.
    pub fn collect_new(&self, pages: &Pages, keep: &mut BTreeSet<Hash>) {
        if !pages.contains_key(&self.hash) || !keep.insert(self.hash) {
            return;
        }
        if let Body::Branch(left, right) = &self.body {
            left.collect_new(pages, keep);
            right.collect_new(pages, keep);
        }
    }

    pub fn load(
        hash: Hash,
        start: u32,
        end: u32,
        read: &mut impl FnMut(Hash) -> io::Result<Vec<u8>>,
        memo: &mut BTreeMap<Hash, Arc<Page>>,
    ) -> io::Result<Arc<Self>> {
        if end <= start || !(end - start).is_power_of_two() {
            return Err(invalid("invalid page interval"));
        }
        if let Some(page) = memo.get(&hash) {
            if page.start != start || page.end != end {
                return Err(invalid("shared page interval mismatch"));
            }
            return Ok(page.clone());
        }
        let bytes = read(hash)?;
        if bytes.len() > super::MAX_PAGE_BYTES || BlockId::for_payload(&bytes).0 != hash {
            return Err(invalid("page hash or byte limit failed"));
        }
        let body = match serde_json::from_slice::<EncodedPage>(&bytes)? {
            EncodedPage::Leaf {
                slot,
                value,
                evidence,
            } if end == start + 1
                && slot == start
                && evidence.len() <= super::MAX_EVIDENCE_BYTES =>
            {
                Body::Leaf {
                    value,
                    evidence: Arc::new(evidence),
                }
            }
            EncodedPage::Branch {
                start: s,
                end: e,
                left,
                right,
            } if s == start && e == end && end - start > 1 => {
                let mid = start + (end - start) / 2;
                Body::Branch(
                    Self::load(left, start, mid, read, memo)?,
                    Self::load(right, mid, end, read, memo)?,
                )
            }
            _ => return Err(invalid("page shape differs from root geometry")),
        };
        let page = Arc::new(Self {
            hash,
            start,
            end,
            body,
        });
        memo.insert(hash, page.clone());
        Ok(page)
    }
}

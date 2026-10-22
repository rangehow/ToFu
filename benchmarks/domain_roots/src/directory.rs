//! Persistent balanced directory of domain roots. The key set is fixed by
//! genesis; publication copies O(log domains) metadata, never whole state.
use crate::{DomainKey, DomainState};
use std::{io, sync::Arc};

#[derive(Debug)]
pub struct Directory {
    key: DomainKey,
    state: DomainState,
    left: Option<Arc<Directory>>,
    right: Option<Arc<Directory>>,
}
impl Directory {
    pub fn build(rows: &[(DomainKey, DomainState)]) -> Option<Arc<Self>> {
        if rows.is_empty() {
            return None;
        }
        let middle = rows.len() / 2;
        Some(Arc::new(Self {
            key: rows[middle].0,
            state: rows[middle].1.clone(),
            left: Self::build(&rows[..middle]),
            right: Self::build(&rows[middle + 1..]),
        }))
    }
    pub fn get(&self, key: DomainKey) -> Option<&DomainState> {
        match key.cmp(&self.key) {
            std::cmp::Ordering::Equal => Some(&self.state),
            std::cmp::Ordering::Less => self.left.as_ref()?.get(key),
            std::cmp::Ordering::Greater => self.right.as_ref()?.get(key),
        }
    }
    pub fn replace(
        &self,
        key: DomainKey,
        state: DomainState,
        count: &mut u64,
    ) -> io::Result<Arc<Self>> {
        *count += 1;
        let (mut left, mut right, mut value) =
            (self.left.clone(), self.right.clone(), self.state.clone());
        match key.cmp(&self.key) {
            std::cmp::Ordering::Equal => value = state,
            std::cmp::Ordering::Less => {
                left = Some(
                    left.ok_or_else(|| io::Error::other("unknown domain"))?
                        .replace(key, state, count)?,
                )
            }
            std::cmp::Ordering::Greater => {
                right = Some(
                    right
                        .ok_or_else(|| io::Error::other("unknown domain"))?
                        .replace(key, state, count)?,
                )
            }
        }
        Ok(Arc::new(Self {
            key: self.key,
            state: value,
            left,
            right,
        }))
    }
    pub fn rows(&self, out: &mut Vec<(DomainKey, DomainState)>) {
        if let Some(left) = &self.left {
            left.rows(out);
        }
        out.push((self.key, self.state.clone()));
        if let Some(right) = &self.right {
            right.rows(out);
        }
    }
}

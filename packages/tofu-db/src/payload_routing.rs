//! Reconstructible hash-prefix routing for one installed payload catalog.
//!
//! BlockStore publishes this with the manifest under the same lock boundary.
//! Verified segment indexes provide prefixes; missing/over-budget indexes stay
//! in an explicit fallback set. No negative routing result can hide a segment
//! that was not indexed. Retirement drops all routes from the old catalog.
//! An over-budget verified segment keeps only its immutable prefix count, so a
//! later catalog install re-reads its index only when that route can fit.
//! A catalog publication moves the installed routes in place (`advance`), so
//! its cost follows the segments it changes, not the catalog it keeps.
use crate::block::BlockId;
use crate::payload_manifest::{PayloadManifest, PayloadSegmentReference};
use crate::payload_segment::{PayloadSegment, PayloadSegmentStore};
use std::collections::{BTreeSet, HashMap};
use std::sync::Arc;
use uuid::Uuid;

/// Budget charged per routed prefix.
const PREFIX_ROUTE_BYTES: usize = 256;

#[derive(Default)]
pub(crate) struct PayloadRouting {
    indexed: HashMap<Uuid, (PayloadSegmentReference, Arc<Vec<u16>>)>,
    prefixes: HashMap<u16, Vec<PayloadSegmentReference>>,
    fallback: Vec<PayloadSegmentReference>,
    /// Distinct-prefix counts of verified fallback segments, at most one per
    /// catalogued segment.
    fallback_prefix_counts: HashMap<Uuid, (PayloadSegmentReference, usize)>,
    /// Budget the indexed routes hold.
    routed_bytes: usize,
}

impl PayloadRouting {
    /// Reuse an index the recovery pass already verified. Keep only prefixes
    /// admitted by the same routing budget; all others remain explicit fallback.
    pub(crate) fn remember_verified(
        &mut self,
        reference: PayloadSegmentReference,
        segment: &PayloadSegment,
        remaining: &mut usize,
    ) {
        if *remaining < PREFIX_ROUTE_BYTES {
            return;
        }
        let prefixes: BTreeSet<u16> = segment
            .block_ids()
            .map(|id| u16::from_be_bytes([id.0[0], id.0[1]]))
            .collect();
        let cost = prefixes.len().saturating_mul(PREFIX_ROUTE_BYTES);
        if cost > *remaining {
            self.fallback_prefix_counts
                .insert(reference.segment_id, (reference, prefixes.len()));
            return;
        }
        *remaining -= cost;
        self.indexed.insert(
            reference.segment_id,
            (reference, Arc::new(prefixes.into_iter().collect())),
        );
    }

    /// Routes for `manifest`, reusing what `previous` (the installed routes)
    /// or `verified` (indexes this publication already read) know about each
    /// segment before reading any index.
    pub(crate) fn build(
        manifest: &PayloadManifest,
        store: Option<&PayloadSegmentStore>,
        previous: &Self,
        verified: &Self,
        budget: usize,
        read_indexes: bool,
    ) -> Self {
        let mut routing = Self::default();
        let mut remaining = budget;
        for reference in &manifest.segments {
            let known_count = [previous, verified].into_iter().find_map(|known| {
                known
                    .fallback_prefix_counts
                    .get(&reference.segment_id)
                    .filter(|(old, _)| old == reference)
                    .map(|(_, count)| *count)
            });
            let prefixes = [previous, verified]
                .into_iter()
                .find_map(|known| {
                    known
                        .indexed
                        .get(&reference.segment_id)
                        .filter(|(old, _)| old == reference)
                        .map(|(_, prefixes)| Arc::clone(prefixes))
                })
                .or_else(|| {
                    if !read_indexes || remaining < PREFIX_ROUTE_BYTES {
                        return None;
                    }
                    if known_count
                        .is_some_and(|count| count.saturating_mul(PREFIX_ROUTE_BYTES) > remaining)
                    {
                        return None;
                    }
                    let segment = store?.open_segment(reference.segment_id).ok()?;
                    if PayloadSegmentReference::new(
                        segment.metadata(),
                        segment.first_block_id(),
                        segment.last_block_id(),
                    )
                    .ok()
                    .as_ref()
                        != Some(reference)
                    {
                        return None;
                    }
                    let prefixes: BTreeSet<u16> = segment
                        .block_ids()
                        .map(|id| u16::from_be_bytes([id.0[0], id.0[1]]))
                        .collect();
                    Some(Arc::new(prefixes.into_iter().collect()))
                });
            let prefix_count = prefixes
                .as_ref()
                .map(|prefixes| prefixes.len())
                .or(known_count);
            let Some(prefixes) = prefixes
                .filter(|prefixes| prefixes.len().saturating_mul(PREFIX_ROUTE_BYTES) <= remaining)
            else {
                routing.fallback.push(*reference);
                if let Some(count) = prefix_count {
                    routing
                        .fallback_prefix_counts
                        .insert(reference.segment_id, (*reference, count));
                }
                continue;
            };
            remaining -= prefixes.len() * PREFIX_ROUTE_BYTES;
            routing.route(*reference, prefixes);
        }
        routing
    }

    /// Segments of `manifest` that neither these routes nor `verified` route
    /// or count, while the budget these routes leave has room for one prefix:
    /// the only indexes `advance` would want an install to read.
    pub(crate) fn unrouted(
        &self,
        manifest: &PayloadManifest,
        verified: &Self,
        budget: usize,
    ) -> Vec<PayloadSegmentReference> {
        if budget.saturating_sub(self.routed_bytes) < PREFIX_ROUTE_BYTES {
            return Vec::new();
        }
        let fallback: HashMap<Uuid, &PayloadSegmentReference> = self
            .fallback
            .iter()
            .map(|reference| (reference.segment_id, reference))
            .collect();
        manifest
            .segments
            .iter()
            .filter(|reference| {
                !self
                    .indexed
                    .get(&reference.segment_id)
                    .is_some_and(|(old, _)| old == *reference)
                    && fallback.get(&reference.segment_id) != Some(reference)
                    && !verified.indexed.contains_key(&reference.segment_id)
                    && !verified
                        .fallback_prefix_counts
                        .contains_key(&reference.segment_id)
            })
            .copied()
            .collect()
    }

    /// Moves these routes, built for an earlier catalog, to `manifest` in
    /// place: every route of a segment it no longer names leaves, each segment
    /// it adds is routed from `verified` (or left fallback), and every kept
    /// route stays exactly as it was, so the work follows the segments that
    /// changed. A rebuild of the routes of a 4,096-segment catalog took about
    /// 250 ms of each live spill's install. Budget a dropped route frees is
    /// available to later additions; an addition that does not fit stays
    /// fallback with its prefix count when one is known.
    pub(crate) fn advance(&mut self, manifest: &PayloadManifest, verified: &Self, budget: usize) {
        let named: HashMap<Uuid, &PayloadSegmentReference> = manifest
            .segments
            .iter()
            .map(|reference| (reference.segment_id, reference))
            .collect();
        let current = |reference: &PayloadSegmentReference| {
            named.get(&reference.segment_id) == Some(&reference)
        };
        let dropped: Vec<Uuid> = self
            .indexed
            .iter()
            .filter(|(_, (reference, _))| !current(reference))
            .map(|(segment_id, _)| *segment_id)
            .collect();
        for segment_id in dropped {
            self.unroute(segment_id);
        }
        self.fallback.retain(|reference| current(reference));
        self.fallback_prefix_counts
            .retain(|_, (reference, _)| current(reference));
        let mut fallback: HashMap<Uuid, PayloadSegmentReference> = self
            .fallback
            .iter()
            .map(|reference| (reference.segment_id, *reference))
            .collect();
        for reference in &manifest.segments {
            if self.indexed.contains_key(&reference.segment_id)
                || fallback.contains_key(&reference.segment_id)
            {
                continue;
            }
            let prefixes = verified
                .indexed
                .get(&reference.segment_id)
                .filter(|(old, _)| old == reference)
                .map(|(_, prefixes)| Arc::clone(prefixes));
            let prefix_count = prefixes
                .as_ref()
                .map(|prefixes| prefixes.len())
                .or_else(|| {
                    verified
                        .fallback_prefix_counts
                        .get(&reference.segment_id)
                        .filter(|(old, _)| old == reference)
                        .map(|(_, count)| *count)
                });
            let remaining = budget.saturating_sub(self.routed_bytes);
            match prefixes
                .filter(|prefixes| prefixes.len().saturating_mul(PREFIX_ROUTE_BYTES) <= remaining)
            {
                Some(prefixes) => self.route(*reference, prefixes),
                None => {
                    self.fallback.push(*reference);
                    fallback.insert(reference.segment_id, *reference);
                    if let Some(count) = prefix_count {
                        self.fallback_prefix_counts
                            .insert(reference.segment_id, (*reference, count));
                    }
                }
            }
        }
    }

    fn route(&mut self, reference: PayloadSegmentReference, prefixes: Arc<Vec<u16>>) {
        #[cfg(test)]
        route_counters::note_prefix_route_writes(prefixes.len());
        self.routed_bytes += prefixes.len() * PREFIX_ROUTE_BYTES;
        for prefix in prefixes.iter() {
            self.prefixes.entry(*prefix).or_default().push(reference);
        }
        self.indexed
            .insert(reference.segment_id, (reference, prefixes));
    }

    fn unroute(&mut self, segment_id: Uuid) {
        let Some((_, prefixes)) = self.indexed.remove(&segment_id) else {
            return;
        };
        #[cfg(test)]
        route_counters::note_prefix_route_writes(prefixes.len());
        self.routed_bytes -= prefixes.len() * PREFIX_ROUTE_BYTES;
        for prefix in prefixes.iter() {
            if let Some(routed) = self.prefixes.get_mut(prefix) {
                routed.retain(|reference| reference.segment_id != segment_id);
                if routed.is_empty() {
                    self.prefixes.remove(prefix);
                }
            }
        }
    }

    pub(crate) fn candidates(&self, id: BlockId) -> Vec<PayloadSegmentReference> {
        let prefix = u16::from_be_bytes([id.0[0], id.0[1]]);
        let mut result: Vec<_> = self
            .prefixes
            .get(&prefix)
            .into_iter()
            .flatten()
            .chain(self.fallback.iter())
            .filter(|reference| reference.may_contain(id))
            .copied()
            .collect();
        result.sort_by_key(|reference| reference.segment_id);
        result
    }
}

#[cfg(test)]
pub(crate) mod route_counters {
    use std::cell::Cell;

    thread_local! {
        static PREFIX_ROUTE_WRITES: Cell<u64> = const { Cell::new(0) };
    }

    pub(crate) fn note_prefix_route_writes(writes: usize) {
        PREFIX_ROUTE_WRITES.with(|count| count.set(count.get() + writes as u64));
    }

    /// Prefix routes added or removed on this thread since the last call.
    pub(crate) fn take_prefix_route_writes() -> u64 {
        PREFIX_ROUTE_WRITES.with(|count| count.replace(0))
    }
}

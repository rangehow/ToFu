"""Experimental process cache reservations with explicit execution ownership.

Reserve before constructing a cache; release only after scope quiescence.
No module-global pool, durable task registry or application default is added.
"""
from threading import RLock
from owned_scope import OwnedIndexScope


class CacheQuotaExceeded(RuntimeError):
    pass


class CacheReservation:
    def __init__(self, pool, owner_id, execution_id, maximum_bytes):
        self._pool = pool
        self._owner_id = owner_id
        self._execution_id = execution_id
        self._maximum_bytes = maximum_bytes
        self._released = False

    @property
    def maximum_bytes(self):
        return self._maximum_bytes

    def release(self, owner_id):
        self._pool._release(self, owner_id)


class ReservedIndexScope:
    def __init__(self, scope, reservation):
        self._scope = scope
        self._reservation = reservation

    def scan(self, owner_id, **options):
        return self._scope.scan(owner_id, **options)

    def retained_bytes(self, owner_id):
        return self._scope.retained_bytes(owner_id)

    def close(self, owner_id):
        self._scope.close(owner_id)
        self._reservation.release(owner_id)


class CacheReservationPool:
    def __init__(self, plan):
        for value in (plan.total_bytes, plan.per_scope_bytes, plan.maximum_live_scopes):
            if type(value) is not int or value < 0:
                raise ValueError('cache quota plan requires nonnegative integer bounds')
        if plan.maximum_live_scopes < 1 or plan.per_scope_bytes * plan.maximum_live_scopes > plan.total_bytes:
            raise ValueError('cache quota plan exceeds its total reservation budget')
        self._plan = plan
        self._lock = RLock()
        self._reservations = {}
        self._reserved_bytes = 0
        self._accepting = True

    def reserve(self, owner_id, execution_id):
        if type(owner_id) is not int or owner_id < 1:
            raise ValueError('positive integer owner required')
        if type(execution_id) is not str or not execution_id.strip():
            raise ValueError('explicit execution identity required')
        with self._lock:
            if not self._accepting:
                raise RuntimeError('cache reservation admission stopped')
            if execution_id in self._reservations:
                raise ValueError('execution already has a cache reservation')
            size = self._plan.per_scope_bytes
            if (size == 0 or len(self._reservations) >= self._plan.maximum_live_scopes
                    or self._reserved_bytes + size > self._plan.total_bytes):
                raise CacheQuotaExceeded('cache reservation quota exhausted')
            reservation = CacheReservation(self, owner_id, execution_id, size)
            self._reservations[execution_id] = reservation
            self._reserved_bytes += size
            return reservation

    def _release(self, reservation, owner_id):
        if type(owner_id) is not int or owner_id != reservation._owner_id:
            raise PermissionError('foreign cache reservation owner')
        with self._lock:
            if reservation._released:
                return
            if self._reservations.get(reservation._execution_id) is not reservation:
                raise RuntimeError('cache reservation generation mismatch')
            del self._reservations[reservation._execution_id]
            self._reserved_bytes -= reservation.maximum_bytes
            reservation._released = True

    def create_scope(self, owner_id, execution_id, root, *, epoch_source, scope_factory=OwnedIndexScope):
        reservation = self.reserve(owner_id, execution_id)
        try:
            scope = scope_factory(owner_id, root, reservation.maximum_bytes, epoch_source=epoch_source)
            return ReservedIndexScope(scope, reservation)
        except BaseException:
            reservation.release(owner_id)
            raise

    def stop_admission(self):
        """Stop new leases; active owners still must close their own scopes."""
        with self._lock:
            self._accepting = False

    def snapshot(self):
        with self._lock:
            return {'active_scopes': len(self._reservations), 'reserved_bytes': self._reserved_bytes,
                    'total_bytes': self._plan.total_bytes, 'accepting': self._accepting}

"""Experiment-owned lazy decoder initialization; no production registration.

The process-owned lock only publishes an instance's initialization state. Native
loading uses its separate reentrant lock, so independent services do not serialize
loading. Ineligible requests allocate no lock/state. No inputs/results are retained.
"""
import threading
from lib.llm import conversion
from decoder import eligible_native_input, NativeArgumentDecoder
_state_creation_lock = threading.Lock()

class _InitializationState:
    __slots__ = ('lock', 'decoder', 'initializing', 'activations')
    def __init__(self):
        self.lock = threading.RLock()
        self.decoder = None
        self.initializing = False
        self.activations = 0

class LazyArgumentDecoder:
    __slots__ = ('_factory', '_minimum_characters', '_state')
    def __init__(self, factory, *, minimum_characters=512):
        self._factory = factory
        self._minimum_characters = minimum_characters
        self._state = None

    @property
    def activations(self):
        state = self._state
        return state.activations if state is not None else 0

    @property
    def _decoder(self):
        state = self._state
        return state.decoder if state is not None else None

    @property
    def _initializing(self):
        state = self._state
        return state.initializing if state is not None else False

    def __call__(self, value):
        if not eligible_native_input(value, self._minimum_characters):
            return conversion.object_arguments(value)
        state = self._state
        if state is None:
            with _state_creation_lock:
                state = self._state
                if state is None:
                    state = _InitializationState()
                    self._state = state
        decoder = state.decoder
        if decoder is None:
            with state.lock:
                decoder = state.decoder
                if decoder is None:
                    if state.initializing:
                        return conversion.object_arguments(value)
                    state.initializing = True
                    try:
                        decoder = self._factory()
                        if not callable(decoder):
                            raise TypeError('native decoder factory must return a callable')
                        if (type(decoder) is NativeArgumentDecoder
                                and decoder._minimum_characters == self._minimum_characters):
                            decoder = decoder._decode_eligible
                        state.decoder = decoder
                        state.activations += 1
                    finally:
                        state.initializing = False
        return decoder(value)

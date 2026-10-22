"""Lazy actor/project-scoped Hope MCP transport port.

Entry HopeMCPPort accepts trusted actor-aware bridge/configuration factories.
No host module dependency, ambient login, process or credential access at import.
Host owns close(), after dependent operations settle. Provider/JobService retain
approval and durable at-most-once authority; this port never retries mutations.
"""
from threading import RLock
from tofu_environments.contracts import EnvironmentError

_WRITES = {'mcp__hope__submit_job', 'mcp__hope__stop_job'}
_READS = {'mcp__hope__get_queue_resource', 'mcp__hope__get_job', 'mcp__hope__get_status',
          'mcp__hope__get_job_attempts', 'mcp__hope__diagnose_job', 'mcp__hope__get_log_file', 'mcp__hope__list_log_files'}

class HopeMCPPort:
    def __init__(self, actor, bridge_factory, configuration_loader, *, timeout_seconds=60):
        if not callable(bridge_factory) or not callable(configuration_loader):
            raise EnvironmentError('hope_unconfigured', 'Trusted actor-aware MCP factories required')
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 300:
            raise EnvironmentError('hope_timeout_invalid', 'Explicit bounded MCP deadline required')
        self.actor, self.bridge_factory, self.configuration_loader = actor, bridge_factory, configuration_loader
        self.timeout_seconds = timeout_seconds
        self.lock, self.bridge, self.closed = RLock(), None, False

    def __call__(self, actor, tool, arguments, *, retry_on_disconnect=False):
        with self.lock:
            if actor != self.actor or self.closed:
                raise EnvironmentError('hope_scope_invalid', 'MCP port belongs to one active actor/project scope')
            if tool not in _WRITES | _READS or not isinstance(arguments, dict):
                raise EnvironmentError('hope_tool_invalid', 'Only declared Hope job transport calls allowed')
            if tool in _WRITES and retry_on_disconnect:
                raise EnvironmentError('hope_replay_forbidden', 'Mutating Hope calls cannot reconnect and replay')
            if self.bridge is None:
                # Both factories receive the full identity; selecting credentials by
                # ambient account or guessed project name is never a fallback.
                bridge = self.bridge_factory(actor)
                try:
                    configuration = self.configuration_loader(actor)
                    if not isinstance(configuration, dict) or not configuration:
                        raise EnvironmentError('hope_unconfigured', 'Owned Hope configuration unavailable')
                    bridge.connect_server('hope', configuration)
                except Exception as error:
                    # A partially started bridge belongs to this failed connection.
                    bridge.disconnect_all()
                    raise EnvironmentError('hope_connection_failed', 'Owned Hope MCP connection failed') from error
                self.bridge = bridge
            try:
                return self.bridge.call_tool(tool, arguments, timeout_override=self.timeout_seconds,
                                             retry_on_disconnect=retry_on_disconnect)
            except Exception as error:
                raise EnvironmentError('hope_transport_unknown', 'Owned Hope MCP response unavailable',
                                       uncertain=tool in _WRITES) from error

    def close(self):
        """Serializes with active calls; no disconnect while a write is in flight."""
        with self.lock:
            self.closed = True
            if self.bridge is not None:
                self.bridge.disconnect_all()
                self.bridge = None

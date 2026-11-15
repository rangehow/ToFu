/** swarm endpoint facade. Owns wire paths and projections; transport/retry policy arrives through explicit ports. */
import type { EndpointPorts, EndpointOptions } from './contracts';
export function createSwarmEndpoints(ports: EndpointPorts) {
  const { get, post } = ports;
  const swarm = {
    /** `run` scopes the answer to one run; `agents` names its launch roster,
     * answered from durable run results once that run's session has ended. */
    status: (taskId: string, opts?: EndpointOptions & { run?: string; agents?: readonly string[] }) => {
      const { run, agents, ...request } = opts || {};
      const query = new URLSearchParams();
      if (run) {
        query.set('run', run);
        for (const agent of agents || []) query.append('agent', agent);
      }
      return get(`/api/v4/swarm/status/${encodeURIComponent(taskId)}${run ? `?${query}` : ''}`,
          Object.assign({ onError: 'null' }, request));
    },
    abort:  (taskId: string) =>
      post(`/api/v4/swarm/abort/${encodeURIComponent(taskId)}`, {}, { onError: 'null' }),
    stopAgent: (swarmId: string, agentId: string) =>
      post(`/api/v4/swarm/runs/${encodeURIComponent(swarmId)}/agents/${encodeURIComponent(agentId)}/stop`, {}),
  };
  return swarm;
}

/** Typed ports over the existing authenticated task/research API; no transport. */
import type { JsonObject } from "../state/stream";
export interface TaskApi {
  start(kind: string, payload: JsonObject): Promise<JsonObject>;
  events?(taskId: string, cursor: number): Promise<JsonObject>;
  get(taskId: string): Promise<JsonObject>;
  abort(taskId: string): Promise<unknown>;
  list?(kind: string, status?: string): Promise<JsonObject>;
}

export interface ResearchApi {
  record?(taskId: string): Promise<JsonObject>;
  records?(limit: number): Promise<JsonObject>;
  resumeRecord?(taskId: string): Promise<JsonObject>;
  lookup(direction: string, lang: string): Promise<JsonObject>;
}


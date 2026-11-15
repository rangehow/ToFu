/** Recorded live shapes for the office specs (GET reads of 2026-11-13, text trimmed, identifiers kept):
 * a tofu team page row while its attempt runs and after, an assignment row before and after its Worker
 * reported (XUECHENG-DELETE-1), the lead's report state, a delivery report, an overdue commitment, an
 * ask_human round awaiting the owner and the push hint that wakes a read. Test-only data. */
import type { ProjectDispatchHistoryRow } from '../../../api/project-dispatch.generated';
import type {
  CoordinatorConversationPage, CoordinatorProgressState, CoordinatorReportState,
} from '../../../api/project-coordination.generated';
import type { QuestionTurn } from '../attention';
import type { TeamInput } from '../model';

export const PROJECT = '/path/to/your/project';
export const LEAD = 'coordinator-1a390889aa954da2b18551d1b4a32625';
export const REPORTER = 'worker-15082de381a8dafbf673dde3';
export const RUNNER = 'worker-dfaa412580f8e85ff5bf48d6';

export const memberRunning = {
  busy: true as const, conversationId: RUNNER, coordinatorId: LEAD, createdAt: 1794532745898,
  folderId: 'f_7a71cbe201ffc7567f657d52', turnCount: 2, updatedAt: 1794533799658,
  title: '[SCALE-REPO-2] Remove the per-project repo lock bottleneck and per-Worker lock/ref growth (successor of the drain-stopped SCALE-REPO-1)',
};
const reporterTitle = "[XUECHENG-DELETE-1] Delete exactly one junk 学城 doc 「ignore」 (contentId 2798016815) on the owner's approval, and verify";
export const reporterBusy = {
  busy: true as const, conversationId: REPORTER, coordinatorId: LEAD, createdAt: 1794531479778,
  folderId: 'f_7a71cbe201ffc7567f657d52', turnCount: 2, updatedAt: 1794531900000, title: reporterTitle,
};
export const reporterIdle = {
  conversationId: REPORTER, coordinatorId: LEAD, createdAt: 1794531479778, folderId: 'f_7a71cbe201ffc7567f657d52',
  turnCount: 2, updatedAt: 1794531966747, title: reporterTitle,
};

export function teamPage(...conversations: CoordinatorConversationPage['conversations']): CoordinatorConversationPage {
  return { conversations, nextCursor: 'eyJzY29wZSI6MX0', coverage: 'page' };
}

const request = {
  acceptance: [
    "Before deleting, the document's identity is verified from metadata only (contentId 2798016815, title 'ignore')",
    'Exactly that one document is deleted, through a 学城 MCP delete tool or the 学城 web UI in the owner\'s own session',
    'Deletion is verified afterwards (the page reports deleted or not found)',
  ],
  brief: 'OWNER AUTHORIZATION (11-13 ~09:20 CST): 「可以删。」, answering a question that named exactly one 学城 document',
  coordinatorId: LEAD,
  profile: { mode: 'project' as const, readPaths: ['.'], tools: ['ask_human', 'browser_check', 'browser_click'], writePaths: ['.'] },
  projectPath: PROJECT,
  recipientId: REPORTER,
};
/** The assignment while its Worker's attempt ran. */
export const assignmentRunning: ProjectDispatchHistoryRow = {
  assignmentId: '3c3d9702-19a5-5fcf-ba5d-7685af881da6', canonicalDelivery: false, grantRevision: 1, publication: null, revoked: false,
  delivery: { checkpointSha: '', reason: '', status: 'working', targetSha: '', taskId: 'project-worker:3c3d9702-19a5-5fcf-ba5d-7685af881da6' },
  execution: { attemptId: '171ad418-61a0-4f6f-be89-20ddab98c523', result: { content: '' }, resultRevision: 702, status: 'running',
    taskId: 'conversation-attempt:171ad418-61a0-4f6f-be89-20ddab98c523', turnId: '2587198f-b0dd-40c8-bff1-64a3bbfbd3d1' },
  request, review: null,
};
/** The same assignment after the Worker reported its result to the lead (and the lead reviewed it). */
export const assignmentReported: ProjectDispatchHistoryRow = {
  ...assignmentRunning, canonicalDelivery: true,
  delivery: { checkpointSha: '47aa9b62c3657c521030a5a5e6be56bdbfebe385', reason: '', status: 'delivered',
    targetSha: '47aa9b62c3657c521030a5a5e6be56bdbfebe385', taskId: 'project-worker:3c3d9702-19a5-5fcf-ba5d-7685af881da6' },
  execution: { ...assignmentRunning.execution!, status: 'completed', resultRevision: 711,
    result: { content: 'I deleted the 学城 document 「ignore」 (contentId 2798016815) and confirmed it is deleted.' } },
  review: {
    acceptanceCounts: { met: 3, unknown: 0, unmet: 0 }, actor: 'coordinator', decision: 'accept', resultRevision: 711,
    note: 'Accepted: the owner-authorized deletion of exactly one 学城 document.',
    acceptanceResults: [
      { check: 1, evidence: 'mcp__xuecheng__get_doc_meta at 08:59:43 CST (metadata only, body not read)', verdict: 'met' },
      { check: 2, evidence: "Exactly one mcp__xuecheng__delete_doc {doc:'2798016815', confirm:true}", verdict: 'met' },
      { check: 3, evidence: "mcp__xuecheng__read_doc 2798016815 returned 404 'Document has been deleted.'", verdict: 'met' },
    ],
  },
};
export const assignmentOtherRunning: ProjectDispatchHistoryRow = {
  assignmentId: 'e35878e1-c894-5164-989c-67f956abd872', canonicalDelivery: false, grantRevision: 1, publication: null, revoked: false,
  delivery: { checkpointSha: '', reason: '', status: 'working', targetSha: '', taskId: 'project-worker:e35878e1-c894-5164-989c-67f956abd872' },
  execution: { attemptId: 'aa44af72-e554-4727-bcb0-420f458ac3f2', result: { content: '' }, resultRevision: 702, status: 'running',
    taskId: 'conversation-attempt:aa44af72-e554-4727-bcb0-420f458ac3f2', turnId: 'dca88fb1-d50b-41f0-b9e4-df9def122739' },
  request: { ...request, recipientId: RUNNER, acceptance: ['A spec that fails first shows the symlinked rule file is read'],
    brief: 'CONTEXT\n- Two small harness defects.\n\nRemove the per-project repo lock bottleneck.' },
  review: null,
};

export const reportProgress: CoordinatorReportState = {
  bindingRevision: 6, conversationId: LEAD, projectPath: PROJECT, unread: true,
  latest: { content: '停下的线已全部由接替线接上并在运行；TofuDB 现有证据：多数指标 SQLite 领先\n\n**重启后停下的几条线**', createdAt: 1794533071987,
    kind: 'progress', ordinal: 528, title: '停下的线已全部由接替线接上并在运行；TofuDB 现有证据：多数指标 SQLite 领先', turnId: '1187ce16-c17c-4ff6-81fc-943d19fee295' },
};
export const reportDelivery: CoordinatorReportState = {
  ...reportProgress,
  latest: { ...reportProgress.latest!, kind: 'delivery', title: 'NDP 08:38 recovery closed: verdict merged, helper preserved',
    turnId: 'b2c3a9e0-5c1d-4f0e-9a51-0f1e2d3c4b5a', ordinal: 531, createdAt: 1794533500000 },
};

export const progressOverdue: CoordinatorProgressState = {
  projectPath: PROJECT, conversationId: LEAD, bindingRevision: 6,
  board: { revision: 12, updatedAt: 1794533071806, summary: '服务在线；存储卡死修复第三版认证中', tasks: [
    { title: '存储卡死修复（第三版）', status: 'in_progress', progress: '第四轮认证' },
    { title: '记忆主动查询', status: 'completed' },
    { title: 'MCP 默认关闭', status: 'pending' },
  ] },
  commitments: [
    { createdAt: 1794525965925, dueAt: 1794546000000, firstDueAt: 1794538800000, slips: 1, state: 'open', title: 'Tofu 稳定性：存储卡死修复（第三版）上线' },
    { createdAt: 1794352142469, dueAt: 1794500000000, firstDueAt: 1794358800000, slips: 15, state: 'open', title: '下一次重启准备（引擎门禁、回滚演练、中断清单）' },
  ],
};

/** An ask_human round awaiting the owner, as Conversation Sync v3 projects it in the lead's Turn. */
export const askTurn: QuestionTurn = {
  turnId: '6364198d-7e89-45fd-a7b4-22fe15be7e8f', ordinal: 530, actor: 'assistant', createdAt: 1794533200000,
  projection: { toolRounds: [{ toolName: 'ask_human', guidanceId: 'hg_069759980950', guidanceBlocking: true, status: 'awaiting_human',
    guidanceQuestion: '**Only you** can approve the Paper A run: keep 8 jobs or cut to 4?', guidanceDefault: '' }] },
};
export const toolChurnTurn: QuestionTurn = {
  turnId: '7a1d2c3b-0000-4000-8000-000000000001', ordinal: 531, actor: 'assistant', createdAt: 1794533300000,
  projection: { toolRounds: [{ toolName: 'run_command', status: 'done' }, { toolName: 'read_files', status: 'done' }] },
};

export const convChangedFrame = { channel: 'notify', type: 'conv_changed', convId: REPORTER, userId: 1, rev: 1228 };

export function tofu(overrides: Partial<TeamInput> = {}): TeamInput {
  return {
    key: PROJECT, kind: 'project', name: 'tofu', color: '#58b7a8', leadId: LEAD,
    team: teamPage(memberRunning, reporterBusy), assignments: [assignmentOtherRunning, assignmentRunning],
    progress: progressOverdue, reports: { ...reportProgress, unread: false }, questions: [], failed: false,
    ...overrides,
  };
}

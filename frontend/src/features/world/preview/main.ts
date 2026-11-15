/** Local office preview over a recorded fixture: the same world, styles and art the application mounts,
 * inside a stand-in shell (grid, dock and a placeholder chat column). Query: ?fixture=<url>&scale=<n>.
 * The preview never talks to a server beyond loading its fixture file. */
import '../../../styles/fonts.css';
import '../../../styles/application/00-design-tokens.css';
import '../../../styles/application/30-ui-kit.css';
import '../../../styles/application/01-workspace-dock.css';
import '../../../styles/application/27-world-office.css';
import { ready, t } from '../../../i18n';
import { mountWorld, type ConversationDock } from '../app';
import { fixtureLive, type FixtureSnapshot } from './fixture';

const params = new URLSearchParams(location.search);
void Promise.all([ready(), fetch(params.get('fixture') || './live.json').then(response => response.json() as Promise<FixtureSnapshot>)])
  .then(([, snapshot]) => {
    document.title = t('world.title');
    const root = document.getElementById('worldPageContainer')!;
    const chat = document.querySelector<HTMLElement>('.chat-wrapper')!;
    let source: ReturnType<typeof fixtureLive> | null = null;
    const world = mountWorld(root, {
      openChat: id => { chat.dataset.conversation = id; },
      dock: (mode: ConversationDock) => { document.body.dataset.conversationDock = mode; },
      classic: () => { document.body.dataset.classic = 'requested'; },
      push: null,
    }, {
      live: onChange => (source = fixtureLive(snapshot, onChange, { scale: Number(params.get('scale') || 0) })),
      now: () => (params.has('now') ? Number(params.get('now')) : Date.now()),
    });
    (window as unknown as { __world: unknown }).__world = { report: (team: string) => source?.report(team), office: () => world.office() };
  });

'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { applyEvents, type CommandEvent } from '@/lib/command-center/events';

type View = 'overview' | 'tasks' | 'jobs' | 'sessions' | 'profiles' | 'approvals' | 'blockers' | 'services';

type Snapshot = {
  source: string;
  cursor: string;
  summary: Record<string, number>;
  accounts: Array<Record<string, string | number | boolean | null>>;
  jobs: Array<Record<string, string | number | boolean | null>>;
  tasks: Array<Record<string, string | number | boolean | null>>;
  sessions: Array<Record<string, string | number | boolean | null>>;
  approvals: Array<Record<string, string | number | boolean | null>>;
  blockers: Array<Record<string, string | number>>;
  services: Array<{ name: string; state: string; role: string }>;
  message_states: Record<string, number>;
};

const NAV: View[] = ['overview', 'tasks', 'jobs', 'sessions', 'profiles', 'approvals', 'blockers', 'services'];

export default function CommandCenterPage() {
  const [authed, setAuthed] = useState<boolean | null>(null);
  const [password, setPassword] = useState('');
  const [loginError, setLoginError] = useState('');
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [error, setError] = useState('');
  const [view, setView] = useState<View>('overview');
  const [events, setEvents] = useState<CommandEvent[]>([]);
  const [cursor, setCursor] = useState('');
  const cursorRef = useRef('');
  const [connected, setConnected] = useState(false);
  const [streamAttempt, setStreamAttempt] = useState(0);
  const [taskId, setTaskId] = useState<number | null>(null);
  const [detail, setDetail] = useState<Record<string, unknown> | null>(null);

  const load = useCallback(async () => {
    const response = await fetch('/api/control-plane/command-center/snapshot', { cache: 'no-store' });
    if (response.status === 401) {
      setAuthed(false);
      return;
    }
    if (!response.ok) throw new Error(`snapshot ${response.status}`);
    const body = await response.json() as Snapshot;
    setSnapshot(body);
    cursorRef.current = body.cursor;
    setCursor(body.cursor);
    setAuthed(true);
  }, []);

  useEffect(() => {
    fetch('/api/control-plane/command-center/session')
      .then((response) => setAuthed(response.ok))
      .catch(() => setAuthed(false));
  }, []);

  useEffect(() => {
    if (!authed) return;
    load().catch((exc: Error) => setError(exc.message));
  }, [authed, load]);

  useEffect(() => {
    if (!authed || !snapshot) return;
    const source = new EventSource(`/api/control-plane/command-center/events?after=${encodeURIComponent(cursorRef.current || snapshot.cursor)}`);
    source.onopen = () => setConnected(true);
    source.onerror = () => setConnected(false);
    source.onmessage = (message) => {
      const page = JSON.parse(message.data) as { events: CommandEvent[]; cursor: string };
      cursorRef.current = page.cursor;
      setEvents((current) => applyEvents(current, page.events));
      setCursor(page.cursor);
    };
    return () => source.close();
  }, [authed, snapshot]);

  const openTask = async (id: number) => {
    setTaskId(id);
    const response = await fetch(`/api/control-plane/command-center/tasks/${id}`, { cache: 'no-store' });
    if (!response.ok) {
      setDetail({ error: `task ${response.status}` });
      return;
    }
    setDetail(await response.json());
  };

  const signIn = async () => {
    setLoginError('');
    const response = await fetch('/api/control-plane/command-center/session', {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'x-vicoa-command': '1' },
      body: JSON.stringify({ password }),
    });
    if (!response.ok) {
      setLoginError('Sign-in failed.');
      return;
    }
    setPassword('');
    setAuthed(true);
  };

  const attention = useMemo(() => (snapshot?.tasks || []).filter((task) => task.locked || task.worker_status === 'needs_input' || task.stale), [snapshot]);

  if (authed === null) return <main className="p-6 text-sm text-muted-foreground">Checking session.</main>;
  if (!authed) {
    return (
      <main className="mx-auto flex min-h-screen w-full max-w-md flex-col justify-center gap-4 p-6">
        <h1 className="text-2xl font-semibold">Vicoa Command Center</h1>
        <p className="text-sm text-muted-foreground">Private operator session. Tailscale reachability is not enough.</p>
        <input className="rounded-md border bg-background px-3 py-2" type="password" value={password} onChange={(event) => setPassword(event.target.value)} placeholder="Operator password" autoComplete="current-password" />
        <button className="rounded-md bg-foreground px-3 py-2 text-sm text-background" type="button" onClick={signIn}>Sign in</button>
        {loginError ? <p className="text-sm text-muted-foreground">{loginError}</p> : null}
      </main>
    );
  }

  return (
    <main className="mx-auto flex w-full max-w-6xl flex-col gap-4 overflow-x-hidden p-4 pb-24 sm:p-6">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold">Command Center</h1>
          <p className="text-sm text-muted-foreground">State comes from the Vicoa control plane, not an agent report. {connected ? 'Live' : 'Stream reconnecting'} · cursor {cursor || 'none'}</p>
        </div>
        <button className="text-sm text-muted-foreground" type="button" onClick={() => load()}>Refresh</button>
      </header>
      {error ? <p className="text-sm text-muted-foreground">{error}</p> : null}
      <nav className="hidden flex-wrap gap-2 sm:flex">
        {NAV.map((item) => (
          <button key={item} className={`rounded-md px-3 py-1 text-sm ${view === item ? 'bg-foreground text-background' : 'bg-muted text-muted-foreground'}`} type="button" onClick={() => setView(item)}>{item}</button>
        ))}
      </nav>
      {view === 'overview' && snapshot ? (
        <section className="grid gap-3 sm:grid-cols-3">
          {Object.entries(snapshot.summary).map(([key, value]) => (
            <div key={key} className="rounded-lg border p-3">
              <div className="text-xs uppercase text-muted-foreground">{key.replaceAll('_', ' ')}</div>
              <div className="text-2xl font-semibold">{value}</div>
            </div>
          ))}
          <div className="rounded-lg border p-3 sm:col-span-3">
            <div className="text-sm font-medium">Needs attention</div>
            {attention.slice(0, 8).map((task) => (
              <button key={String(task.id)} className="block text-left text-sm text-muted-foreground" type="button" onClick={() => openTask(Number(task.id))}>
                #{String(task.id)} {String(task.title)} · {String(task.worker_status)}{task.locked ? ' · held' : ''}
              </button>
            ))}
          </div>
        </section>
      ) : null}
      {view === 'tasks' && snapshot ? <TaskList tasks={snapshot.tasks} onOpen={openTask} /> : null}
      {view === 'jobs' && snapshot ? <Rows rows={snapshot.jobs} empty="No jobs." /> : null}
      {view === 'sessions' && snapshot ? <Rows rows={snapshot.sessions} empty="No recorded sessions." /> : null}
      {view === 'profiles' && snapshot ? <Rows rows={snapshot.accounts} empty="No profiles." /> : null}
      {view === 'approvals' && snapshot ? <Rows rows={snapshot.approvals} empty="No approvals." /> : null}
      {view === 'blockers' && snapshot ? <Rows rows={snapshot.blockers} empty="No blockers." /> : null}
      {view === 'services' && snapshot ? (
        <section className="flex flex-col gap-2 text-sm">
          {snapshot.services.map((service) => (
            <div key={service.name}>{service.name} · {service.state} · {service.role}. Not restarted from this page.</div>
          ))}
        </section>
      ) : null}
      {taskId ? (
        <section className="rounded-lg border p-3 text-sm">
          <div className="mb-2 flex items-center justify-between">
            <h2 className="font-medium">Task {taskId}</h2>
            <button type="button" onClick={() => { setTaskId(null); setDetail(null); }}>Close</button>
          </div>
          {detail ? <TaskDetail detail={detail} /> : <p className="text-sm text-muted-foreground">Loading task.</p>}
        </section>
      ) : null}
      <section className="rounded-lg border p-3">
        <h2 className="text-sm font-medium">Live events</h2>
        <div className="mt-2 flex max-h-48 flex-col gap-1 overflow-auto text-xs text-muted-foreground">
          {events.length ? events.slice(-30).map((event) => (
            <div key={event.id}>{event.created_at} · {event.event_type} · {event.message}</div>
          )) : 'Waiting for a new control-plane event. Existing history is not replayed.'}
        </div>
      </section>
      <nav className="fixed inset-x-0 bottom-0 flex justify-around border-t bg-background p-2 sm:hidden">
        {NAV.slice(0, 5).map((item) => (
          <button key={item} className="text-xs" type="button" onClick={() => setView(item)}>{item}</button>
        ))}
      </nav>
    </main>
  );
}

function TaskList({ tasks, onOpen }: { tasks: Array<Record<string, string | number | boolean | null>>; onOpen: (id: number) => void }) {
  return (
    <section className="flex flex-col gap-2 text-sm">
      {tasks.map((task) => (
        <button key={String(task.id)} className="break-words rounded-md border p-2 text-left" type="button" onClick={() => onOpen(Number(task.id))}>
          #{String(task.id)} {String(task.title)} · {String(task.worker_status)} · {String(task.verification_status)}
          {task.account_id ? ` · ${String(task.account_id)}` : ' · no profile'}
          {task.session_set ? ' · session set' : ' · no session'}
          {task.locked ? ' · held' : ''}
        </button>
      ))}
    </section>
  );
}

function TaskDetail({ detail }: { detail: Record<string, unknown> }) {
  const task = (detail.task || {}) as Record<string, unknown>;
  const lineage = (detail.lineage || []) as Array<Record<string, unknown>>;
  const handoffs = (detail.handoffs || []) as Array<Record<string, unknown>>;
  const messages = (detail.messages || []) as Array<Record<string, unknown>>;
  const context = detail.context as Record<string, unknown> | null;
  const skills = detail.skills as { active?: Array<Record<string, unknown>>; skipped?: Array<Record<string, unknown>> } | undefined;
  return (
    <div className="flex flex-col gap-2 text-sm">
      <div>{String(task.title || '')} · worker {String(task.worker_status || '')} · verification {String(task.verification_status || '')}{detail.locked ? ' · held, no actions' : ''}</div>
      <div>Conversation: {conversationNote(detail)}</div>
      <div>Verification: {verificationNote(detail)}</div>
      <div>Files: {fileNote(detail)}</div>
      <div>Lineage: {lineage.map((row) => `${String(row.session_id)} on ${String(row.account_id)} (${String(row.status)}${row.parent_session_id ? `, parent ${String(row.parent_session_id)}` : ''}${row.handoff_id ? `, handoff ${String(row.handoff_id)}` : ''})`).join(' → ') || 'none'}</div>
      <div>Handoffs: {handoffs.map((row) => `#${String(row.id)} ${String(row.reason)} ${String(row.status)}`).join(' · ') || 'none'}</div>
      <div>Context: {context ? `pack ${String(context.id)} used ${String(context.used_tokens)}/${String(context.budget_tokens)}` : 'none recorded'}</div>
      <div>Skills: {(skills?.active || []).map((row) => String(row.skill_key)).join(', ') || 'none'}{(skills?.skipped || []).length ? ` · skipped ${(skills?.skipped || []).length}` : ''}</div>
      <div>Messages: {messages.map((row) => `${String(row.body)} ${String(row.state)}`).join(' · ') || 'none'}</div>
    </div>
  );
}

function conversationNote(detail: Record<string, unknown>): string {
  const conversation = detail.conversation as { note?: string; items?: Array<Record<string, unknown>> } | undefined;
  const items = conversation?.items || [];
  const boundaries = items.filter((item) => item.kind === 'boundary').map((item) => `${String(item.session_id)} ${String(item.status)} on ${String(item.account_id)}${item.handoff_id ? ` handoff ${String(item.handoff_id)}` : ''}`);
  return boundaries.join(' → ') || conversation?.note || 'No stored provider transcript.';
}

function verificationNote(detail: Record<string, unknown>): string {
  const rows = (detail.verifications || []) as Array<Record<string, unknown>>;
  if (!rows.length) return 'No verification recorded.';
  return rows.map((row) => `${String(row.status)} ${Array.isArray(row.checks) ? `${row.checks.length} checks` : ''}`.trim()).join(' · ');
}

function fileNote(detail: Record<string, unknown>): string {
  const files = detail.files as { available?: boolean; reason?: string; files?: Array<Record<string, unknown>> } | undefined;
  if (!files?.available) return files?.reason || 'Worktree is not available.';
  return files.files?.map((row) => `${String(row.status)} ${String(row.path)}`).join(', ') || 'No changed files.';
}

function Rows({ rows, empty }: { rows: Array<Record<string, unknown>>; empty: string }) {
  if (!rows.length) return <p className="text-sm text-muted-foreground">{empty}</p>;
  return (
    <section className="flex flex-col gap-2 text-sm">
      {rows.map((row, index) => (
        <div key={String(row.id ?? row.name ?? index)} className="break-words rounded-md border p-2">
          {Object.entries(row).filter(([, value]) => value !== null && value !== '').map(([key, value]) => `${key} ${String(value)}`).join(' · ')}
        </div>
      ))}
    </section>
  );
}

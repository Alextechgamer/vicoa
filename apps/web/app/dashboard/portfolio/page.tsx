'use client';

import { useEffect, useState } from 'react';
import { getBackendAPI } from '@/lib/backend-api';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';

type Status = Awaited<ReturnType<ReturnType<typeof getBackendAPI>['controlPlaneStatus']>>;

export default function PortfolioPage() {
  const [status, setStatus] = useState<Status | null>(null);
  const [error, setError] = useState('');

  useEffect(() => {
    let cancelled = false;
    fetch('/api/control-plane/status')
      .then(async (response) => {
        if (!response.ok) {
          throw new Error(`control plane status ${response.status}`);
        }
        return response.json() as Promise<Status>;
      })
      .then((value) => {
        if (!cancelled) setStatus(value);
      })
      .catch((exc: Error) => {
        if (!cancelled) setError(exc.message);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <main className="mx-auto flex w-full max-w-5xl flex-col gap-4 overflow-x-hidden p-4 sm:p-6">
      <header>
        <h1 className="text-2xl font-semibold">Portfolio</h1>
        <p className="text-sm text-muted-foreground">
          Profiles, verification, queued messages, routing, and protected work. Shadow mode does not dispatch imported live work.
        </p>
      </header>
      {error ? <p className="text-sm text-muted-foreground">{error}</p> : null}
      {!status && !error ? <p className="text-sm text-muted-foreground">Loading control-plane status.</p> : null}
      {status ? (
        <>
          <Card>
            <CardHeader>
              <CardTitle>Profiles</CardTitle>
              <CardDescription>Runtime homes stay server-side. A constraint on one profile does not disable another.</CardDescription>
            </CardHeader>
            <CardContent className="flex flex-col gap-2 text-sm">
              {status.accounts.map((account) => (
                <div key={account.id} className="break-words">
                  {account.id} · {account.provider} · {account.auth_state} · workers {account.active_workers}/{account.max_workers}
                  {account.quota_state === 'unknown' ? ' · quota unknown' : ` · quota ${account.quota_state}`}
                  {account.constrained ? ' · constrained' : ''}
                  {account.drained ? ' · draining' : ' · available'}
                  {account.enabled ? '' : ' · disabled'}
                </div>
              ))}
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>Jobs</CardTitle>
              <CardDescription>Import holds and shadow jobs are not started from this page.</CardDescription>
            </CardHeader>
            <CardContent className="flex flex-col gap-2 text-sm">
              {status.jobs.map((job) => (
                <div key={job.id}>
                  {job.project} · {job.status}
                  {job.import_hold ? ' · held' : ''}
                  {job.shadow ? ' · shadow' : ''}
                  {job.source_id ? ` · source ${job.source_id}` : ''}
                </div>
              ))}
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>Tasks</CardTitle>
              <CardDescription>Worker finished is not verified. Protected and held tasks are not started from this page.</CardDescription>
            </CardHeader>
            <CardContent className="flex flex-col gap-2 text-sm">
              {status.tasks.length
                ? status.tasks.map((task) => (
                    <div key={task.id} className="break-words">
                      {task.title} · worker {task.worker_status} · verification {task.verification_status}
                      {task.account_id ? ` · ${task.account_id}` : ' · no profile'}
                      {task.session_id ? ' · session set' : ' · no session'}
                      {task.worktree_set ? ' · worktree set' : ' · no worktree'}
                      {task.protected ? ' · protected' : ''}
                      {task.owner_only ? ' · owner-only' : ''}
                      {task.import_hold ? ' · held' : ''}
                      {task.policy !== 'routine' ? ` · ${task.policy}` : ''}
                    </div>
                  ))
                : 'No tasks.'}
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>Approvals</CardTitle>
              <CardDescription>Approve once does not create a standing rule. Prompt text is not shown here.</CardDescription>
            </CardHeader>
            <CardContent className="text-sm">
              {status.approvals.length
                ? status.approvals.map((item) => (
                    <div key={item.id}>task {item.task_id} · {item.status}{item.permanent ? ' · fingerprint rule' : ''}</div>
                  ))
                : 'No approvals.'}
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>Queued messages</CardTitle>
              <CardDescription>Queued is not delivered. Sent is not acknowledged.</CardDescription>
            </CardHeader>
            <CardContent className="text-sm">
              {Object.entries(status.message_states).length
                ? Object.entries(status.message_states).map(([state, count]) => (
                    <div key={state}>{state}: {count}</div>
                  ))
                : 'No steer messages in this control-plane database.'}
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>Blockers</CardTitle>
              <CardDescription>Owner-only, protected, unconsumed steer, and stale session.</CardDescription>
            </CardHeader>
            <CardContent className="text-sm">
              {status.blockers.length
                ? status.blockers.map((item) => (
                    <div key={`${item.kind}-${item.task_id}`}>{item.kind} · task {item.task_id}</div>
                  ))
                : 'No blockers.'}
              <div className="mt-2">Shadow jobs: {status.shadow_jobs.join(', ') || 'none'}</div>
              <div>Protected: {status.health.protected_tasks.join(', ') || 'none'}</div>
            </CardContent>
          </Card>
        </>
      ) : null}
    </main>
  );
}

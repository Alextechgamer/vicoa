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
    getBackendAPI()
      .controlPlaneStatus()
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
    <main className="mx-auto flex max-w-5xl flex-col gap-4 p-6">
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
                <div key={account.id}>
                  {account.id} · {account.provider} · {account.auth_state}
                  {account.constrained ? ' · constrained' : ''}
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

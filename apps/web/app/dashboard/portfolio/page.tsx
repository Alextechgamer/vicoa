'use client';

import { useEffect, useState } from 'react';
import { getBackendAPI } from '@/lib/backend-api';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';

type Status = {
  accounts: Array<{ id: string; enabled: number; constrained: number; auth_state: string }>;
  jobs: Array<{ id: number; project: string; status: string; import_hold: number; source_id: string }>;
  health: {
    stale_sessions: number[];
    unconsumed_steers: number[];
    protected_tasks: number[];
    owner_blockers: Array<{ task_id: number; blocker: string }>;
  };
  blockers: Array<{ kind: string; task_id: number; blocker?: string }>;
};

export default function PortfolioPage() {
  const [status, setStatus] = useState<Status | null>(null);
  const [error, setError] = useState('');

  useEffect(() => {
    let cancelled = false;
    getBackendAPI()
      .controlPlaneStatus()
      .then((next) => {
        if (!cancelled) setStatus(next as Status);
      })
      .catch((err: Error) => {
        if (!cancelled) setError(err.message || 'Control plane is not reachable');
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div className="flex h-full flex-col gap-4 overflow-auto p-6">
      <div>
        <h1 className="text-lg font-medium">Portfolio</h1>
        <p className="text-sm text-muted-foreground">
          Verification-gated jobs, account health, and owner blockers. A worker finishing does not unlock the next task.
        </p>
      </div>
      {error ? (
        <Card>
          <CardHeader>
            <CardTitle>Control plane</CardTitle>
            <CardDescription>{error}</CardDescription>
          </CardHeader>
          <CardContent className="text-sm text-muted-foreground">
            The API stays closed until VICOA_CONTROL_PLANE_TOKEN is set. This page does not message live workers.
          </CardContent>
        </Card>
      ) : null}
      {status ? (
        <div className="grid gap-4 md:grid-cols-3">
          <Card>
            <CardHeader>
              <CardTitle>Accounts</CardTitle>
              <CardDescription>Isolated runtime profiles</CardDescription>
            </CardHeader>
            <CardContent className="space-y-2 text-sm">
              {status.accounts.map((account) => (
                <div key={account.id} className="flex justify-between">
                  <span>{account.id}</span>
                  <span>{account.constrained ? 'constrained' : account.enabled ? 'enabled' : 'disabled'}</span>
                </div>
              ))}
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>Jobs</CardTitle>
              <CardDescription>Imported work stays held</CardDescription>
            </CardHeader>
            <CardContent className="space-y-2 text-sm">
              {status.jobs.length === 0 ? <p>No jobs yet.</p> : null}
              {status.jobs.map((job) => (
                <div key={job.id} className="flex justify-between gap-3">
                  <span className="truncate">{job.project}</span>
                  <span>{job.import_hold ? 'held' : job.status}</span>
                </div>
              ))}
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>Blockers</CardTitle>
              <CardDescription>Owner-only stops automation</CardDescription>
            </CardHeader>
            <CardContent className="space-y-2 text-sm">
              <p>Stale sessions: {status.health.stale_sessions.length}</p>
              <p>Unconsumed steering: {status.health.unconsumed_steers.length}</p>
              <p>Protected: {status.health.protected_tasks.length}</p>
              {status.blockers.map((blocker) => (
                <p key={`${blocker.kind}-${blocker.task_id}`}>
                  {blocker.kind} {blocker.task_id}
                  {blocker.blocker ? `: ${blocker.blocker}` : ''}
                </p>
              ))}
            </CardContent>
          </Card>
        </div>
      ) : null}
    </div>
  );
}

import { NextResponse } from 'next/server';

import { COMMAND_COOKIE, verifySession } from '@/lib/command-center/session';

export const dynamic = 'force-dynamic';

const SECURITY_HEADERS = {
  'x-frame-options': 'DENY',
  'x-content-type-options': 'nosniff',
  'referrer-policy': 'no-referrer',
  'cache-control': 'no-store',
};

export function requireOperator(request: Request): NextResponse | null {
  const raw = request.headers.get('cookie') || '';
  const token = raw.split(';').map((part) => part.trim()).find((part) => part.startsWith(`${COMMAND_COOKIE}=`))?.split('=').slice(1).join('=');
  const claims = verifySession(token ? decodeURIComponent(token) : undefined, process.env.VICOA_COMMAND_CENTER_SECRET || '');
  if (!claims) {
    return NextResponse.json({ detail: 'unauthorized' }, { status: 401, headers: SECURITY_HEADERS });
  }
  return null;
}

export async function proxyControlPlane(path: string): Promise<NextResponse> {
  const token = process.env.VICOA_CONTROL_PLANE_TOKEN;
  const base = process.env.VICOA_CONTROL_PLANE_URL;
  if (!token || !base) {
    return NextResponse.json({ detail: 'control plane proxy is not configured' }, { status: 503, headers: SECURITY_HEADERS });
  }
  const response = await fetch(`${base}${path}`, {
    headers: { Authorization: `Bearer ${token}` },
    cache: 'no-store',
  });
  const body = await response.text();
  return new NextResponse(body, {
    status: response.status,
    headers: { ...SECURITY_HEADERS, 'content-type': 'application/json' },
  });
}

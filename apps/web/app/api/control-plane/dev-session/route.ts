import { NextResponse } from 'next/server';

import { BUILTIN_SESSION_COOKIE } from '@/lib/auth/auth-provider';
import { devSessionAllowed } from '@/lib/auth/dev-session';

export const dynamic = 'force-dynamic';

function sessionToken(): string {
  const payload = Buffer.from(JSON.stringify({
    sub: 'local-operator',
    email: 'operator@localhost',
    name: 'Local operator',
    exp: Math.floor(Date.now() / 1000) + 3600,
  })).toString('base64url');
  return `eyJhbGciOiJub25lIn0.${payload}.local`;
}

export function GET(request: Request) {
  if (!devSessionAllowed()) {
    return NextResponse.json({ detail: 'not found' }, { status: 404 });
  }
  const response = NextResponse.redirect(new URL('/dashboard/portfolio', request.url));
  response.cookies.set(BUILTIN_SESSION_COOKIE, sessionToken(), {
    httpOnly: true,
    sameSite: 'lax',
    path: '/',
  });
  return response;
}

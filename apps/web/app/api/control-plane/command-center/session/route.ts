import { NextResponse } from 'next/server';

import { COMMAND_COOKIE, passwordsMatch, signSession, verifySession } from '@/lib/command-center/session';

export const dynamic = 'force-dynamic';

function secret(): string {
  return process.env.VICOA_COMMAND_CENTER_SECRET || '';
}

function password(): string {
  return process.env.VICOA_COMMAND_CENTER_PASSWORD || '';
}

function cookieOptions() {
  return {
    httpOnly: true,
    sameSite: 'lax' as const,
    secure: process.env.VICOA_COMMAND_CENTER_SECURE === '1',
    path: '/',
    maxAge: 60 * 60 * 12,
  };
}

export async function POST(request: Request) {
  const expected = password();
  const signing = secret();
  if (!expected || !signing) {
    return NextResponse.json({ detail: 'command center auth is not configured' }, { status: 503 });
  }
  const body = await request.json().catch(() => ({}));
  if (request.headers.get('x-vicoa-command') !== '1') {
    return NextResponse.json({ detail: 'missing command header' }, { status: 403 });
  }
  if (!passwordsMatch(String(body.password || ''), expected)) {
    return NextResponse.json({ detail: 'unauthorized' }, { status: 401 });
  }
  const response = NextResponse.json({ ok: true });
  response.cookies.set(COMMAND_COOKIE, signSession(signing), cookieOptions());
  return response;
}

export function GET(request: Request) {
  const token = request.headers.get('cookie')?.split(';').map((part) => part.trim()).find((part) => part.startsWith(`${COMMAND_COOKIE}=`))?.split('=').slice(1).join('=');
  const claims = verifySession(token ? decodeURIComponent(token) : undefined, secret());
  if (!claims) return NextResponse.json({ detail: 'unauthorized' }, { status: 401 });
  return NextResponse.json({ ok: true, subject: claims.sub });
}

export function DELETE() {
  const response = NextResponse.json({ ok: true });
  response.cookies.set(COMMAND_COOKIE, '', { ...cookieOptions(), maxAge: 0 });
  return response;
}

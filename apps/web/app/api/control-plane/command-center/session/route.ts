import { NextResponse } from 'next/server';

import {
  COMMAND_COOKIE,
  passwordMatchesStored,
  rateLimited,
  signSession,
  verifySession,
} from '@/lib/command-center/session';

export const dynamic = 'force-dynamic';

function signingSecret(): string {
  return process.env.VICOA_COMMAND_CENTER_SECRET || '';
}

function storedPassword(): string {
  return process.env.VICOA_COMMAND_CENTER_PASSWORD_HASH || process.env.VICOA_COMMAND_CENTER_PASSWORD || '';
}

function cookieOptions() {
  return {
    httpOnly: true,
    sameSite: 'strict' as const,
    secure: process.env.NODE_ENV === 'production' || process.env.VICOA_COMMAND_CENTER_SECURE === '1',
    path: '/',
    maxAge: 60 * 60 * 8,
  };
}

function clientKey(request: Request): string {
  return request.headers.get('x-forwarded-for')?.split(',')[0]?.trim() || 'local';
}

function readCookie(request: Request): string | undefined {
  const raw = request.headers.get('cookie') || '';
  const token = raw.split(';').map((part) => part.trim()).find((part) => part.startsWith(`${COMMAND_COOKIE}=`))?.split('=').slice(1).join('=');
  return token ? decodeURIComponent(token) : undefined;
}

export async function POST(request: Request) {
  const stored = storedPassword();
  const signing = signingSecret();
  if (!stored || !signing) {
    return NextResponse.json({ detail: 'command center auth is not configured' }, { status: 503 });
  }
  if (rateLimited(clientKey(request))) {
    return NextResponse.json({ detail: 'unauthorized' }, { status: 429 });
  }
  const body = await request.json().catch(() => ({}));
  if (!passwordMatchesStored(String(body.password || ''), stored)) {
    return NextResponse.json({ detail: 'unauthorized' }, { status: 401 });
  }
  const token = signSession(signing);
  const claims = verifySession(token, signing);
  const response = NextResponse.json({ ok: true, csrf: claims?.csrf });
  response.cookies.set(COMMAND_COOKIE, token, cookieOptions());
  return response;
}

export function GET(request: Request) {
  const claims = verifySession(readCookie(request), signingSecret());
  if (!claims) return NextResponse.json({ detail: 'unauthorized' }, { status: 401 });
  return NextResponse.json({ ok: true, subject: claims.sub, csrf: claims.csrf, exp: claims.exp });
}

export function DELETE(request: Request) {
  const claims = verifySession(readCookie(request), signingSecret());
  if (!claims || request.headers.get('x-csrf-token') !== claims.csrf) {
    return NextResponse.json({ detail: 'unauthorized' }, { status: 401 });
  }
  const response = NextResponse.json({ ok: true });
  response.cookies.set(COMMAND_COOKIE, '', { ...cookieOptions(), maxAge: 0 });
  return response;
}

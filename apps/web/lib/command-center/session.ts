import { createHmac, timingSafeEqual } from 'node:crypto';

export const COMMAND_COOKIE = 'vicoa_command_session';

type Claims = { sub: string; exp: number };

export function signSession(secret: string, subject = 'operator', ttlSeconds = 60 * 60 * 12): string {
  const payload = Buffer.from(JSON.stringify({ sub: subject, exp: Math.floor(Date.now() / 1000) + ttlSeconds })).toString('base64url');
  const mac = createHmac('sha256', secret).update(payload).digest('base64url');
  return `${payload}.${mac}`;
}

export function verifySession(token: string | undefined, secret: string): Claims | null {
  if (!token || !secret || !token.includes('.')) return null;
  const [payload, mac] = token.split('.');
  const expected = createHmac('sha256', secret).update(payload).digest('base64url');
  const left = Buffer.from(mac);
  const right = Buffer.from(expected);
  if (left.length !== right.length || !timingSafeEqual(left, right)) return null;
  try {
    const claims = JSON.parse(Buffer.from(payload, 'base64url').toString()) as Claims;
    if (!claims.sub || claims.exp * 1000 <= Date.now()) return null;
    return claims;
  } catch {
    return null;
  }
}

export function passwordsMatch(presented: string, expected: string): boolean {
  const left = Buffer.from(presented);
  const right = Buffer.from(expected);
  if (!expected || left.length !== right.length) return false;
  return timingSafeEqual(left, right);
}

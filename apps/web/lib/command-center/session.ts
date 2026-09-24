import { createHmac, randomBytes, scryptSync, timingSafeEqual } from 'node:crypto';

export const COMMAND_COOKIE = 'vicoa_command_session';

type Claims = { sub: string; exp: number; csrf: string };

export function signSession(secret: string, subject = 'operator', ttlSeconds = 60 * 60 * 8, csrf = randomBytes(18).toString('base64url')): string {
  const payload = Buffer.from(JSON.stringify({ sub: subject, exp: Math.floor(Date.now() / 1000) + ttlSeconds, csrf })).toString('base64url');
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
    if (!claims.sub || !claims.csrf || claims.exp * 1000 <= Date.now()) return null;
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

export function hashPassword(password: string): string {
  const salt = randomBytes(16).toString('base64url');
  const digest = scryptSync(password, salt, 32).toString('base64url');
  return `scrypt:${salt}:${digest}`;
}

export function passwordMatchesStored(presented: string, stored: string): boolean {
  if (stored.startsWith('scrypt:')) {
    const [, salt, digest] = stored.split(':');
    if (!salt || !digest) return false;
    const actual = scryptSync(presented, salt, 32);
    const expected = Buffer.from(digest, 'base64url');
    return actual.length === expected.length && timingSafeEqual(actual, expected);
  }
  return passwordsMatch(presented, stored);
}

export function csrfMatches(header: string | null, claims: Claims | null): boolean {
  if (!header || !claims) return false;
  return passwordsMatch(header, claims.csrf);
}

const attempts = new Map<string, number[]>();

export function rateLimited(key: string, now = Date.now(), limit = 5, windowMs = 15 * 60 * 1000): boolean {
  const recent = (attempts.get(key) || []).filter((stamp) => now - stamp < windowMs);
  recent.push(now);
  attempts.set(key, recent);
  return recent.length > limit;
}

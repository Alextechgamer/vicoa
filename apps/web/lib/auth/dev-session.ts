export function devSessionAllowed(
  env: Record<string, string | undefined> = process.env,
): boolean {
  return (
    env.NEXT_PUBLIC_AUTH_PROVIDER === 'builtin' &&
    env.VICOA_DEV_SESSION === '1' &&
    env.NODE_ENV !== 'production'
  );
}

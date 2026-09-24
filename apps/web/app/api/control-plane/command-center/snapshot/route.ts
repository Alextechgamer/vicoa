import { proxyControlPlane, requireOperator } from '../_proxy';

export const dynamic = 'force-dynamic';

export async function GET(request: Request) {
  const denied = requireOperator(request);
  if (denied) return denied;
  return proxyControlPlane('/api/v1/control-plane/command-center/snapshot');
}

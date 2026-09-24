import { proxyControlPlane, requireOperator } from '../../_proxy';

export const dynamic = 'force-dynamic';

export async function GET(request: Request, context: { params: Promise<{ taskId: string }> }) {
  const denied = requireOperator(request);
  if (denied) return denied;
  const { taskId } = await context.params;
  if (!/^[0-9]+$/.test(taskId)) {
    return new Response(JSON.stringify({ detail: 'invalid task' }), { status: 400 });
  }
  return proxyControlPlane(`/api/v1/control-plane/command-center/tasks/${taskId}`);
}

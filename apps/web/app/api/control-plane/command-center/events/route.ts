import { requireOperator } from '../_proxy';

export const dynamic = 'force-dynamic';

export async function GET(request: Request) {
  const denied = requireOperator(request);
  if (denied) return denied;
  const token = process.env.VICOA_CONTROL_PLANE_TOKEN;
  const base = process.env.VICOA_CONTROL_PLANE_URL;
  if (!token || !base) {
    return new Response('control plane proxy is not configured', { status: 503 });
  }
  const cursor = new URL(request.url).searchParams.get('after') || 'plane=0&legacy=0';
  const encoder = new TextEncoder();
  let closed = false;
  const stream = new ReadableStream({
    async start(controller) {
      let current = cursor;
      const started = Date.now();
      try {
        while (!closed && Date.now() - started < 20000) {
          const response = await fetch(`${base}/api/v1/control-plane/command-center/events?after=${encodeURIComponent(current)}`, {
            headers: { Authorization: `Bearer ${token}` },
            cache: 'no-store',
          });
          if (response.ok) {
            const page = await response.json() as { events: unknown[]; cursor: string };
            if (page.events?.length) {
              controller.enqueue(encoder.encode(`data: ${JSON.stringify(page)}\n\n`));
              current = page.cursor;
            }
          }
          await new Promise((resolve) => setTimeout(resolve, 1500));
        }
      } finally {
        controller.close();
      }
    },
    cancel() {
      closed = true;
    },
  });
  return new Response(stream, {
    headers: {
      'content-type': 'text/event-stream',
      'cache-control': 'no-cache, no-transform',
      'x-accel-buffering': 'no',
    },
  });
}

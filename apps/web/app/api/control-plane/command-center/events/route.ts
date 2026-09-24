import { requireOperator } from '../_proxy';

export const dynamic = 'force-dynamic';
export const runtime = 'nodejs';

const HEARTBEAT_MS = 15000;
const MAX_STREAM_MS = 30 * 60 * 1000;

export async function GET(request: Request) {
  const denied = requireOperator(request);
  if (denied) return denied;
  const token = process.env.VICOA_CONTROL_PLANE_TOKEN;
  const base = process.env.VICOA_CONTROL_PLANE_URL;
  if (!token || !base) {
    return new Response('control plane proxy is not configured', { status: 503 });
  }
  const requested = request.headers.get('last-event-id') || new URL(request.url).searchParams.get('after') || 'plane=0&legacy=0';
  const encoder = new TextEncoder();
  let closed = false;
  const stream = new ReadableStream({
    async start(controller) {
      let current = requested;
      const started = Date.now();
      let lastBeat = 0;
      try {
        while (!closed && Date.now() - started < MAX_STREAM_MS) {
          if (Date.now() - lastBeat >= HEARTBEAT_MS) {
            controller.enqueue(encoder.encode(`: heartbeat ${Date.now()}\n\n`));
            lastBeat = Date.now();
          }
          try {
            const response = await fetch(`${base}/api/v1/control-plane/command-center/events?after=${encodeURIComponent(current)}`, {
              headers: { Authorization: `Bearer ${token}` },
              cache: 'no-store',
            });
            if (response.ok) {
              const page = await response.json() as { events: Array<{ id: string }>; cursor: string };
              if (page.events?.length) {
                controller.enqueue(encoder.encode(`id: ${page.cursor}\ndata: ${JSON.stringify(page)}\n\n`));
                current = page.cursor;
              }
            }
          } catch {
            // A brief control-plane miss must not close the stream.
          }
          await new Promise((resolve) => setTimeout(resolve, 2000));
        }
      } catch {
        // Client disconnect closes the stream. Do not log request headers.
      } finally {
        if (!closed) controller.close();
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
      connection: 'keep-alive',
      'x-accel-buffering': 'no',
    },
  });
}

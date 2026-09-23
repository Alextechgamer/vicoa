import { NextResponse } from 'next/server';

export const dynamic = 'force-dynamic';

export async function GET() {
  const token = process.env.VICOA_CONTROL_PLANE_TOKEN;
  const base = process.env.VICOA_CONTROL_PLANE_URL;
  if (!token || !base) {
    return NextResponse.json(
      { detail: 'control plane proxy is not configured' },
      { status: 503 },
    );
  }
  const response = await fetch(`${base}/api/v1/control-plane/status`, {
    headers: { Authorization: `Bearer ${token}` },
    cache: 'no-store',
  });
  const body = await response.text();
  return new NextResponse(body, {
    status: response.status,
    headers: { 'content-type': 'application/json' },
  });
}

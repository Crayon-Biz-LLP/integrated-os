import { NextRequest, NextResponse } from "next/server";

export async function PATCH(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const backendUrl = process.env.NEXT_PUBLIC_BACKEND_URL || "http://localhost:8000";
  const body = await request.json();
  const apiKey = process.env.API_SECRET_KEY || "";
  const resolvedParams = await params;

  const res = await fetch(`${backendUrl}/api/graph-node/${resolvedParams.id}/type`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json", "X-API-Key": apiKey },
    body: JSON.stringify(body),
  });

  const data = await res.json();
  return NextResponse.json(data, { status: res.status });
}

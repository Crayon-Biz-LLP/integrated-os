import { NextRequest, NextResponse } from "next/server";

export async function POST(request: NextRequest) {
  const backendUrl = process.env.NEXT_PUBLIC_BACKEND_URL || "http://localhost:8000";
  const body = await request.json();

  const apiKey = process.env.API_SECRET_KEY || "";

  const res = await fetch(`${backendUrl}/api/clarification`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-API-Key": apiKey },
    body: JSON.stringify(body),
  });

  if (!res.ok) {
    try {
      const err = await res.json();
      return NextResponse.json(err, { status: res.status });
    } catch {
      return NextResponse.json({ detail: "Failed to fetch from backend" }, { status: res.status });
    }
  }

  const data = await res.json();
  return NextResponse.json(data, { status: res.status });
}

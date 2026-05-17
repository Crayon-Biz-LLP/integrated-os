import { NextRequest, NextResponse } from "next/server";

export async function GET(req: NextRequest) {
  const backendUrl = process.env.NEXT_PUBLIC_BACKEND_URL || "http://localhost:8000";
  const apiKey = process.env.API_SECRET_KEY || "";

  const { searchParams } = new URL(req.url);
  const params = new URLSearchParams();
  const date = searchParams.get("date");
  const start = searchParams.get("start");
  const end = searchParams.get("end");
  if (date) params.set("date", date);
  if (start) params.set("start", start);
  if (end) params.set("end", end);
  const query = params.toString();

  const res = await fetch(`${backendUrl}/api/calendar-events${query ? `?${query}` : ""}`, {
    headers: {
      "Content-Type": "application/json",
      "X-API-Key": apiKey,
    },
  });

  const data = await res.json();
  return NextResponse.json(data, { status: res.status });
}

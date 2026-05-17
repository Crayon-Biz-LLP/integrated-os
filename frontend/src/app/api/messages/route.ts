import { NextRequest, NextResponse } from "next/server";

export async function GET(req: NextRequest) {
  const backendUrl = process.env.NEXT_PUBLIC_BACKEND_URL || "http://localhost:8000";
  const apiKey = process.env.API_SECRET_KEY || "";

  const { searchParams } = new URL(req.url);
  const limit = searchParams.get("limit") || "50";
  const offset = searchParams.get("offset") || "0";

  const res = await fetch(`${backendUrl}/api/messages?limit=${limit}&offset=${offset}`, {
    headers: {
      "Content-Type": "application/json",
      "X-API-Key": apiKey,
    },
  });

  const data = await res.json();
  return NextResponse.json(data, { status: res.status });
}

import { NextRequest, NextResponse } from "next/server";

export async function GET(request: NextRequest) {
  const backendUrl = process.env.NEXT_PUBLIC_BACKEND_URL || "http://localhost:8000";
  const { searchParams } = new URL(request.url);
  const q = searchParams.get("q");
  const type = searchParams.get("type");

  const apiKey = process.env.API_SECRET_KEY || "";
  
  let url = `${backendUrl}/api/graph-nodes/search?q=${encodeURIComponent(q || '')}`;
  if (type) {
    url += `&type=${encodeURIComponent(type)}`;
  }

  const res = await fetch(url, {
    method: "GET",
    headers: { "X-API-Key": apiKey },
  });

  const data = await res.json();
  return NextResponse.json(data, { status: res.status });
}
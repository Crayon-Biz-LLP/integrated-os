import { NextRequest, NextResponse } from "next/server";
import { errMsg } from "@/lib/errors";

export const dynamic = 'force-dynamic';

export async function GET(_request: NextRequest) {
  const backendUrl = process.env.NEXT_PUBLIC_BACKEND_URL || "http://localhost:8000";
  const apiKey = process.env.API_SECRET_KEY || "";

  try {
    const res = await fetch(`${backendUrl}/api/graph-nodes/live`, {
      headers: { "X-API-Key": apiKey },
      cache: 'no-store'
    });

    if (!res.ok) {
      const text = await res.text();
      return NextResponse.json(
        { error: `Backend returned ${res.status}`, details: text.substring(0, 200) },
        { status: res.status }
      );
    }

    const data = await res.json();
    return NextResponse.json(data, { status: res.status });
  } catch (error) {
    console.error("Proxy fetch error:", error);
    return NextResponse.json(
      { error: "Failed to connect to backend", details: errMsg(error) },
      { status: 500 }
    );
  }
}

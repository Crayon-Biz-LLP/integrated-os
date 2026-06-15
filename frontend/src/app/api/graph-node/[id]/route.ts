import { NextRequest, NextResponse } from "next/server";

export async function PUT(request: NextRequest, { params }: { params: { id: string } }) {
  const backendUrl = process.env.NEXT_PUBLIC_BACKEND_URL || "http://localhost:8000";
  const body = await request.json();
  const apiKey = process.env.API_SECRET_KEY || "";

  const res = await fetch(`${backendUrl}/api/graph-node/${params.id}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json", "X-API-Key": apiKey },
    body: JSON.stringify(body),
  });

  const data = await res.json();
  return NextResponse.json(data, { status: res.status });
}

export async function DELETE(request: NextRequest, { params }: { params: { id: string } }) {
  const backendUrl = process.env.NEXT_PUBLIC_BACKEND_URL || "http://localhost:8000";
  const apiKey = process.env.API_SECRET_KEY || "";

  const res = await fetch(`${backendUrl}/api/graph-node/${params.id}`, {
    method: "DELETE",
    headers: { "Content-Type": "application/json", "X-API-Key": apiKey },
  });

  const data = await res.json();
  return NextResponse.json(data, { status: res.status });
}

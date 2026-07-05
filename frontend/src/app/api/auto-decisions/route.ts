import { NextRequest, NextResponse } from "next/server";
import { createServerSupabaseClient } from "@/lib/supabase-server";

export async function GET(req: NextRequest) {
  if (!process.env.NEXT_PUBLIC_SUPABASE_URL || !process.env.SUPABASE_SERVICE_ROLE_KEY) {
    return NextResponse.json({ error: 'Missing Supabase environment variables' }, { status: 500 });
  }

  try {
    const { searchParams } = new URL(req.url);
    const supabase = await createServerSupabaseClient();

    const limit = Math.min(Number(searchParams.get("limit")) || 100, 500);
    const includeVerified = searchParams.get("include_verified") === "true";

    let query = supabase
      .from("decisions")
      .select("*")
      .eq("auto_decided", true)
      .order("decided_at", { ascending: false })
      .limit(limit);

    if (!includeVerified) {
      query = query.is("verified_at", null);
    }

    const { data, error } = await query;

    if (error) {
      console.error("Supabase error fetching auto-decisions:", error);
      return NextResponse.json({ error: error.message }, { status: 500 });
    }

    return NextResponse.json(data ?? []);
  } catch (err: any) {
    console.error("Unexpected error in auto-decisions route:", err);
    return NextResponse.json({ error: err.message || "Internal server error" }, { status: 500 });
  }
}

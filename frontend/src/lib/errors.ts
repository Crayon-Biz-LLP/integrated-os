/**
 * Shared error-message extraction.
 *
 * `catch` bindings are `unknown` under `strict` — this helper narrows them to
 * a safe string without forcing every handler to repeat instanceof checks.
 */
export function errMsg(err: unknown, fallback = "Something went wrong"): string {
  if (err instanceof Error) return err.message;
  if (typeof err === "string") return err;
  if (err && typeof err === "object" && "message" in err) {
    const msg = (err as { message?: unknown }).message;
    if (typeof msg === "string") return msg;
  }
  return fallback;
}

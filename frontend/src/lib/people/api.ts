import { Person, PersonTask, PersonAlias } from "./types";

export async function fetchPersonTasks(personId: number, personName: string): Promise<PersonTask[]> {
  const params = new URLSearchParams();
  params.set("name", personName);

  const res = await fetch(`/api/people/${personId}/tasks?${params.toString()}`, { cache: "no-store" });
  if (!res.ok) throw new Error("Failed to fetch person tasks");
  return res.json();
}

export async function updatePerson(id: number, data: { role?: string; strategic_weight?: number }): Promise<Person> {
  const res = await fetch(`/api/people/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  if (!res.ok) throw new Error("Failed to update person");
  return res.json();
}

// ── Aliases ────────────────────────────────────────────────────────────────

export async function fetchAliasesForPerson(canonicalName: string): Promise<PersonAlias[]> {
  const params = new URLSearchParams();
  // Fetch all aliases, filter client-side by canonical_name
  // We do client-side filtering because the FastAPI /api/aliases doesn't support
  // server-side filtering by canonical_name, and we don't want to add a new endpoint.
  const res = await fetch(`/api/aliases`, { cache: "no-store" });
  if (!res.ok) throw new Error("Failed to fetch aliases");
  const data = await res.json();
  const aliases: PersonAlias[] = data.aliases || [];
  return aliases.filter((a) => a.canonical_name.toLowerCase() === canonicalName.toLowerCase());
}

export async function createAlias(alias: string, canonicalName: string): Promise<{ success: boolean; alias?: PersonAlias; message?: string }> {
  const res = await fetch(`/api/aliases`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ alias, canonical_name: canonicalName }),
  });
  return res.json();
}

export async function deleteAlias(aliasId: number): Promise<{ success: boolean; message?: string }> {
  const res = await fetch(`/api/aliases/${aliasId}`, {
    method: "DELETE",
  });
  return res.json();
}

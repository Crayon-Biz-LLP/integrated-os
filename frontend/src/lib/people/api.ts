import { Person, PersonTask } from "./types";

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

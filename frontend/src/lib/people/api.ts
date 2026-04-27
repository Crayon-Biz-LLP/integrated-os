import { Person, PersonTask, PeopleStats, PeopleFilters } from "./types";

export async function fetchPeople(filters?: PeopleFilters): Promise<Person[]> {
  const params = new URLSearchParams();
  if (filters?.search) params.set("search", filters.search);
  if (filters?.tier) params.set("tier", filters.tier);
  if (filters?.sort) params.set("sort", filters.sort);

  const res = await fetch(`/api/people?${params.toString()}`, { cache: "no-store" });
  if (!res.ok) throw new Error("Failed to fetch people");
  return res.json();
}

export async function fetchPeopleStats(): Promise<PeopleStats> {
  const res = await fetch(`/api/people/stats`, { cache: "no-store" });
  if (!res.ok) throw new Error("Failed to fetch people stats");
  return res.json();
}

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

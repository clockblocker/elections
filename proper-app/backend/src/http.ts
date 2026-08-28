export function json(value: unknown, init: ResponseInit = {}): Response {
  const body = JSON.stringify(value, (_, item) => typeof item === "bigint" ? item.toString() : item);
  return new Response(body, {
    ...init,
    headers: { "content-type": "application/json; charset=utf-8", ...init.headers }
  });
}

export function errorResponse(status: number, message: string): Response {
  return json({ error: message }, { status });
}

export function parseId(value: string | null, label: string): number {
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed) || parsed <= 0) throw new Error(`${label} must be a positive integer`);
  return parsed;
}

export function regionFilters(search: URLSearchParams): string[] {
  return search.getAll("region").flatMap((value) => value.split(","))
    .map((value) => value.trim()).filter(Boolean)
    .filter((value, index, all) => all.indexOf(value) === index);
}

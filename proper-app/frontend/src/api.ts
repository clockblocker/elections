import type { Metadata, Point, ProtocolDetail } from "./types";

async function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(path, { signal, headers: { accept: "application/json" } });
  const body = await response.json() as T & { error?: string };
  if (!response.ok) throw new Error(body.error ?? `Request failed with status ${response.status}`);
  return body;
}

export const api = {
  metadata: (signal?: AbortSignal) => get<Metadata>("/api/elections/2021-duma/meta", signal),
  points: async (option: number, region: string | null, signal?: AbortSignal) => {
    const search = new URLSearchParams({ option: String(option) });
    if (region) search.set("region", region);
    return (await get<{ points: Point[] }>(`/api/elections/2021-duma/points?${search}`, signal)).points;
  },
  protocol: (id: string, signal?: AbortSignal) => get<ProtocolDetail>(`/api/elections/2021-duma/protocols/${encodeURIComponent(id)}`, signal)
};

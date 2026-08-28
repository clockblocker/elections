import type { ElectionSummary, Metadata, Point, ProtocolDetail } from "./types";

async function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(path, { signal, headers: { accept: "application/json" } });
  const body = await response.json() as T & { error?: string };
  if (!response.ok) throw new Error(body.error ?? `Request failed with status ${response.status}`);
  return body;
}

export const api = {
  elections: async (signal?: AbortSignal) => (await get<{ elections: ElectionSummary[] }>("/api/elections", signal)).elections,
  metadata: (election: string, signal?: AbortSignal) => get<Metadata>(`/api/elections/${encodeURIComponent(election)}/meta`, signal),
  points: async (election: string, option: number, region: string | null, signal?: AbortSignal) => {
    const search = new URLSearchParams({ option: String(option) });
    if (region) search.set("region", region);
    return (await get<{ points: Point[] }>(`/api/elections/${encodeURIComponent(election)}/points?${search}`, signal)).points;
  },
  protocol: (election: string, id: string, signal?: AbortSignal) => get<ProtocolDetail>(`/api/elections/${encodeURIComponent(election)}/protocols/${encodeURIComponent(id)}`, signal)
};

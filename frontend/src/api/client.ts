// The REST client, typed by the generated contract (src/api/types.gen.ts, plan M9.5).
// Every request carries the launch token; errors arrive as the server's ErrorBody.

import { authHeaders } from "../lib/auth";
import type { components, paths } from "./types.gen";

export type Schemas = components["schemas"];

type GetPath = { [P in keyof paths]: paths[P] extends { get: object } ? P : never }[keyof paths];
type PostPath = { [P in keyof paths]: paths[P] extends { post: object } ? P : never }[keyof paths];

type Json<R> = R extends { content: { "application/json": infer T } } ? T : never;
type GetResult<P extends GetPath> = paths[P] extends { get: { responses: { 200: infer R } } }
  ? Json<R>
  : never;
type PostResult<P extends PostPath> = paths[P] extends { post: { responses: { 200: infer R } } }
  ? Json<R>
  : never;
type PostBody<P extends PostPath> = paths[P] extends {
  post: { requestBody?: { content: { "application/json": infer B } } };
}
  ? B
  : never;

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
    readonly fields: string[] = [],
  ) {
    super(message);
  }
}

export type Query = Record<string, string | number | boolean | null | undefined>;

function url(path: string, params?: Record<string, string>, query?: Query): string {
  let resolved = path;
  for (const [key, value] of Object.entries(params ?? {})) {
    resolved = resolved.replace(`{${key}}`, encodeURIComponent(value));
  }
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value !== undefined && value !== null && value !== "") search.set(key, String(value));
  }
  const qs = search.toString();
  return qs ? `${resolved}?${qs}` : resolved;
}

async function parse<T>(res: Response): Promise<T> {
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const error = (body as { error?: string }).error ?? `HTTP ${res.status}`;
    throw new ApiError(res.status, error, (body as { fields?: string[] }).fields ?? []);
  }
  return body as T;
}

export async function get<P extends GetPath>(
  path: P,
  options: { params?: Record<string, string>; query?: Query; signal?: AbortSignal } = {},
): Promise<GetResult<P>> {
  const res = await fetch(url(path, options.params, options.query), {
    headers: authHeaders(),
    signal: options.signal,
  });
  return parse<GetResult<P>>(res);
}

export async function post<P extends PostPath>(path: P, body: PostBody<P>): Promise<PostResult<P>> {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(body ?? {}),
  });
  return parse<PostResult<P>>(res);
}

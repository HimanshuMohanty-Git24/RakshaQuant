// Per-launch access token (plan M9.1). The server prints a URL with `#token=...`; we keep the
// token in sessionStorage and strip it from the address bar so it is not bookmarked or shared.

const KEY = "rq.token";

export function bootstrapToken(): void {
  const match = window.location.hash.match(/token=([A-Za-z0-9_-]+)/);
  if (match) {
    sessionStorage.setItem(KEY, match[1]);
    history.replaceState(null, "", window.location.pathname + window.location.search);
  }
}

export function token(): string {
  return sessionStorage.getItem(KEY) ?? "";
}

export function authHeaders(): Record<string, string> {
  return { Authorization: `Bearer ${token()}` };
}

// Browsers cannot set headers on a WebSocket: the token travels as a subprotocol.
export function wsProtocols(): string[] {
  return ["rq.v1", `rq.token.${token()}`];
}

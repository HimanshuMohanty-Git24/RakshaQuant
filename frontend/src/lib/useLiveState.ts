import { useCallback, useEffect, useRef, useState } from "react";
import type { ConnState, CycleTrace, Snapshot, WsMessage } from "../types";
import { wsProtocols } from "./auth";

interface LiveState {
  snapshot: Snapshot | null;
  cycles: CycleTrace[];
  running: boolean;
  demo: boolean;
  conn: ConnState;
  error: string | null;
}

// Subscribes to /ws (the console snapshot, the summary and run notices) and reconnects with
// capped backoff. A disconnect surfaces as conn="reconnecting" (never a silent stall). Cycle
// traces are gone with the legacy pipeline; the M10 console replaces this hook.
export function useLiveState(): LiveState {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [cycles] = useState<CycleTrace[]>([]);
  const [running, setRunning] = useState(false);
  const [demo, setDemo] = useState(false);
  const [conn, setConn] = useState<ConnState>("connecting");
  const [error, setError] = useState<string | null>(null);

  const wsRef = useRef<WebSocket | null>(null);
  const retryRef = useRef(0);
  const timerRef = useRef<number | null>(null);
  const closedRef = useRef(false);

  const connect = useCallback(() => {
    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    const url = `${proto}://${window.location.host}/ws`;
    let ws: WebSocket;
    try {
      ws = new WebSocket(url, wsProtocols());
    } catch {
      scheduleReconnect();
      return;
    }
    wsRef.current = ws;

    ws.onopen = () => {
      retryRef.current = 0;
      setConn("open");
      setError(null);
      // Latest console snapshot + run notices; no replay of stored events (since_seq = max).
      ws.send(
        JSON.stringify({ subscribe: ["console", "summary", "system"], since_seq: Number.MAX_SAFE_INTEGER }),
      );
    };

    ws.onmessage = (ev) => {
      let msg: WsMessage;
      try {
        msg = JSON.parse(ev.data) as WsMessage;
      } catch {
        return;
      }
      switch (msg.type) {
        case "console":
          setSnapshot(msg.data);
          setRunning(msg.data.run.status === "RUNNING");
          break;
        case "summary":
          setDemo(msg.data.demo);
          setRunning(msg.data.running);
          break;
        case "stopped":
          setRunning(false);
          break;
        case "error":
          setError(msg.data.message);
          break;
      }
    };

    ws.onclose = () => {
      if (closedRef.current) return;
      scheduleReconnect();
    };

    ws.onerror = () => {
      ws.close();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const scheduleReconnect = useCallback(() => {
    if (closedRef.current) return;
    setConn("reconnecting");
    retryRef.current = Math.min(retryRef.current + 1, 6);
    const delay = Math.min(1000 * 2 ** (retryRef.current - 1), 8000);
    timerRef.current = window.setTimeout(() => connect(), delay);
  }, [connect]);

  useEffect(() => {
    closedRef.current = false;
    connect();
    return () => {
      closedRef.current = true;
      if (timerRef.current) window.clearTimeout(timerRef.current);
      wsRef.current?.close();
    };
  }, [connect]);

  return { snapshot, cycles, running, demo, conn, error };
}

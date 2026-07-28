import { useCallback, useEffect, useRef, useState } from "react";
import type { ConnState, CycleTrace, Snapshot, WsMessage } from "../types";

interface LiveState {
  snapshot: Snapshot | null;
  cycles: CycleTrace[];
  running: boolean;
  demo: boolean;
  conn: ConnState;
  error: string | null;
}

const MAX_CYCLES = 300;

// Subscribes to /ws, keeps the latest snapshot + a rolling list of cycle traces, and
// reconnects with capped backoff. A disconnect surfaces as conn="reconnecting" (never a
// silent stall); on reconnect the server replays an "init" frame so state resumes cleanly.
export function useLiveState(): LiveState {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [cycles, setCycles] = useState<CycleTrace[]>([]);
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
      ws = new WebSocket(url);
    } catch {
      scheduleReconnect();
      return;
    }
    wsRef.current = ws;

    ws.onopen = () => {
      retryRef.current = 0;
      setConn("open");
      setError(null);
    };

    ws.onmessage = (ev) => {
      let msg: WsMessage;
      try {
        msg = JSON.parse(ev.data) as WsMessage;
      } catch {
        return;
      }
      switch (msg.type) {
        case "init":
          if (msg.snapshot) setSnapshot(msg.snapshot);
          setCycles(msg.cycles ?? []);
          setRunning(msg.running);
          setDemo(msg.demo);
          break;
        case "snapshot":
          setSnapshot(msg.data);
          setRunning(msg.data.run.status === "RUNNING");
          break;
        case "cycle":
          setCycles((prev) => {
            const next = [...prev, msg.data];
            return next.length > MAX_CYCLES ? next.slice(-MAX_CYCLES) : next;
          });
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

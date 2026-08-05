import { useEffect, useState } from "react";
import { healthz, type HealthReport } from "../lib/api/client";
import { HEALTH_POLL_MS } from "../config";

export function useHealth() {
  const [report, setReport] = useState<HealthReport | null>(null);

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      const r = await healthz();
      if (alive) setReport(r);
    };
    void tick();
    const id = setInterval(tick, HEALTH_POLL_MS);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  return report;
}

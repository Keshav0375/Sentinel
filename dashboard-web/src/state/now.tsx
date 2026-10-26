// One shared 30 s clock for every "waiting 4 h 20 m" / "3 h ago" on the page.
import { createContext, useContext, useEffect, useState, type ReactNode } from 'react';

const TICK_MS = 30_000;

const NowContext = createContext<Date | null>(null);

export function NowProvider({ children }: { children: ReactNode }) {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const id = window.setInterval(() => setNow(new Date()), TICK_MS);
    return () => window.clearInterval(id);
  }, []);
  return <NowContext.Provider value={now}>{children}</NowContext.Provider>;
}

export function useNow(): Date {
  const now = useContext(NowContext);
  if (!now) throw new Error('useNow must be used inside <NowProvider>');
  return now;
}

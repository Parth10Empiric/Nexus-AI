import { useUptime } from "../hooks/useUptime.js";

/**
 * StatusIndicator — shows tracker connectivity + a live session uptime clock.
 *
 * Props:
 *   status -> "online" | "offline"
 *
 * Layout note: the dot, label, and clock all sit in a fixed-height row with
 * tabular-nums on the clock, so the digits never change width and the layout
 * never shifts as the seconds tick.
 */
export default function StatusIndicator({ status }) {
  const uptime = useUptime();
  const online = status === "online";

  return (
    <div className="flex items-center gap-4 text-sm">
      {/* Connectivity pill */}
      <div className="flex items-center gap-2">
        <span className="relative flex h-2.5 w-2.5">
          {online && (
            <span className="absolute inline-flex h-full w-full rounded-full bg-nexus-online opacity-60 animate-pulse-slow" />
          )}
          <span
            className={`relative inline-flex h-2.5 w-2.5 rounded-full ${
              online ? "bg-nexus-online" : "bg-nexus-offline"
            }`}
          />
        </span>
        <span
          className={`font-medium tracking-wide ${
            online ? "text-nexus-online" : "text-nexus-offline"
          }`}
        >
          {online ? "ONLINE" : "OFFLINE"}
        </span>
      </div>

      {/* Divider */}
      <span className="h-4 w-px bg-nexus-border" aria-hidden="true" />

      {/* Uptime clock */}
      <div className="flex items-center gap-2 text-nexus-muted">
        <span className="text-nexus-faint">UPTIME</span>
        <span className="font-mono tabular-nums text-nexus-text">{uptime}</span>
      </div>
    </div>
  );
}

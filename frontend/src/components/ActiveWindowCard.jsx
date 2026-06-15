/**
 * ActiveWindowCard — the centerpiece. Displays what the developer is currently
 * working on (application + window/file title + when it was observed).
 *
 * Props:
 *   entry -> { appName, title, timestamp, event } | null
 *
 * THE LAYOUT-STABILITY CONTRACT:
 *   - The card has a FIXED min-height, so an empty/loading state and a fully
 *     populated state occupy exactly the same space.
 *   - The title row uses `truncate-line` (overflow-hidden + ellipsis) inside a
 *     `min-w-0` flex child. No matter how absurdly long the file path is, the
 *     text clips to one line and the card never grows or reflows.
 *   - We attach the full title as a native `title` tooltip so nothing is lost.
 */

const APP_LABELS = {
  code: "VS Code",
  "gnome-terminal": "Terminal",
  chrome: "Chrome",
  firefox: "Firefox",
  unknown: "Unknown App",
};

function prettyApp(name) {
  if (!name) return "—";
  return APP_LABELS[name] || name;
}

function formatClock(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

// Derive a stable monogram for the app avatar (no images -> no asset loading).
function monogram(name) {
  if (!name) return "?";
  return name.replace(/[^a-z0-9]/gi, "").slice(0, 2).toUpperCase() || "?";
}

export default function ActiveWindowCard({ entry }) {
  const hasData = Boolean(entry);
  const isHeartbeat = entry?.event === "heartbeat";

  return (
    <section
      className="nexus-card p-6 transition-colors duration-200 ease-nexus hover:border-nexus-borderStrong"
      style={{ minHeight: "168px" }}
    >
      <header className="flex items-center justify-between mb-5">
        <h2 className="text-xs font-semibold uppercase tracking-[0.15em] text-nexus-faint">
          Active Window
        </h2>
        {hasData && (
          <span
            className={`text-[10px] font-medium uppercase tracking-wider px-2 py-0.5 rounded-full border ${
              isHeartbeat
                ? "text-nexus-muted border-nexus-border"
                : "text-nexus-accent border-nexus-accent/40 bg-nexus-accent/10"
            }`}
          >
            {isHeartbeat ? "idle ping" : "switched"}
          </span>
        )}
      </header>

      {/* Body: avatar + text column. min-w-0 lets the text column shrink so
          ellipsis works instead of pushing the avatar off-screen. */}
      <div className="flex items-center gap-4">
        <div
          className={`flex-shrink-0 grid place-items-center h-12 w-12 rounded-lg font-mono text-sm font-semibold transition-transform duration-200 ease-nexus group-hover:scale-105 ${
            hasData
              ? "bg-nexus-elevated text-nexus-accent"
              : "bg-nexus-elevated text-nexus-faint"
          }`}
          aria-hidden="true"
        >
          {monogram(entry?.appName)}
        </div>

        <div className="min-w-0 flex-1">
          {/* App name line */}
          <div className="flex items-baseline gap-2">
            <span className="text-base font-semibold text-nexus-text truncate-line">
              {hasData ? prettyApp(entry.appName) : "Waiting for tracker…"}
            </span>
          </div>

          {/* Title line — the stress test for long paths. */}
          <p
            className="mt-1 text-sm text-nexus-muted truncate-line font-mono"
            title={entry?.title || ""}
          >
            {hasData ? entry.title : "No active window detected yet."}
          </p>

          {/* Timestamp line */}
          <div className="mt-3 flex items-center gap-2 text-xs text-nexus-faint">
            <span className="font-mono tabular-nums">
              {formatClock(entry?.timestamp)}
            </span>
            <span className="h-1 w-1 rounded-full bg-nexus-faint" />
            <span>local time</span>
          </div>
        </div>
      </div>
    </section>
  );
}

/**
 * PulseHistoryPanel — shows the last N nudges Foundry tried to send the
 * user via Telegram. Rendered under the TelegramConnectCard so the user
 * can audit what the proactive CoS flagged (even if they missed the
 * Telegram ping or the bot was blocked).
 *
 * Each row shows: org label · kind icon · text preview · time · delivery
 * status (✓ delivered, ✗ failed-to-deliver with the inline error).
 */
import { useEffect, useState } from "react";
import api from "../lib/api";
import { Bell, Check, X, Loader2, AlertTriangle, RefreshCw, FileText, CheckCircle2, XCircle, Rocket, Send } from "lucide-react";

// Map each Pulse `kind` to an icon + colour so the row is scannable.
const KIND_META = {
  mission_succeeded:  { icon: CheckCircle2, color: "text-emerald-300", label: "Mission succeeded" },
  mission_failed:     { icon: XCircle,      color: "text-rose-300",    label: "Mission failed" },
  post_failed:        { icon: AlertTriangle,color: "text-rose-300",    label: "Post failed" },
  post_partial:       { icon: AlertTriangle,color: "text-amber-300",   label: "Post partial" },
  post_published:     { icon: Send,         color: "text-blue-300",    label: "Post published" },
  draft_stalled:      { icon: FileText,     color: "text-amber-300",   label: "Draft stalled" },
};

function timeAgo(iso) {
  if (!iso) return "";
  const t = new Date(iso).getTime();
  const sec = Math.max(0, Math.floor((Date.now() - t) / 1000));
  if (sec < 60) return `${sec}s ago`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`;
  return `${Math.floor(sec / 86400)}d ago`;
}

export default function PulseHistoryPanel() {
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);

  const load = async () => {
    try {
      const { data } = await api.get("/telegram/pulse-history?limit=10");
      setItems(data?.items || []);
    } catch (err) {
      console.error("pulse-history failed", err);
      setItems([]);
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  };

  useEffect(() => { load(); /* eslint-disable-next-line */ }, []);

  if (loading) {
    return (
      <section className="sp-glass rounded-2xl p-5 mt-4" data-testid="pulse-history-panel">
        <div className="text-sm sp-subtle inline-flex items-center gap-2">
          <Loader2 className="w-3 h-3 animate-spin" /> Loading recent pulses…
        </div>
      </section>
    );
  }

  return (
    <section className="sp-glass rounded-2xl p-5 mt-4" data-testid="pulse-history-panel">
      <div className="flex items-center justify-between gap-3 mb-3">
        <h3 className="text-sm font-medium inline-flex items-center gap-2">
          <Bell className="w-4 h-4" /> Pulse history
          <span className="sp-chip text-[10px] !bg-white/5">last 10</span>
        </h3>
        <button
          type="button"
          onClick={() => { setRefreshing(true); load(); }}
          className="text-[11px] sp-subtle hover:opacity-100 inline-flex items-center gap-1"
          data-testid="pulse-history-refresh"
        >
          {refreshing ? <Loader2 className="w-3 h-3 animate-spin" /> : <RefreshCw className="w-3 h-3" />} Refresh
        </button>
      </div>

      {items.length === 0 ? (
        <p className="text-xs sp-subtle">
          Nothing flagged yet. The hourly Pulse sweep runs once an hour — if any of
          your missions, posts, or drafts triggers a nudge, it'll land here and (if
          connected) in Telegram.
        </p>
      ) : (
        <ul className="space-y-2" data-testid="pulse-history-list">
          {items.map((row, idx) => {
            const meta = KIND_META[row.kind] || { icon: Bell, color: "text-white/60", label: row.kind };
            const Icon = meta.icon;
            return (
              <li
                key={`${row.ref_id}-${idx}`}
                className="rounded-lg p-3 border border-white/5 bg-white/[0.02]"
                data-testid={`pulse-history-row-${idx}`}
              >
                <div className="flex items-start gap-2 mb-1">
                  <Icon className={`w-3.5 h-3.5 mt-0.5 ${meta.color} shrink-0`} />
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 text-[11px] sp-subtle mb-0.5 flex-wrap">
                      <span className="font-medium text-white/80">{meta.label}</span>
                      {row.org_name && <span className="opacity-60">·</span>}
                      {row.org_name && <span>{row.org_name}</span>}
                      <span className="opacity-60">·</span>
                      <span>{timeAgo(row.at)}</span>
                    </div>
                    <p className="text-xs leading-snug text-white/85 break-words">
                      {/* Strip the [OrgName] prefix from the preview since
                          we already show the org name in the header row. */}
                      {(row.text_preview || "").replace(/^\[[^\]]+\]\s*/, "")}
                    </p>
                  </div>
                  <span
                    className={`text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded ${
                      row.delivered
                        ? "bg-emerald-500/15 text-emerald-300"
                        : "bg-rose-500/15 text-rose-300"
                    }`}
                    title={row.error || ""}
                  >
                    {row.delivered
                      ? (<span className="inline-flex items-center gap-1"><Check className="w-2.5 h-2.5" />Sent</span>)
                      : (<span className="inline-flex items-center gap-1"><X className="w-2.5 h-2.5" />Failed</span>)}
                  </span>
                </div>
                {!row.delivered && row.error && (
                  <div className="text-[11px] text-rose-300/80 pl-5 italic" data-testid={`pulse-history-error-${idx}`}>
                    {row.error}
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

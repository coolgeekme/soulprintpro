/**
 * TelegramConnectCard — user-level Telegram bot connection.
 *
 * Two states:
 *   • Disconnected  →  "Connect Telegram" → opens the t.me deep link.
 *                      We poll /status every 2s so the card flips
 *                      to "Connected" the moment the user taps /start
 *                      in Telegram.
 *   • Connected    →  shows handle + manage notifications + test send +
 *                      disconnect.
 *
 * Why per-user (not per-org)? Telegram is the user's inbox. Every org
 * the user belongs to fires nudges into the same chat, each tagged
 * with its `[OrgName]` prefix — so the user keeps one place for
 * everything, but data isolation between orgs is preserved.
 */
import { useEffect, useRef, useState } from "react";
import api from "../lib/api";
import { toast } from "sonner";
import { MessageCircle, Loader2, Send, LinkIcon, Check, Trash2, ExternalLink, Bell, BellOff, AlertTriangle } from "lucide-react";

export default function TelegramConnectCard() {
  const [status, setStatus] = useState(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [deepLink, setDeepLink] = useState("");
  const [waitingForTap, setWaitingForTap] = useState(false);
  const pollRef = useRef(null);

  const load = async () => {
    try {
      const { data } = await api.get("/telegram/status");
      setStatus(data);
      if (data.connected && waitingForTap) {
        setWaitingForTap(false);
        setDeepLink("");
        toast.success(`Connected to @${data.username || "Telegram"}`);
      }
    } catch (err) {
      console.error("telegram/status failed", err);
    } finally {
      setLoading(false);
    }
  };

  // First load + cleanup on unmount.
  useEffect(() => {
    load();
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // While we're waiting for the user to tap the deep link in Telegram,
  // poll status every 2s so the UI flips the moment the webhook fires.
  // 5-minute ceiling so we don't poll forever if the user walked away.
  useEffect(() => {
    if (!waitingForTap) return;
    let elapsed = 0;
    pollRef.current = setInterval(async () => {
      elapsed += 2;
      await load();
      if (elapsed >= 300) {
        setWaitingForTap(false);
        clearInterval(pollRef.current);
      }
    }, 2000);
    return () => clearInterval(pollRef.current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [waitingForTap]);

  const startConnect = async () => {
    setBusy(true);
    try {
      const { data } = await api.post("/telegram/initiate");
      setDeepLink(data.deeplink);
      setWaitingForTap(true);
      // Open in a new tab so users on desktop land in Telegram Web
      // and mobile users land in their native Telegram app.
      window.open(data.deeplink, "_blank", "noopener,noreferrer");
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Couldn't start Telegram connection");
    } finally {
      setBusy(false);
    }
  };

  const disconnect = async () => {
    if (!window.confirm("Disconnect Telegram? You'll stop receiving Chief of Staff nudges in Telegram until you reconnect.")) return;
    setBusy(true);
    try {
      await api.post("/telegram/disconnect");
      toast.success("Disconnected");
      await load();
    } catch (err) {
      toast.error("Disconnect failed");
    } finally {
      setBusy(false);
    }
  };

  const testSend = async () => {
    setBusy(true);
    try {
      await api.post("/telegram/test-send");
      toast.success("Sent! Check Telegram.");
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Send failed");
    } finally {
      setBusy(false);
    }
  };

  const togglePref = async (key) => {
    if (!status?.preferences) return;
    const next = !status.preferences[key];
    setBusy(true);
    try {
      await api.patch("/telegram/preferences", { [key]: next });
      await load();
    } catch (err) {
      toast.error("Preference update failed");
    } finally {
      setBusy(false);
    }
  };

  if (loading) {
    return (
      <section className="sp-glass rounded-2xl p-5 mt-6" data-testid="telegram-card">
        <div className="text-sm sp-subtle inline-flex items-center gap-2">
          <Loader2 className="w-3 h-3 animate-spin" /> Loading Telegram status…
        </div>
      </section>
    );
  }

  if (!status?.configured) {
    return (
      <section className="sp-glass rounded-2xl p-5 mt-6" data-testid="telegram-card">
        <div className="flex items-start gap-3">
          <AlertTriangle className="w-4 h-4 mt-0.5 text-amber-400" />
          <div className="text-sm">
            <div className="font-medium">Telegram not configured</div>
            <div className="sp-subtle text-xs mt-1">
              Set <code className="px-1 rounded bg-white/10">TELEGRAM_BOT_TOKEN</code> and{" "}
              <code className="px-1 rounded bg-white/10">TELEGRAM_BOT_USERNAME</code> in the backend env.
            </div>
          </div>
        </div>
      </section>
    );
  }

  const connected = Boolean(status.connected);
  const prefs = status.preferences || {};

  return (
    <section className="sp-glass rounded-2xl p-5 mt-6" data-testid="telegram-card">
      <div className="flex items-start justify-between gap-3 mb-3">
        <div>
          <h2 className="text-sm font-medium flex items-center gap-2">
            <MessageCircle className="w-4 h-4" /> Telegram &mdash; proactive Chief of Staff
          </h2>
          <p className="text-xs sp-subtle mt-1 max-w-prose">
            Get actionable nudges from your Chief of Staff in Telegram: mission outcomes,
            social-post failures, stalled drafts, more. Every org you belong to fires into
            the same chat, tagged so you always know which workspace is talking.
          </p>
        </div>
        {connected ? (
          <span className="sp-chip text-[11px] !bg-emerald-500/15 !text-emerald-300 whitespace-nowrap" data-testid="telegram-connected-badge">
            <Check className="w-3 h-3 inline -mt-0.5" /> Connected
          </span>
        ) : (
          <span className="sp-chip text-[11px] !bg-white/5 whitespace-nowrap">Not connected</span>
        )}
      </div>

      {!connected && !waitingForTap && (
        <button
          type="button"
          onClick={startConnect}
          disabled={busy}
          className="sp-btn sp-btn-primary text-sm"
          data-testid="telegram-connect-btn"
        >
          {busy ? <Loader2 className="w-3 h-3 animate-spin" /> : <LinkIcon className="w-3 h-3" />}
          Connect to @{status.bot_username}
        </button>
      )}

      {!connected && waitingForTap && (
        <div className="space-y-3" data-testid="telegram-waiting">
          <div className="text-sm flex items-center gap-2 text-amber-300">
            <Loader2 className="w-3 h-3 animate-spin" />
            Waiting for you to tap <strong>Start</strong> in Telegram…
          </div>
          <div className="text-xs sp-subtle">
            We opened the chat in a new tab. If nothing happened, tap the link manually:
          </div>
          <a
            href={deepLink}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1 text-xs underline text-orange-300"
            data-testid="telegram-deeplink"
          >
            <ExternalLink className="w-3 h-3" /> {deepLink}
          </a>
          <div>
            <button
              type="button"
              onClick={() => setWaitingForTap(false)}
              className="text-xs sp-subtle underline"
              data-testid="telegram-cancel-wait"
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {connected && (
        <div className="space-y-4" data-testid="telegram-connected-panel">
          <div className="text-xs sp-subtle">
            Connected as <strong>@{status.username || "you"}</strong>
            {status.connected_at && <> · since {new Date(status.connected_at).toLocaleDateString()}</>}
          </div>

          <div className="grid sm:grid-cols-2 gap-3">
            <PrefRow
              label="Proactive nudges"
              description="Mission outcomes, post failures, stalled drafts"
              enabled={prefs.outbound_enabled !== false}
              onToggle={() => togglePref("outbound_enabled")}
              testId="telegram-pref-outbound"
            />
            <PrefRow
              label="Weekly digest"
              description="Sunday-evening recap from each org's CoS"
              enabled={prefs.digest_enabled !== false}
              onToggle={() => togglePref("digest_enabled")}
              testId="telegram-pref-digest"
            />
          </div>

          <div className="flex items-center gap-2 pt-2">
            <button
              type="button"
              onClick={testSend}
              disabled={busy}
              className="sp-btn sp-btn-ghost text-xs"
              data-testid="telegram-test-btn"
            >
              {busy ? <Loader2 className="w-3 h-3 animate-spin" /> : <Send className="w-3 h-3" />}
              Send test message
            </button>
            <button
              type="button"
              onClick={disconnect}
              disabled={busy}
              className="sp-btn sp-btn-ghost text-xs hover:!text-red-300"
              data-testid="telegram-disconnect-btn"
            >
              <Trash2 className="w-3 h-3" /> Disconnect
            </button>
          </div>
        </div>
      )}
    </section>
  );
}

function PrefRow({ label, description, enabled, onToggle, testId }) {
  return (
    <button
      type="button"
      onClick={onToggle}
      className={`text-left rounded-xl p-3 border transition ${
        enabled ? "border-emerald-400/30 bg-emerald-400/[0.04]" : "border-white/10 hover:bg-white/5"
      }`}
      data-testid={testId}
    >
      <div className="flex items-center justify-between gap-2 mb-1">
        <span className="text-xs font-medium">{label}</span>
        {enabled ? (
          <Bell className="w-3 h-3 text-emerald-300" />
        ) : (
          <BellOff className="w-3 h-3 sp-subtle" />
        )}
      </div>
      <div className="text-[11px] sp-subtle leading-snug">{description}</div>
    </button>
  );
}

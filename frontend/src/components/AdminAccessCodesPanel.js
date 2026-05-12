import React, { useEffect, useState } from "react";
import { Loader2, Plus, Copy, Trash2, Check, KeyRound } from "lucide-react";
import { toast } from "sonner";
import api from "../lib/api";
import { parseServerDate, formatLocalDate } from "../lib/datetime";

/**
 * AdminAccessCodesPanel — superadmin tab for the pre-launch site
 * access gate. Lists all access codes, lets the admin mint new ones
 * (single or multi-use, with optional expiry), and revoke leaked
 * codes. Revoking is soft (is_active=false) so the audit trail
 * survives.
 */

function CodeRow({ row, onRevoke }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(row.code);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch (_) {
      toast.error("Couldn't copy. Select the code manually.");
    }
  };
  const expired =
    row.expires_at && (parseServerDate(row.expires_at)?.getTime() || 0) < Date.now();
  const exhausted =
    row.usage_type === "single"
      ? row.used_count >= 1
      : row.max_uses != null && row.used_count >= row.max_uses;
  const status = !row.is_active
    ? "revoked"
    : expired
    ? "expired"
    : exhausted
    ? "spent"
    : "active";
  const statusColor = {
    active: "text-emerald-400",
    spent: "text-amber-400",
    expired: "text-amber-400",
    revoked: "text-red-400",
  }[status];

  const usageLabel =
    row.usage_type === "single"
      ? "single-use"
      : row.max_uses != null
      ? `multi · ${row.used_count}/${row.max_uses}`
      : `multi · ${row.used_count} used`;

  return (
    <tr className="border-b border-white/5" data-testid={`access-code-row-${row.id}`}>
      <td className="py-3 pr-4">
        <div className="flex items-center gap-2">
          <code className="font-mono text-sm tracking-widest text-white">
            {row.code}
          </code>
          <button
            type="button"
            onClick={copy}
            className="text-xs sp-subtle hover:text-white inline-flex items-center gap-1"
            title="Copy code"
            data-testid={`access-code-copy-${row.id}`}
          >
            {copied ? (
              <Check className="w-3 h-3 text-emerald-400" />
            ) : (
              <Copy className="w-3 h-3" />
            )}
          </button>
        </div>
        {row.note && (
          <div className="text-xs sp-subtle mt-1 max-w-[260px] truncate" title={row.note}>
            {row.note}
          </div>
        )}
      </td>
      <td className="py-3 pr-4 text-xs sp-subtle">{usageLabel}</td>
      <td className="py-3 pr-4 text-xs sp-subtle">
        {row.expires_at
          ? formatLocalDate(row.expires_at)
          : "never"}
      </td>
      <td className="py-3 pr-4 text-xs sp-subtle">
        {row.created_at
          ? formatLocalDate(row.created_at)
          : "—"}
      </td>
      <td className={`py-3 pr-4 text-xs uppercase tracking-widest ${statusColor}`}>
        {status}
      </td>
      <td className="py-3 pr-4 text-right">
        {row.is_active && (
          <button
            type="button"
            onClick={() => onRevoke(row)}
            className="text-xs text-red-300 hover:text-red-200 inline-flex items-center gap-1"
            data-testid={`access-code-revoke-${row.id}`}
          >
            <Trash2 className="w-3 h-3" /> Revoke
          </button>
        )}
      </td>
    </tr>
  );
}

function CreateCodeForm({ onCreated }) {
  const [open, setOpen] = useState(false);
  const [usageType, setUsageType] = useState("single");
  const [maxUses, setMaxUses] = useState("");
  const [expiresAt, setExpiresAt] = useState("");
  const [note, setNote] = useState("");
  const [customCode, setCustomCode] = useState("");
  const [busy, setBusy] = useState(false);

  const reset = () => {
    setUsageType("single");
    setMaxUses("");
    setExpiresAt("");
    setNote("");
    setCustomCode("");
  };

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    try {
      const body = {
        usage_type: usageType,
        note: note.trim() || null,
      };
      if (customCode.trim()) body.code = customCode.trim();
      if (usageType === "multi" && maxUses) {
        body.max_uses = Number(maxUses) || null;
      }
      if (expiresAt) {
        // datetime-local gives us "YYYY-MM-DDTHH:mm" in local time;
        // convert to ISO UTC.
        body.expires_at = new Date(expiresAt).toISOString();
      }
      const { data } = await api.post("/admin/access-codes", body);
      toast.success(`Code ${data.code} created.`);
      reset();
      setOpen(false);
      onCreated?.(data);
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Couldn't create code.");
    } finally {
      setBusy(false);
    }
  };

  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="sp-btn sp-btn-primary text-xs"
        data-testid="access-code-new-toggle"
      >
        <Plus className="w-3.5 h-3.5" /> New access code
      </button>
    );
  }

  return (
    <form
      onSubmit={submit}
      className="sp-glass rounded-2xl p-5 mb-4"
      data-testid="access-code-create-form"
    >
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        <div>
          <label className="text-xs sp-subtle uppercase tracking-widest">
            Usage
          </label>
          <select
            value={usageType}
            onChange={(e) => setUsageType(e.target.value)}
            className="w-full mt-1 bg-white/[0.03] border border-white/10 rounded-xl px-3 py-2 text-sm text-white"
            data-testid="access-code-usage-type"
          >
            <option value="single">Single-use (one redemption)</option>
            <option value="multi">Multi-use (shared cohort)</option>
          </select>
        </div>
        {usageType === "multi" && (
          <div>
            <label className="text-xs sp-subtle uppercase tracking-widest">
              Max uses (blank = unlimited)
            </label>
            <input
              type="number"
              min={1}
              value={maxUses}
              onChange={(e) => setMaxUses(e.target.value)}
              placeholder="e.g. 50"
              className="w-full mt-1 bg-white/[0.03] border border-white/10 rounded-xl px-3 py-2 text-sm text-white"
              data-testid="access-code-max-uses"
            />
          </div>
        )}
        <div>
          <label className="text-xs sp-subtle uppercase tracking-widest">
            Expires (optional)
          </label>
          <input
            type="datetime-local"
            value={expiresAt}
            onChange={(e) => setExpiresAt(e.target.value)}
            className="w-full mt-1 bg-white/[0.03] border border-white/10 rounded-xl px-3 py-2 text-sm text-white"
            data-testid="access-code-expires"
          />
        </div>
        <div>
          <label className="text-xs sp-subtle uppercase tracking-widest">
            Custom code (optional)
          </label>
          <input
            value={customCode}
            onChange={(e) => setCustomCode(e.target.value.toUpperCase())}
            placeholder="auto-generated if blank"
            className="w-full mt-1 bg-white/[0.03] border border-white/10 rounded-xl px-3 py-2 text-sm text-white font-mono tracking-widest"
            maxLength={40}
            data-testid="access-code-custom"
          />
        </div>
        <div className="md:col-span-2">
          <label className="text-xs sp-subtle uppercase tracking-widest">
            Note (who is this for?)
          </label>
          <input
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="e.g. Beta cohort — Acme Inc"
            className="w-full mt-1 bg-white/[0.03] border border-white/10 rounded-xl px-3 py-2 text-sm text-white"
            maxLength={240}
            data-testid="access-code-note"
          />
        </div>
      </div>
      <div className="flex items-center gap-2 mt-4">
        <button
          type="submit"
          disabled={busy}
          className="sp-btn sp-btn-primary text-xs disabled:opacity-60"
          data-testid="access-code-create-submit"
        >
          {busy ? (
            <span className="inline-flex items-center gap-2">
              <Loader2 className="w-3 h-3 animate-spin" /> Creating
            </span>
          ) : (
            "Create code"
          )}
        </button>
        <button
          type="button"
          onClick={() => { reset(); setOpen(false); }}
          className="sp-btn sp-btn-ghost text-xs"
        >
          Cancel
        </button>
      </div>
    </form>
  );
}

export default function AdminAccessCodesPanel() {
  const [data, setData] = useState(null);

  const load = async () => {
    setData(null);
    try {
      const { data: d } = await api.get("/admin/access-codes?page_size=200");
      setData(d);
    } catch (_) {
      setData({ rows: [] });
    }
  };

  useEffect(() => { load(); }, []);

  const onRevoke = async (row) => {
    if (!window.confirm(`Revoke code ${row.code}? This is permanent.`)) return;
    try {
      await api.delete(`/admin/access-codes/${row.id}`);
      toast.success("Code revoked.");
      load();
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Couldn't revoke.");
    }
  };

  if (!data) {
    return (
      <div className="sp-subtle text-sm flex items-center gap-2">
        <Loader2 className="w-4 h-4 animate-spin" /> Loading…
      </div>
    );
  }

  const rows = data.rows || [];

  return (
    <div data-testid="admin-access-codes-panel">
      <div className="flex items-center justify-between mb-4">
        <div>
          <div className="flex items-center gap-2">
            <KeyRound className="w-4 h-4 text-amber-400" />
            <h2 className="sp-display text-xl">Pre-launch access codes</h2>
          </div>
          <p className="text-xs sp-subtle mt-1">
            Mint private codes for invitees. Trial codes from the audit
            funnel still work — these are for users who shouldn't have
            to give an email first.
          </p>
        </div>
        <CreateCodeForm onCreated={load} />
      </div>

      {rows.length === 0 ? (
        <div className="sp-glass rounded-2xl p-8 text-center sp-subtle text-sm">
          No access codes yet. Mint one above.
        </div>
      ) : (
        <div className="sp-glass rounded-2xl p-2 overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs sp-subtle uppercase tracking-widest">
                <th className="py-2 px-4">Code</th>
                <th className="py-2 px-4">Usage</th>
                <th className="py-2 px-4">Expires</th>
                <th className="py-2 px-4">Created</th>
                <th className="py-2 px-4">Status</th>
                <th className="py-2 px-4 text-right">&nbsp;</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <CodeRow key={r.id} row={r} onRevoke={onRevoke} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

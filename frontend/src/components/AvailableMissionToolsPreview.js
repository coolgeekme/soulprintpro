import React, { useEffect, useMemo, useState } from "react";
import { Wrench, Loader2, ChevronDown, ChevronRight, Plug } from "lucide-react";
import api from "../lib/api";

/**
 * AvailableMissionToolsPreview
 * ----------------------------
 * Rendered inside the Start-Mission dialog to give users an at-a-glance
 * preview of the Composio tools the lead agent will have access to once
 * the mission begins. Calls `GET /api/orgs/{orgId}/missions/available-tools`
 * and renders tools grouped by toolkit (gmail, slack, googledocs…) as
 * compact chips. Collapsed by default so it never overshadows the goal
 * input — one click expands.
 *
 * Empty / unconfigured states render a soft hint pointing users at the
 * Connections panel rather than failing silently. Errors are also
 * absorbed gracefully — this preview is purely informational and must
 * never block a mission launch.
 */
export default function AvailableMissionToolsPreview({ orgId }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const res = await api.get(`/orgs/${orgId}/missions/available-tools`);
        if (alive) setData(res.data);
      } catch (_err) {
        if (alive) setData({ composio_available: false, tools: [], budget: null });
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => { alive = false; };
  }, [orgId]);

  // Group tools by toolkit so the chip view is visually scannable.
  const grouped = useMemo(() => {
    const tools = data?.tools || [];
    const out = {};
    for (const t of tools) {
      if (!out[t.toolkit]) out[t.toolkit] = [];
      out[t.toolkit].push(t);
    }
    return out;
  }, [data]);

  const total = data?.tools?.length || 0;
  const composioAvailable = !!data?.composio_available;

  if (loading) {
    return (
      <div
        className="flex items-center gap-2 text-[11px] sp-subtle px-2 py-1.5"
        data-testid="available-tools-loading"
      >
        <Loader2 className="w-3 h-3 animate-spin" /> Checking available tools…
      </div>
    );
  }

  // Composio not configured at all on this deployment — silent. We
  // don't want to nag users on a self-hosted deploy without Composio.
  if (!composioAvailable) return null;

  return (
    <div
      className="mt-3 rounded-xl border border-white/10 bg-white/[0.025]"
      data-testid="available-tools-preview"
    >
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="w-full flex items-center gap-2 px-3 py-2 text-left hover:bg-white/[0.03] rounded-xl"
        data-testid="available-tools-toggle"
      >
        <Wrench className="w-3.5 h-3.5 text-white/70" />
        <span className="text-xs font-medium text-white/90">
          Tools your mission can use
        </span>
        <span className="sp-chip text-[10px]" data-testid="available-tools-count">
          {total} connected
        </span>
        <span className="ml-auto sp-subtle">
          {open ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
        </span>
      </button>
      {open && (
        <div className="px-3 pb-3 pt-1" data-testid="available-tools-body">
          {total === 0 ? (
            <div className="text-[11px] sp-subtle leading-relaxed">
              <div className="flex items-center gap-2 text-white/85">
                <Plug className="w-3 h-3" /> No Composio connections active yet.
              </div>
              <p className="mt-1">
                Missions still ship deliverables using web search, browsing, image
                generation and file writes. To add Gmail / Slack / Google Docs / Notion
                etc., open the org page → Connections panel and connect a toolkit.
              </p>
            </div>
          ) : (
            <>
              <p className="text-[11px] sp-subtle leading-relaxed mb-2">
                Your lead agent picks tools per turn. Budget per mission:{" "}
                <span className="text-white">
                  {data?.budget?.max_total ?? 20} total calls
                </span>{" "}
                ·{" "}
                <span className="text-white">
                  {data?.budget?.max_per_platform ?? 5} per platform
                </span>
                . If the picked lead has "Tools" turned off in its prefs, none of these
                are exposed during execution.
              </p>
              <div className="space-y-2">
                {Object.entries(grouped).map(([toolkit, items]) => (
                  <div key={toolkit} data-testid={`toolkit-group-${toolkit}`}>
                    <div className="text-[10px] uppercase tracking-widest sp-subtle mb-1">
                      {toolkit} <span className="text-white/40">· {items.length}</span>
                    </div>
                    <div className="flex flex-wrap gap-1">
                      {items.map((t) => (
                        <span
                          key={t.slug}
                          className="sp-chip text-[10px] !py-0.5"
                          title={t.description || t.short_name}
                          data-testid={`tool-chip-${t.slug}`}
                        >
                          {t.short_name.split(".")[1] || t.short_name}
                        </span>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}

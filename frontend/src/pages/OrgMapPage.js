import React, { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams, Link } from "react-router-dom";
import api from "../lib/api";
import { toast } from "sonner";
import {
  Loader2, ArrowLeft, Sparkles, RefreshCw, Network, Users, Bot,
  FileText, Send, MessageCircle, DollarSign, Hash, Megaphone,
} from "lucide-react";
import ForceGraph2D from "react-force-graph-2d";
import WorkingIndicator from "../components/WorkingIndicator";

// Colour palette per node kind. Stays consistent in both themes — we draw
// nodes onto a transparent canvas that sits on top of the page background,
// so saturated brand-orange + readable text colors are what we want.
const KIND_STYLES = {
  soul:    { fill: "#F64000", stroke: "#FF7A36", text: "#FFFFFF", glyph: "S" },
  team:    { fill: "#1B1B22", stroke: "#3B3B45", text: "#F5F5F7", glyph: "T" },
  agent:   { fill: "#2A2A36", stroke: "#54546A", text: "#E2E2E8", glyph: "A" },
  mission: { fill: "#0E5F46", stroke: "#1FA37A", text: "#E6FFF6", glyph: "M" },
  post:    { fill: "#1E3A5F", stroke: "#3D7BB8", text: "#E0F2FF", glyph: "P" },
  user:    { fill: "#5B2E70", stroke: "#9F66B7", text: "#F2E0FF", glyph: "U" },
};

const WINDOW_OPTIONS = [
  { value: 1,  label: "24h" },
  { value: 7,  label: "7d" },
  { value: 30, label: "30d" },
  { value: 90, label: "90d" },
  { value: 0,  label: "All" },
];

function formatUSD(n) {
  if (!n) return "$0";
  if (n < 0.01) return `$${(n * 100).toFixed(2)}¢`;
  return `$${n.toFixed(2)}`;
}

function StatCard({ icon: Icon, label, value, accent = "var(--fg)" }) {
  return (
    <div className="sp-glass rounded-xl px-3.5 py-3 flex items-center gap-3" data-testid={`stat-${label.toLowerCase().replace(/\s+/g, '-')}`}>
      <div
        className="w-9 h-9 rounded-lg flex items-center justify-center shrink-0"
        style={{ background: "var(--input-bg)", border: "1px solid var(--input-border)" }}
      >
        <Icon className="w-4 h-4" style={{ color: accent }} />
      </div>
      <div className="min-w-0">
        <div className="text-[10px] uppercase tracking-widest sp-subtle">{label}</div>
        <div className="text-base font-medium tabular-nums truncate" style={{ color: "var(--fg)" }}>
          {value}
        </div>
      </div>
    </div>
  );
}

export default function OrgMapPage() {
  const { orgId } = useParams();
  const nav = useNavigate();
  const containerRef = useRef(null);
  const fgRef = useRef(null);
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [windowDays, setWindowDays] = useState(30);
  const [selected, setSelected] = useState(null);
  const [size, setSize] = useState({ w: 800, h: 600 });

  const load = async (refresh = false) => {
    if (refresh) setRefreshing(true);
    else setLoading(true);
    try {
      const { data } = await api.get(`/orgs/${orgId}/map`, { params: { window_days: windowDays } });
      setData(data);
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Couldn't load the map");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  };

  useEffect(() => { load(); /* eslint-disable-next-line */ }, [orgId, windowDays]);

  // Resize the force-graph to the container — react-force-graph-2d
  // doesn't auto-fit, so we observe the wrapper and feed dimensions in.
  useEffect(() => {
    if (!containerRef.current) return;
    const el = containerRef.current;
    const update = () => {
      const r = el.getBoundingClientRect();
      setSize({ w: Math.max(320, r.width), h: Math.max(420, r.height) });
    };
    update();
    const ro = new ResizeObserver(update);
    ro.observe(el);
    window.addEventListener("resize", update);
    return () => { ro.disconnect(); window.removeEventListener("resize", update); };
  }, []);

  // Compute a *visual* node radius (graph units). We compress the raw
  // backend weight via sqrt so the Soul (~22) doesn't visually dwarf
  // Posts (~5). End result: Soul ~13, Team ~10–11, Agent ~8, Post ~7.
  const nodeRadius = (node) => {
    const w = Math.max(1, node.weight || 6);
    return 4 + Math.sqrt(w) * 2.0;
  };

  // Custom node painter. Coordinates here are in graph-space; force-graph
  // applies its own zoom transform on top. To make text + glyphs render
  // at a roughly CONSTANT screen size regardless of zoom, we divide by
  // globalScale for screen-pinned text.
  const paintNode = (node, ctx, globalScale) => {
    const style = KIND_STYLES[node.kind] || KIND_STYLES.agent;
    const r = nodeRadius(node);
    // Filled circle
    ctx.beginPath();
    ctx.arc(node.x, node.y, r, 0, 2 * Math.PI, false);
    ctx.fillStyle = style.fill;
    ctx.fill();
    ctx.lineWidth = 1.5 / globalScale;
    ctx.strokeStyle = node.id === selected?.id ? "#FFFFFF" : style.stroke;
    ctx.stroke();
    // Glyph — kept at a constant screen size so it stays legible at any
    // zoom level (regardless of the node's graph-unit radius).
    ctx.fillStyle = style.text;
    const glyphPxScreen = Math.max(10, Math.min(18, r * globalScale * 0.7));
    const glyphPxGraph = glyphPxScreen / globalScale;
    ctx.font = `600 ${glyphPxGraph}px Inter, sans-serif`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(style.glyph, node.x, node.y);
    // Label below the node. Truncate aggressively + only show important
    // labels until the user zooms in or hovers/selects a node — this
    // prevents the dense overlap we saw on dense graphs.
    const isImportant = node.kind === "soul" || node.kind === "team" ||
                        node.id === selected?.id;
    const showLabel = isImportant || globalScale > 1.4;
    if (!showLabel) return;
    const raw = (node.label || "").trim();
    const max = node.kind === "soul" ? 28 : 20;
    const label = raw.length > max ? raw.slice(0, max - 1) + "…" : raw;
    const fontPxScreen = node.kind === "soul" ? 13 : 11;
    const fontPxGraph = fontPxScreen / globalScale;
    ctx.font = `500 ${fontPxGraph}px Inter, sans-serif`;
    const padX = 5 / globalScale;
    const padY = 3 / globalScale;
    const tw = ctx.measureText(label).width;
    const lh = fontPxGraph;
    const labelTop = node.y + r + 6 / globalScale;
    ctx.fillStyle = "rgba(18,18,24,0.82)";
    // Rounded background pill for the label
    const bx = node.x - tw / 2 - padX;
    const by = labelTop;
    const bw = tw + padX * 2;
    const bh = lh + padY * 2;
    const br = Math.min(bh / 2, 4 / globalScale);
    if (ctx.roundRect) {
      ctx.beginPath();
      ctx.roundRect(bx, by, bw, bh, br);
      ctx.fill();
    } else {
      ctx.fillRect(bx, by, bw, bh);
    }
    ctx.fillStyle = "#FFFFFF";
    ctx.textBaseline = "top";
    ctx.fillText(label, node.x, labelTop + padY);
  };

  const graphData = useMemo(() => {
    if (!data) return { nodes: [], links: [] };
    return {
      nodes: data.nodes.map((n) => ({ ...n })),
      links: data.edges.map((e) => ({
        source: e.source,
        target: e.target,
        value: e.weight || 1,
        kind: e.kind,
      })),
    };
  }, [data]);

  // Cool the simulation gradually so it settles into a nice pose.
  // Also inject custom force tunings via the imperative ref so nodes
  // spread out nicely and labels don't overlap.
  useEffect(() => {
    const fg = fgRef.current;
    if (!fg || !data) return;
    try {
      const linkF = fg.d3Force?.("link");
      if (linkF?.distance) linkF.distance(75);
      const chargeF = fg.d3Force?.("charge");
      if (chargeF?.strength) chargeF.strength(-260);
      // Reheat so the new forces actually apply
      fg.d3ReheatSimulation?.();
    } catch (_) { /* no-op */ }
    setTimeout(() => fg.zoomToFit?.(400, 90), 700);
  }, [data]);

  const totals = data?.totals || {};

  return (
    <div className="min-h-screen sp-page">
      <div
        className="max-w-[1400px] mx-auto px-4 sm:px-6"
        style={{
          paddingTop: "calc(env(safe-area-inset-top, 0px) + 1rem)",
          paddingBottom: "2rem",
        }}
      >
        {/* Top bar */}
        <div className="flex items-center gap-2 mb-5">
          <button
            type="button"
            onClick={() => nav(`/app/org/${orgId}`)}
            className="sp-btn sp-btn-ghost text-xs"
            data-testid="map-back-btn"
          >
            <ArrowLeft className="w-3.5 h-3.5" /> Back to org
          </button>
          <h1 className="text-lg sm:text-xl font-medium tracking-tight ml-2 flex items-center gap-2">
            <Network className="w-4 h-4 text-orange-400" />
            <span>{data?.org?.name || "Mind map"}</span>
          </h1>
          <div className="ml-auto flex items-center gap-1.5">
            <div className="sp-glass rounded-lg p-1 flex items-center gap-0.5" data-testid="window-picker">
              {WINDOW_OPTIONS.map(opt => (
                <button
                  key={opt.value}
                  type="button"
                  onClick={() => setWindowDays(opt.value)}
                  className={`px-2.5 py-1 text-[11px] rounded ${windowDays === opt.value ? "bg-orange-500/30 text-white" : "sp-subtle hover:text-white"}`}
                  data-testid={`window-${opt.label.toLowerCase()}`}
                >
                  {opt.label}
                </button>
              ))}
            </div>
            <button
              type="button"
              onClick={() => load(true)}
              disabled={refreshing}
              className="sp-btn sp-btn-ghost text-xs disabled:opacity-50"
              data-testid="map-refresh-btn"
              title="Recompute the map"
            >
              {refreshing ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <RefreshCw className="w-3.5 h-3.5" />}
            </button>
          </div>
        </div>

        {/* KPI strip */}
        <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-2 mb-4" data-testid="map-kpis">
          <StatCard icon={Users}        label="Teams"     value={totals.teams ?? 0} />
          <StatCard icon={Bot}          label="Agents"    value={totals.agents_active ?? 0} accent="#F0A050" />
          <StatCard icon={MessageCircle} label="Messages"  value={(totals.messages ?? 0).toLocaleString()} accent="#7AB7FF" />
          <StatCard icon={Sparkles}     label="Missions"  value={totals.missions ?? 0} accent="#3FE0A2" />
          <StatCard icon={Megaphone}    label="Posts"     value={totals.social_posts ?? 0} accent="#A0C4FF" />
          <StatCard icon={DollarSign}   label="LLM cost"  value={formatUSD(totals.llm_cost_usd ?? 0)} accent="#FFC65A" />
        </div>

        {/* Body: graph + side panel */}
        <div className="grid lg:grid-cols-[1fr_320px] gap-3">
          <div
            ref={containerRef}
            className="sp-glass rounded-2xl overflow-hidden relative"
            style={{ height: "min(70dvh, 720px)" }}
            data-testid="map-canvas"
          >
            {(loading || !data) ? (
              <div className="absolute inset-0 flex items-center justify-center">
                <WorkingIndicator
                  label="Building your org map"
                  hints={[
                    "Reading teams, agents, conversations, missions…",
                    "Counting messages and tallying LLM spend per team…",
                    "Composing the layout — almost there.",
                  ]}
                />
              </div>
            ) : (
              <ForceGraph2D
                ref={fgRef}
                graphData={graphData}
                width={size.w}
                height={size.h}
                backgroundColor="rgba(0,0,0,0)"
                nodeRelSize={1}
                nodeCanvasObject={paintNode}
                nodePointerAreaPaint={(node, color, ctx) => {
                  ctx.fillStyle = color;
                  const r = nodeRadius(node);
                  ctx.beginPath();
                  ctx.arc(node.x, node.y, r, 0, 2 * Math.PI, false);
                  ctx.fill();
                }}
                linkColor={() => "rgba(180,180,200,0.22)"}
                linkWidth={(l) => Math.min(2.5, 0.4 + Math.log10((l.value || 1) + 1))}
                cooldownTicks={140}
                d3AlphaDecay={0.022}
                d3VelocityDecay={0.32}
                onEngineStop={() => fgRef.current?.zoomToFit?.(400, 90)}
                onNodeClick={(node) => {
                  setSelected(node);
                  fgRef.current?.centerAt(node.x, node.y, 700);
                  fgRef.current?.zoom(2.2, 700);
                }}
                onBackgroundClick={() => setSelected(null)}
              />
            )}
          </div>

          {/* Side panel */}
          <aside
            className="sp-glass rounded-2xl p-4 self-start"
            data-testid="map-side-panel"
            style={{ minHeight: "200px" }}
          >
            {selected ? (
              <NodeDetail
                node={selected}
                orgId={orgId}
                onClose={() => setSelected(null)}
              />
            ) : (
              <div className="space-y-3">
                <div>
                  <div className="text-xs uppercase tracking-widest sp-subtle mb-1">About this map</div>
                  <p className="text-sm leading-relaxed sp-subtle">
                    Your org's brain at a glance. Bigger nodes = more activity in the selected window.
                    Click any node for a deeper view — or use the time picker to zoom into the last day,
                    week, month, or quarter.
                  </p>
                </div>
                <div className="sp-glass rounded-lg p-3 space-y-1.5 text-xs">
                  <div className="text-[10px] uppercase tracking-widest sp-subtle mb-1">Legend</div>
                  {Object.entries(KIND_STYLES).map(([k, s]) => (
                    <div key={k} className="flex items-center gap-2">
                      <span
                        className="inline-block w-3 h-3 rounded-full"
                        style={{ background: s.fill, border: `1.5px solid ${s.stroke}` }}
                      />
                      <span className="capitalize">{k}</span>
                    </div>
                  ))}
                </div>
                {data?.org?.soul && (
                  <div className="sp-glass rounded-lg p-3 text-xs leading-relaxed">
                    <div className="text-[10px] uppercase tracking-widest sp-subtle mb-1">Company Soul</div>
                    <div className="sp-subtle">{data.org.soul}</div>
                  </div>
                )}
              </div>
            )}
          </aside>
        </div>
      </div>
    </div>
  );
}

// Side-panel detail for the currently selected node. Each kind has a
// tailored "jump in" link — clicking takes the user to the right place
// (org page, team, conversation, mission, etc.).
function NodeDetail({ node, orgId, onClose }) {
  const style = KIND_STYLES[node.kind] || KIND_STYLES.agent;
  const jumpHref = (() => {
    switch (node.kind) {
      case "soul": return `/app/org/${orgId}`;
      case "team": return `/app/org/${orgId}`;
      case "mission": {
        const id = node.id.split(":")[1];
        return `/app/org/${orgId}/missions?focus=${id}`;
      }
      case "post":   return `/app/org/${orgId}/social`;
      case "agent":  return `/app/org/${orgId}`;
      case "user":   return `/app/org/${orgId}`;
      default: return null;
    }
  })();

  return (
    <div className="space-y-3" data-testid="map-node-detail">
      <div className="flex items-center justify-between gap-2">
        <span
          className="inline-flex items-center gap-2 sp-chip text-[11px]"
          style={{ background: style.fill, color: style.text, borderColor: style.stroke }}
        >
          {style.glyph} · {node.kind}
        </span>
        <button onClick={onClose} className="sp-btn sp-btn-ghost text-[11px]">Close</button>
      </div>
      <div>
        <div className="text-[10px] uppercase tracking-widest sp-subtle">Label</div>
        <div className="text-sm font-medium" style={{ color: "var(--fg)" }}>{node.label}</div>
      </div>
      {node.subtitle && (
        <div>
          <div className="text-[10px] uppercase tracking-widest sp-subtle">Activity</div>
          <div className="text-sm sp-subtle">{node.subtitle}</div>
        </div>
      )}
      {(node.cost ?? 0) > 0 && (
        <div className="flex items-center gap-2 text-sm">
          <DollarSign className="w-3.5 h-3.5 text-amber-400" />
          <span className="tabular-nums" style={{ color: "var(--fg)" }}>{formatUSD(node.cost)}</span>
          <span className="sp-subtle text-xs">on LLM</span>
        </div>
      )}
      {node.kind === "mission" && node.status && (
        <div className="flex items-center gap-2 text-sm">
          <span className="sp-chip text-[10px]">{node.status}</span>
        </div>
      )}
      {node.kind === "post" && (
        <div className="flex items-center gap-2 text-sm">
          <span className="sp-chip text-[10px]">{node.platform || "any"}</span>
          <span className="sp-chip text-[10px]">{node.status || "draft"}</span>
        </div>
      )}
      {jumpHref && (
        <Link
          to={jumpHref}
          className="sp-btn sp-btn-primary text-xs w-full justify-center"
          data-testid="map-node-jump"
        >
          <Send className="w-3.5 h-3.5" /> Jump in
        </Link>
      )}
    </div>
  );
}

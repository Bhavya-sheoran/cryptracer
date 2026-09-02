import { useEffect, useMemo, useRef, useState } from 'react';
import PropTypes from 'prop-types';
import { select } from 'd3-selection';
import { sankey, sankeyLinkHorizontal } from 'd3-sankey';

/**
 * Sankey flow visualisation of the money trail.
 *
 * The backend hands us `nodes` (with a `hop` depth) and `links` (source/target
 * address + value). d3-sankey needs numeric indices, so we remap addresses to
 * positions and drop any link whose endpoints are missing.
 *
 * Two deliberate choices:
 *  - Columns are pinned to the backend's own `hop` number. d3-sankey's default
 *    alignments place every sink in the last column, which would draw a small
 *    peel-off at hop 3 level with the terminal exchange at hop 7 - visually
 *    claiming a depth that is not true. Horizontal position is evidence here,
 *    so it must mean hop count and nothing else.
 *  - Links are drawn with a floor on width. A peel chain shaves tiny amounts off
 *    at each hop, and a strictly value-proportional ribbon would render those
 *    hops as invisible hairlines - exactly the hops that matter most.
 */
export default function SankeyTrace({ tracePath, onSelectAddress, selectedAddress, theme }) {
  const svgRef = useRef(null);
  const wrapRef = useRef(null);
  const [width, setWidth] = useState(860);

  // Track container width so the diagram is responsive without a redraw loop.
  useEffect(() => {
    if (!wrapRef.current) return undefined;
    const el = wrapRef.current;
    const update = () => setWidth(Math.max(el.clientWidth || 860, 320));
    update();
    const ro = new ResizeObserver(update);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const graph = useMemo(() => {
    const rawNodes = tracePath?.nodes || [];
    const rawLinks = tracePath?.links || [];
    if (rawNodes.length === 0) return null;

    const index = new Map(rawNodes.map((n, i) => [n.address, i]));
    const nodes = rawNodes.map((n) => ({ ...n }));

    // Collapse parallel edges: several transactions between the same pair
    // should read as one thicker ribbon, not stacked hairlines.
    const merged = new Map();
    rawLinks.forEach((l) => {
      const s = index.get(l.source);
      const t = index.get(l.target);
      if (s === undefined || t === undefined || s === t) return;
      const key = `${s}->${t}`;
      const value = Number(l.value) || 0;
      if (merged.has(key)) {
        const existing = merged.get(key);
        existing.value += value;
        existing.txids.push(l.txid);
      } else {
        merged.set(key, { source: s, target: t, value, txids: [l.txid], asset: l.asset });
      }
    });

    const links = [...merged.values()];
    if (links.length === 0) return null;

    // d3-sankey throws on a zero-value link; give every ribbon a positive value.
    const maxValue = Math.max(...links.map((l) => l.value), 1);
    links.forEach((l) => {
      l.rawValue = l.value;
      l.value = Math.max(l.value, maxValue * 0.02);
    });

    return { nodes, links };
  }, [tracePath]);

  const height = useMemo(() => {
    const n = graph?.nodes?.length || 0;
    return Math.min(Math.max(n * 34, 260), 620);
  }, [graph]);

  useEffect(() => {
    const svg = select(svgRef.current);
    svg.selectAll('*').remove();
    if (!graph) return;

    const margin = { top: 12, right: 168, bottom: 12, left: 12 };
    const innerW = Math.max(width - margin.left - margin.right, 240);
    const innerH = height - margin.top - margin.bottom;

    let layout;
    try {
      layout = sankey()
        .nodeWidth(13)
        .nodePadding(14)
        .nodeAlign((d) => (Number.isFinite(d.hop) ? d.hop : d.depth))
        .extent([[0, 0], [innerW, innerH]])({
        nodes: graph.nodes.map((d) => ({ ...d })),
        links: graph.links.map((d) => ({ ...d })),
      });
    } catch {
      // A cyclic flow (funds returning to an earlier address) is legitimate
      // on-chain but not representable as a Sankey. Fail visibly, not silently.
      const cssErr = getComputedStyle(document.documentElement);
      svg
        .append('text')
        .attr('x', 16)
        .attr('y', 28)
        .attr('fill', (cssErr.getPropertyValue('--text-muted') || '').trim() || '#6b7480')
        .attr('font-size', 13)
        .text('Flow contains a cycle and cannot be drawn as a Sankey. See the hop list below.');
      return;
    }

    const g = svg
      .attr('viewBox', `0 0 ${width} ${height}`)
      .append('g')
      .attr('transform', `translate(${margin.left},${margin.top})`);

    // Colours come from the theme tokens, so the diagram follows light/dark
    // instead of being a hardcoded palette that only works on one background.
    const css = getComputedStyle(document.documentElement);
    const token = (name, fallback) => (css.getPropertyValue(name) || '').trim() || fallback;
    const C = {
      mixer: token('--mix-500', '#c2670d'),
      sanctioned: token('--dang-500', '#c0392b'),
      entity: token('--ok-500', '#1a8a52'),
      root: token('--accent', '#2f6bdd'),
      hop: token('--n-400', '#9aa3b2'),
      link: token('--border-strong', '#cdd2db'),
      text: token('--text-muted', '#6b7480'),
      entityText: token('--ok-700', '#126b3f'),
    };
    const colourFor = (d) => {
      if (d.entity_type === 'mixer') return C.mixer;
      if (d.entity_type === 'sanctioned') return C.sanctioned;
      if (d.entity_name) return C.entity;
      if (d.hop === 0) return C.root;
      return C.hop;
    };

    // --- links ---
    g.append('g')
      .attr('fill', 'none')
      .selectAll('path')
      .data(layout.links)
      .join('path')
      .attr('d', sankeyLinkHorizontal())
      .attr('stroke', (d) => (d.target.entity_name ? C.entity : C.link))
      .attr('stroke-opacity', (d) =>
        selectedAddress &&
        d.source.address !== selectedAddress &&
        d.target.address !== selectedAddress
          ? 0.12
          : 0.4,
      )
      .attr('stroke-width', (d) => Math.max(1.5, d.width))
      .append('title')
      .text(
        (d) =>
          `${d.source.address} → ${d.target.address}\n` +
          `${d.rawValue?.toFixed(6) ?? d.value} ${d.asset || ''}\n` +
          `${d.txids.length} transaction(s)`,
      );

    // --- nodes ---
    const node = g
      .append('g')
      .selectAll('g')
      .data(layout.nodes)
      .join('g')
      .attr('cursor', onSelectAddress ? 'pointer' : 'default')
      .on('click', (_event, d) => onSelectAddress?.(d.address));

    node
      .append('rect')
      .attr('x', (d) => d.x0)
      .attr('y', (d) => d.y0)
      .attr('height', (d) => Math.max(2, d.y1 - d.y0))
      .attr('width', (d) => d.x1 - d.x0)
      .attr('fill', colourFor)
      .attr('stroke', (d) => (d.address === selectedAddress ? C.root : 'none'))
      .attr('stroke-width', 2)
      .attr('rx', 2)
      .append('title')
      .text(
        (d) =>
          `${d.address}\nhop ${d.hop}` +
          (d.entity_name ? `\n${d.entity_name} (${d.entity_type})` : '') +
          (d.cluster_key ? `\ncluster: ${d.cluster_key}` : ''),
      );

    node
      .append('text')
      .attr('x', (d) => d.x1 + 6)
      .attr('y', (d) => (d.y1 + d.y0) / 2)
      .attr('dy', '0.35em')
      .attr('font-size', 11).attr('font-family', 'var(--font-sans)')
      .attr('fill', (d) => (d.entity_name ? C.entityText : C.text))
      .text((d) => {
        if (d.entity_name) return `${d.entity_name} · h${d.hop}`;
        const a = d.address || '';
        return `${a.slice(0, 8)}…${a.slice(-4)} · h${d.hop}`;
      });
  }, [graph, width, height, onSelectAddress, selectedAddress, theme]);

  if (!graph) {
    return (
      <div className="empty">
        <span className="glyph" aria-hidden="true">⇢</span>
        <p>No traced flow to draw yet. File the wallet so the graph is built, then re-run.</p>
      </div>
    );
  }

  return (
    <div className="flow-canvas" ref={wrapRef}>
      <svg ref={svgRef} width="100%" height={height} role="img" aria-label="Money flow trace" />
      <ul className="flow-legend">
        <li><span className="swatch" style={{ background: 'var(--accent)' }} /> Reported wallet</li>
        <li><span className="swatch" style={{ background: 'var(--n-400)' }} /> Intermediate hop</li>
        <li><span className="swatch" style={{ background: 'var(--ok-500)' }} /> Attributed service</li>
        <li><span className="swatch" style={{ background: 'var(--mix-500)' }} /> Mixer (flagged)</li>
        <li><span className="swatch" style={{ background: 'var(--dang-500)' }} /> Sanctioned</li>
        <li className="subtle">Ribbon width is value-proportional with a floor · hover for amounts</li>
      </ul>
    </div>
  );
}

SankeyTrace.propTypes = {
  theme: PropTypes.string,
  tracePath: PropTypes.shape({
    nodes: PropTypes.arrayOf(
      PropTypes.shape({
        address: PropTypes.string.isRequired,
        hop: PropTypes.number,
        entity_name: PropTypes.string,
        entity_type: PropTypes.string,
        cluster_key: PropTypes.string,
      }),
    ),
    links: PropTypes.arrayOf(
      PropTypes.shape({
        source: PropTypes.string,
        target: PropTypes.string,
        value: PropTypes.number,
        txid: PropTypes.string,
        asset: PropTypes.string,
      }),
    ),
  }),
  onSelectAddress: PropTypes.func,
  selectedAddress: PropTypes.string,
};

SankeyTrace.defaultProps = {
  theme: 'light',
  tracePath: null,
  onSelectAddress: undefined,
  selectedAddress: undefined,
};

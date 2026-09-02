import { useCallback, useEffect, useRef, useState } from 'react';
import PropTypes from 'prop-types';
import WalletInputForm from '../components/WalletInputForm.jsx';
import SankeyTrace from '../components/SankeyTrace.jsx';
import RiskPanel from '../components/RiskPanel.jsx';
import ContributingCases from '../components/ContributingCases.jsx';
import AttributionCard from '../components/AttributionCard.jsx';
import CaseWorkspace from '../components/CaseWorkspace.jsx';
import Tabs from '../components/ui/Tabs.jsx';
import AddressChip from '../components/ui/AddressChip.jsx';
import { Callout, EmptyState, RiskBadge, Skeleton, Stat } from '../components/ui/Bits.jsx';
import useTheme from '../components/ui/useTheme.js';
import { useToast } from '../components/ui/toast-context.js';
import { analyseWallet, fetchRankedExchanges, submitWallet } from '../api/client.js';

/**
 * The investigator's workspace: report a wallet, follow the money, see who the
 * funds reached and how confident that attribution is, and read the cases
 * behind the risk score.
 *
 * The detail is tabbed rather than stacked. A trace produces four different
 * kinds of evidence - the flow, the attribution, the score, the case file - and
 * stacking them means scrolling past three to reach the fourth.
 */
export default function Investigate({ currentUser, submitted }) {
  const { theme } = useTheme();
  const toast = useToast();
  const [analysis, setAnalysis] = useState(null);
  const [intake, setIntake] = useState(null);
  const [scoring, setScoring] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [selected, setSelected] = useState(null);
  const [tab, setTab] = useState('flow');
  const runRef = useRef(null);

  useEffect(() => {
    if (!currentUser) return undefined;
    let cancelled = false;
    fetchRankedExchanges(1)
      .then((d) => !cancelled && setScoring(d.scoring || null))
      .catch(() => {});
    return () => { cancelled = true; };
  }, [currentUser]);

  const runAnalysis = useCallback(async (address) => {
    setBusy(true);
    setError(null);
    setSelected(null);
    try {
      setAnalysis(await analyseWallet(address));
      setTab('flow');
    } catch (err) {
      setAnalysis(null);
      setError(
        err.status === 401
          ? 'Sign in to analyse a wallet. The result names contributing case numbers, so it is not available anonymously.'
          : err.status === 404
            ? 'No graph data for this address yet. File the complaint first so the flow can be traced.'
            : err.message,
      );
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => { runRef.current = runAnalysis; }, [runAnalysis]);

  // A search from the top bar drives the analysis.
  useEffect(() => {
    if (submitted?.address) runRef.current?.(submitted.address);
  }, [submitted]);

  const handleSubmit = useCallback(
    async (payload) => {
      setBusy(true);
      setError(null);
      try {
        const result = await submitWallet(payload);
        setIntake(result);
        toast(`Case ${result.case_number} opened · ${result.trace.hops_discovered} hops traced`);
        setAnalysis(await analyseWallet(payload.address));
        setTab('flow');
        return result;
      } catch (err) {
        setError(err.message);
        toast(err.message, 'error');
        return null;
      } finally {
        setBusy(false);
      }
    },
    [toast],
  );

  const caseId = intake?.case_id || analysis?.reported_in_cases?.[0]?.case_id;

  return (
    <>
      <div className="page-head">
        <div>
          <div className="crumb">Workspace</div>
          <h1>Investigate a suspect wallet</h1>
          <p className="lede">
            Trace victim-reported funds across hops, attribute the terminal cluster to an exchange,
            and score how often reported fraud arrives there.
          </p>
        </div>
      </div>

      <section className="panel">
        <div className="panel-head"><h2>Report a suspect wallet</h2></div>
        <div className="panel-body">
          <WalletInputForm onAnalyse={runAnalysis} onSubmitted={handleSubmit} busy={busy} />
        </div>
      </section>

      {error ? <Callout tone="danger" glyph="⚠">{error}</Callout> : null}

      {intake?.duplicate?.is_duplicate ? (
        <Callout tone="warn" glyph="⚑">
          <strong>Repeat report — possible fraud ring.</strong> {intake.duplicate.note}
          <div className="tiny mono" style={{ marginTop: 4 }}>
            Earlier cases: {intake.duplicate.prior_case_numbers.join(', ')}
          </div>
        </Callout>
      ) : null}

      {busy && !analysis ? (
        <section className="panel">
          <div className="panel-body stack">
            <Skeleton height="18px" width="40%" />
            <Skeleton height="140px" />
            <Skeleton height="18px" />
          </div>
        </section>
      ) : null}

      {analysis ? (
        <>
          <div className="stat-row">
            <Stat label="Chain" value={analysis.chain} sub={analysis.address_kind} />
            <Stat
              label="Addresses traced"
              value={analysis.trace_path.node_count}
              sub={`${analysis.trace_path.link_count} transfers`}
            />
            <Stat label="Trace depth" value={analysis.trace_path.depth} sub="max hops followed" />
            <Stat
              label="Terminal service"
              value={analysis.attribution?.entity_name || '—'}
              sub={
                analysis.attribution?.method === 'tagged_db'
                  ? 'curated tag'
                  : analysis.attribution?.method === 'classifier'
                    ? 'inferred category'
                    : 'unattributed'
              }
            />
            <Stat
              label="Fraud linkage"
              value={
                <span className="row">
                  <RiskBadge label={analysis.risk_label} />
                  <span className="mono">{Number(analysis.risk_score).toFixed(1)}</span>
                </span>
              }
              sub={`${analysis.contributing_case_ids.length} contributing case(s)`}
            />
          </div>

          <div className="row wrap sm">
            <span className="muted">Subject</span>
            <AddressChip
              address={analysis.address}
              full
              entityName={analysis.attribution?.entity_name}
              entityType={analysis.attribution?.entity_type}
            />
          </div>

          {analysis.mixer_interaction ? (
            <Callout tone="mixer" glyph="⚠">
              This flow interacts with a tagged mixer. Recorded as a layering signal — the system
              makes no attempt to unwind mixing.
            </Callout>
          ) : null}

          <section className="panel">
            <Tabs
              active={tab}
              onChange={setTab}
              tabs={[
                { id: 'flow', label: 'Money flow' },
                { id: 'attribution', label: 'Attribution' },
                { id: 'risk', label: 'Risk', count: analysis.contributing_case_ids.length },
                { id: 'case', label: 'Case file' },
              ]}
            />

            {tab === 'flow' ? (
              <>
                <div className="flow-toolbar">
                  <span className="sm muted">
                    {analysis.trace_path.node_count} addresses · {analysis.trace_path.link_count}{' '}
                    transfers · depth {analysis.trace_path.depth}
                  </span>
                  <span className="grow" />
                  {selected ? (
                    <span className="row sm">
                      <span className="muted">Pinned</span>
                      <AddressChip address={selected} />
                      <button type="button" className="btn btn-ghost btn-sm" onClick={() => setSelected(null)}>
                        Clear
                      </button>
                    </span>
                  ) : (
                    <span className="tiny subtle">Click a node to pin it</span>
                  )}
                </div>
                <SankeyTrace
                  tracePath={analysis.trace_path}
                  onSelectAddress={setSelected}
                  selectedAddress={selected}
                  theme={theme}
                />
              </>
            ) : null}

            {tab === 'attribution' ? (
              <div className="panel-body">
                <AttributionCard
                  attribution={analysis.attribution}
                  terminals={analysis.terminal_attributions}
                  onSelectAddress={setSelected}
                />
              </div>
            ) : null}

            {tab === 'risk' ? (
              <div className="panel-body stack" style={{ gap: 'var(--sp-6)' }}>
                <RiskPanel
                  label={analysis.risk_label}
                  score={analysis.risk_score}
                  factors={analysis.risk_factors}
                  halfLifeDays={scoring?.half_life_days}
                />
                <div>
                  <h4>Contributing cases — why this score</h4>
                  <ContributingCases
                    contributions={analysis.contributions}
                    totalScore={analysis.risk_score}
                  />
                </div>
              </div>
            ) : null}

            {tab === 'case' ? (
              currentUser ? (
                <CaseWorkspace
                  caseId={caseId}
                  currentUser={currentUser}
                  targetAddress={analysis.terminal_attributions?.[0]?.address}
                  entityName={analysis.attribution?.entity_name}
                  embedded
                />
              ) : (
                <div className="panel-body">
                  <EmptyState glyph="🔒" title="Sign in to open the case file">
                    Notes, evidence, the hashed forensic report, STR drafts and freeze requests are
                    available to a signed-in officer. Approving a freeze additionally requires a
                    supervisor.
                  </EmptyState>
                </div>
              )
            ) : null}

            <div className="panel-foot">{analysis.notice}</div>
          </section>
        </>
      ) : !busy && !error ? (
        <section className="panel">
          <div className="panel-body">
            <EmptyState glyph="⌕" title="No wallet analysed yet">
              Paste a suspect address above, or search from the bar at the top. Press <kbd>/</kbd>{' '}
              anywhere to jump to search.
            </EmptyState>
          </div>
        </section>
      ) : null}
    </>
  );
}

Investigate.propTypes = {
  currentUser: PropTypes.shape({ role: PropTypes.string, can_approve: PropTypes.bool }),
  submitted: PropTypes.shape({ address: PropTypes.string, at: PropTypes.number }),
};
Investigate.defaultProps = { currentUser: null, submitted: null };

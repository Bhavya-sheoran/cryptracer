import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import RiskPanel from './RiskPanel.jsx';

/**
 * The score must never appear without its reasoning.
 *
 * That is the project's explainability requirement expressed in the UI: an
 * investigator has to be able to justify a "high" rating in a case file, and a
 * number with no factors beside it cannot be justified.
 */
describe('RiskPanel', () => {
  it('shows the score and its band', () => {
    render(<RiskPanel label="high" score={81.4} factors={['3 reported case(s)']} />);

    expect(screen.getByText('81.4')).toBeInTheDocument();
    expect(screen.getByText(/high fraud linkage/i)).toBeInTheDocument();
  });

  it('lists every contributing factor', () => {
    const factors = [
      '3 reported case(s) trace to this cluster (time-decayed, 90-day half-life)',
      'traced flow interacted with a tagged mixer (flagged, not unwound)',
      'destination is on the OFAC SDN list',
    ];
    render(<RiskPanel label="high" score={92} factors={factors} />);

    factors.forEach((factor) => expect(screen.getByText(factor)).toBeInTheDocument());
  });

  it('shows the classifier contribution as advisory, with its precision', () => {
    // The ML factor must carry the model version and how often it is wrong.
    // A score that silently included a 0.84-precision signal would not be
    // reconstructable by hand, which is the whole point of the factor list.
    const mlFactor =
      '2 of 40 scored transaction(s) flagged by the Elliptic classifier at p>=0.95 '
      + '(model elliptic-xgb-servable-v1, ~0.84 precision; advisory, capped at 6.0 points)';

    render(<RiskPanel label="medium" score={55} factors={[mlFactor]} />);

    const rendered = screen.getByText(/Elliptic classifier/);
    expect(rendered).toBeInTheDocument();
    expect(rendered.textContent).toMatch(/advisory/);
    expect(rendered.textContent).toMatch(/precision/);
  });

  it('clamps a score above 100 rather than overflowing the gauge', () => {
    render(<RiskPanel label="high" score={140} factors={[]} />);
    expect(screen.getByText('100.0')).toBeInTheDocument();
  });

  it('clamps a negative score to zero', () => {
    render(<RiskPanel label="low" score={-5} factors={[]} />);
    expect(screen.getByText('0.0')).toBeInTheDocument();
  });

  it('survives a non-numeric score without rendering NaN', () => {
    render(<RiskPanel label="low" score={undefined} factors={[]} />);
    expect(screen.getByText('0.0')).toBeInTheDocument();
    expect(screen.queryByText(/NaN/)).toBeNull();
  });

  it('names the half-life when one is configured', () => {
    render(<RiskPanel label="low" score={10} factors={[]} halfLifeDays={90} />);
    expect(screen.getByText(/90-day half-life/)).toBeInTheDocument();
  });

  it('still explains the decay when no half-life is given', () => {
    render(<RiskPanel label="low" score={10} factors={[]} />);
    expect(screen.getByText(/with time decay/)).toBeInTheDocument();
  });
});

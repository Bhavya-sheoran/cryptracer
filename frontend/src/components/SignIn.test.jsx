import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import SignIn from './SignIn.jsx';

// The toast context is a provider the panel expects but this test is not about.
vi.mock('./ui/toast-context.js', () => ({ useToast: () => vi.fn() }));
vi.mock('../api/client.js', () => ({ login: vi.fn(), seedDemoUsers: vi.fn() }));

/**
 * The load-bearing test here is the demo-credential gate.
 *
 * `/auth/seed-demo-users` creates a supervisor whose password is published in
 * this repository. The server refuses it outside demo mode, but a UI that
 * still shows the buttons would advertise an attack that looks supported -
 * so the panel has to disappear, not merely stop working.
 */
describe('SignIn', () => {
  it('always offers a real credential form', () => {
    render(<SignIn onSignedIn={vi.fn()} demoAuthEnabled={false} />);

    expect(screen.getByLabelText(/username/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/password/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^sign in$/i })).toBeInTheDocument();
  });

  it('shows the demonstration accounts when the server allows them', () => {
    render(<SignIn onSignedIn={vi.fn()} demoAuthEnabled />);

    expect(screen.getByRole('button', { name: /sign in as investigator/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /sign in as supervisor/i })).toBeInTheDocument();
  });

  it('hides the demonstration accounts when the server refuses them', () => {
    render(<SignIn onSignedIn={vi.fn()} demoAuthEnabled={false} />);

    expect(screen.queryByRole('button', { name: /sign in as investigator/i })).toBeNull();
    expect(screen.queryByRole('button', { name: /sign in as supervisor/i })).toBeNull();

    // The panel says *why* rather than vanishing silently. An officer who
    // expected the demo buttons should learn they are switched off, not be
    // left wondering whether the page failed to load.
    expect(screen.getByText(/demonstration accounts are disabled/i)).toBeInTheDocument();
  });

  it('defaults to hiding them when the prop is absent', () => {
    // Fails closed: a caller that forgets to pass the flag must not leak the
    // demo panel, because the mistake and the exposure look identical on screen.
    render(<SignIn onSignedIn={vi.fn()} />);

    expect(screen.queryByRole('button', { name: /sign in as investigator/i })).toBeNull();
  });

  it('does not name the demo passwords when they are disabled', () => {
    const { container } = render(<SignIn onSignedIn={vi.fn()} demoAuthEnabled={false} />);
    expect(container.textContent).not.toMatch(/investigator123|supervisor123/);
  });

  it('keeps the sign-in button disabled until both fields are filled', () => {
    render(<SignIn onSignedIn={vi.fn()} demoAuthEnabled={false} />);
    expect(screen.getByRole('button', { name: /^sign in$/i })).toBeDisabled();
  });
});

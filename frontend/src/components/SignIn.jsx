import { useState } from 'react';
import PropTypes from 'prop-types';
import { login, seedDemoUsers } from '../api/client.js';
import { useToast } from './ui/toast-context.js';

/**
 * Officer sign-in.
 *
 * The role matters beyond access control: only a supervisor can authorise a
 * freeze or an STR, so who is signed in changes what the dashboard permits.
 */
export default function SignIn({ onSignedIn }) {
  const toast = useToast();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);

  function accept(data) {
    onSignedIn({
      full_name: data.full_name,
      role: data.role,
      can_approve: data.role === 'supervisor' || data.role === 'admin',
    });
    toast(`Signed in as ${data.full_name} (${data.role})`);
  }

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    try {
      accept(await login(username, password));
    } catch (err) {
      toast(err.message, 'error');
    } finally {
      setBusy(false);
    }
  }

  async function quick(user) {
    setBusy(true);
    try {
      await seedDemoUsers();
      accept(await login(user, `${user}123`));
    } catch (err) {
      toast(err.message, 'error');
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="panel">
      <div className="panel-head"><h2>Officer sign-in</h2></div>
      <div className="panel-body">
        <div className="grid-2">
          <form onSubmit={submit} className="stack">
            <div className="field">
              <label className="label" htmlFor="u">Username</label>
              <input id="u" className="input" value={username} autoComplete="username"
                     onChange={(e) => setUsername(e.target.value)} />
            </div>
            <div className="field">
              <label className="label" htmlFor="p">Password</label>
              <input id="p" className="input" type="password" value={password} autoComplete="current-password"
                     onChange={(e) => setPassword(e.target.value)} />
            </div>
            <div className="row">
              <button className="btn" type="submit" disabled={busy || !username || !password}>
                {busy ? 'Signing in…' : 'Sign in'}
              </button>
            </div>
          </form>

          <div className="stack">
            <h4>Demonstration accounts</h4>
            <p className="hint">
              Published credentials for this prototype only. The two roles exist to demonstrate
              separation of duties: an investigator drafts a freeze request, a supervisor
              authorises it. Neither can approve their own.
            </p>
            <div className="row wrap">
              <button type="button" className="btn btn-secondary" disabled={busy} onClick={() => quick('investigator')}>
                Sign in as investigator
              </button>
              <button type="button" className="btn btn-secondary" disabled={busy} onClick={() => quick('supervisor')}>
                Sign in as supervisor
              </button>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}

SignIn.propTypes = { onSignedIn: PropTypes.func.isRequired };

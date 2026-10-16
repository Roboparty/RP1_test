import { useState, type FormEvent } from "react";
import { ArrowRight, KeyRound, LogOut, ShieldCheck } from "lucide-react";
import { useAuth } from "../auth";
import { ApiError } from "../client";

export function AuthLoading() {
  return (
    <div className="auth-stage" aria-live="polite">
      <div className="auth-loading-mark"><img src="/assets/roboparty-logo.png" alt="" /></div>
      <p>正在建立安全会话…</p>
    </div>
  );
}

export function LoginScreen() {
  const { login, bootError } = useAuth();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState(bootError);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      await login(username, password);
    } catch (caught) {
      setError(
        caught instanceof ApiError && caught.code === "invalid_credentials"
          ? "用户名或密码错误"
          : caught instanceof Error
            ? caught.message
            : "登录失败，请稍后重试。"
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="login-screen">
      <section className="login-brand">
        <img src="/assets/roboparty-logo.png" alt="RoboParty" />
        <span className="eyebrow">RELIABILITY CONTROL</span>
        <h1>RP1 可靠性测试平台</h1>
        <p>Safe. Reliable. Built to Endure.</p>
        <div className="login-signal" aria-hidden="true"><i /><i /><i /><i /></div>
        <figure className="login-robot" aria-hidden="true" />
      </section>
      <section className="login-panel" aria-labelledby="login-title">
        <div className="login-panel-heading">
          <span><ShieldCheck aria-hidden="true" /></span>
          <div><small>SECURE SESSION</small><h2 id="login-title">登录平台</h2></div>
        </div>
        <form onSubmit={submit}>
          <label>用户名<input autoFocus autoComplete="username" value={username} onChange={(event) => setUsername(event.target.value)} required /></label>
          <label>密码<input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} required /></label>
          {error && <p className="form-error" role="alert">{error}</p>}
          <button className="primary-button login-submit" type="submit" disabled={busy || !username || !password}>
            {busy ? "正在验证…" : <>进入可靠性工作台 <ArrowRight aria-hidden="true" /></>}
          </button>
        </form>
      </section>
    </main>
  );
}

export function PasswordChangeScreen() {
  const { session, changePassword, logout } = useAuth();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    if (next.length < 12 || next !== confirm || next === current) {
      setError(next.length < 12 ? "新密码至少需要 12 个字符。" : next !== confirm ? "两次输入的新密码不一致。" : "新密码不能与当前密码相同。");
      return;
    }
    setBusy(true);
    setError("");
    try {
      await changePassword(current, next);
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "密码修改失败，请重试。");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="password-screen">
      <section className="password-panel">
        <div className="password-icon"><KeyRound aria-hidden="true" /></div>
        <span className="eyebrow">FIRST LOGIN PROTECTION</span>
        <h1>首次登录，请立即修改密码</h1>
        <p>账户 <strong>{session?.user.username}</strong> 使用的是一次性本地初始密码。完成修改前，仅可访问本页或退出。</p>
        <form onSubmit={submit}>
          <label>当前密码<input type="password" autoComplete="current-password" value={current} onChange={(event) => setCurrent(event.target.value)} required /></label>
          <label>新密码<input type="password" autoComplete="new-password" minLength={12} value={next} onChange={(event) => setNext(event.target.value)} required /><small>至少 12 个字符，且不能与当前密码相同</small></label>
          <label>确认新密码<input type="password" autoComplete="new-password" value={confirm} onChange={(event) => setConfirm(event.target.value)} required /></label>
          {error && <p className="form-error" role="alert">{error}</p>}
          <button className="primary-button" type="submit" disabled={busy}>{busy ? "正在更新…" : "更新密码并进入平台"}</button>
        </form>
        <button className="text-button" type="button" onClick={() => void logout()}><LogOut aria-hidden="true" />退出当前账户</button>
      </section>
    </main>
  );
}

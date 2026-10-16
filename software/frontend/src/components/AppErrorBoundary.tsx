import { Component, type ErrorInfo, type ReactNode } from "react";
import { AlertTriangle, RefreshCw } from "lucide-react";

type Props = { children: ReactNode };
type State = { failed: boolean };

export class AppErrorBoundary extends Component<Props, State> {
  state: State = { failed: false };

  static getDerivedStateFromError(): State {
    return { failed: true };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // Keep the user-facing fallback free of stack details while retaining browser diagnostics.
    console.error("Application render failed", error, info.componentStack);
  }

  render() {
    if (!this.state.failed) return this.props.children;
    return (
      <main className="fatal-error" role="alert">
        <AlertTriangle aria-hidden="true" />
        <h1>页面暂时无法显示</h1>
        <p>界面渲染发生异常。请重新加载页面；如果问题持续出现，请联系平台管理员。</p>
        <button type="button" onClick={() => window.location.reload()}>
          <RefreshCw aria-hidden="true" />重新加载
        </button>
      </main>
    );
  }
}

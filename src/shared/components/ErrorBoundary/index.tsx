import { Component, type ErrorInfo, type ReactNode } from 'react';
import { t } from '@/app/i18n';
import './index.css';

interface Props {
  children: ReactNode;
  /** Optional label so a failure is attributable when several boundaries exist. */
  label?: string;
}

interface State {
  error: Error | null;
}

/**
 * Keeps one broken subtree from blanking the whole app. A render error anywhere
 * below would otherwise unmount the root and leave a white screen with no clue
 * about what happened, so failures surface as a recoverable panel instead.
 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error('[ErrorBoundary]', this.props.label ?? 'app', error, info.componentStack);
  }

  private reset = () => this.setState({ error: null });

  private reload = () => window.location.reload();

  render(): ReactNode {
    const { error } = this.state;
    if (!error) return this.props.children;
    return (
      <div className="error-boundary" role="alert">
        <div className="error-boundary-card">
          <h2>{t('error.boundary.title')}</h2>
          <p>{t('error.boundary.detail')}</p>
          <pre className="error-boundary-detail">{error.message || String(error)}</pre>
          <div className="error-boundary-actions">
            <button type="button" className="error-boundary-btn" onClick={this.reset}>
              {t('error.boundary.retry')}
            </button>
            <button type="button" className="error-boundary-btn is-primary" onClick={this.reload}>
              {t('error.boundary.reload')}
            </button>
          </div>
        </div>
      </div>
    );
  }
}

export default ErrorBoundary;

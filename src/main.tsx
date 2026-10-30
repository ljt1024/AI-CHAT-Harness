import { MessagePopProvider } from './shared/components/MessagePop';
import ErrorBoundary from './shared/components/ErrorBoundary';
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { AuthProvider } from './app/providers/AuthContext.tsx'
import { ChatProvider } from './app/providers/ChatContext.tsx'
import { LanguageProvider } from './app/providers/LanguageContext.tsx'
import { ThemeProvider, applyTheme, getPreferredTheme } from './app/providers/ThemeContext.tsx'

const rootElement = document.getElementById('root')
if (!rootElement) {
  throw new Error('Failed to find the root element')
}

applyTheme(getPreferredTheme())

createRoot(rootElement).render(
  <LanguageProvider>
    <ThemeProvider>
      <AuthProvider>
        <ChatProvider>
          <MessagePopProvider>
            <ErrorBoundary><App /></ErrorBoundary>
          </MessagePopProvider>
        </ChatProvider>
      </AuthProvider>
    </ThemeProvider>
  </LanguageProvider>
)

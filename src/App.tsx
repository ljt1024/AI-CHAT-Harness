import { BrowserRouter as Router, Navigate, Routes, Route } from 'react-router-dom';
import ChatAI from './features/chat/index.tsx'
import ShareMsg from './pages/ShareMsg/index.tsx'

function App() {

  return (
      <Router>
        <Routes>
          <Route path="/" element={<Navigate to="/ai" replace />} />
          <Route path="/ai" element={<ChatAI />} />
          <Route path="/share" element={<ShareMsg />} />
        </Routes>
      </Router>
  )
}

export default App

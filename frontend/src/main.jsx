import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import ErrorBoundary from './ui/ErrorBoundary'
import './globals.css'
import './argo.css'

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <ErrorBoundary title="The dashboard hit an error" message="Reload the page to continue." onReset={() => window.location.reload()} resetLabel="Reload"><App /></ErrorBoundary>
  </React.StrictMode>
)

import { BrowserRouter, Routes, Route } from 'react-router-dom'
import { AuthProvider } from './lib/AuthContext'
import { LocationProvider } from './lib/LocationContext'
import RequireAuth from './components/RequireAuth'
import Layout from './components/Layout'
import Login from './pages/Login'
import Home from './pages/Home'
import Menu from './pages/Menu'
import Orders from './pages/Orders'
import Robots from './pages/Robots'
import Clients from './pages/admin/Clients'

export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <Routes>
          <Route path="/login" element={<Login />} />
          <Route
            path="/"
            element={
              <RequireAuth>
                <LocationProvider>
                  <Layout />
                </LocationProvider>
              </RequireAuth>
            }
          >
            <Route index element={<Home />} />
            <Route path="menu" element={<Menu />} />
            <Route path="orders" element={<Orders />} />
            <Route path="robots" element={<Robots />} />
            <Route path="admin/clients" element={<Clients />} />
          </Route>
        </Routes>
      </AuthProvider>
    </BrowserRouter>
  )
}

import { Suspense, lazy } from 'react';
import { Navigate, Route, Routes, useParams } from 'react-router-dom';
import { ProtectedRoute, PublicOnlyRoute } from './components/ProtectedRoute';
import Landing from './pages/Landing';
import Login from './pages/Login';
import Dashboard from './pages/Dashboard';
import ProjectWorkspace from './pages/ProjectWorkspace';

// Lazy so maplibre stays out of the first load for every other page.
const OpticalPage = lazy(() => import('./pages/OpticalPage'));

function OpticalRoute() {
  return (
    <ProtectedRoute>
      <Suspense fallback={<div className="min-h-screen bg-[#09090b]" />}>
        <OpticalPage />
      </Suspense>
    </ProtectedRoute>
  );
}

// Retained as a compatibility export for incremental migration of legacy views.
export { getSupabase } from './lib/supabase';

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<Landing />} />
      <Route
        path="/login"
        element={(
          <PublicOnlyRoute>
            <Login />
          </PublicOnlyRoute>
        )}
      />
      <Route
        path="/dashboard"
        element={(
          <ProtectedRoute>
            <Dashboard />
          </ProtectedRoute>
        )}
      />
      <Route
        path="/projects/:projectId"
        element={(
          <ProtectedRoute>
            <ProjectWorkspace />
          </ProtectedRoute>
        )}
      />
      <Route path="/optical" element={<OpticalRoute />} />
      <Route path="/optical/:sceneId" element={<OpticalRoute />} />
      <Route path="/project/:projectId" element={<LegacyProjectRedirect />} />
      <Route path="/auth" element={<Navigate to="/login" replace />} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}

function LegacyProjectRedirect() {
  const { projectId } = useParams();
  return <Navigate to={`/projects/${projectId}`} replace />;
}

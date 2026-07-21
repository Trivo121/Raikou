import { Navigate, useLocation } from 'react-router-dom';
import { useAuth } from '../auth/AuthProvider';

export function ProtectedRoute({ children }) {
  const { loading, user, configurationError } = useAuth();
  const location = useLocation();

  if (loading) return <RouteLoading />;
  if (configurationError) return <ConfigurationError message={configurationError} />;
  if (!user) return <Navigate to="/login" replace state={{ from: location }} />;

  return children;
}

export function PublicOnlyRoute({ children }) {
  const { loading, user, configurationError } = useAuth();

  if (loading) return <RouteLoading />;
  if (configurationError) return <ConfigurationError message={configurationError} />;
  if (user) return <Navigate to="/dashboard" replace />;

  return children;
}

function RouteLoading() {
  return (
    <main className="min-h-screen grid place-items-center bg-[#09090b] text-zinc-400">
      Loading Raikou…
    </main>
  );
}

function ConfigurationError({ message }) {
  return (
    <main className="min-h-screen grid place-items-center bg-[#09090b] p-6 text-zinc-300">
      <section className="max-w-lg rounded-xl border border-red-900/50 bg-red-950/20 p-6">
        <h1 className="mb-2 text-lg font-semibold text-white">Frontend configuration is incomplete</h1>
        <p className="text-sm leading-6 text-zinc-400">{message}</p>
      </section>
    </main>
  );
}

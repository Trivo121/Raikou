import { useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  Archive,
  ChevronDown,
  FolderPlus,
  Grid,
  LogOut,
  Plus,
  Radar,
  Search,
  Users,
  X,
} from 'lucide-react';
import { useNavigate } from 'react-router-dom';
import { useApi, useAuth } from '../auth/AuthProvider';

/* ─── Helpers (unchanged) ──────────────────────────────────────────────────── */

function formatDate(value) {
  if (!value) return 'Recently created';
  const date = new Date(value);
  return Number.isNaN(date.valueOf())
    ? 'Recently created'
    : date.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
}

function projectName(project) {
  return project.name || project.title || 'Untitled project';
}

/* ─── Project Card ─────────────────────────────────────────────────────────── */

function ProjectCard({ project, onClick }) {
  return (
    <button type="button" onClick={onClick} className="group flex flex-col text-left w-full">
      {/* Thumbnail */}
      <div className="relative w-full aspect-[4/3] mb-3 rounded-xl overflow-hidden border border-white/[0.07] bg-[#111114] transition-all duration-200 group-hover:border-white/[0.14] group-hover:bg-[#141418]">
        {/* Dot-grid texture */}
        <div className="absolute inset-0 bg-[radial-gradient(circle,#ffffff_1px,transparent_1px)] [background-size:18px_18px] opacity-[0.03]" />
        {/* Hover tint */}
        <div className="absolute inset-0 bg-gradient-to-br from-[#0088ff]/[0.06] via-transparent to-transparent opacity-0 group-hover:opacity-100 transition-opacity duration-300" />
        {/* Document glyph */}
        <div className="absolute inset-0 flex items-center justify-center">
          <div className="flex flex-col w-9 h-12 rounded-[5px] border border-zinc-700/60 overflow-hidden transition-colors duration-200 group-hover:border-zinc-600/60">
            <span className="flex-1 border-b border-zinc-700/60 bg-zinc-700/40 group-hover:bg-zinc-600/40 transition-colors" />
            <span className="flex-1 border-b border-zinc-700/60 bg-zinc-700/60 group-hover:bg-zinc-600/60 transition-colors" />
            <span className="flex-1 bg-zinc-700/80 group-hover:bg-zinc-600/80 transition-colors" />
          </div>
        </div>
        {/* Scene count badge */}
        <div className="absolute top-2.5 right-2.5">
          <span className="text-[9px] font-bold uppercase tracking-widest text-zinc-500 bg-[#09090b]/80 border border-white/[0.07] rounded-md px-1.5 py-0.5">
            {Number(project.scene_count || 0)} scenes
          </span>
        </div>
      </div>
      {/* Meta */}
      <div className="px-0.5">
        <h2 className="text-[13px] font-medium text-zinc-300 truncate leading-snug group-hover:text-white transition-colors duration-150">
          {projectName(project)}
        </h2>
        <span className="mt-0.5 block text-[11px] text-zinc-600">
          {formatDate(project.updated_at || project.created_at)}
        </span>
      </div>
    </button>
  );
}

/* ─── Dashboard ────────────────────────────────────────────────────────────── */

const NAV_ITEMS = [
  { id: 'All', icon: Grid, label: 'All Projects' },
  { id: 'Archive', icon: Archive, label: 'Archive' },
];

export default function Dashboard() {
  /* ── Hooks (unchanged) ── */
  const api = useApi();
  const { user, signOut } = useAuth();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const [activeTab, setActiveTab] = useState('All');
  const [searchTerm, setSearchTerm] = useState('');
  const [isCreating, setIsCreating] = useState(false);
  const [newProjectName, setNewProjectName] = useState('');
  const [copySuccess, setCopySuccess] = useState(false);

  const projectsQuery = useQuery({
    queryKey: ['projects', user?.id],
    queryFn: () => api.projects.list(),
    enabled: Boolean(user?.id),
  });

  const createProject = useMutation({
    mutationFn: (name) => api.projects.create({ name }),
    onSuccess: (project) => {
      queryClient.invalidateQueries({ queryKey: ['projects', user?.id] });
      setNewProjectName('');
      setIsCreating(false);
      if (project?.id) navigate(`/projects/${project.id}`);
    },
  });

  const visibleProjects = useMemo(() => {
    if (activeTab === 'Archive') return [];
    const normalizedSearch = searchTerm.trim().toLocaleLowerCase();
    const projects = projectsQuery.data || [];
    if (!normalizedSearch) return projects;
    return projects.filter((p) => projectName(p).toLocaleLowerCase().includes(normalizedSearch));
  }, [activeTab, projectsQuery.data, searchTerm]);

  const displayName = user?.user_metadata?.full_name || user?.email?.split('@')[0] || 'My Workspace';
  const initials = displayName.slice(0, 1).toUpperCase();

  /* ── Handlers (unchanged) ── */
  function submitProject(event) {
    event.preventDefault();
    const name = newProjectName.trim();
    if (!name || createProject.isPending) return;
    createProject.mutate(name);
  }

  async function copyWorkspaceLink() {
    try {
      await navigator.clipboard.writeText(window.location.href);
      setCopySuccess(true);
      window.setTimeout(() => setCopySuccess(false), 2_000);
    } catch {
      setCopySuccess(false);
    }
  }

  /* ── Render ── */
  return (
    <div className="flex min-h-screen bg-[#09090b] font-['Inter'] text-[13px] text-zinc-400 selection:bg-[#0088ff]/30">

      {/* ── Sidebar ─────────────────────────────────────────────────────────── */}
      <aside className="hidden md:flex h-screen w-64 shrink-0 flex-col border-r border-white/[0.06] bg-[#0c0c0f]">

        {/* Workspace header */}
        <div className="px-4 pt-5 pb-3">
          <button
            type="button"
            className="group w-full flex items-center gap-3 rounded-lg px-2.5 py-2 hover:bg-white/[0.05] transition-colors"
          >
            <div className="h-[22px] w-[22px] shrink-0 rounded-[5px] bg-[#0088ff] flex items-center justify-center text-[11px] font-bold text-white select-none">
              {initials}
            </div>
            <span className="flex-1 min-w-0 text-[13px] font-medium text-zinc-200 truncate text-left">
              {displayName.split(' ')[0]}&apos;s Workspace
            </span>
            <ChevronDown
              size={13}
              className="shrink-0 text-zinc-600 group-hover:text-zinc-400 transition-colors"
            />
          </button>
        </div>

        {/* Search */}
        <div className="px-4 pb-4">
          <label className="sr-only" htmlFor="sidebar-search">Search projects</label>
          <div className="relative">
            <Search size={13} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-zinc-600" />
            <input
              id="sidebar-search"
              type="search"
              value={searchTerm}
              onChange={(e) => setSearchTerm(e.target.value)}
              placeholder="Search…"
              className="w-full rounded-lg border border-white/[0.07] bg-white/[0.04] py-[7px] pl-8 pr-3 text-[12px] text-zinc-300 outline-none placeholder:text-zinc-600 focus:border-zinc-600 focus:bg-white/[0.07] transition-all"
            />
          </div>
        </div>

        {/* Nav */}
        <nav aria-label="Project views" className="flex-1 px-3">
          <p className="mb-1.5 px-2.5 text-[10px] font-semibold uppercase tracking-wider text-zinc-600">
            Projects
          </p>

          <div className="flex flex-col gap-0.5">
            {NAV_ITEMS.map(({ id, icon: Icon, label }) => (
              <button
                key={id}
                type="button"
                onClick={() => setActiveTab(id)}
                className={`w-full flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-left text-[12px] font-medium transition-all duration-150 ${activeTab === id
                    ? 'bg-white/[0.08] text-white'
                    : 'text-zinc-500 hover:bg-white/[0.04] hover:text-zinc-300'
                  }`}
              >
                <Icon size={14} className={activeTab === id ? 'text-[#0088ff]' : 'text-zinc-600'} />
                {label}
              </button>
            ))}

            <div className="my-1.5 h-px bg-white/[0.05]" />

            <button
              type="button"
              onClick={() => setIsCreating(true)}
              className="w-full flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-left text-[12px] font-medium text-zinc-600 hover:bg-white/[0.04] hover:text-zinc-400 transition-all duration-150"
            >
              <Plus size={14} />
              New Project…
            </button>
          </div>
        </nav>

        {/* Footer */}
        <div className="border-t border-white/[0.06] px-4 py-4 flex items-center justify-between gap-3">
          <div className="flex min-w-0 items-center gap-2 text-[11px] text-zinc-600">
            <Users size={13} className="shrink-0" />
            <span className="truncate">Private workspace</span>
          </div>
          <button
            type="button"
            onClick={copyWorkspaceLink}
            className={`shrink-0 rounded-md px-2.5 py-1 text-[11px] font-medium transition-all ${copySuccess
                ? 'bg-emerald-950/50 text-emerald-400'
                : 'border border-white/[0.07] bg-white/[0.05] text-zinc-400 hover:bg-white/[0.09] hover:text-zinc-200'
              }`}
          >
            {copySuccess ? '✓ Copied' : 'Copy link'}
          </button>
        </div>
      </aside>

      {/* ── Main ────────────────────────────────────────────────────────────── */}
      <main className="flex min-w-0 flex-1 flex-col overflow-y-auto">

        {/* Sticky page header */}
        <header className="sticky top-0 z-10 flex items-center justify-between gap-4 border-b border-white/[0.06] bg-[#09090b]/80 px-8 py-[14px] backdrop-blur-md">
          <h1 className="text-[15px] font-semibold tracking-tight text-white select-none">
            {activeTab === 'All' ? 'All Projects' : activeTab}
          </h1>

          <div className="flex items-center gap-2">
            <button
              type="button"
              className="hidden sm:inline-flex items-center gap-1.5 rounded-lg border border-white/[0.08] px-3 py-1.5 text-[12px] text-zinc-400 transition-all hover:border-white/[0.14] hover:text-zinc-300"
            >
              Last viewed by me <ChevronDown size={11} className="text-zinc-600" />
            </button>

            <button
              type="button"
              onClick={() => setIsCreating((v) => !v)}
              className="inline-flex items-center gap-1.5 rounded-lg bg-[#0088ff] px-3 py-1.5 text-[12px] font-semibold text-white transition-all hover:bg-[#0077ee] active:scale-[0.97]"
            >
              <Plus size={14} />
              New Project
            </button>

            <button
              type="button"
              onClick={() => signOut()}
              className="inline-flex items-center gap-1.5 rounded-lg border border-transparent px-3 py-1.5 text-[12px] font-medium text-red-400/70 transition-all hover:border-red-900/30 hover:bg-red-950/20 hover:text-red-400"
            >
              <LogOut size={14} />
              <span className="hidden sm:inline">Sign out</span>
            </button>
          </div>
        </header>

        {/* Page body */}
        <div className="flex-1 p-8">

          {/* Create-project inline form */}
          {isCreating && (
            <div className="mb-8 rounded-xl border border-white/[0.09] bg-[#111114] p-5">
              <div className="mb-4 flex items-start justify-between gap-4">
                <div>
                  <h2 className="text-[13px] font-semibold text-white">Create new project</h2>
                  <p className="mt-0.5 text-[12px] text-zinc-500">Give your project a descriptive name to get started.</p>
                </div>
                <button
                  type="button"
                  onClick={() => setIsCreating(false)}
                  className="shrink-0 rounded-lg p-1 text-zinc-600 transition-all hover:bg-white/[0.05] hover:text-zinc-300"
                >
                  <X size={15} />
                </button>
              </div>

              <form onSubmit={submitProject} className="flex flex-col gap-3 sm:flex-row">
                <div className="min-w-0 flex-1">
                  <label htmlFor="project-name" className="sr-only">Project name</label>
                  <input
                    autoFocus
                    id="project-name"
                    value={newProjectName}
                    onChange={(e) => setNewProjectName(e.target.value)}
                    placeholder="e.g. Rotterdam port review"
                    maxLength={120}
                    className="w-full rounded-lg border border-white/[0.1] bg-[#09090b] px-3 py-2.5 text-[13px] text-white outline-none placeholder:text-zinc-600 focus:border-[#0088ff]/50 focus:ring-2 focus:ring-[#0088ff]/10 transition-all"
                  />
                  {createProject.error && (
                    <p className="mt-2 flex items-center gap-1.5 text-[12px] text-red-400">
                      <span className="h-1 w-1 shrink-0 rounded-full bg-red-400" />
                      {createProject.error.message}
                    </p>
                  )}
                </div>

                <div className="flex shrink-0 items-center gap-2">
                  <button
                    type="button"
                    onClick={() => setIsCreating(false)}
                    className="rounded-lg px-3 py-2.5 text-[12px] text-zinc-500 transition-all hover:bg-white/[0.05] hover:text-zinc-300"
                  >
                    Cancel
                  </button>
                  <button
                    type="submit"
                    disabled={!newProjectName.trim() || createProject.isPending}
                    className="rounded-lg bg-[#0088ff] px-4 py-2.5 text-[12px] font-semibold text-white transition-all hover:bg-[#0077ee] disabled:cursor-not-allowed disabled:opacity-40"
                  >
                    {createProject.isPending ? 'Creating…' : 'Create Project'}
                  </button>
                </div>
              </form>
            </div>
          )}

          {/* Content */}
          {activeTab === 'Archive' ? (
            <ArchivePlaceholder />
          ) : (
            <section aria-label="Projects">
              {projectsQuery.isPending && (
                <ProjectMessage title="Loading projects" detail="Fetching your private workspace…" />
              )}
              {projectsQuery.isError && (
                <ProjectMessage
                  title="Could not load projects"
                  detail={projectsQuery.error.message}
                  action={() => projectsQuery.refetch()}
                />
              )}
              {projectsQuery.isSuccess && visibleProjects.length === 0 && (
                <EmptyProjects hasSearch={Boolean(searchTerm.trim())} onCreate={() => setIsCreating(true)} />
              )}
              {projectsQuery.isSuccess && visibleProjects.length > 0 && (
                <div className="grid grid-cols-1 gap-5 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5">
                  {visibleProjects.map((project) => (
                    <ProjectCard
                      key={project.id}
                      project={project}
                      onClick={() => navigate(`/projects/${project.id}`)}
                    />
                  ))}
                </div>
              )}
            </section>
          )}
        </div>
      </main>
    </div>
  );
}

/* ─── Supporting components ────────────────────────────────────────────────── */

function PlaceholderShell({ icon: Icon, children }) {
  return (
    <div className="flex min-h-[220px] flex-col items-center justify-center gap-4 rounded-xl border border-white/[0.07] bg-[#0f0f12] px-6 text-center">
      <div className="flex h-10 w-10 items-center justify-center rounded-xl border border-white/[0.08] bg-white/[0.03]">
        <Icon size={18} className="text-zinc-600" />
      </div>
      {children}
    </div>
  );
}

function ProjectMessage({ title, detail, action }) {
  return (
    <PlaceholderShell icon={Radar}>
      <div>
        <h2 className="text-[14px] font-semibold text-zinc-200">{title}</h2>
        <p className="mt-1 max-w-[280px] text-[12px] text-zinc-500">{detail}</p>
      </div>
      {action && (
        <button
          type="button"
          onClick={action}
          className="rounded-lg border border-white/[0.1] bg-white/[0.04] px-3 py-1.5 text-[12px] font-medium text-zinc-300 transition-all hover:bg-white/[0.08] hover:text-white"
        >
          Try again
        </button>
      )}
    </PlaceholderShell>
  );
}

function EmptyProjects({ hasSearch, onCreate }) {
  return (
    <div className="flex min-h-[220px] flex-col items-center justify-center gap-4 rounded-xl border border-dashed border-white/[0.08] bg-[#0f0f12] px-6 text-center">
      <div className="flex h-10 w-10 items-center justify-center rounded-xl border border-white/[0.08] bg-white/[0.03]">
        <FolderPlus size={18} className="text-zinc-600" />
      </div>
      <div>
        <h2 className="text-[14px] font-semibold text-zinc-200">
          {hasSearch ? 'No matching projects' : 'No projects yet'}
        </h2>
        <p className="mt-1 text-[12px] text-zinc-500">
          {hasSearch
            ? 'Try a different search term.'
            : 'Create a project to start organising SAR scenes.'}
        </p>
      </div>
      {!hasSearch && (
        <button
          type="button"
          onClick={onCreate}
          className="rounded-lg border border-[#0088ff]/25 bg-[#0088ff]/10 px-3 py-1.5 text-[12px] font-medium text-[#0088ff] transition-all hover:border-[#0088ff]/35 hover:bg-[#0088ff]/20"
        >
          Create your first project
        </button>
      )}
    </div>
  );
}

function ArchivePlaceholder() {
  return (
    <div className="flex min-h-[220px] flex-col items-center justify-center gap-4 rounded-xl border border-dashed border-white/[0.08] bg-[#0f0f12] px-6 text-center">
      <div className="flex h-10 w-10 items-center justify-center rounded-xl border border-white/[0.08] bg-white/[0.03]">
        <Archive size={18} className="text-zinc-600" />
      </div>
      <div>
        <h2 className="text-[14px] font-semibold text-zinc-200">Archive coming soon</h2>
        <p className="mt-1 text-[12px] text-zinc-500">Archived project management isn&apos;t part of the first release.</p>
      </div>
    </div>
  );
}
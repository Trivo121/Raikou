import React, { useState, useEffect } from 'react';
import { 
  Search, Grid, Archive, Plus, Users, ChevronDown 
} from 'lucide-react';
import { getSupabase } from '../App';

export default function Dashboard() {
  const [activeTab, setActiveTab] = useState('All');
  const [copySuccess, setCopySuccess] = useState(false);
  const [profile, setProfile] = useState(null);

  useEffect(() => {
    async function fetchProfile() {
      const supabase = getSupabase();
      if (!supabase) return;
      
      const { data: { session } } = await supabase.auth.getSession();
      if (!session) {
        window.history.pushState({}, '', '/login');
        window.dispatchEvent(new PopStateEvent('popstate'));
        return;
      }

      if (session) {
        // Fetch from the public.profiles table
        const { data, error } = await supabase
          .from('profiles')
          .select('*')
          .eq('id', session.user.id)
          .single();
          
        if (data && !error) {
          setProfile(data);
        } else {
          // If no profile found (e.g. trigger didn't run because user existed before trigger was created),
          // fallback to auth metadata
          setProfile({
            full_name: session.user.user_metadata?.full_name || session.user.email,
            avatar_url: session.user.user_metadata?.avatar_url
          });
        }
      }
    }
    fetchProfile();
  }, []);

  // Simple copy link logic
  const handleCopyLink = () => {
    navigator.clipboard.writeText(window.location.href);
    setCopySuccess(true);
    setTimeout(() => setCopySuccess(false), 2000);
  };

  const handleLogout = async () => {
    const supabase = getSupabase();
    if (supabase) {
      await supabase.auth.signOut();
    }
  };

  const handleNewProject = () => {
    window.history.pushState({}, '', '/ingestion');
    window.dispatchEvent(new PopStateEvent('popstate'));
  };

  // Redesigned navigation items
  const navItems = [
    { name: 'All', icon: Grid },
    { name: 'Archive', icon: Archive },
  ];

  // Project List matching reference
  const sarProjects = [
    { id: 'proj-1', title: 'Primary Anything', subtext: 'Viewed 19h ago', badge: 'FREE' },
    { id: 'proj-2', title: 'S1A_IW_Rotterdam', subtext: 'Viewed 1d ago', badge: 'FREE' },
    { id: 'proj-3', title: 'South_China_Sea_Detection', subtext: 'Viewed 2d ago', badge: 'FREE' },
    { id: 'proj-4', title: 'Amazon_Deforestation_SAR', subtext: 'Viewed 5d ago', badge: 'FREE' },
    { id: 'proj-5', title: 'Suez_Transit_RAG', subtext: 'Viewed 1w ago', badge: 'FREE' },
    { id: 'proj-6', title: 'Strait_of_Hormuz_Scan', subtext: 'Viewed 2w ago', badge: 'FREE' },
  ];

  return (
    // Outer Container: Geometric Inter look, ultra-dark tone background, light silver-gray text
    <div className="min-h-screen bg-[#09090b] text-[#c5c5c9] font-['Inter'] text-[13px] flex selection:bg-[#0088ff]/30">
      
      {/* Sidebar: Clean, very dark, borders kept thin and subtle */}
      <aside className="w-60 shrink-0 border-r border-[#1c1c22] bg-[#0c0c0e] p-3 flex flex-col h-screen select-none">
        
        {/* Workspace Dropdown Selector */}
        <div className="flex items-center justify-between p-1.5 mb-2.5 hover:bg-[#1a1a1f] rounded-lg cursor-pointer transition-colors duration-150 group">
          <div className="flex items-center gap-2">
            {profile?.avatar_url ? (
              <img src={profile.avatar_url} alt="Avatar" className="w-5.5 h-5.5 rounded" style={{ width: '22px', height: '22px' }} />
            ) : (
              <div className="w-5.5 h-5.5 rounded bg-[#0088ff] flex items-center justify-center text-[11px] font-bold text-white uppercase" style={{ width: '22px', height: '22px' }}>
                {profile?.full_name ? profile.full_name.charAt(0).toUpperCase() : 'U'}
              </div>
            )}
            <span className="text-white font-medium text-[13px] tracking-tight truncate max-w-[130px]">
              {profile?.full_name ? `${profile.full_name.split(' ')[0]}'s Workspace` : 'My Workspace'}
            </span>
          </div>
          <ChevronDown size={14} className="text-zinc-500 group-hover:text-zinc-300 transition-colors" />
        </div>

        {/* Custom Muted Search Field */}
        <div className="relative mb-4">
          <Search size={13} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-zinc-500" />
          <input 
            type="text" 
            placeholder="Search..." 
            className="w-full bg-[#18181b] border border-[#242429] text-white placeholder:text-zinc-500 rounded-lg pl-8 pr-3 py-1.5 outline-none text-[12px] focus:border-zinc-700 transition-colors"
          />
        </div>

        {/* Projects Categorization */}
        <div className="flex-1 flex flex-col">
          <span className="text-zinc-500 font-semibold text-[10px] uppercase tracking-wider mb-2 px-2.5">
            Projects
          </span>
          <nav className="flex flex-col gap-0.5">
            {navItems.map((item) => {
              const Icon = item.icon;
              const isActive = activeTab === item.name;
              return (
                <button
                  key={item.name}
                  onClick={() => setActiveTab(item.name)}
                  className={`
                    flex items-center gap-2.5 px-2.5 py-1.5 rounded-lg text-left transition-colors duration-150
                    ${isActive 
                      ? 'bg-[#1e1e24] text-white font-medium' 
                      : 'text-zinc-400 hover:text-zinc-200 hover:bg-[#15151a]'
                    }
                  `}
                >
                  <Icon size={14} className={isActive ? 'text-white' : 'text-zinc-400'} />
                  <span className="text-[12px]">{item.name}</span>
                </button>
              );
            })}
            
            {/* New Folder Action Button */}
            <button className="flex items-center gap-2.5 px-2.5 py-1.5 rounded-lg text-left text-zinc-500 hover:text-zinc-300 hover:bg-[#15151a] transition-colors duration-150">
              <Plus size={14} className="text-zinc-500" />
              <span className="text-[12px]">New Folder...</span>
            </button>
          </nav>
        </div>

        {/* Sidebar Footer */}
        <div className="mt-auto border-t border-[#1c1c22] pt-3 flex items-center justify-between">
          <button className="flex items-center gap-2 text-zinc-400 hover:text-white transition-colors py-1 px-1.5 rounded hover:bg-[#1a1a1f] text-[12px]">
            <Users size={14} className="text-zinc-500" />
            <span>Invite your team</span>
          </button>
          <button 
            onClick={handleCopyLink}
            className={`
              text-[11px] font-medium px-2.5 py-1.5 rounded-lg border transition-all active:scale-[0.96]
              ${copySuccess 
                ? 'bg-emerald-950/40 border-emerald-800 text-emerald-400' 
                : 'bg-[#18181c] border-[#25252b] text-zinc-200 hover:bg-[#202026] hover:text-white'
              }
            `}
          >
            {copySuccess ? 'Copied!' : 'Copy Link'}
          </button>
        </div>

      </aside>

      {/* Main Panel Canvas Area */}
      <main className="flex-1 p-8 overflow-y-auto h-screen flex flex-col">
        
        {/* Header toolbar matching reference */}
        <header className="flex items-center justify-between mb-8 select-none">
          <h1 className="text-xl text-white font-semibold tracking-tight">{activeTab}</h1>
          <div className="flex items-center gap-2">
            
            {/* Sort/Filter Selection Menu */}
            <button className="flex items-center gap-1.5 bg-[#18181b] border border-[#242429] text-zinc-300 px-3 py-1.5 rounded-lg hover:bg-[#202025] hover:text-white transition-colors text-[12px] font-medium">
              <span>Last viewed by me</span>
              <ChevronDown size={12} className="text-zinc-500" />
            </button>

            {/* Accent Primary Blue Action Button */}
            <button onClick={handleNewProject} className="bg-[#0088ff] hover:bg-[#007cdb] active:scale-[0.97] transition-all text-white font-semibold px-3 py-1.5 rounded-lg text-[12px]">
              New Project
            </button>

            {/* Logout Button */}
            <button onClick={handleLogout} className="bg-transparent border border-red-900/50 hover:bg-red-950/30 text-red-400 hover:text-red-300 active:scale-[0.97] transition-all font-semibold px-3 py-1.5 rounded-lg text-[12px] ml-2">
              Sign Out
            </button>

          </div>
        </header>

        {/* Project Vertical / Portrait grid */}
        <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 gap-x-6 gap-y-8">
          {sarProjects.map((project) => (
            <div 
              key={project.id} 
              className="group flex flex-col cursor-pointer"
            >
              {/* Portrait Preview Box: Aspect Ratio 3:4 */}
              <div className="aspect-[3/4] w-full bg-[#131316] rounded-xl border border-[#202025] group-hover:border-[#383840] hover:bg-[#16161a] transition-all duration-200 flex items-center justify-center relative overflow-hidden mb-3 shadow-sm">
                
                {/* Simulated Grid Texture/Dotted pattern */}
                <div className="absolute inset-0 opacity-2 bg-[radial-gradient(#ffffff_1px,transparent_1px)] [background-size:16px_16px]"></div>
                
                {/* SVG Framer Logo centered in the vertical card */}
                <svg width="24" height="36" viewBox="0 0 20 30" fill="none" xmlns="http://www.w3.org/2000/svg" className="text-zinc-700 group-hover:text-zinc-400 transition-colors duration-200">
                  <path d="M0 0H20V10H0V0Z" fill="currentColor" fillOpacity="0.25" />
                  <path d="M0 10H20L10 20H0V10Z" fill="currentColor" fillOpacity="0.5" />
                  <path d="M0 20L10 30V20H0Z" fill="currentColor" fillOpacity="0.75" />
                  <path d="M0 0H20V10H0V0Z" stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round" />
                  <path d="M0 10H20L10 20H0V10Z" stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round" />
                  <path d="M0 20L10 30V20H0Z" stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round" />
                </svg>
                
              </div>

              {/* Card Title, Relative viewing time, and Badge Pill */}
              <div className="flex items-start justify-between px-1">
                <div className="flex flex-col gap-0.5 min-w-0">
                  <h3 className="text-zinc-200 font-medium text-[13px] truncate group-hover:text-[#0088ff] transition-colors duration-150">
                    {project.title}
                  </h3>
                  <span className="text-zinc-500 text-[11px] font-normal">
                    {project.subtext}
                  </span>
                </div>
                
                {/* Capsule Status Badge */}
                <span className="shrink-0 bg-[#202025]/60 border border-[#2b2b35]/65 text-[9px] font-bold text-zinc-400 px-1.5 py-0.5 rounded uppercase tracking-wider scale-95 origin-right">
                  {project.badge}
                </span>
              </div>
            </div>
          ))}
        </div>
      </main>

    </div>
  );
}

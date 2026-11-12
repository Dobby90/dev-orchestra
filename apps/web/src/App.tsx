import { Component, lazy, Suspense, useState, type ReactNode } from "react";
import {
  BrowserRouter,
  Link,
  NavLink,
  Route,
  Routes,
  useLocation,
} from "react-router-dom";
import {
  QueryClient,
  QueryClientProvider,
  useMutation,
  useQueryClient,
} from "@tanstack/react-query";
import {
  AudioLines,
  ChevronLeft,
  ChevronRight,
  FolderGit2,
  LayoutDashboard,
  LogOut,
  Menu,
  Moon,
  Settings2,
  Sun,
  X,
} from "lucide-react";
import { RunDraftProvider } from "./hooks/runDraft";
import { NotificationCenter } from "./components/NotificationCenter";
import { SessionProvider } from "./hooks/session";
import { api } from "./api/resources";
import { setCsrf } from "./api/client";
const DashboardPage = lazy(() =>
  import("./pages/DashboardPage").then((module) => ({
    default: module.DashboardPage,
  })),
);
const ProjectsPage = lazy(() =>
  import("./pages/ProjectsPage").then((module) => ({
    default: module.ProjectsPage,
  })),
);
const ProjectDetailPage = lazy(() =>
  import("./pages/ProjectDetailPage").then((module) => ({
    default: module.ProjectDetailPage,
  })),
);
const NewRunPage = lazy(() =>
  import("./pages/NewRunPage").then((module) => ({
    default: module.NewRunPage,
  })),
);
const RunDetailPage = lazy(() =>
  import("./pages/RunDetailPage").then((module) => ({
    default: module.RunDetailPage,
  })),
);
const SettingsPage = lazy(() =>
  import("./pages/SettingsPage").then((module) => ({
    default: module.SettingsPage,
  })),
);
import { Empty, ErrorNotice, Loading } from "./components/common";
import { Button } from "./components/ui/button";
class PageBoundary extends Component<
  { children: ReactNode },
  { failed: boolean }
> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  render() {
    return this.state.failed ? (
      <ErrorNotice
        error={
          new Error(
            "화면을 불러오지 못했습니다. 새로고침해 다시 연결해 주세요.",
          )
        }
        retry={() => window.location.reload()}
      />
    ) : (
      this.props.children
    );
  }
}
export function createQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, refetchOnWindowFocus: true, staleTime: 1000 },
      mutations: { retry: false },
    },
  });
}
function Shell() {
  const [menu, setMenu] = useState(false);
  const [collapsed, setCollapsed] = useState(() => {
    try {
      return localStorage.getItem("devorchestra-sidebar-collapsed") === "true";
    } catch {
      return false;
    }
  });
  const [light, setLight] = useState(() => {
    try {
      return localStorage.getItem("devorchestra-theme") === "light";
    } catch {
      return false;
    }
  });
  const client = useQueryClient(),
    location = useLocation();
  const logout = useMutation({
    mutationFn: api.logout,
    onSuccess: () => {
      setCsrf(null);
      client.cancelQueries();
      client.removeQueries({
        predicate: (query) => query.queryKey[0] !== "session",
      });
      client.setQueryData(["session"], null);
    },
  });
  function theme() {
    const next = !light;
    setLight(next);
    document.documentElement.classList.toggle("dark", !next);
    try {
      localStorage.setItem("devorchestra-theme", next ? "light" : "dark");
    } catch {
      /* Theme persistence is optional. */
    }
  }
  function toggleSidebar() {
    const next = !collapsed;
    setCollapsed(next);
    try {
      localStorage.setItem("devorchestra-sidebar-collapsed", String(next));
    } catch {
      /* Sidebar persistence is optional. */
    }
  }
  return (
    <div className={`app-shell${collapsed ? " sidebar-collapsed" : ""}`}>
      <a href="#main" className="skip-link">
        본문으로 건너뛰기
      </a>
      {menu && (
        <button
          className="sidebar-backdrop"
          aria-label="메뉴 닫기"
          onClick={() => setMenu(false)}
        />
      )}
      <aside
        id="workspace-nav"
        className={`sidebar ${menu ? "sidebar-open" : ""}`}
        aria-label="주 메뉴"
      >
        <button
          className="icon-button sidebar-toggle"
          aria-label={collapsed ? "메뉴 펼치기" : "메뉴 접기"}
          aria-expanded={!collapsed}
          aria-controls="workspace-nav"
          onClick={toggleSidebar}
        >
          {collapsed ? <ChevronRight size={16} /> : <ChevronLeft size={16} />}
        </button>
        <Link to="/" className="brand" aria-label="DevOrchestra">
          <span className="brand-mark">
            <AudioLines size={21} />
          </span>
          <span className="sidebar-text">DevOrchestra</span>
        </Link>
        <div className="workspace-selector">
          <span className="workspace-avatar">L</span>
          <div>
            <strong>Local workspace</strong>
            <span>개인 개발 환경</span>
          </div>
          <span className="local-dot" />
        </div>
        <p className="nav-label">WORKSPACE</p>
        <nav>
          {[
            { to: "/", label: "대시보드", icon: LayoutDashboard },
            { to: "/projects", label: "프로젝트", icon: FolderGit2 },
            { to: "/settings", label: "연결 및 환경", icon: Settings2 },
          ].map((item) => (
            <NavLink
              end={item.to === "/"}
              to={item.to}
              key={item.to}
              className="sidebar-item"
              aria-label={item.label}
              data-tooltip={item.label}
              onClick={() => setMenu(false)}
            >
              <item.icon size={17} />
              <span className="sidebar-text">{item.label}</span>
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="local-status">
            <span className="local-dot" />
            <div>
              <strong>Local environment</strong>
              <p>격리된 Worktree 실행</p>
            </div>
          </div>
          <Button
            variant="ghost"
            className="sidebar-item"
            aria-label="로그아웃"
            data-tooltip="로그아웃"
            onClick={() => logout.mutate()}
            disabled={logout.isPending}
          >
            <LogOut size={16} />
            <span className="sidebar-text">로그아웃</span>
          </Button>
        </div>
      </aside>
      <div className="main-shell">
        <header className="topbar">
          <div className="breadcrumb">
            <button
              className="icon-button mobile-menu"
              aria-label="메뉴 열기"
              aria-expanded={menu}
              aria-controls="workspace-nav"
              onClick={() => setMenu(!menu)}
            >
              {menu ? <X size={18} /> : <Menu size={18} />}
            </button>
            <span>Workspace</span>
            <ChevronRight size={14} />
            <strong>
              {location.pathname.startsWith("/projects")
                ? "프로젝트"
                : location.pathname.startsWith("/runs")
                  ? "개발 작업"
                  : location.pathname === "/settings"
                    ? "연결 및 환경"
                    : "개요"}
            </strong>
          </div>
          <div className="topbar-actions">
            <NotificationCenter />
            <span className="local-pill">
              <span className="local-dot" />
              로컬 전용
            </span>
            <button
              className="icon-button"
              aria-label={light ? "다크 모드로 전환" : "라이트 모드로 전환"}
              onClick={theme}
            >
              {light ? <Moon size={18} /> : <Sun size={18} />}
            </button>
          </div>
        </header>
        <main id="main" tabIndex={-1}>
          {logout.error && <ErrorNotice error={logout.error} />}
          <PageBoundary key={location.pathname}>
            <Suspense fallback={<Loading />}>
              <Routes>
                <Route path="/" element={<DashboardPage />} />
                <Route path="/projects" element={<ProjectsPage />} />
                <Route path="/projects/:id" element={<ProjectDetailPage />} />
                <Route path="/runs/new" element={<NewRunPage />} />
                <Route path="/runs/:id" element={<RunDetailPage />} />
                <Route path="/settings" element={<SettingsPage />} />
                <Route
                  path="*"
                  element={
                    <Empty title="페이지를 찾을 수 없습니다">
                      <Link to="/">대시보드로 이동</Link>
                    </Empty>
                  }
                />
              </Routes>
            </Suspense>
          </PageBoundary>
          <footer className="app-footer">
            DEVORCHESTRA<span>기획부터 검증까지, 하나의 흐름으로.</span>
          </footer>
        </main>
      </div>
    </div>
  );
}
export function App() {
  const [client] = useState(createQueryClient);
  return (
    <QueryClientProvider client={client}>
      <BrowserRouter>
        <SessionProvider>
          <RunDraftProvider>
            <Shell />
          </RunDraftProvider>
        </SessionProvider>
      </BrowserRouter>
    </QueryClientProvider>
  );
}

import { useEffect, useState } from "react";
import { Agents } from "./pages/Agents";
import { Approvals } from "./pages/Approvals";
import { Dashboard } from "./pages/Dashboard";
import { Projects } from "./pages/Projects";
import { Settings } from "./pages/Settings";
import { TaskDetail } from "./pages/TaskDetail";
import { Tasks } from "./pages/Tasks";

const NAV = [["#/", "Dashboard"], ["#/tasks", "Tasks"], ["#/projects", "Projects"], ["#/agents", "Agent Activity"], ["#/approvals", "Approvals"], ["#/settings", "Settings"]] as const;

export function useHash(): string {
  const [hash, setHash] = useState(window.location.hash || "#/");
  useEffect(() => {
    const on = () => setHash(window.location.hash || "#/");
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return hash;
}

export function App() {
  const hash = useHash();
  let page;
  const m = hash.match(/^#\/tasks\/(.+)$/);
  if (m) page = <TaskDetail id={m[1]} />;
  else if (hash.startsWith("#/tasks")) page = <Tasks />;
  else if (hash.startsWith("#/projects")) page = <Projects />;
  else if (hash.startsWith("#/agents")) page = <Agents />;
  else if (hash.startsWith("#/approvals")) page = <Approvals />;
  else if (hash.startsWith("#/settings")) page = <Settings />;
  else page = <Dashboard />;
  const active = (href: string) => (href === "#/" ? hash === "#/" || hash === "#" : hash.startsWith(href));
  return (
    <div className="shell">
      <nav className="nav" aria-label="Main">
        <div className="brand">Command Center</div>
        {NAV.map(([href, label]) => (
          <a key={href} href={href} className={active(href) ? "nav-link active" : "nav-link"} aria-current={active(href) ? "page" : undefined}>{label}</a>
        ))}
      </nav>
      <main className="main">{page}</main>
    </div>
  );
}

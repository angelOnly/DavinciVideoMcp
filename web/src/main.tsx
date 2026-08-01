import { ChangeEvent, FormEvent, useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";

type Asset = {
  id: string;
  original_name: string;
  role: string;
  state: "uploading" | "validating" | "ready" | "invalid";
  warnings: Array<{ message: string }>;
  errors: Array<{ message: string }>;
};

type Version = {
  id: string;
  version_number: number;
  output_path: string;
  state: string;
};

type Project = {
  id: string;
  title: string;
  status: string;
  brief: Record<string, unknown>;
  assets?: Asset[];
  versions?: Version[];
};

type ApiError = { error?: { message?: string } };

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init);
  const payload = (await response.json()) as T & ApiError;
  if (!response.ok) {
    throw new Error(payload.error?.message || "请求失败，请稍后重试。");
  }
  return payload;
}

function App() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [title, setTitle] = useState("");
  const [preset, setPreset] = useState<"fragment_montage" | "interview_excerpt">("fragment_montage");
  const [message, setMessage] = useState("正在读取本地项目…");
  const [busy, setBusy] = useState(false);

  const activeProject = useMemo(
    () => projects.find((project) => project.id === activeId) ?? null,
    [activeId, projects],
  );

  const refresh = async (keepActive = true) => {
    const result = await request<{ projects: Project[] }>("/api/projects");
    setProjects(result.projects);
    if (!keepActive && result.projects[0]) {
      setActiveId(result.projects[0].id);
    }
    if (activeId) {
      const detailed = await request<Project>(`/api/projects/${activeId}`);
      setProjects((current) => current.map((project) => (project.id === detailed.id ? detailed : project)));
    }
  };

  useEffect(() => {
    void refresh(false)
      .then(() => setMessage("本地工作台已就绪。"))
      .catch((error: Error) => setMessage(error.message));
    const interval = window.setInterval(() => void refresh(), 4000);
    return () => window.clearInterval(interval);
  }, [activeId]);

  const selectProject = async (id: string) => {
    setActiveId(id);
    const detailed = await request<Project>(`/api/projects/${id}`);
    setProjects((current) => current.map((project) => (project.id === id ? detailed : project)));
  };

  const createProject = async (event: FormEvent) => {
    event.preventDefault();
    if (!title.trim()) return;
    setBusy(true);
    try {
      const brief = preset === "fragment_montage"
        ? { testing_preset: preset, orientation: "portrait", max_clip_seconds: 8 }
        : { testing_preset: preset, orientation: "landscape", max_duration_seconds: 90 };
      const project = await request<Project>("/api/projects", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title, brief }),
      });
      setTitle("");
      setProjects((current) => [project, ...current]);
      setActiveId(project.id);
      setMessage("项目已创建。请上传已在本机准备好的素材。");
    } catch (error) {
      setMessage((error as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const uploadFiles = async (event: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(event.target.files || []);
    if (!activeProject || files.length === 0) return;
    setBusy(true);
    try {
      for (const file of files) {
        const role = activeProject.brief.testing_preset === "interview_excerpt" ? "interview" : "primary";
        await request<Asset>(
          `/api/projects/${activeProject.id}/assets/upload?filename=${encodeURIComponent(file.name)}&role=${role}`,
          { method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: file },
        );
      }
      await selectProject(activeProject.id);
      setMessage("服务端校验完成；请查看每个素材的状态与可操作提示。");
    } catch (error) {
      setMessage((error as Error).message);
    } finally {
      setBusy(false);
      event.target.value = "";
    }
  };

  const startRun = async () => {
    if (!activeProject?.assets) return;
    setBusy(true);
    try {
      const assetIds = activeProject.assets.map((asset) => asset.id);
      await request(`/api/projects/${activeProject.id}/runs`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ asset_ids: assetIds }),
      });
      setMessage("剪辑任务已持久化，后台 Worker 会领取并执行。页面会自动刷新状态。");
      await selectProject(activeProject.id);
    } catch (error) {
      setMessage((error as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const readyToStart = Boolean(
    activeProject?.assets?.length && activeProject.assets.every((asset) => asset.state === "ready"),
  );

  return (
    <main>
      <section className="hero">
        <p className="eyebrow">LOCAL VIDEO EDITING WORKBENCH</p>
        <h1>让真实素材先通过验证，再进入剪辑。</h1>
        <p>本地项目、素材状态、任务与版本都保存在工作站；Resolve 只由后台 Worker 写入。</p>
      </section>

      <section className="workspace">
        <aside className="project-list" aria-label="项目列表">
          <h2>项目</h2>
          <form onSubmit={createProject} className="new-project">
            <label>
              项目名称
              <input value={title} onChange={(event) => setTitle(event.target.value)} placeholder="例如：猫咪日常" />
            </label>
            <label>
              测试预设
              <select value={preset} onChange={(event) => setPreset(event.target.value as typeof preset)}>
                <option value="fragment_montage">拍摄碎片拼接</option>
                <option value="interview_excerpt">人物访谈开场节选</option>
              </select>
            </label>
            <button disabled={busy}>创建项目</button>
          </form>
          <div className="project-buttons">
            {projects.map((project) => (
              <button
                key={project.id}
                className={project.id === activeId ? "project active" : "project"}
                onClick={() => void selectProject(project.id)}
              >
                <span>{project.title}</span>
                <small>{project.status}</small>
              </button>
            ))}
          </div>
        </aside>

        <section className="details" aria-live="polite">
          <p className="notice">{message}</p>
          {!activeProject ? (
            <div className="empty"><p>创建或选择一个项目开始。</p></div>
          ) : (
            <>
              <header className="project-header">
                <div>
                  <p className="eyebrow">当前项目</p>
                  <h2>{activeProject.title}</h2>
                </div>
                <span className="status">{activeProject.status}</span>
              </header>

              <section className="panel">
                <div className="panel-heading">
                  <div><h3>素材与服务端校验</h3><p>上传完成后才会产生权威状态；浏览器预检不能替代它。</p></div>
                  <label className="upload-button">
                    <input type="file" accept="video/*,audio/*,image/*" multiple onChange={uploadFiles} disabled={busy} />
                    选择素材
                  </label>
                </div>
                <div className="asset-list">
                  {(activeProject.assets || []).map((asset) => (
                    <article key={asset.id} className={`asset ${asset.state}`}>
                      <div><strong>{asset.original_name}</strong><span>{asset.role}</span></div>
                      <b>{asset.state}</b>
                      {[...asset.errors, ...asset.warnings].map((item, index) => <p key={index}>{item.message}</p>)}
                    </article>
                  ))}
                  {activeProject.assets?.length === 0 && <p className="muted">尚未上传素材。</p>}
                </div>
                <button className="primary" disabled={!readyToStart || busy} onClick={() => void startRun()}>
                  开始制作
                </button>
              </section>

              <section className="panel">
                <div className="panel-heading"><div><h3>成片候选</h3><p>发布后的版本不会被后续剪辑覆盖。</p></div></div>
                <div className="versions">
                  {(activeProject.versions || []).map((version) => (
                    <article key={version.id}>
                      <h4>v{version.version_number}</h4>
                      <p>{version.state}</p>
                      <video controls preload="metadata" src={`/api/versions/${version.id}/media`} />
                    </article>
                  ))}
                  {activeProject.versions?.length === 0 && <p className="muted">当前还没有可播放成片候选。</p>}
                </div>
              </section>
            </>
          )}
        </section>
      </section>
    </main>
  );
}

createRoot(document.getElementById("root")!).render(<App />);

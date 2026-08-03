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

type VideoArtifact = {
  id: string;
  artifact_type: "technical_preview" | "work_preview" | "candidate_render";
  state: string;
  output_path: string;
};

type Project = {
  id: string;
  title: string;
  status: string;
  brief: Record<string, unknown>;
  assets?: Asset[];
  versions?: Version[];
  artifacts?: VideoArtifact[];
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
      setMessage("技术测试项目已创建。请上传已在本机准备好的素材。技术预览不会发布为成片候选。");
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
        body: JSON.stringify({ asset_ids: assetIds, kind: "engine_smoke" }),
      });
      setMessage("Engine 冒烟任务已持久化。它只会生成内部技术预览，不会生成成片候选。页面会自动刷新状态。");
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
  const technicalPreviews = (activeProject?.artifacts || []).filter((artifact) => artifact.artifact_type === "technical_preview");
  const workPreviews = (activeProject?.artifacts || []).filter((artifact) => artifact.artifact_type === "work_preview");

  return (
    <main>
      <section className="hero">
        <p className="eyebrow">DEVELOPMENT TEST WORKBENCH</p>
        <h1>这里仅用于验证素材与 Resolve 技术链路。</h1>
        <p>技术预览、内部工作版与成片候选严格分开；本页不能把任何测试渲染发布为成片候选。</p>
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
              Engine 测试预设
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
                  运行 Engine 冒烟测试
                </button>
              </section>

              <section className="panel">
                <div className="panel-heading"><div><h3>内部技术预览</h3><p>仅证明受管 Resolve 写入与渲染；没有转写、素材理解、选片、收尾或候选资格。</p></div></div>
                <div className="versions">
                  {technicalPreviews.map((artifact) => (
                    <article key={artifact.id}>
                      <h4>技术预览</h4>
                      <p>{artifact.state}</p>
                      <video controls preload="metadata" src={`/api/artifacts/${artifact.id}/media`} />
                    </article>
                  ))}
                  {technicalPreviews.length === 0 && <p className="muted">当前还没有内部技术预览。</p>}
                </div>
              </section>

              <section className="panel">
                <div className="panel-heading"><div><h3>内部工作版</h3><p>只有完整专业链路生成并复核后才会出现；它仍不是用户可见的成片候选。</p></div></div>
                <div className="versions">
                  {workPreviews.map((artifact) => (
                    <article key={artifact.id}>
                      <h4>工作版</h4>
                      <p>{artifact.state}</p>
                      <video controls preload="metadata" src={`/api/artifacts/${artifact.id}/media`} />
                    </article>
                  ))}
                  {workPreviews.length === 0 && <p className="muted">当前还没有经过专业链路复核的内部工作版。</p>}
                </div>
              </section>

              <section className="panel">
                <div className="panel-heading"><div><h3>成片候选</h3><p>仅展示通过完整专业前置、收尾和候选验证后发布的不可覆盖版本。</p></div></div>
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

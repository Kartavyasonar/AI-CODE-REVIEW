import { useState, useEffect, useRef } from "react";

const API = "https://ai-code-review-backend-4b80.onrender.com";

const T = {
  bg:"#0a0a0f", surface:"#111118", card:"#16161f", border:"#1e1e2e",
  borderHi:"#2d2d44", purple:"#7c3aed", purpleL:"#a78bfa", blue:"#3b82f6",
  blueL:"#93c5fd", green:"#10b981", greenL:"#6ee7b7", amber:"#f59e0b",
  red:"#ef4444", text:"#e2e0ff", textSub:"#8b8aaa", textDim:"#4a4a6a",
};

const SEV = {
  critical:{ bg:"#2d0d0d", border:"#7f1d1d", text:"#fca5a5", dot:"#ef4444", label:"CRITICAL" },
  high:    { bg:"#1c1208", border:"#78350f", text:"#fcd34d", dot:"#f59e0b", label:"HIGH" },
  medium:  { bg:"#0d1a0d", border:"#14532d", text:"#6ee7b7", dot:"#10b981", label:"MEDIUM" },
  low:     { bg:"#0d1120", border:"#1e3a8a", text:"#93c5fd", dot:"#3b82f6", label:"LOW" },
  info:    { bg:"#111118", border:"#1e1e2e", text:"#8b8aaa", dot:"#4a4a6a", label:"INFO" },
};

const STEPS = {
  waking:        { label:"Waking backend...",         icon:"⏳", color:"#8b8aaa" },
  cloning:       { label:"Cloning repository",        icon:"⬇", color:"#3b82f6" },
  walking:       { label:"Walking file tree",         icon:"🌲", color:"#3b82f6" },
  chunking:      { label:"AST chunking code",         icon:"✂", color:"#7c3aed" },
  embedding:     { label:"Embedding into ChromaDB",   icon:"🧠", color:"#7c3aed" },
  hyde:          { label:"HyDE query expansion",      icon:"🔮", color:"#a78bfa" },
  agents:        { label:"4 Agents starting",         icon:"🤖", color:"#f59e0b" },
  agents_running:{ label:"Agents analyzing",          icon:"⚙",  color:"#f59e0b" },
  agent_bug:     { label:"Bug agent running",         icon:"🐛", color:"#ef4444" },
  agent_security:{ label:"Security agent running",    icon:"🔒", color:"#ef4444" },
  agent_quality: { label:"Quality agent running",     icon:"🧹", color:"#3b82f6" },
  agent_perf:    { label:"Performance agent running", icon:"⚡", color:"#10b981" },
  chains:        { label:"Multi-hop chain detection", icon:"⛓",  color:"#ef4444" },
  synthesizing:  { label:"Synthesizing report",       icon:"📝", color:"#10b981" },
  done:          { label:"Complete",                  icon:"✅", color:"#10b981" },
  error:         { label:"Error",                     icon:"❌", color:"#ef4444" },
};

function grade(s) {
  if (s>=85) return { label:"A", color:T.green };
  if (s>=70) return { label:"B", color:T.greenL };
  if (s>=55) return { label:"C", color:T.amber };
  if (s>=40) return { label:"D", color:"#f97316" };
  return            { label:"F", color:T.red };
}

function elapsedStr(ms) {
  const s = Math.floor(ms/1000);
  return s < 60 ? `${s}s` : `${Math.floor(s/60)}m ${s%60}s`;
}

function ScoreRing({ score, size=160 }) {
  const g = grade(score);
  const r = size*0.38, circ = 2*Math.PI*r;
  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
      <circle cx={size/2} cy={size/2} r={r} fill="none" stroke={T.border} strokeWidth="8"/>
      <circle cx={size/2} cy={size/2} r={r} fill="none" stroke={g.color} strokeWidth="8"
        strokeDasharray={`${(score/100)*circ} ${circ}`} strokeLinecap="round"
        transform={`rotate(-90 ${size/2} ${size/2})`}
        style={{transition:"stroke-dasharray 1.2s ease"}}/>
      <text x={size/2} y={size/2-8} textAnchor="middle" fontSize={size*0.22} fontWeight="800" fill={g.color}>{score}</text>
      <text x={size/2} y={size/2+14} textAnchor="middle" fontSize={size*0.09} fill={T.textSub}>/100</text>
      <text x={size/2} y={size/2+30} textAnchor="middle" fontSize={size*0.09} fontWeight="600" fill={g.color}>{g.label}</text>
    </svg>
  );
}

function LiveLog({ steps }) {
  const ref = useRef();
  useEffect(() => { if(ref.current) ref.current.scrollTop = ref.current.scrollHeight; }, [steps]);
  return (
    <div ref={ref} style={{height:200,overflowY:"auto",fontFamily:"monospace",fontSize:12,
      background:"#080810",borderRadius:10,padding:"12px 16px",border:`1px solid ${T.border}`}}>
      {steps.map((s,i) => {
        const meta = STEPS[s.step] || { icon:"·", color:T.textDim };
        return (
          <div key={i} style={{display:"flex",gap:10,marginBottom:6,opacity:i<steps.length-1?0.55:1}}>
            <span style={{color:meta.color,minWidth:16}}>{meta.icon}</span>
            <span style={{color:T.textDim,minWidth:80,flexShrink:0}}>
              {new Date(s.ts*1000).toLocaleTimeString("en-GB",{hour12:false})}
            </span>
            <span style={{color:i===steps.length-1?T.text:T.textSub}}>{s.detail||meta.label}</span>
          </div>
        );
      })}
      {steps.length===0 && <span style={{color:T.textDim}}>Waiting for events...</span>}
    </div>
  );
}

function AgentCard({ name, icon, color, active }) {
  return (
    <div style={{background:T.card,border:`1px solid ${active?color:T.border}`,borderRadius:12,
      padding:"14px 16px",textAlign:"center",transition:"all 0.4s",
      boxShadow:active?`0 0 20px ${color}22`:"none"}}>
      <div style={{fontSize:22,marginBottom:6}}>{icon}</div>
      <div style={{fontSize:11,fontWeight:700,color:active?color:T.textSub,
        textTransform:"uppercase",letterSpacing:"0.08em"}}>{name}</div>
      <div style={{width:8,height:8,borderRadius:"50%",background:active?color:T.border,
        margin:"6px auto 0",boxShadow:active?`0 0 8px ${color}`:"none",
        animation:active?"pulse 1s ease-in-out infinite":"none"}}/>
    </div>
  );
}

function FindingCard({ f }) {
  const [open, setOpen] = useState(false);
  const sev = SEV[f.severity] || SEV.info;
  const isChain = f.title?.includes("[CHAIN]");
  return (
    <div style={{border:`1px solid ${isChain?T.red:sev.border}`,borderRadius:10,marginBottom:8,
      overflow:"hidden",background:isChain?"#1a0808":sev.bg}}>
      <div onClick={()=>setOpen(o=>!o)} style={{display:"flex",alignItems:"center",gap:10,
        padding:"10px 14px",cursor:"pointer"}}>
        <span style={{width:8,height:8,borderRadius:"50%",background:sev.dot,flexShrink:0,
          boxShadow:`0 0 5px ${sev.dot}`}}/>
        <span style={{fontSize:10,fontWeight:700,color:sev.dot,textTransform:"uppercase",
          letterSpacing:"0.08em",minWidth:60,flexShrink:0}}>{sev.label}</span>
        <span style={{fontWeight:600,color:sev.text,flex:1,fontSize:13,lineHeight:1.4}}>
          {isChain && <span style={{background:T.red,color:"#fff",fontSize:9,borderRadius:4,
            padding:"1px 5px",marginRight:6,fontWeight:700}}>CHAIN</span>}
          {f.title}
        </span>
        <span style={{fontSize:11,color:T.textDim,fontFamily:"monospace",flexShrink:0}}>
          {f.file?.split(/[/\\]/).pop()}{f.line?`:${f.line}`:""}
        </span>
        <span style={{color:T.textDim,fontSize:11}}>{open?"▲":"▼"}</span>
      </div>
      {open && (
        <div style={{padding:"10px 14px 14px",borderTop:`1px solid ${sev.border}`}}>
          <p style={{margin:"0 0 10px",fontSize:13,color:T.textSub,lineHeight:1.7}}>
            <b style={{color:sev.text}}>Issue: </b>{f.description}</p>
          <p style={{margin:"0 0 10px",fontSize:13,color:T.textSub,lineHeight:1.7}}>
            <b style={{color:T.green}}>Fix: </b>{f.suggestion}</p>
          {f.code_snippet && (
            <pre style={{background:"#05050d",color:"#a78bfa",borderRadius:8,padding:"10px 14px",
              fontSize:12,overflow:"auto",margin:0,lineHeight:1.6,border:`1px solid ${T.border}`}}>
              {f.code_snippet.trim()}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}

// ── Main ───────────────────────────────────────────────────────────────────
export default function App() {
  const [url, setUrl]         = useState("");
  const [steps, setSteps]     = useState([]);
  const [progress, setProgress] = useState(0);
  const [curStep, setCurStep] = useState("");
  const [curDetail, setCurDetail] = useState("");
  const [result, setResult]   = useState(null);
  const [error, setError]     = useState(null);
  const [loading, setLoading] = useState(false);
  const [tab, setTab]         = useState("all");
  const [startTime, setStartTime] = useState(null);
  const [elapsed, setElapsed] = useState(0);
  const [fileCt, setFileCt]   = useState(0);
  const [chunkCt, setChunkCt] = useState(0);
  const esRef = useRef(null);
  const timerRef = useRef(null);

  useEffect(() => {
    if (loading && startTime) {
      timerRef.current = setInterval(() => setElapsed(Date.now()-startTime), 1000);
    } else {
      clearInterval(timerRef.current);
    }
    return () => clearInterval(timerRef.current);
  }, [loading, startTime]);

  // POST with retry — handles Render cold start (backend sleeping)
  async function postWithRetry(url_path, body, maxAttempts=5) {
    for (let i=0; i<maxAttempts; i++) {
      try {
        const res = await fetch(`${API}${url_path}`, {
          method:"POST",
          headers:{"Content-Type":"application/json"},
          body: JSON.stringify(body),
        });
        if (res.ok) return res.json();
        if (res.status === 404 || res.status === 503) {
          // Backend waking up — wait and retry
          await new Promise(r => setTimeout(r, 3000));
          continue;
        }
        throw new Error(`HTTP ${res.status}`);
      } catch(e) {
        if (i === maxAttempts-1) throw e;
        await new Promise(r => setTimeout(r, 3000));
      }
    }
    throw new Error("Backend did not respond after 5 attempts");
  }

  const startReview = async () => {
    if (!url.trim()) return;
    setError(null); setResult(null); setSteps([]);
    setProgress(0); setCurStep("waking"); setCurDetail("Waking backend — free tier may take 30s...");
    setLoading(true); setTab("all"); setFileCt(0); setChunkCt(0);
    setStartTime(Date.now());

    // Show waking step in log immediately
    const wakeStep = { step:"waking", detail:"Waking Render backend (free tier cold start ~30s)...", progress:2, ts:Date.now()/1000 };
    setSteps([wakeStep]);

    try {
      const data = await postWithRetry("/review", { repo_url: url.trim() });

      if (!data?.job_id) throw new Error("Backend returned no job_id — check Render logs");

      setCurStep("cloning");
      setCurDetail("Backend alive! Starting review...");

      if (esRef.current) esRef.current.close();
      const es = new EventSource(`${API}/review/${data.job_id}/stream`);
      esRef.current = es;

      es.onmessage = (e) => {
        const ev = JSON.parse(e.data);
        if (ev.type === "step") {
          setSteps(prev => [...prev, ev]);
          setProgress(ev.progress||0);
          setCurStep(ev.step||"");
          setCurDetail(ev.detail||"");
        } else if (ev.type === "done") {
          setProgress(100); setCurStep("done");
          setFileCt(ev.file_count||0); setChunkCt(ev.chunk_count||0);
          fetch(`${API}/review/${data.job_id}`)
            .then(r=>r.json())
            .then(d=>{ setResult(d); setLoading(false); });
          es.close();
        } else if (ev.type === "error") {
          setError(ev.error||"Unknown error");
          setLoading(false); es.close();
        }
      };
      es.onerror = () => { es.close(); };
    } catch(e) {
      setError(`${e.message}. Make sure the Render backend is deployed and running.`);
      setLoading(false);
    }
  };

  const allFindings = result ? [
    ...(result.bug_findings||[]), ...(result.security_findings||[]),
    ...(result.quality_findings||[]), ...(result.perf_findings||[]),
  ] : [];

  const filtered = tab==="all" ? allFindings
    : tab==="bug"      ? (result?.bug_findings||[])
    : tab==="security" ? (result?.security_findings||[])
    : tab==="quality"  ? (result?.quality_findings||[])
    : (result?.perf_findings||[]);

  const chains = allFindings.filter(f=>f.title?.includes("[CHAIN]"));
  const agentActive = loading && (curStep.startsWith("agent_")||["agents","agents_running","chains","synthesizing"].includes(curStep));

  const TABS = [
    { id:"all",      label:"All",        count:allFindings.length,              color:T.purpleL },
    { id:"bug",      label:"🐛 Bugs",    count:result?.findings?.bugs||0,        color:T.red },
    { id:"security", label:"🔒 Security",count:result?.findings?.security||0,    color:T.amber },
    { id:"quality",  label:"🧹 Quality", count:result?.findings?.quality||0,     color:T.blue },
    { id:"perf",     label:"⚡ Perf",    count:result?.findings?.performance||0, color:T.green },
  ];

  return (
    <div style={{minHeight:"100vh",background:T.bg,color:T.text,
      fontFamily:"-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif"}}>
      <style>{`
        @keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
        @keyframes fadeIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}
        ::-webkit-scrollbar{width:6px} ::-webkit-scrollbar-track{background:#0a0a0f}
        ::-webkit-scrollbar-thumb{background:#2d2d44;border-radius:99px}
        *{box-sizing:border-box}
      `}</style>

      {/* Header */}
      <div style={{borderBottom:`1px solid ${T.border}`,padding:"0 32px",display:"flex",
        alignItems:"center",height:60,background:"rgba(10,10,15,0.9)",
        position:"sticky",top:0,zIndex:50}}>
        <div style={{display:"flex",alignItems:"center",gap:12}}>
          <div style={{width:32,height:32,borderRadius:8,display:"flex",alignItems:"center",
            justifyContent:"center",fontSize:18,
            background:`linear-gradient(135deg,${T.purple},${T.blue})`}}>🤖</div>
          <div>
            <div style={{fontWeight:700,fontSize:15}}>AI Code Review Agent</div>
            <div style={{fontSize:10,color:T.textDim,textTransform:"uppercase",letterSpacing:"0.06em"}}>
              LangGraph · HyDE · Reranker · Self-Reflection
            </div>
          </div>
        </div>
        {result && <div style={{marginLeft:"auto",fontSize:12,color:T.textDim}}>
          {fileCt} files · {chunkCt} chunks · {elapsedStr(elapsed)}
        </div>}
      </div>

      <div style={{maxWidth:960,margin:"0 auto",padding:"28px 20px"}}>

        {/* Input card */}
        <div style={{background:T.card,borderRadius:16,border:`1px solid ${T.border}`,
          padding:24,marginBottom:20}}>
          <div style={{fontSize:12,fontWeight:600,color:T.textSub,textTransform:"uppercase",
            letterSpacing:"0.08em",marginBottom:12}}>Repository URL</div>
          <div style={{display:"flex",gap:10}}>
            <input value={url} onChange={e=>setUrl(e.target.value)}
              onKeyDown={e=>e.key==="Enter"&&!loading&&startReview()}
              placeholder="https://github.com/username/repo" disabled={loading}
              style={{flex:1,padding:"11px 16px",background:"#0a0a0f",
                border:`1px solid ${T.borderHi}`,borderRadius:10,fontSize:14,
                color:T.text,outline:"none",fontFamily:"monospace"}}/>
            <button onClick={startReview} disabled={loading||!url.trim()}
              style={{padding:"11px 24px",
                background:loading?T.border:`linear-gradient(135deg,${T.purple},${T.blue})`,
                color:loading?T.textDim:"#fff",border:"none",borderRadius:10,
                fontWeight:700,fontSize:14,cursor:loading?"not-allowed":"pointer",
                whiteSpace:"nowrap",boxShadow:loading?"none":`0 4px 20px ${T.purple}44`}}>
              {loading?"Reviewing...":"Review →"}
            </button>
          </div>
          {error && <div style={{marginTop:12,color:T.red,fontSize:13,padding:"8px 12px",
            background:"#1a0808",borderRadius:8,border:`1px solid ${T.red}33`}}>{error}</div>}
          <div style={{marginTop:12,display:"flex",gap:8,flexWrap:"wrap"}}>
            {["psf/requests","pallets/flask","encode/httpx"].map(r=>(
              <button key={r} onClick={()=>setUrl(`https://github.com/${r}`)} disabled={loading}
                style={{padding:"4px 10px",background:"transparent",border:`1px solid ${T.border}`,
                  borderRadius:6,fontSize:11,color:T.textDim,cursor:"pointer",fontFamily:"monospace"}}>
                {r}
              </button>
            ))}
          </div>
          <div style={{marginTop:10,fontSize:11,color:T.textDim}}>
            ⚠ Free tier backend sleeps after 15min inactivity — first request takes ~30s to wake
          </div>
        </div>

        {/* Live progress */}
        {loading && (
          <div style={{background:T.card,borderRadius:16,border:`1px solid ${T.purple}44`,
            padding:24,marginBottom:20,animation:"fadeIn 0.4s ease"}}>
            <div style={{marginBottom:16}}>
              <div style={{display:"flex",justifyContent:"space-between",alignItems:"center",marginBottom:8}}>
                <div style={{display:"flex",alignItems:"center",gap:10}}>
                  <div style={{width:8,height:8,borderRadius:"50%",background:T.purple,
                    animation:"pulse 1s ease-in-out infinite",boxShadow:`0 0 8px ${T.purple}`}}/>
                  <span style={{fontSize:14,fontWeight:600,color:T.purpleL}}>
                    {STEPS[curStep]?.label||"Processing..."}
                  </span>
                </div>
                <div style={{display:"flex",gap:16,alignItems:"center"}}>
                  <span style={{fontSize:12,color:T.textDim,fontFamily:"monospace"}}>{elapsedStr(elapsed)}</span>
                  <span style={{fontSize:14,fontWeight:700,color:T.purple}}>{progress}%</span>
                </div>
              </div>
              <div style={{height:4,background:T.border,borderRadius:99,overflow:"hidden"}}>
                <div style={{height:"100%",width:`${progress}%`,background:T.purple,
                  borderRadius:99,transition:"width 0.6s ease",boxShadow:`0 0 8px ${T.purple}`}}/>
              </div>
            </div>

            <div style={{fontSize:12,color:T.textSub,fontFamily:"monospace",padding:"8px 12px",
              background:"#080810",borderRadius:8,marginBottom:16,border:`1px solid ${T.border}`,minHeight:34}}>
              {curDetail||"Starting..."}
            </div>

            <div style={{display:"grid",gridTemplateColumns:"repeat(4,1fr)",gap:10,marginBottom:16}}>
              {[
                {name:"Bug",     icon:"🐛",color:T.red},
                {name:"Security",icon:"🔒",color:T.amber},
                {name:"Quality", icon:"🧹",color:T.blue},
                {name:"Perf",    icon:"⚡",color:T.green},
              ].map(a=><AgentCard key={a.name} {...a} active={agentActive}/>)}
            </div>

            <div style={{fontSize:10,fontWeight:600,color:T.textDim,textTransform:"uppercase",
              letterSpacing:"0.08em",marginBottom:6}}>Live event log</div>
            <LiveLog steps={steps}/>

            <div style={{marginTop:14,display:"flex",gap:8,flexWrap:"wrap"}}>
              {["HyDE Query Expansion","Cross-Encoder Reranking","Self-Reflection","Multi-Hop RAG","LangGraph Fan-out"].map(c=>(
                <span key={c} style={{fontSize:10,padding:"3px 8px",
                  background:`${T.purple}18`,border:`1px solid ${T.purple}33`,
                  borderRadius:99,color:T.purpleL}}>{c}</span>
              ))}
            </div>
          </div>
        )}

        {/* Results */}
        {result && (
          <div style={{animation:"fadeIn 0.5s ease"}}>
            <div style={{display:"grid",gridTemplateColumns:"auto 1fr",gap:20,marginBottom:16,
              background:T.card,borderRadius:16,border:`1px solid ${T.border}`,padding:24}}>
              <ScoreRing score={result.score} size={150}/>
              <div>
                <div style={{display:"flex",alignItems:"center",gap:10,marginBottom:10,flexWrap:"wrap"}}>
                  <span style={{fontWeight:800,fontSize:17}}>Review Complete</span>
                  <span style={{fontSize:10,padding:"3px 8px",borderRadius:99,fontWeight:700,
                    background:`${T.green}20`,color:T.green,border:`1px solid ${T.green}44`,
                    textTransform:"uppercase"}}>Done</span>
                </div>
                {chains.length>0 && (
                  <div style={{display:"flex",alignItems:"center",gap:8,marginBottom:10,
                    padding:"8px 12px",background:"#1a0808",border:`1px solid ${T.red}44`,borderRadius:8}}>
                    <span style={{color:T.red}}>⛓</span>
                    <span style={{fontSize:13,color:"#fca5a5",fontWeight:600}}>
                      {chains.length} Vulnerability Chain{chains.length>1?"s":""} Detected
                    </span>
                  </div>
                )}
                <p style={{fontSize:13,color:T.textSub,lineHeight:1.75,margin:"0 0 14px"}}>{result.summary}</p>
                <div style={{display:"flex",gap:10,flexWrap:"wrap"}}>
                  {[
                    {label:"Bugs",     val:result.findings?.bugs||0,        color:T.red},
                    {label:"Security", val:result.findings?.security||0,    color:T.amber},
                    {label:"Quality",  val:result.findings?.quality||0,     color:T.blue},
                    {label:"Perf",     val:result.findings?.performance||0, color:T.green},
                  ].map(s=>(
                    <div key={s.label} style={{padding:"8px 14px",background:"#0a0a0f",
                      borderRadius:10,border:`1px solid ${T.border}`,textAlign:"center",minWidth:60}}>
                      <div style={{fontSize:20,fontWeight:800,color:s.color}}>{s.val}</div>
                      <div style={{fontSize:10,color:T.textDim,marginTop:2,textTransform:"uppercase"}}>{s.label}</div>
                    </div>
                  ))}
                </div>
              </div>
            </div>

            <div style={{background:T.card,borderRadius:16,border:`1px solid ${T.border}`,padding:24}}>
              <div style={{display:"flex",gap:6,marginBottom:18,overflowX:"auto",paddingBottom:4}}>
                {TABS.map(t=>(
                  <button key={t.id} onClick={()=>setTab(t.id)}
                    style={{padding:"6px 14px",borderRadius:8,border:"1px solid",
                      borderColor:tab===t.id?t.color:T.border,
                      background:tab===t.id?`${t.color}18`:"transparent",
                      color:tab===t.id?t.color:T.textDim,
                      fontWeight:tab===t.id?700:400,fontSize:12,cursor:"pointer",whiteSpace:"nowrap"}}>
                    {t.label}
                    {t.count>0 && <span style={{marginLeft:6,background:`${t.color}22`,
                      borderRadius:99,padding:"1px 6px",fontSize:10,color:t.color}}>{t.count}</span>}
                  </button>
                ))}
                <div style={{flex:1}}/>
                <button onClick={()=>{
                  const b=new Blob([result.report_markdown||""],{type:"text/markdown"});
                  const a=document.createElement("a"); a.href=URL.createObjectURL(b);
                  a.download="code_review_report.md"; a.click();
                }} style={{padding:"6px 14px",borderRadius:8,cursor:"pointer",
                  background:`${T.purple}18`,border:`1px solid ${T.purple}44`,
                  color:T.purpleL,fontSize:12,fontWeight:600,whiteSpace:"nowrap"}}>
                  ⬇ Download .md
                </button>
              </div>

              {filtered.length===0
                ? <div style={{textAlign:"center",padding:"40px 0",color:T.textDim}}>
                    <div style={{fontSize:28,marginBottom:8}}>✓</div>
                    <div>No findings in this category</div>
                  </div>
                : filtered.map((f,i)=><FindingCard key={i} f={f}/>)
              }
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
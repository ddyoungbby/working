import { useState, useEffect, useMemo, useCallback, useRef } from "react";

/* ============================================================
   AI TECH TILT — signal daemon console
   Modeled on the r/RobinhoodApp agentic trading bot dashboard.
   Monitoring + signal generation only. No order execution.
   Data: Alpaca (IEX feed) live, or seeded demo mode.
   AI catalyst classification: Claude + web search.
   ============================================================ */

const UNIVERSE = ["NVDA","AMD","AVGO","SMH","SOXX","ANET","ASML","PANW","SNOW","QLD","MRVL","MU","VRT","PLTR","TSM"];
const BENCH = "QQQ";

const C = {
  bg: "#F4F6F5", card: "#FFFFFF", ink: "#17231E", label: "#6B7A75",
  green: "#0E6B4F", red: "#BF3B2F", amber: "#A6720C", border: "#E3E8E6",
  greenSoft: "#E8F3EE", redSoft: "#FBEBE9", amberSoft: "#FBF3E2", inkSoft: "#EFF2F1",
};

/* ---------- deterministic demo data ---------- */
function mulberry32(a){return function(){a|=0;a=(a+0x6D2B79F5)|0;let t=Math.imul(a^(a>>>15),1|a);t=(t+Math.imul(t^(t>>>7),61|t))^t;return((t^(t>>>14))>>>0)/4294967296}}
function hashStr(s){let h=2166136261;for(let i=0;i<s.length;i++){h^=s.charCodeAt(i);h=Math.imul(h,16777619)}return h>>>0}

const DEMO_BASE = {NVDA:1180,AMD:212,AVGO:1890,SMH:298,SOXX:262,ANET:412,ASML:1044,PANW:388,SNOW:241,QLD:132,MRVL:118,MU:158,VRT:129,PLTR:96,TSM:224,QQQ:552};

function genDemoBars(sym, n=250){
  const rnd = mulberry32(hashStr(sym));
  const drift = (rnd()-0.35)*0.0022;
  let px = DEMO_BASE[sym]*(0.72+rnd()*0.2);
  const closes=[];
  for(let i=0;i<n;i++){
    const shock = (rnd()-0.5)*0.036 + drift + (i>n-30 ? (rnd()-0.42)*0.012 : 0);
    px = Math.max(1, px*(1+shock));
    closes.push(px);
  }
  return closes;
}
function genDemoQuote(sym, last, tick){
  const rnd = mulberry32(hashStr(sym+"q")+tick);
  const px = last*(1+(rnd()-0.5)*0.004);
  const spreadPct = rnd()<0.12 ? 0.45+rnd()*0.4 : 0.02+rnd()*0.12;
  const half = px*spreadPct/200;
  return { bid:px-half, ask:px+half, last:px, spreadPct, ageSec: rnd()<0.1 ? 12+rnd()*30 : rnd()*3 };
}

/* ---------- metrics + scoring ---------- */
const pct=(a,b)=>((a/b)-1)*100;
const sma=(arr,n)=>{if(arr.length<n)return null;let s=0;for(let i=arr.length-n;i<arr.length;i++)s+=arr[i];return s/n};

function computeMetrics(closes, bench){
  const last=closes[closes.length-1];
  const mom5=pct(last, closes[closes.length-6]);
  const mom20=pct(last, closes[closes.length-21]);
  const bLast=bench[bench.length-1];
  const rs20=mom20 - pct(bLast, bench[bench.length-21]);
  const s50=sma(closes,50), s200=sma(closes,200);
  return { last, mom5, mom20, rs20, above50:s50!=null&&last>s50, above200:s200!=null&&last>s200 };
}
const clamp=(v,lo,hi)=>Math.max(lo,Math.min(hi,v));
function scoreSetup(m){
  const momScore = clamp(50 + m.mom5*2.2 + m.mom20*1.1, 0, 100);
  const rsScore  = clamp(50 + m.rs20*2.5, 0, 100);
  const trend    = (m.above50?55:35) + (m.above200?45:20);
  const composite = 0.42*momScore + 0.33*rsScore + 0.25*clamp(trend,0,100);
  return { momScore, rsScore, trendScore: clamp(trend,0,100), composite };
}

function nowET(){
  const p = new Intl.DateTimeFormat("en-US",{timeZone:"America/New_York",hour12:false,weekday:"short",hour:"numeric",minute:"numeric"}).formatToParts(new Date());
  const get=t=>p.find(x=>x.type===t)?.value;
  return { day:get("weekday"), mins: parseInt(get("hour"),10)*60+parseInt(get("minute"),10) };
}
function inRegularHours(){
  const {day,mins}=nowET();
  if(day==="Sat"||day==="Sun") return false;
  return mins>=570 && mins<960; // 9:30–16:00 ET
}

/* ---------- deterministic risk engine ---------- */
function evaluateSignal(setup, q, acct, safety, activeTickers, rth){
  const reasons=[];
  if(safety.emergencyStop) reasons.push("manual_emergency_stop");
  if(safety.opFreeze) reasons.push("operational_freeze");
  if(q.spreadPct>0.4) reasons.push("spread_too_wide");
  if(q.ageSec>10) reasons.push("quote_stale");
  if(setup.last<8) reasons.push("low_liquidity");
  const qty=Math.max(0,Math.floor(acct.maxPosition/q.ask));
  const notional=qty*q.ask;
  if(qty<1) reasons.push("position_size_exceeds_cap");
  if(notional>acct.spendableBP) reasons.push("not_enough_buying_power");
  if(activeTickers.has(setup.ticker)) reasons.push("duplicate_signal");
  if(!rth) reasons.push("outside_regular_market_hours");
  return { pass: reasons.length===0, reasons, qty, notional };
}

/* ---------- Claude catalyst classification ----------
   Calls the server-side /api/classify endpoint (see server/classify.mjs),
   which holds the Anthropic API key and runs Claude + web search. The key
   is never exposed to the browser. */
async function classifyCatalysts(tickers){
  const res = await fetch("/api/classify",{
    method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({ tickers })
  });
  let data={};
  try{ data=await res.json(); }catch{ /* non-JSON error body */ }
  if(!res.ok) throw new Error(data.error || `Classifier request failed (HTTP ${res.status}).`);
  if(!Array.isArray(data.rows)) throw new Error("Classifier returned an unexpected response shape.");
  return data.rows;
}

/* ---------- Alpaca ---------- */
async function alpacaGET(path, keys){
  const r = await fetch(`https://data.alpaca.markets${path}`,{
    headers:{ "APCA-API-KEY-ID":keys.key, "APCA-API-SECRET-KEY":keys.secret }
  });
  if(!r.ok) throw new Error(`Alpaca ${r.status}: ${(await r.text()).slice(0,140)}`);
  return r.json();
}
async function fetchAlpacaBars(keys){
  const syms=[...UNIVERSE,BENCH].join(",");
  const j = await alpacaGET(`/v2/stocks/bars?symbols=${syms}&timeframe=1Day&limit=250&adjustment=split&feed=iex`, keys);
  const out={};
  for(const s of [...UNIVERSE,BENCH]) out[s]=(j.bars?.[s]||[]).map(b=>b.c);
  return out;
}
async function fetchAlpacaQuotes(keys){
  const syms=UNIVERSE.join(",");
  const j = await alpacaGET(`/v2/stocks/quotes/latest?symbols=${syms}&feed=iex`, keys);
  const out={};
  for(const s of UNIVERSE){
    const q=j.quotes?.[s]; if(!q) continue;
    const bid=q.bp||0, ask=q.ap||bid, mid=(bid+ask)/2 || ask || bid;
    out[s]={ bid, ask, last: mid, spreadPct: mid? ((ask-bid)/mid)*100 : 0,
      ageSec:(Date.now()-new Date(q.t).getTime())/1000, ts:q.t };
  }
  return out;
}

/* ---------- tiny UI atoms ---------- */
const fm$=v=>"$"+v.toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
const fm2=v=>v==null?"—":v.toFixed(2);

function Kpi({label,value,sub,tone,big}){
  const col = tone==="green"?C.green : tone==="red"?C.red : tone==="amber"?C.amber : C.ink;
  return (
    <div style={{background:C.card,border:`1px solid ${C.border}`,borderRadius:10,padding:"12px 14px",minWidth:0,
      borderTop:`3px solid ${tone?col:C.border}`}}>
      <div style={{fontFamily:"'IBM Plex Mono',monospace",fontSize:10,letterSpacing:"0.09em",color:C.label,textTransform:"uppercase"}}>{label}</div>
      <div style={{fontFamily:"'Space Grotesk',sans-serif",fontWeight:700,color:col,fontSize:big?26:20,lineHeight:1.25,marginTop:2,overflowWrap:"anywhere"}}>{value}</div>
      {sub && <div style={{fontSize:11,color:C.label,marginTop:2}}>{sub}</div>}
    </div>
  );
}
function Chip({text,tone}){
  const map={red:[C.redSoft,C.red],amber:[C.amberSoft,C.amber],green:[C.greenSoft,C.green],ink:[C.inkSoft,C.label]};
  const [bg,fg]=map[tone]||map.ink;
  return <span style={{background:bg,color:fg,borderRadius:5,padding:"2px 7px",fontFamily:"'IBM Plex Mono',monospace",
    fontSize:10.5,whiteSpace:"nowrap",display:"inline-block",margin:"1px 3px 1px 0"}}>{text}</span>;
}
function StatusDot({on, live}){
  return <span style={{width:9,height:9,borderRadius:"50%",display:"inline-block",marginRight:6,
    background:on?C.green:C.label, animation: on&&live ? "tiltPulse 1.6s ease-in-out infinite" : "none"}}/>;
}
function Th({children,right}){return <th style={{padding:"7px 10px",fontFamily:"'IBM Plex Mono',monospace",fontSize:10,
  letterSpacing:"0.07em",textTransform:"uppercase",color:C.label,textAlign:right?"right":"left",whiteSpace:"nowrap",
  borderBottom:`1px solid ${C.border}`,background:"#FAFBFB",position:"sticky",top:0}}>{children}</th>;}
function Td({children,right,mono,tone,bold}){
  const col=tone==="green"?C.green:tone==="red"?C.red:tone==="amber"?C.amber:C.ink;
  return <td style={{padding:"7px 10px",fontSize:12.5,color:col,textAlign:right?"right":"left",whiteSpace:"nowrap",
    fontFamily:mono?"'IBM Plex Mono',monospace":"inherit",fontWeight:bold?600:400,borderBottom:`1px solid ${C.border}`}}>{children}</td>;
}
function Section({title,children,right}){
  return (
    <div style={{background:C.card,border:`1px solid ${C.border}`,borderRadius:10,marginTop:14,overflow:"hidden"}}>
      <div style={{display:"flex",justifyContent:"space-between",alignItems:"center",padding:"10px 14px",borderBottom:`1px solid ${C.border}`}}>
        <div style={{fontFamily:"'Space Grotesk',sans-serif",fontWeight:600,fontSize:14}}>{title}</div>
        {right}
      </div>
      {children}
    </div>
  );
}
function Btn({children,onClick,tone,disabled}){
  const solid=tone==="red"||tone==="green";
  const bg=tone==="red"?C.red:tone==="green"?C.green:"#fff";
  return <button onClick={onClick} disabled={disabled} style={{
    background:disabled?"#D8DEDC":bg,color:solid?"#fff":C.ink,border:solid?"none":`1px solid ${C.border}`,
    borderRadius:8,padding:"8px 14px",fontFamily:"'Space Grotesk',sans-serif",fontWeight:600,fontSize:12.5,
    cursor:disabled?"default":"pointer"}}>{children}</button>;
}

/* ============================================================ */
export default function App(){
  const [tab,setTab]=useState("Account");
  const [demo,setDemo]=useState(true);
  const [keys,setKeys]=useState({key:"",secret:""});
  const [err,setErr]=useState(null);
  const [bars,setBars]=useState(null);
  const [quotes,setQuotes]=useState({});
  const [tick,setTick]=useState(0);
  const [lastSync,setLastSync]=useState(null);

  const [acctIn,setAcctIn]=useState({equity:5000, buyingPower:1377.04, targetDeployPct:100, cashReservePct:3, maxPosPct:30, dailyLossPct:3, tradeCap:15});
  const [safety,setSafety]=useState({emergencyStop:false, opFreeze:false});
  const [signals,setSignals]=useState([]);
  const [rejections,setRejections]=useState([]);
  const [ai,setAi]=useState({rows:[],loading:false,err:null,at:null});
  const idRef=useRef(1100);

  const rth = useMemo(()=>inRegularHours(),[tick]);

  /* ----- data load ----- */
  const loadDemo=useCallback(()=>{
    const b={}; for(const s of [...UNIVERSE,BENCH]) b[s]=genDemoBars(s);
    setBars(b); setErr(null); setLastSync(new Date());
  },[]);
  const loadLive=useCallback(async()=>{
    try{
      setErr(null);
      const b=await fetchAlpacaBars(keys);
      if(!b[BENCH]||b[BENCH].length<30) throw new Error("Not enough bar history returned — check keys/feed.");
      setBars(b);
      setQuotes(await fetchAlpacaQuotes(keys));
      setLastSync(new Date());
    }catch(e){ setErr(String(e.message||e)); }
  },[keys]);

  useEffect(()=>{ loadDemo(); },[loadDemo]);

  useEffect(()=>{
    const iv=setInterval(()=>setTick(t=>t+1), demo?4000:15000);
    return ()=>clearInterval(iv);
  },[demo]);

  useEffect(()=>{
    if(!bars) return;
    if(demo){
      const q={};
      for(const s of UNIVERSE) q[s]=genDemoQuote(s, bars[s][bars[s].length-1], tick);
      setQuotes(q);
    } else if(keys.key && keys.secret){
      fetchAlpacaQuotes(keys).then(setQuotes).catch(e=>setErr(String(e.message||e)));
    }
  },[tick,bars,demo]); // eslint-disable-line

  /* ----- derived account ----- */
  const acct=useMemo(()=>{
    const targetDeployed = acctIn.equity*acctIn.targetDeployPct/100;
    const cashReserve = acctIn.buyingPower*acctIn.cashReservePct/100;
    const spendableBP = Math.max(0, acctIn.buyingPower - cashReserve);
    const maxPosition = acctIn.equity*acctIn.maxPosPct/100;
    const dailyLossCap = acctIn.equity*acctIn.dailyLossPct/100;
    return {...acctIn, targetDeployed, cashReserve, spendableBP, maxPosition, dailyLossCap};
  },[acctIn]);

  /* ----- setups ----- */
  const setups=useMemo(()=>{
    if(!bars) return [];
    const bench=bars[BENCH];
    return UNIVERSE.filter(s=>bars[s]?.length>=201).map(s=>{
      const m=computeMetrics(bars[s],bench);
      const sc=scoreSetup(m);
      const q=quotes[s];
      const vetoes=[];
      if(!rth) vetoes.push("outside_regular_market_hours");
      if(q?.spreadPct>0.4) vetoes.push("spread_too_wide");
      if(q?.ageSec>10) vetoes.push("quote_stale");
      return { ticker:s, ...m, ...sc, vetoes, q };
    }).sort((a,b)=>b.composite-a.composite);
  },[bars,quotes,rth]);

  const opFreezeAuto = useMemo(()=>{
    if(!quotes||Object.keys(quotes).length===0) return false;
    const stale=Object.values(quotes).filter(q=>q.ageSec>60).length;
    return stale>UNIVERSE.length*0.6;
  },[quotes]);
  const safetyAll={...safety, opFreeze: safety.opFreeze||opFreezeAuto};

  /* ----- run the signal pass ----- */
  const runPass=useCallback(()=>{
    if(!setups.length) return;
    const active=new Set(signals.filter(x=>x.status==="ACTIVE").map(x=>x.ticker));
    const newSig=[], newRej=[];
    const stamp=new Date().toISOString().replace("T"," ").slice(0,19);
    for(const st of setups.slice(0,8)){
      if(st.composite<70) continue;
      const q=st.q; if(!q) continue;
      const ev=evaluateSignal(st,q,acct,safetyAll,active,rth);
      const base={ id:++idRef.current, created_at:stamp, ticker:st.ticker, strategy:"AI Tech Tilt Entry",
        action:"BUY", qty:ev.qty, order_type:"LIMIT", limit_price:+(q.ask*1.001).toFixed(2),
        stop_price:+(q.ask*0.97).toFixed(2), target_price:+(q.ask*1.06).toFixed(2), time_stop:"3d",
        notional:+ev.notional.toFixed(2), score:+st.composite.toFixed(1) };
      if(ev.pass){ newSig.push({...base,status:"ACTIVE"}); active.add(st.ticker); }
      else newRej.push({...base,status:"REJECTED",reasons:ev.reasons});
    }
    if(newSig.length) setSignals(p=>[...newSig,...p].slice(0,60));
    if(newRej.length) setRejections(p=>[...newRej,...p].slice(0,80));
    if(!newSig.length && !newRej.length) setRejections(p=>[{id:++idRef.current,created_at:stamp,ticker:"—",strategy:"AI Tech Tilt Entry",action:"—",reasons:["no_setup_above_score_threshold"],score:setups[0]?+setups[0].composite.toFixed(1):null,status:"NO-OP"},...p].slice(0,80));
  },[setups,acct,safetyAll,signals,rth]);

  /* ----- AI catalysts ----- */
  const runAI=useCallback(async()=>{
    setAi(a=>({...a,loading:true,err:null}));
    try{
      const top=setups.slice(0,5).map(s=>s.ticker);
      const rows=await classifyCatalysts(top);
      setAi({rows,loading:false,err:null,at:new Date()});
    }catch(e){ setAi(a=>({...a,loading:false,err:"Classifier failed: "+String(e.message||e)})); }
  },[setups]);

  const best=setups[0];
  const tabs=["Account","Setups","Market Signals","News / AI","Strategy Signals","Risk Rejections","Settings"];
  const activeCount=signals.filter(s=>s.status==="ACTIVE").length;

  return (
    <div style={{minHeight:"100vh",background:C.bg,color:C.ink,fontFamily:"-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif",padding:"14px 14px 40px"}}>
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap');
        @keyframes tiltPulse{0%,100%{opacity:1}50%{opacity:.35}}
        @media (prefers-reduced-motion: reduce){ *{animation:none!important} }
        button:focus-visible, input:focus-visible{outline:2px solid ${C.green}; outline-offset:2px;}
        input{font-family:'IBM Plex Mono',monospace;}
        ::-webkit-scrollbar{height:8px;width:8px} ::-webkit-scrollbar-thumb{background:#CBD4D1;border-radius:4px}
      `}</style>

      {/* header */}
      <div style={{display:"flex",flexWrap:"wrap",alignItems:"baseline",gap:10,marginBottom:12}}>
        <div style={{fontFamily:"'Space Grotesk',sans-serif",fontWeight:700,fontSize:24}}>AI Tech Tilt <span style={{color:C.green}}>Portfolio</span></div>
        <div style={{fontFamily:"'IBM Plex Mono',monospace",fontSize:11,color:C.label}}>
          signal daemon console · {demo?"demo data":"Alpaca IEX"} {lastSync && `· synced ${lastSync.toLocaleTimeString()}`}
        </div>
      </div>

      {err && <div style={{background:C.redSoft,color:C.red,border:`1px solid ${C.red}33`,borderRadius:8,padding:"9px 12px",fontSize:12.5,marginBottom:10}}>{err}</div>}

      {/* KPI grid */}
      <div style={{display:"grid",gridTemplateColumns:"repeat(auto-fill,minmax(150px,1fr))",gap:10}}>
        <Kpi label="Strategy" value="AI Tech Tilt" sub="rules-based · stocks/ETFs only" />
        <Kpi label="Mode" value="MONITOR" tone="green" sub={demo?"demo feed":"live data · no execution"} />
        <Kpi label="Emergency stop" value={safety.emergencyStop?"ON":"OFF"} tone={safety.emergencyStop?"red":"green"} sub="blocks all new signals" />
        <Kpi label="Setups" value={setups.length} sub="daemon-ranked snapshot" />
        <Kpi label="Best setup score" value={best?best.composite.toFixed(1):"—"} tone="green" sub={best?best.ticker+" composite":""} />
        <Kpi label="Trade cap" value={`${acct.tradeCap}/day`} sub={`${activeCount} active signals`} />
      </div>
      <div style={{display:"grid",gridTemplateColumns:"repeat(auto-fill,minmax(150px,1fr))",gap:10,marginTop:10}}>
        <Kpi label="Deployable capital" value={fm$(acct.buyingPower)} sub="reported buying power" />
        <Kpi label="Target deployed" value={fm$(acct.targetDeployed)} sub={`${acct.targetDeployPct}% target`} />
        <Kpi label="Cash reserve" value={fm$(acct.cashReserve)} sub={`${acct.cashReservePct}% reserve`} />
        <Kpi label="Spendable BP" value={fm$(acct.spendableBP)} tone="green" sub="after reserve" />
        <Kpi label="Max position" value={fm$(acct.maxPosition)} sub={`${acct.maxPosPct}% per ticker`} />
        <Kpi label="Daily loss cap" value={fm$(acct.dailyLossCap)} tone="red" sub={`${acct.dailyLossPct}% of equity`} />
      </div>

      {/* safety spine — the separated safety states */}
      <div style={{display:"flex",flexWrap:"wrap",gap:8,alignItems:"center",background:C.card,border:`1px solid ${C.border}`,
        borderRadius:10,padding:"10px 14px",marginTop:12}}>
        <span style={{fontFamily:"'IBM Plex Mono',monospace",fontSize:10,letterSpacing:"0.09em",color:C.label,textTransform:"uppercase",marginRight:4}}>Safety states</span>
        <Chip text={`manual emergency stop: ${safety.emergencyStop?"ON":"off"}`} tone={safety.emergencyStop?"red":"ink"} />
        <Chip text={`operational freeze: ${safetyAll.opFreeze?"ON":"off"}`} tone={safetyAll.opFreeze?"red":"ink"} />
        <Chip text={rth?"regular session":"protective stops deferred (outside RTH)"} tone={rth?"green":"amber"} />
        <span style={{flex:1}}/>
        <Btn tone={safety.emergencyStop?undefined:"red"} onClick={()=>setSafety(s=>({...s,emergencyStop:!s.emergencyStop}))}>
          {safety.emergencyStop?"Clear stop":"Emergency stop"}
        </Btn>
        <Btn tone="green" onClick={runPass} disabled={!setups.length}>Run signal pass</Btn>
      </div>
      {!rth && <div style={{fontSize:11.5,color:C.amber,marginTop:6}}>Outside regular hours: entries carry a hard veto and protective-stop attempts are deferred until the next session instead of erroring out.</div>}

      {/* tabs */}
      <div style={{display:"flex",gap:6,flexWrap:"wrap",marginTop:14}}>
        {tabs.map(t=>(
          <button key={t} onClick={()=>setTab(t)} style={{
            background:tab===t?C.ink:"#fff",color:tab===t?"#fff":C.ink,border:`1px solid ${tab===t?C.ink:C.border}`,
            borderRadius:99,padding:"6px 13px",fontSize:12,fontFamily:"'Space Grotesk',sans-serif",fontWeight:600,cursor:"pointer"}}>
            {t}{t==="Strategy Signals"&&signals.length?` · ${signals.length}`:""}{t==="Risk Rejections"&&rejections.length?` · ${rejections.length}`:""}
          </button>
        ))}
      </div>

      {/* ---------------- TABS ---------------- */}
      {tab==="Account" && (
        <Section title="Account & strategy configuration">
          <div style={{padding:14,display:"grid",gridTemplateColumns:"repeat(auto-fill,minmax(170px,1fr))",gap:12}}>
            {[
              ["equity","Account equity ($)"],["buyingPower","Buying power ($)"],["targetDeployPct","Target deployment (%)"],
              ["cashReservePct","Cash reserve (%)"],["maxPosPct","Max position (% equity)"],["dailyLossPct","Daily loss cap (%)"],["tradeCap","Trade cap (/day)"],
            ].map(([k,lab])=>(
              <label key={k} style={{fontSize:11,color:C.label,display:"flex",flexDirection:"column",gap:4}}>
                {lab}
                <input type="number" value={acctIn[k]} onChange={e=>setAcctIn(a=>({...a,[k]:parseFloat(e.target.value)||0}))}
                  style={{border:`1px solid ${C.border}`,borderRadius:7,padding:"8px 10px",fontSize:13,color:C.ink}}/>
              </label>
            ))}
          </div>
          <div style={{padding:"0 14px 14px",fontSize:12,color:C.label,lineHeight:1.5}}>
            Guardrails mirror the reference bot: no margin, no market buys, limit entries only, protective exits required, stocks/ETFs only.
            Sell proceeds settle T+1 — treat pending settlement as a bridgeable reserve, never spend past reported buying power.
            This console generates and gates signals; it never places orders.
          </div>
        </Section>
      )}

      {tab==="Setups" && (
        <Section title="Current AI Tech Tilt signal fields" right={<span style={{fontSize:11,color:C.label}}>{lastSync?`ranked ${lastSync.toLocaleTimeString()}`:""}</span>}>
          <div style={{overflowX:"auto"}}>
            <table style={{borderCollapse:"collapse",width:"100%",minWidth:900}}>
              <thead><tr>
                <Th>rank</Th><Th>ticker</Th><Th>action</Th><Th right>composite</Th><Th right>momentum</Th><Th right>rel strength</Th>
                <Th right>mom 5d %</Th><Th right>mom 20d %</Th><Th right>RS vs {BENCH} 20d</Th><Th>50dma</Th><Th>200dma</Th><Th>hard_veto_reasons</Th>
              </tr></thead>
              <tbody>
                {setups.map((s,i)=>(
                  <tr key={s.ticker}>
                    <Td mono>{i+1}</Td>
                    <Td mono bold>{s.ticker}</Td>
                    <Td tone={s.vetoes.length?"amber":"green"} bold>{s.vetoes.length?"WATCH":"ELIGIBLE"}</Td>
                    <Td right mono bold tone="green">{s.composite.toFixed(1)}</Td>
                    <Td right mono>{s.momScore.toFixed(1)}</Td>
                    <Td right mono>{s.rsScore.toFixed(1)}</Td>
                    <Td right mono tone={s.mom5>=0?"green":"red"}>{fm2(s.mom5)}</Td>
                    <Td right mono tone={s.mom20>=0?"green":"red"}>{fm2(s.mom20)}</Td>
                    <Td right mono tone={s.rs20>=0?"green":"red"}>{fm2(s.rs20)}</Td>
                    <Td tone={s.above50?"green":"red"}>{s.above50?"✓":"✗"}</Td>
                    <Td tone={s.above200?"green":"red"}>{s.above200?"✓":"✗"}</Td>
                    <Td>{s.vetoes.length? s.vetoes.map(v=><Chip key={v} text={v} tone="amber"/>) : <Chip text="none" tone="green"/>}</Td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Section>
      )}

      {tab==="Market Signals" && (
        <>
          <div style={{display:"grid",gridTemplateColumns:"repeat(auto-fill,minmax(180px,1fr))",gap:10,marginTop:14}}>
            <Kpi label="Data REST" value={<span><StatusDot on live/>On</span>} sub={demo?"demo generator":"Alpaca IEX REST"} />
            <Kpi label="Refresh loop" value={<span><StatusDot on={!safetyAll.opFreeze} live/>{safetyAll.opFreeze?"Frozen":"On"}</span>} sub={demo?"4s cadence":"15s cadence"} />
            <Kpi label="Latest ticks" value={Object.keys(quotes).length} sub="latest-tick cache" />
          </div>
          <Section title="Latest ticks">
            <div style={{overflowX:"auto"}}>
              <table style={{borderCollapse:"collapse",width:"100%",minWidth:720}}>
                <thead><tr><Th>provider</Th><Th>symbol</Th><Th right>bid</Th><Th right>ask</Th><Th right>last/mid</Th><Th right>spread %</Th><Th right>age (s)</Th><Th>flags</Th></tr></thead>
                <tbody>
                  {UNIVERSE.map(s=>{
                    const q=quotes[s]; if(!q) return null;
                    return (
                      <tr key={s}>
                        <Td mono>{demo?"demo":"alpaca"}</Td>
                        <Td mono bold>{s}</Td>
                        <Td right mono>{fm2(q.bid)}</Td>
                        <Td right mono>{fm2(q.ask)}</Td>
                        <Td right mono bold>{fm2(q.last)}</Td>
                        <Td right mono tone={q.spreadPct>0.4?"red":"ink"}>{q.spreadPct.toFixed(3)}</Td>
                        <Td right mono tone={q.ageSec>10?"red":"ink"}>{q.ageSec.toFixed(1)}</Td>
                        <Td>{q.spreadPct>0.4&&<Chip text="spread_too_wide" tone="red"/>}{q.ageSec>10&&<Chip text="quote_stale" tone="red"/>}
                            {q.spreadPct<=0.4&&q.ageSec<=10&&<Chip text="clean" tone="green"/>}</Td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </Section>
        </>
      )}

      {tab==="News / AI" && (
        <Section title="News / AI catalyst signals"
          right={<Btn tone="green" onClick={runAI} disabled={ai.loading||!setups.length}>{ai.loading?"Classifying…":"Classify top 5"}</Btn>}>
          <div style={{padding:14}}>
            <div style={{fontSize:12,color:C.label,marginBottom:10}}>
              Same rule as the reference bot: AI only classifies and summarizes catalysts — it never approves trades.
              Runs Claude with live web search over your current top-ranked tickers. {ai.at && `Last run ${ai.at.toLocaleTimeString()}.`}
            </div>
            {ai.err && <div style={{color:C.red,fontSize:12.5,marginBottom:8}}>{ai.err}</div>}
            {ai.loading && <div style={{fontSize:13,color:C.label}}>Searching news and scoring catalysts…</div>}
            {!ai.loading && !ai.rows.length && !ai.err && <div style={{fontSize:13,color:C.label}}>No catalyst rows yet. Run the classifier to pull fresh context.</div>}
            {ai.rows.map((r,i)=>(
              <div key={i} style={{border:`1px solid ${C.border}`,borderLeft:`4px solid ${r.classification==="bullish"?C.green:r.classification==="bearish"?C.red:C.label}`,
                borderRadius:8,padding:"10px 12px",marginBottom:8}}>
                <div style={{display:"flex",gap:8,flexWrap:"wrap",alignItems:"center"}}>
                  <span style={{fontFamily:"'IBM Plex Mono',monospace",fontWeight:600}}>{r.ticker}</span>
                  <Chip text={r.classification} tone={r.classification==="bullish"?"green":r.classification==="bearish"?"red":"ink"}/>
                  <Chip text={`confidence ${r.confidence}`} tone="ink"/>
                </div>
                <div style={{fontSize:13.5,fontWeight:600,marginTop:5}}>{r.headline}</div>
                <div style={{fontSize:12,color:C.label,marginTop:3}}>{r.rationale}</div>
              </div>
            ))}
          </div>
        </Section>
      )}

      {tab==="Strategy Signals" && (
        <Section title="Recent AI Tech Tilt signals" right={<span style={{fontSize:11,color:C.label}}>signals only — no orders placed</span>}>
          <div style={{overflowX:"auto"}}>
            <table style={{borderCollapse:"collapse",width:"100%",minWidth:900}}>
              <thead><tr><Th>id</Th><Th>created_at</Th><Th>ticker</Th><Th>action</Th><Th right>qty</Th><Th>order</Th>
                <Th right>limit</Th><Th right>stop</Th><Th right>target</Th><Th>time stop</Th><Th right>notional</Th><Th right>score</Th></tr></thead>
              <tbody>
                {signals.length===0 && <tr><Td>—</Td><Td>No signals yet. Run a signal pass — anything above score 70 that clears every deterministic gate lands here.</Td>
                  <Td/><Td/><Td/><Td/><Td/><Td/><Td/><Td/><Td/><Td/></tr>}
                {signals.map(s=>(
                  <tr key={s.id}>
                    <Td mono>{s.id}</Td><Td mono>{s.created_at}</Td><Td mono bold>{s.ticker}</Td>
                    <Td tone="green" bold>{s.action}</Td><Td right mono>{s.qty}</Td><Td mono>{s.order_type}</Td>
                    <Td right mono>{fm2(s.limit_price)}</Td><Td right mono tone="red">{fm2(s.stop_price)}</Td>
                    <Td right mono tone="green">{fm2(s.target_price)}</Td><Td mono>{s.time_stop}</Td>
                    <Td right mono>{fm$(s.notional)}</Td><Td right mono bold>{s.score}</Td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Section>
      )}

      {tab==="Risk Rejections" && (
        <Section title="Risk rejections" right={<span style={{fontSize:11,color:C.label}}>deterministic gate — every block has a reason</span>}>
          <div style={{overflowX:"auto"}}>
            <table style={{borderCollapse:"collapse",width:"100%",minWidth:760}}>
              <thead><tr><Th>id</Th><Th>created_at</Th><Th>ticker</Th><Th right>score</Th><Th>status</Th><Th>reasons</Th></tr></thead>
              <tbody>
                {rejections.length===0 && <tr><Td>—</Td><Td>Nothing rejected yet. Trip the gates to test them: hit Emergency stop, or run a pass outside market hours.</Td><Td/><Td/><Td/><Td/></tr>}
                {rejections.map(r=>(
                  <tr key={r.id}>
                    <Td mono>{r.id}</Td><Td mono>{r.created_at}</Td><Td mono bold>{r.ticker}</Td>
                    <Td right mono>{r.score??"—"}</Td>
                    <Td tone={r.status==="REJECTED"?"red":"amber"} bold>{r.status}</Td>
                    <Td>{r.reasons.map(x=><Chip key={x} text={x} tone={x==="manual_emergency_stop"||x==="operational_freeze"?"red":"amber"}/>)}</Td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Section>
      )}

      {tab==="Settings" && (
        <Section title="Data source">
          <div style={{padding:14,maxWidth:560}}>
            <div style={{display:"flex",gap:8,marginBottom:14}}>
              <Btn tone={demo?"green":undefined} onClick={()=>{setDemo(true);loadDemo();}}>Demo data</Btn>
              <Btn tone={!demo?"green":undefined} onClick={()=>{ if(keys.key&&keys.secret){setDemo(false);loadLive();} else setErr("Enter Alpaca key + secret first."); }}>Alpaca live</Btn>
              {!demo && <Btn onClick={loadLive}>Re-sync bars</Btn>}
            </div>
            <label style={{fontSize:11,color:C.label,display:"block",marginBottom:10}}>Alpaca API key ID
              <input value={keys.key} onChange={e=>setKeys(k=>({...k,key:e.target.value.trim()}))}
                style={{display:"block",width:"100%",border:`1px solid ${C.border}`,borderRadius:7,padding:"9px 10px",fontSize:13,marginTop:4,boxSizing:"border-box"}}/>
            </label>
            <label style={{fontSize:11,color:C.label,display:"block"}}>Alpaca API secret
              <input type="password" value={keys.secret} onChange={e=>setKeys(k=>({...k,secret:e.target.value.trim()}))}
                style={{display:"block",width:"100%",border:`1px solid ${C.border}`,borderRadius:7,padding:"9px 10px",fontSize:13,marginTop:4,boxSizing:"border-box"}}/>
            </label>
            <div style={{fontSize:12,color:C.label,marginTop:12,lineHeight:1.55}}>
              Uses the free IEX feed (no SIP subscription needed). Keys live only in this session's memory — nothing is stored,
              so re-enter them if you reload. Paper-account keys work fine since this only reads market data.
            </div>
          </div>
        </Section>
      )}
    </div>
  );
}

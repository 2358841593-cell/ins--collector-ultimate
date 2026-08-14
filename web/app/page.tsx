"use client";

import Image from "next/image";
import { useEffect, useState } from "react";

type TourKey = "difference" | "run" | "delivery" | "memory";

const tourTabs: Array<{ id: TourKey; label: string; helper: string }> = [
  { id: "difference", label: "为什么不同", helper: "完整任务，不是更多按钮" },
  { id: "run", label: "Agent 如何执行", helper: "从发现到交付连续推进" },
  { id: "delivery", label: "交付什么", helper: "画像、判断和原始证据" },
  { id: "memory", label: "数据留下什么", helper: "形成团队自己的达人资产" },
];

function BrandMark() {
  return <span className="brand-mark" aria-hidden><i/><i/><i/></span>;
}

function ArchitectureModal({ onClose }: { onClose: () => void }) {
  const [zoom, setZoom] = useState(1.18);

  useEffect(() => {
    document.body.classList.add("no-scroll");
    const closeOnEscape = (event: KeyboardEvent) => event.key === "Escape" && onClose();
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.body.classList.remove("no-scroll");
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [onClose]);

  return <div className="editorial-modal-backdrop" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
    <section className="repo-flow-modal" role="dialog" aria-modal="true" aria-labelledby="architecture-title">
      <header>
        <div><span>AGENT EXECUTION MAP</span><h2 id="architecture-title">一条会校验、补采和学习的执行链</h2><p>Agent 直接研究公开页面，并调用团队已经积累的历史数据。证据不足时只补缺失项，通过校验后才进入决策和交付。</p></div>
        <button onClick={onClose} aria-label="关闭架构图">×</button>
      </header>
      <div className="repo-flow-toolbar" aria-label="架构图缩放控件">
        <button onClick={() => setZoom((value) => Math.min(1.55,value + .15))} aria-label="放大架构图">＋</button>
        <button onClick={() => setZoom(1.18)} aria-label="适应窗口">复位</button>
        <button onClick={() => setZoom((value) => Math.max(.8,value - .15))} aria-label="缩小架构图">−</button>
      </div>
      <div className="repo-flow-viewport">
        <svg className="repo-flow-svg" style={{ width: `${zoom * 100}%` }} viewBox="0 0 2860 760" role="img" aria-labelledby="flow-title flow-description">
          <title id="flow-title">COLLECTOR 红人研究 Agent 执行流程</title>
          <desc id="flow-description">任务配置和历史反馈进入候选发现，经过主页浅扫和深度研究两道门禁，通过后完成决策、版本化交付和客户反馈回流。</desc>
          <defs>
            <marker id="flow-arrow" markerWidth="9" markerHeight="9" refX="7" refY="3.5" orient="auto"><path d="M0,0 L0,7 L8,3.5 z"/></marker>
            <marker id="flow-arrow-warm" markerWidth="9" markerHeight="9" refX="7" refY="3.5" orient="auto"><path d="M0,0 L0,7 L8,3.5 z"/></marker>
          </defs>

          <g className="repo-flow-edges">
            <path className="repo-flow-edge" d="M270 292 H335"/><path className="repo-flow-edge" d="M605 292 H670"/>
            <path className="repo-flow-edge" d="M940 292 H998"/><path className="repo-flow-edge" d="M1132 292 H1195"/>
            <path className="repo-flow-edge" d="M1485 292 H1538"/><path className="repo-flow-edge" d="M1672 292 H1735"/>
            <path className="repo-flow-edge" d="M2005 292 H2070"/><path className="repo-flow-edge" d="M2310 292 H2375"/>
            <path className="repo-flow-edge" d="M2595 292 H2650"/>
            <path className="repo-flow-edge warm" d="M1065 359 C1065 445 1120 510 1260 510 H1735"/>
            <path className="repo-flow-edge retry" d="M1605 359 V475 H1485 C1395 475 1360 430 1360 372"/>
            <path className="repo-flow-edge warm" d="M2790 345 V650 H465 C390 650 350 610 350 550 V360"/>
            <path className="repo-flow-edge database" d="M2190 360 V535"/>
          </g>

          <g className="repo-flow-node input-node">
            <rect x="40" y="245" width="230" height="94" rx="8"/>
            <text x="155" y="278" textAnchor="middle"><tspan className="node-kicker">TEAM INPUT</tspan><tspan x="155" dy="25" className="node-title">任务要求与历史反馈</tspan><tspan x="155" dy="20" className="node-copy">门槛 / 字段 / 团队选择</tspan></text>
          </g>
          <g className="repo-flow-node stage-node stage-one">
            <rect x="335" y="218" width="270" height="148" rx="9"/>
            <text x="470" y="252" textAnchor="middle"><tspan className="node-kicker">DISCOVERY AGENT</tspan><tspan x="470" dy="32" className="node-title">发现候选</tspan><tspan x="470" dy="25" className="node-copy">历史优质达人 + 公开页面</tspan><tspan x="470" dy="20" className="node-copy">结构化搜索，全局去重</tspan></text>
          </g>
          <g className="repo-flow-node stage-node stage-two">
            <rect x="670" y="218" width="270" height="148" rx="9"/>
            <text x="805" y="252" textAnchor="middle"><tspan className="node-kicker">QUALIFY AGENT</tspan><tspan x="805" dy="32" className="node-title">主页初筛</tspan><tspan x="805" dy="25" className="node-copy">身份 / 简介 / 赛道</tspan><tspan x="805" dy="20" className="node-copy">活跃度 / 商业入口</tspan></text>
          </g>
          <g className="repo-flow-node barrier-node barrier-one">
            <polygon points="1065,205 1132,292 1065,379 998,292"/>
            <text x="1065" y="280" textAnchor="middle"><tspan className="node-kicker">QUALITY GATE</tspan><tspan x="1065" dy="24" className="node-title small">符合硬门槛？</tspan></text>
            <text x="1140" y="276" className="edge-label">是</text><text x="1078" y="420" className="edge-label warm-label">否</text>
          </g>
          <g className="repo-flow-node stage-node stage-three">
            <rect x="1195" y="218" width="290" height="148" rx="9"/>
            <text x="1340" y="252" textAnchor="middle"><tspan className="node-kicker">RESEARCH SWARM</tspan><tspan x="1340" dy="32" className="node-title">并行深度研究</tspan><tspan x="1340" dy="25" className="node-copy">帖子 / 评论 / 受众 / 播放证据</tspan><tspan x="1340" dy="20" className="node-copy">翻译 / 商业信号 / 原帖链接</tspan></text>
          </g>
          <g className="repo-flow-node barrier-node barrier-two">
            <polygon points="1605,205 1672,292 1605,379 1538,292"/>
            <text x="1605" y="280" textAnchor="middle"><tspan className="node-kicker">EVIDENCE GATE</tspan><tspan x="1605" dy="24" className="node-title small">证据完整？</tspan></text>
            <text x="1680" y="276" className="edge-label">是</text><text x="1618" y="420" className="edge-label retry-label">否</text>
          </g>
          <g className="repo-flow-node stage-node stage-four">
            <rect x="1735" y="218" width="270" height="148" rx="9"/>
            <text x="1870" y="252" textAnchor="middle"><tspan className="node-kicker">DECISION AGENT</tspan><tspan x="1870" dy="32" className="node-title">最终决策</tspan><tspan x="1870" dy="25" className="node-copy">门槛 / 评分 / 报价</tspan><tspan x="1870" dy="20" className="node-copy">固定复核，结果路由</tspan></text>
          </g>
          <g className="repo-flow-node delivery-node stage-five">
            <rect x="2070" y="232" width="240" height="120" rx="9"/>
            <text x="2190" y="263" textAnchor="middle"><tspan className="node-kicker">VERSIONED DELIVERY</tspan><tspan x="2190" dy="31" className="node-title">版本化交付</tspan><tspan x="2190" dy="23" className="node-copy">HTML / XLSX / JSON</tspan></text>
          </g>
          <g className="repo-flow-node client-node stage-six">
            <rect x="2375" y="232" width="220" height="120" rx="9"/>
            <text x="2485" y="263" textAnchor="middle"><tspan className="node-kicker">TEAM REVIEW</tspan><tspan x="2485" dy="31" className="node-title">团队选择</tspan><tspan x="2485" dy="23" className="node-copy">合适 / 不合适 / 待定</tspan></text>
          </g>
          <g className="repo-flow-node feedback-node stage-seven">
            <rect x="2650" y="232" width="170" height="120" rx="9"/>
            <text x="2735" y="263" textAnchor="middle"><tspan className="node-kicker">FEEDBACK LOOP</tspan><tspan x="2735" dy="31" className="node-title">安全回流</tspan><tspan x="2735" dy="23" className="node-copy">策略与资产入库</tspan></text>
          </g>

          <g className="repo-flow-node rejection-node">
            <rect x="1135" y="470" width="250" height="80" rx="8"/>
            <text x="1260" y="502" textAnchor="middle"><tspan className="node-title small">记录业务排除原因</tspan><tspan x="1260" dy="23" className="node-copy">保留证据，进入最终路由</tspan></text>
          </g>
          <g className="repo-flow-node retry-node">
            <rect x="1460" y="455" width="250" height="82" rx="8"/>
            <text x="1585" y="487" textAnchor="middle"><tspan className="node-title small">仅补失败项</tspan><tspan x="1585" dy="23" className="node-copy">已完成证据不会重复采集</tspan></text>
          </g>
          <g className="repo-flow-node database-node">
            <ellipse cx="2190" cy="550" rx="185" ry="28"/><rect x="2005" y="550" width="370" height="82"/><ellipse cx="2190" cy="632" rx="185" ry="28"/>
            <text x="2190" y="573" textAnchor="middle"><tspan className="node-kicker">USER-OWNED DATABASE</tspan><tspan x="2190" dy="28" className="node-title">团队自己的本地达人数据库</tspan><tspan x="2190" dy="22" className="node-copy">画像 / 原始证据 / 反馈 / 策略版本</tspan></text>
          </g>
          <text x="1430" y="690" textAnchor="middle" className="loop-caption">下一轮任务先调用已有画像、证据与团队偏好</text>

          <circle className="flow-particle" r="7"><animateMotion dur="8s" repeatCount="indefinite" path="M270 292 H998 L1065 205 L1132 292 H1538 L1605 205 L1672 292 H2820"/></circle>
          <circle className="flow-particle retry-particle" r="6"><animateMotion dur="4.5s" repeatCount="indefinite" path="M1605 379 V475 H1485 C1395 475 1360 430 1360 372"/></circle>
          <circle className="flow-particle loop-particle" r="6"><animateMotion dur="11s" repeatCount="indefinite" path="M2790 345 V650 H465 C390 650 350 610 350 550 V360"/></circle>
        </svg>
      </div>
      <footer><span><i/> 主执行链</span><span><i/> 证据校验</span><span><i/> 缺失项补采</span><strong>画像、证据和反馈最终沉淀在团队本地</strong></footer>
    </section>
  </div>;
}

function HeroDiscoveryPreview() {
  return <div className="tour-hero-preview" aria-label="COLLECTOR 运行中的 Discovery 任务示意">
    <div className="tour-preview-photo"><Image src="/collector-hero-creator-v2.png" alt="正在使用电脑的内容创作者" fill priority sizes="(max-width: 900px) 100vw, 52vw"/></div>
    <div className="tour-run-status"><span><i/> AGENT 正在执行</span><strong>Instagram 创作者发现与研究</strong><small>无需守在页面前等待每一步</small></div>
    <div className="tour-run-steps"><span className="done"><i>✓</i>发现候选</span><span className="done"><i>✓</i>主页初筛</span><span className="active"><i>3</i>分析评论与受众</span><span><i>4</i>整理交付</span></div>
    <div className="tour-creator-result">
      <span className="tour-avatar"><Image src="/creator-michelle.jpg" alt="@beauty.n.furbabyfanatic 的主页头像" width={44} height={44}/></span><div><small>刚完成研究</small><strong>@beauty.n.furbabyfanatic</strong></div><span><small>购买意向评论</small><b>17 条</b></span><span><small>核心受众</small><b>US 55.1%</b></span><a href="/discovery">查看画像 ↗</a>
    </div>
  </div>;
}

function DifferencePanel() {
  return <div className="tour-panel tour-difference-panel">
    <header><h2>传统服务加速人工处理，<br/>Agent 接手整条业务链。</h2><p>传统发现工具把搜索按钮搬到一个平台里，判断、记录和复查仍由人完成。COLLECTOR 接收完整研究标准，连续执行到可复查的结果。</p></header>
    <div className="tour-two-ways" aria-label="传统发现服务与 COLLECTOR 的工作方式对比">
      <article><small>传统第三方发现服务</small><h3>人是工作流</h3><ol><li>设置条件并开始搜索</li><li>逐个打开创作者主页</li><li>自己判断内容和受众</li><li>复制信息并维护名单</li></ol><strong>每一步都需要人继续操作</strong></article>
      <article><small>COLLECTOR</small><h3>Agent 是工作流</h3><ol><li>接收团队的完整研究要求</li><li>自动发现、初筛和排除</li><li>并行研究评论、受众和商业证据</li><li>交付完整画像并写入本地数据库</li></ol><strong>团队直接检查最终结果</strong></article>
    </div>
  </div>;
}

function RunPanel() {
  return <div className="tour-panel tour-run-panel">
    <header><h2>提交一次标准，<br/>Agent 自动推进任务。</h2><p>平台、市场、粉丝规模、排除项、证据标准和交付字段都可以按照团队业务定制。体验版复现一轮已完成项目。</p></header>
    <div className="tour-task-board">
      <div className="tour-brief"><small>你的任务要求</small><h3>寻找符合团队标准的 Instagram 创作者</h3><dl><div><dt>粉丝规模</dt><dd>10K-150K</dd></div><div><dt>目标受众</dt><dd>目标市场 ≥30%</dd></div><div><dt>粉丝质量</dt><dd>Fake &lt;25%</dd></div><div><dt>需要证据</dt><dd>评论、受众、商业入口</dd></div></dl></div>
      <div className="tour-board-arrow"><span>提交一次</span><b>→</b></div>
      <div className="tour-agent-run"><header><span><i/> AGENT RUNNING</span><b>任务自动推进</b></header><ol><li className="done"><i>✓</i><span><b>发现候选</b><small>多源查找并去重</small></span></li><li className="done"><i>✓</i><span><b>主页初筛</b><small>身份、赛道与硬门槛</small></span></li><li className="active"><i>3</i><span><b>并行深度研究</b><small>评论、受众、内容和商业证据</small></span></li><li><i>4</i><span><b>整理交付</b><small>完整画像与本地入库</small></span></li></ol></div>
    </div>
  </div>;
}

function DeliveryPanel() {
  return <div className="tour-panel tour-delivery-panel">
    <header><h2>每位达人都有一份<br/>可复查的研究档案。</h2><p>画像同时展示关键数据、判断结论和原始链接。团队可以快速判断，也可以直接核对结论依据。</p></header>
    <div className="tour-profile-sheet">
      <div className="tour-profile-person"><span className="tour-avatar large"><Image src="/creator-michelle.jpg" alt="@beauty.n.furbabyfanatic 的主页头像" width={64} height={64}/></span><div><small>INSTAGRAM CREATOR</small><h3>@beauty.n.furbabyfanatic</h3><p>Michelle Guttman，21.0K 粉丝</p></div><a href="/discovery">打开完整画像 ↗</a></div>
      <div className="tour-profile-comment"><small>购买意向评论，保留译文、原文和原帖</small><p>“哇，看起来我确实需要这个 😍”</p><a href="https://www.instagram.com/" target="_blank" rel="noreferrer">查看原帖 ↗</a></div>
      <div className="tour-profile-fields"><span><small>真实互动率</small><b>20.1%</b></span><span><small>虚假粉丝</small><b>21.5%</b></span><span><small>电商入口</small><b>自营店 ↗</b></span><span><small>核心受众</small><b>US 55.1%</b></span><span><small>受众性别</small><b>女性 92.8%</b></span><span><small>预估报价</small><b>$179.86</b></span></div>
    </div>
  </div>;
}

function MemoryPanel() {
  return <div className="tour-panel tour-memory-panel">
    <header><h2>研究结果持续沉淀，<br/>下一次任务直接复用。</h2><p>完整画像、原始证据以及团队的保留或排除理由都会写入本地数据库，形成能够长期调用的数据资产。</p></header>
    <div className="tour-memory-flow">
      <div><small>任务 01</small><strong>已完成创作者研究</strong><span>完整画像＋证据</span></div><i>→</i>
      <div className="tour-memory-core"><b/><b/><b/><small>团队自己的机器</small><strong>达人数据库</strong><span>画像 / 证据 / 团队判断</span></div><i>→</i>
      <div><small>任务 02</small><strong>新的品牌需求</strong><span>先召回已有资产</span></div>
    </div>
    <footer><span><b>避免重复</b>先识别已经研究过的账号</span><span><b>记住判断</b>保留团队为什么选择或排除</span><span><b>越用越懂</b>后续任务沿用团队偏好</span></footer>
  </div>;
}

export default function ProductHome() {
  const [activeTour, setActiveTour] = useState<TourKey>("difference");
  const [showArchitecture, setShowArchitecture] = useState(false);

  const openTour = (id: TourKey) => {
    setActiveTour(id);
    window.requestAnimationFrame(() => document.getElementById("product-tour")?.scrollIntoView({ behavior: "smooth", block: "start" }));
  };

  return <main className="tour-home" id="top">
    <header className="tour-nav">
      <a className="tour-brand" href="#top"><BrandMark/><strong>COLLECTOR</strong><span>INFLUENCER RESEARCH AGENT</span></a>
      <nav aria-label="产品导航"><button onClick={() => openTour("difference")}>工作方式</button><button onClick={() => openTour("run")}>自动执行</button><button onClick={() => openTour("delivery")}>完整画像</button><button onClick={() => openTour("memory")}>本地资产</button><button onClick={() => setShowArchitecture(true)}>完整架构</button></nav>
      <a className="tour-nav-cta" href="/discovery">体验 Discovery ↗</a>
    </header>

    <section className="tour-hero">
      <div className="tour-hero-copy">
        <span className="tour-eyebrow">红人研究 Agent</span>
        <h1>告诉 COLLECTOR <span className="headline-keep">要找谁，</span><br/><b>Agent 完成整轮研究。</b></h1>
        <p>从发现、初筛到评论与受众研究，交付完整画像并存入你的本地数据库。</p>
        <div className="tour-hero-actions"><a href="/discovery">体验 Discovery <b>→</b></a><button onClick={() => openTour("difference")}>了解工作方式</button></div>
      </div>
      <HeroDiscoveryPreview/>
    </section>

    <section className="tour-guide" id="product-tour">
      <nav className="tour-tabs" role="tablist" aria-label="产品导览">
        {tourTabs.map((tab) => <button key={tab.id} role="tab" aria-selected={activeTour === tab.id} className={activeTour === tab.id ? "active" : ""} onClick={() => setActiveTour(tab.id)}><b>{tab.label}</b><small>{tab.helper}</small></button>)}
      </nav>
      <div className="tour-stage" role="tabpanel" key={activeTour}>
        {activeTour === "difference" && <DifferencePanel/>}
        {activeTour === "run" && <RunPanel/>}
        {activeTour === "delivery" && <DeliveryPanel/>}
        {activeTour === "memory" && <MemoryPanel/>}
      </div>
      <footer className="tour-guide-footer"><span>查看门禁、补采、交付和反馈如何连成完整执行链</span><button onClick={() => setShowArchitecture(true)}>打开完整执行架构 ↗</button><a href="/discovery">体验 Discovery →</a></footer>
    </section>

    <footer className="tour-footer"><a className="tour-brand" href="#top"><BrandMark/><strong>COLLECTOR</strong></a><p>为团队自动完成红人发现、研究、判断和数据沉淀。</p><a href="#top">返回顶部 ↑</a></footer>
    {showArchitecture && <ArchitectureModal onClose={() => setShowArchitecture(false)}/>}
  </main>;
}

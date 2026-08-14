"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import demoData from "../demo-creators.json";

type Creator = (typeof demoData.creators)[number];
type MetricItem = { name: string; pct: number };

const nicheGroups = [
  { name: "美妆与个护", items: ["护肤", "彩妆", "护发", "美体与指甲", "香水香氛", "美容工具"] },
  { name: "时尚与配饰", items: ["女装", "男装", "鞋靴", "包袋", "珠宝与配饰", "运动服饰"] },
  { name: "家居与生活", items: ["家居装饰", "厨房与餐厨", "清洁与收纳", "家电与智能家居", "DIY 与家装", "庭院与园艺"] },
  { name: "母婴与亲子", items: ["孕期与产后", "婴幼儿护理", "育儿与家庭", "童装", "玩具与益智"] },
  { name: "健康与养生", items: ["营养与补充剂", "健康饮食", "身心疗愈", "睡眠与恢复", "女性与男性健康"] },
  { name: "食品与饮料", items: ["家常烹饪", "烘焙与甜品", "健康营养", "零食与杂货", "咖啡与饮品"] },
  { name: "数码与游戏", items: ["手机与配件", "电脑与外设", "消费电子", "游戏与电竞", "摄影与创作者设备"] },
  { name: "运动与户外", items: ["健身与力量训练", "瑜伽与普拉提", "跑步与骑行", "露营与徒步", "球类运动"] },
  { name: "宠物与动物", items: ["狗", "猫", "小型与异宠", "宠物护理与健康", "宠物用品"] },
  { name: "旅行与酒店", items: ["目的地攻略", "酒店与度假村", "平价旅行", "奢华旅行", "亲子旅行"] },
  { name: "汽车与出行", items: ["汽车", "摩托车", "汽车配件", "维修与养护", "新能源汽车"] },
  { name: "生活方式与购物", items: ["日常生活", "购物与零售", "极简生活", "可持续生活", "奢华生活"] },
  { name: "文化与娱乐", items: ["影视", "音乐", "图书", "艺术与设计", "摄影", "动漫"] },
  { name: "教育、职业与商业", items: ["教育与语言学习", "商业与创业", "营销与社交媒体", "个人理财", "职业发展"] },
];

const filters = [
  { id: "platform", label: "平台", value: "Instagram", options: ["Instagram", "TikTok", "YouTube"] },
  { id: "followers", label: "粉丝规模", value: "10K - 150K", options: ["10K - 150K", "10K - 50K", "50K - 150K", "150K+"] },
  { id: "market", label: "创作者市场", value: "美国、英国、加拿大及欧洲", options: ["美国、英国、加拿大及欧洲", "仅美国", "欧洲主要市场", "全球"] },
  { id: "audience", label: "目标市场受众", value: "≥ 30%", options: ["≥ 30%", "≥ 40%", "≥ 50%", "≥ 60%"] },
  { id: "fake", label: "虚假粉丝率", value: "< 25%", options: ["< 25%", "< 20%", "< 15%"] },
  { id: "sponsor", label: "赞助内容占比", value: "< 30%", options: ["< 30%", "< 20%", "< 15%"] },
  { id: "er", label: "真实 Reels ER", value: "≥ 1%", options: ["≥ 1%", "≥ 2%", "≥ 3%", "≥ 5%"] },
];

const number = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 1 });
const money = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });
const LOAD_SIZE = 8;

function compactNumber(value: number | null | undefined) {
  if (value == null) return "未采集";
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(value >= 100_000 ? 0 : 1)}K`;
  return number.format(value);
}

function percent(value: number | null | undefined, digits = 1) {
  return value == null ? "未采集" : `${value.toFixed(digits)}%`;
}

function evidenceSourceLabel(value: string | null | undefined) {
  if (!value) return "本地证据";
  const source = value.toLowerCase();
  if (source.includes("instagram") || source.includes("ig_")) return "Instagram 原生证据";
  return source.includes("audience") ? "多源受众证据" : "本地采集证据";
}

function TextLink({ href, children }: { href?: string | null; children: React.ReactNode }) {
  if (!href) return <span className="muted">未采集</span>;
  return <a className="text-link" href={href} target="_blank" rel="noreferrer" onClick={(event) => event.stopPropagation()}>{children} <span aria-hidden>↗</span></a>;
}

function Icon({ name, size = 18 }: { name: string; size?: number }) {
  const paths: Record<string, React.ReactNode> = {
    search: <><circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/></>,
    chevron: <path d="m7 10 5 5 5-5"/>,
    lock: <><rect x="5" y="10" width="14" height="10" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/></>,
    arrow: <path d="m9 18 6-6-6-6"/>,
    close: <><path d="m6 6 12 12"/><path d="m18 6-12 12"/></>,
    tune: <><path d="M4 7h16M7 12h10M10 17h4"/></>,
    spark: <path d="m12 3 1.7 4.8L18 10l-4.3 2.2L12 17l-1.7-4.8L6 10l4.3-2.2L12 3Z"/>,
    database: <><ellipse cx="12" cy="5" rx="7" ry="3"/><path d="M5 5v6c0 1.7 3.1 3 7 3s7-1.3 7-3V5M5 11v6c0 1.7 3.1 3 7 3s7-1.3 7-3v-6"/></>,
    check: <path d="m5 12 4 4L19 6"/>,
  };
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden>{paths[name]}</svg>;
}

function FieldRows({ rows }: { rows: Array<[string, React.ReactNode]> }) {
  return <dl className="field-rows">{rows.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value ?? <span className="muted">未采集</span>}</dd></div>)}</dl>;
}

function MetricList({ items, prefix = "" }: { items?: MetricItem[] | null; prefix?: string }) {
  if (!items?.length) return <span className="muted">未采集</span>;
  return <div className="metric-list">{items.map((item) => <div key={item.name} className="metric-row"><span>{prefix}{item.name}</span><span>{percent(item.pct)}</span><i style={{ width: `${Math.min(item.pct, 100)}%` }} /></div>)}</div>;
}

function CreatorAvatar({ creator, large = false }: { creator: Creator; large?: boolean }) {
  const [failed, setFailed] = useState(false);
  return <div className={large ? "avatar avatar-large" : "avatar"}>{creator.picture && !failed ? <img src={creator.picture} alt="" onError={() => setFailed(true)} /> : <span>{creator.fullName?.slice(0, 1) ?? creator.handle.slice(0, 1)}</span>}</div>;
}

function NicheSelector({ onCustom }: { onCustom: (value: string) => void }) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [expanded, setExpanded] = useState("美妆与个护");
  const root = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const close = (event: MouseEvent) => { if (!root.current?.contains(event.target as Node)) setOpen(false); };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, []);

  const groups = nicheGroups.map((group) => ({ ...group, items: group.items.filter((item) => `${group.name}${item}`.includes(query.trim())) })).filter((group) => !query.trim() || group.items.length);

  return <div className="niche-wrap" ref={root}>
    <button type="button" className={`niche-trigger ${open ? "active" : ""}`} onClick={() => setOpen(!open)} aria-expanded={open}>
      <span className="niche-symbol"><Icon name="spark" size={17} /></span>
      <span><small>当前赛道</small><strong>亚马逊导购类</strong></span>
      <Icon name="chevron" size={18} />
    </button>
    <p className="niche-help"><span>仅此赛道可体验</span> 当前数据主要覆盖美妆个护、护肤与生活好物。切换其他赛道需定制执行。</p>
    {open && <div className="niche-menu">
      <div className="niche-menu-head"><strong>选择赛道</strong><span>单选</span></div>
      <div className="niche-search"><Icon name="search" size={16}/><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索赛道或细分领域" /></div>
      <div className="niche-scroll">
        <p className="menu-eyebrow">当前可体验</p>
        <button className="default-niche" onClick={() => setOpen(false)}><span><b>亚马逊导购类</b><small>Amazon Finds · Storefront · 好物清单</small></span><Icon name="check" /></button>
        <p className="menu-eyebrow custom-label">更多定制赛道</p>
        {groups.map((group) => <div className="niche-group" key={group.name}>
          <button onClick={() => setExpanded(expanded === group.name ? "" : group.name)}><span>{group.name}</span><span className="custom-mark">定制</span><Icon name="chevron" size={15}/></button>
          {(expanded === group.name || query) && <div className="niche-items">{group.items.map((item) => <button key={item} onClick={() => { setOpen(false); onCustom(`${group.name} > ${item}`); }}><span>{item}</span><Icon name="lock" size={13}/></button>)}</div>}
        </div>)}
      </div>
    </div>}
  </div>;
}

function ContactModal({ reason, onClose }: { reason: string; onClose: () => void }) {
  const [copied, setCopied] = useState(false);
  const mailto = `mailto:?subject=${encodeURIComponent("COLLECTOR 定制 Discovery 需求")}&body=${encodeURIComponent(`定制 Discovery 需求：${reason}`)}`;
  const copy = async () => {
    await navigator.clipboard?.writeText(`定制 Discovery 需求：${reason}`);
    setCopied(true);
  };
  return <div className="modal-backdrop" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
    <section className="contact-modal" role="dialog" aria-modal="true" aria-labelledby="contact-title">
      <button className="icon-button modal-close" onClick={onClose} aria-label="关闭"><Icon name="close"/></button>
      <span className="modal-icon"><Icon name="spark" size={23}/></span>
      <p className="eyebrow">CUSTOM DISCOVERY</p>
      <h2 id="contact-title">这个条件需要定制执行</h2>
      <p>演示版仅复现已完成的亚马逊导购项目。你的选择已经整理好，联系团队即可从真实数据采集、核验到交付完整执行。</p>
      <div className="request-summary"><small>本次意向</small><strong>{reason}</strong></div>
      <div className="contact-actions"><a href={mailto} className="primary-button">请联系我们 <Icon name="arrow" size={16}/></a><button className="secondary-button" onClick={copy}>{copied ? "需求已复制" : "复制需求摘要"}</button></div>
      <small className="modal-footnote">关闭后仍保留默认赛道和已验证结果，不会误显示未执行的定制数据。</small>
    </section>
  </div>;
}

function FilterPanel({ onRun, onCustom, mobileOpen, onMobileToggle }: { onRun: () => void; onCustom: (value: string) => void; mobileOpen: boolean; onMobileToggle: () => void }) {
  return <aside className="filter-panel">
    <div className="filter-title"><div><p className="eyebrow">DISCOVERY BRIEF</p><h1>发现匹配的创作者</h1></div><Icon name="tune" /></div>
    <button className="mobile-filter-toggle" type="button" onClick={onMobileToggle} aria-expanded={mobileOpen} aria-controls="discovery-filter-body">
      <span><small>当前体验条件</small><strong>Instagram / 10K-150K / 目标受众 ≥30%</strong></span>
      <span className="mobile-filter-action">{mobileOpen ? "收起" : "调整"}<Icon name="chevron" size={16}/></span>
    </button>
    <div id="discovery-filter-body" className={`filter-body ${mobileOpen ? "open" : ""}`}>
      <NicheSelector onCustom={onCustom}/>
      <div className="filter-divider" />
      <div className="filter-list">{filters.map((filter, index) => <div className="filter-field" key={filter.id}>{index === 0 && <><p className="filter-group-label">创作者条件</p><p className="filter-trial-note"><Icon name="lock" size={12}/> 体验版仅支持下方默认条件，切换任一选项需定制执行。</p></>}{index === 3 && <p className="filter-group-label">受众与质量</p>}<label><span>{filter.label}</span><select value={filter.value} onChange={(event) => event.target.value !== filter.value && onCustom(`${filter.label}：${event.target.value}`)}>{filter.options.map((option) => <option key={option}>{option}</option>)}</select></label></div>)}</div>
      <button className="run-button" onClick={onRun}><Icon name="search" size={17}/>运行默认 Discovery</button>
      <p className="filter-footnote"><Icon name="database" size={14}/> 默认体验复现已完成项目，不会触发新的采集任务。</p>
    </div>
  </aside>;
}

function CommentPreview({ creator }: { creator: Creator }) {
  const comment = creator.intent.comments[0];
  if (!comment) return <span className="muted">没有识别到购买意向评论</span>;
  return <div className="comment-preview"><span className={`grade grade-${comment.grade}`}>{comment.gradeZh ?? comment.grade}</span><p>{comment.translatedZh}</p><TextLink href={comment.postUrl}>看帖</TextLink></div>;
}

function ResultRow({ creator, onOpen }: { creator: Creator; onOpen: () => void }) {
  const quote = creator.evidence.pricing?.quote_usd;
  const hasRealEr = creator.performance.realErMedian != null;
  return <article className="result-row" role="button" tabIndex={0} onClick={onOpen} onKeyDown={(event) => (event.key === "Enter" || event.key === " ") && onOpen()}>
    <div className="creator-cell"><CreatorAvatar creator={creator}/><span><strong>@{creator.handle}</strong><small>{creator.fullName}</small></span></div>
    <div className="cell numeric"><small>{hasRealEr ? "粉丝 / 真实 ER" : "粉丝"}</small><strong>{compactNumber(creator.followerCount)}</strong>{hasRealEr && <span>{percent(creator.performance.realErMedian)} 中位</span>}</div>
    <div className="cell intent-cell"><small>购买意向评论 · {creator.intent.comments.length} 条</small><CommentPreview creator={creator}/></div>
    <div className="cell"><small>电商入口 / 赞助</small><strong className={creator.storefront.url ? "positive" : ""}>{creator.storefront.url ? `${creator.storefront.type ?? "橱窗"} ↗` : "未确认"}</strong><span>赞助 {percent(creator.commerce.sponsorshipSaturation)}</span></div>
    <div className="cell"><small>粉丝质量</small><strong className={creator.integrity.fakePct != null && creator.integrity.fakePct < 25 ? "positive" : "warning"}>Fake {percent(creator.integrity.fakePct)}</strong><span>真人 {percent(creator.integrity.audienceTypes?.real_people)}</span></div>
    <div className="cell"><small>核心受众</small><strong>{creator.audience.topCountry ?? "未采集"} {percent(creator.audience.countries?.[0]?.pct)}</strong><span>{creator.audience.topLanguage ?? "未采集"} · 女性 {percent(creator.audience.genders?.find((item) => item.name === "FEMALE")?.pct)}</span></div>
    <div className="cell price-cell"><small>预估报价</small><strong>{quote?.default != null ? money.format(quote.default) : "未估算"}</strong><span>{quote?.min != null && quote?.max != null ? `${money.format(quote.min)}-${money.format(quote.max)}` : "未估算"}</span></div>
    <span className="row-arrow"><Icon name="arrow" size={18}/></span>
  </article>;
}

function DataCard({ title, className = "", children }: { title: string; className?: string; children: React.ReactNode }) {
  return <section className={`data-card ${className}`}><h3>{title}</h3>{children}</section>;
}

function DiscoveryAnimation({ step }: { step: number }) {
  const phases = [
    ["理解任务条件", "读取平台、市场、粉丝规模与质量要求，建立本轮判断口径。"],
    ["召回候选创作者", "从本地资产中寻找符合条件的账号，并关联已经保存的历史研究。"],
    ["核验受众质量", "交叉检查受众地区、真实互动、异常粉丝与内容相关性。"],
    ["读取评论与商业信号", "分析购买意向评论、电商入口、合作痕迹与原始证据。"],
    ["整理完整创作者画像", "把通过核验的字段与证据整理成可继续调用的本地档案。"],
  ];
  const progress = [12, 34, 57, 79, 96][step] ?? 12;
  const current = phases[step] ?? phases[0];
  return <section className="discovery-animation discovery-cinematic" aria-live="polite">
    <img className="discovery-visual-image" src="/discovery-creator-studio-v1.webp" alt="" />
    <div className="cinematic-scrim" />
    <div className="cinematic-focus" aria-hidden><i/><i/><i/><i/></div>
    <div className="cinematic-topline">
      <span>COLLECTOR AGENT DISCOVERY</span>
      <strong><i/> 正在自动执行</strong>
    </div>
    <div className="cinematic-copy">
      <p>AGENT 正在检索、判断与整理</p>
      <h2 key={current[0]}>{current[0]}</h2>
      <span>{current[1]}</span>
    </div>
    <div className="cinematic-signal-line" aria-hidden>
      <span>主页信号</span><span>评论语义</span><span>受众真实性</span><span>商业入口</span><span>原始证据</span>
    </div>
    <div className="cinematic-progress">
      <span>本轮执行进度</span>
      <div><i style={{ width: `${progress}%` }}/></div>
      <strong>{progress}%</strong>
    </div>
  </section>;
}

function DiscoveryIdle({ onRun }: { onRun: () => void }) {
  return <section className="discovery-idle discovery-visual">
    <img className="discovery-visual-image" src="/discovery-creator-studio-v1.webp" alt="四位不同领域的创作者在工作室中合影" />
    <span className="discovery-visual-index">DISCOVERY / 01</span>
    <div className="discovery-visual-copy">
      <span className="idle-ready"><i/> 默认体验任务已就绪</span>
      <h2>运行后，<br/>再显示创作者</h2>
      <p>Agent 会完成候选发现、受众核验、评论分析和商业证据整理。执行结束前，右侧不会提前摆出任何名单。</p>
      <button className="idle-run-button" onClick={onRun}><Icon name="search" size={17}/>运行默认 Discovery <Icon name="arrow" size={16}/></button>
      <small><Icon name="database" size={13}/> 本次体验读取本机已经完成的项目。</small>
    </div>
    <div className="discovery-visual-note">
      <small>执行完成后出现</small>
      <strong>完整画像与原始证据</strong>
      <span>主页、评论、受众、电商入口与报价</span>
    </div>
  </section>;
}

function CreatorDetail({ creator, onClose }: { creator: Creator; onClose: () => void }) {
  const [showAllComments, setShowAllComments] = useState(false);
  const [activeSection, setActiveSection] = useState("overview");
  const price = creator.evidence.pricing;
  const comments = showAllComments ? creator.intent.comments : creator.intent.comments.slice(0, 8);
  const female = creator.audience.genders?.find((item) => item.name === "FEMALE")?.pct;
  const hasPricing = Boolean(price?.quote_usd);
  const hasReels = Boolean(creator.evidence.pricingReelSamples?.length);
  const hasSponsoredPosts = Boolean(creator.evidence.sponsoredPosts?.length);
  const hasCommercialEvidence = hasPricing || hasReels || hasSponsoredPosts;
  const performanceRows: Array<[string, React.ReactNode]> = [];
  if (creator.performance.generalEr != null) performanceRows.push(["第三方 IG ER", percent(creator.performance.generalEr)]);
  if (creator.performance.realEr != null || creator.performance.realErMedian != null) performanceRows.push(["真实 Reels ER（均值 / 中位）", `${percent(creator.performance.realEr)} / ${percent(creator.performance.realErMedian)}`]);
  if (creator.performance.reelsEr != null || creator.performance.staticEr != null) performanceRows.push(["Reels ER / 静态 ER", `${percent(creator.performance.reelsEr)} / ${percent(creator.performance.staticEr)}`]);
  if (creator.performance.realErWindow) performanceRows.push(["真实窗口", creator.performance.realErWindow]);
  if (creator.commerce.ingredientsScore != null) performanceRows.push(["成分内容得分", creator.commerce.ingredientsScore]);
  if (creator.commerce.deviceSpecsScore != null) performanceRows.push(["设备参数得分", creator.commerce.deviceSpecsScore]);
  if (creator.commerce.skinScienceScore != null) performanceRows.push(["护肤科学得分", creator.commerce.skinScienceScore]);
  const sections = [{ id: "overview", label: "完整画像" }, { id: "comments", label: `购买评论 ${creator.intent.comments.length}` }, ...(hasCommercialEvidence ? [{ id: "evidence", label: "内容与证据" }] : [])];

  useEffect(() => { document.body.classList.add("no-scroll"); return () => document.body.classList.remove("no-scroll"); }, []);

  const scrollTo = (id: string) => { setActiveSection(id); document.getElementById(`detail-${id}`)?.scrollIntoView({ behavior: "smooth", block: "start" }); };

  return <div className="detail-overlay" role="dialog" aria-modal="true" aria-label={`${creator.handle} 完整画像`}>
    <header className="detail-header">
      <button className="back-button" onClick={onClose}><Icon name="close" size={17}/> 关闭画像</button>
      <div className="detail-person"><CreatorAvatar creator={creator} large/><span><p className="eyebrow">VERIFIED CREATOR PROFILE</p><h2>@{creator.handle}</h2><small>{creator.fullName} · {creator.creatorCountry ?? "地区未采集"}</small></span></div>
      <a className="secondary-button" href={creator.instagramUrl} target="_blank" rel="noreferrer">Instagram ↗</a>
    </header>
    <nav className="detail-nav">{sections.map((section) => <button key={section.id} className={activeSection === section.id ? "active" : ""} onClick={() => scrollTo(section.id)}>{section.label}</button>)}</nav>
    <main className="detail-body">
      <section className="profile-hero" id="detail-overview">
        <div><p className="eyebrow">CREATOR OVERVIEW</p><h2>创作者与受众完整画像</h2><p>以下仅展示数据库中已经采集并核验的创作者、受众、互动与商业字段；没有数据的模块不会出现。</p></div>
        <div className="hero-kpis"><span><small>粉丝</small><strong>{compactNumber(creator.followerCount)}</strong></span>{creator.performance.realErMedian != null && <span><small>真实 ER 中位</small><strong>{percent(creator.performance.realErMedian)}</strong></span>}{creator.integrity.fakePct != null && <span><small>虚假粉丝</small><strong>{percent(creator.integrity.fakePct)}</strong></span>}{creator.audience.targetCountriesPct != null && <span><small>目标市场受众</small><strong>{percent(creator.audience.targetCountriesPct)}</strong></span>}{price?.quote_usd?.default != null && <span><small>预估报价</small><strong>{money.format(price.quote_usd.default)}</strong></span>}</div>
      </section>

      <div className="card-grid">
        <DataCard title="创作者档案">
          <FieldRows rows={[
            ["全名", creator.fullName], ["类型", `${creator.creatorGender === "FEMALE" ? "女" : creator.creatorGender ?? "未采集"} · ${creator.accountType ?? "未采集"}`],
            ["账号", `${creator.isBusiness ? "商业账号" : "创作者账号"}${creator.creatorVerified ? " · 已认证" : ""}`], ["表现", `发帖 ${number.format(creator.postsCount ?? 0)} · 均赞 ${number.format(creator.performance.avgLikes ?? 0)} · 均评 ${number.format(creator.performance.avgComments ?? 0)}`],
            ["近月涨粉", <span className={(creator.followersGrowthPct ?? 0) < 0 ? "negative" : "positive"}>{percent(creator.followersGrowthPct, 2)}</span>], ["公开邮箱", creator.contactsHasEmail ? "有 ✓" : "未确认"],
            ["创作者国家", creator.creatorCountry], ["发现来源", creator.discoverySources?.length ? "Agent 多源发现" : null], ["项目批次", creator.discoveryBatch],
          ]}/>
        </DataCard>
        <DataCard title="粉丝质量">
          <FieldRows rows={[["真人 / 网红 / 普通量粉 / 可疑 / 机器人", creator.integrity.audienceTypes ? `${percent(creator.integrity.audienceTypes.real_people)} / ${percent(creator.integrity.audienceTypes.influencers)} / ${percent(creator.integrity.audienceTypes.mass_followers)} / ${percent(creator.integrity.audienceTypes.suspicious)} / ${percent(creator.integrity.audienceTypes.bots)}` : null], ["假粉率（粉丝 / 点赞者）", `${percent(creator.integrity.fakePct)} / ${percent(creator.integrity.likersFakePct)}`], ["低质评论占比", percent(creator.integrity.lowQualityRatio)]]}/>
          <h4>受众可触达性</h4><MetricList items={creator.integrity.audienceReachability}/>
        </DataCard>
        <DataCard title="受众画像（粉丝）" className="span-2">
          <div className="split-data"><div><h4>受众国家</h4><MetricList items={creator.audience.countries}/><h4>受众城市</h4><MetricList items={creator.audience.cities}/></div><div><h4>受众性别</h4><MetricList items={creator.audience.genders}/><h4>受众年龄</h4><MetricList items={creator.audience.ages}/></div><div><h4>受众语言</h4><MetricList items={creator.audience.languages}/><h4>受众兴趣</h4><p className="inline-values">{creator.audience.interests?.join("、") || "未采集"}</p></div></div>
          <div className="audience-callout"><span>目标国家受众合计 <strong>{percent(creator.audience.targetCountriesPct)}</strong></span><span>主力画像 <strong>{creator.audience.ages?.sort((a,b) => b.pct-a.pct)?.[0]?.name ?? "未采集"} · 女性 {percent(female)}</strong></span></div>
        </DataCard>
        <DataCard title="点赞者画像（更难造假）">
          <h4>点赞者国家</h4><MetricList items={creator.likers.countries}/><h4>点赞者性别</h4><MetricList items={creator.likers.genders}/><h4>点赞者年龄</h4><MetricList items={creator.likers.ages}/>
        </DataCard>
        <DataCard title="受众关注">
          <h4>受众高频话题</h4><p className="inline-values">{creator.audience.hashtags?.map((item) => `#${item.name} ${percent(item.pct)}`).join("、") || "未采集"}</p>
          <h4>受众高频提及</h4><p className="inline-values">{creator.audience.mentions?.map((item) => `@${item.name} ${percent(item.pct)}`).join("、") || "未采集"}</p>
        </DataCard>
        <DataCard title="电商与合作品牌">
          <FieldRows rows={[["电商橱窗", <TextLink href={creator.storefront.url}>{creator.storefront.type ?? "查看入口"}</TextLink>], ["Storefront 核验", creator.storefront.status], ["Amazon Finds 占比", percent(creator.commerce.amazonFindsRatio)], ["赞助内容占比", percent(creator.commerce.sponsorshipSaturation)], ["相关自然内容", creator.commerce.organicRelevantPosts], ["SKU 类目命中", creator.commerce.skuCategoriesHit]]}/>
          <h4>合作品牌</h4><p className="inline-values">{creator.commerce.brandCollaborations?.join("、") || "未采集"}</p>
          <h4>公开购物入口</h4><div className="link-list">{creator.storefront.bioLinks?.slice(0, 8).map((url, index) => <TextLink href={url} key={url}>入口 {index + 1}</TextLink>)}</div>
        </DataCard>
        {!!performanceRows.length && <DataCard title="ER 对照与内容相关性"><FieldRows rows={performanceRows}/></DataCard>}
      </div>

      <section className="detail-section" id="detail-comments">
        <div className="section-heading"><div><p className="eyebrow">PURCHASE INTENT</p><h2>购买意向评论（谁说了什么）</h2></div><div className="section-stat"><strong>{creator.intent.comments.length}</strong><span>条意向评论 · 高意向 {creator.intent.highCount ?? 0}</span></div></div>
        <div className="translation-bar"><span><Icon name="check" size={15}/> 翻译状态 {creator.intent.translationSummary?.status ?? "未采集"}</span><span>{creator.intent.translationSummary?.translated_count ?? 0}/{creator.intent.translationSummary?.requested_count ?? 0} 条评论已翻译</span><span>源语言 {Object.entries(creator.intent.translationSummary?.source_languages ?? {}).map(([key, value]) => `${key} ${value}`).join(" / ") || "未采集"}</span></div>
        {comments.length ? <div className="comments-grid">{comments.map((comment, index) => <article className={`comment-card grade-card-${comment.grade}`} key={`${comment.postUrl}-${comment.username}-${index}`}><div><strong>@{comment.username}</strong><span className={`grade grade-${comment.grade}`}>{comment.gradeZh ?? comment.grade}</span><TextLink href={comment.postUrl}>看帖</TextLink></div><p>{comment.translatedZh}</p><small>原文 [{comment.sourceLanguage ?? "und"}]：{comment.originalText}</small></article>)}</div> : <div className="empty-data">本轮没有识别到购买意向评论</div>}
        {creator.intent.comments.length > 8 && <button className="load-more" onClick={() => setShowAllComments(!showAllComments)}>{showAllComments ? "收起评论" : `展开全部 ${creator.intent.comments.length} 条评论`}</button>}
      </section>

      {hasCommercialEvidence && <section className="detail-section" id="detail-evidence">
        <div className="section-heading"><div><p className="eyebrow">COMMERCIAL EVIDENCE</p><h2>{hasReels ? "报价、Reels 与赞助内容证据" : "报价与赞助内容证据"}</h2></div></div>
        <div className="card-grid evidence-grid">
          {hasPricing && <DataCard title="预估报价证据与口径" className="span-2">
            <FieldRows rows={[["性质", "展示型估算，非博主实际报价"], ["公式", price ? `${number.format(price.average_plays ?? 0)} ÷ 1,000 × CPM ${money.format(price.cpm_usd?.default ?? 0)}` : null], ["结果", price?.quote_usd ? `默认 ${money.format(price.quote_usd.default)} · 区间 ${money.format(price.quote_usd.min)}-${money.format(price.quote_usd.max)}` : null], ["样本状态", price ? `${price.status} · ${price.sample_count}/${price.requested_reels} · ${evidenceSourceLabel(price.source)}` : null], ["总体口径", price?.population_basis]]}/>
            {!!price?.reels?.length && <><h4>Reels 明细</h4><div className="reel-list">{price.reels.map((reel, index) => <TextLink href={reel.url} key={reel.url}>{index + 1}. {number.format(reel.play_count ?? 0)} 播放</TextLink>)}</div></>}
          </DataCard>}
          {hasSponsoredPosts && <DataCard title="赞助帖样例">
            <div className="sponsor-list">{creator.evidence.sponsoredPosts!.map((post, index) => <TextLink href={post.url} key={post.url}>帖 {index + 1} · ♥{number.format(post.likes ?? 0)} · 💬{number.format(post.comments ?? 0)}</TextLink>)}</div>
          </DataCard>}
          {hasReels && <DataCard title="近期 Reels 证据" className="span-3">
            <div className="reel-samples">{creator.evidence.pricingReelSamples!.map((reel, index) => <article key={reel.url}><div><span>REEL {String(index + 1).padStart(2, "0")}</span><TextLink href={reel.url}>{number.format(reel.playCount ?? 0)} 播放</TextLink></div><p>{reel.caption}</p><small>♥ {number.format(reel.likeCount ?? 0)} · 💬 {number.format(reel.commentCount ?? 0)}{reel.pinned ? " · 置顶" : ""}</small></article>)}</div>
          </DataCard>}
        </div>
      </section>}
      <footer className="detail-footer">数据来自已完成项目批次 {creator.discoveryBatch} · 演示页不会重新采集或改写数据</footer>
    </main>
  </div>;
}

export default function Home() {
  const [phase, setPhase] = useState<"idle" | "ready" | "searching" | "revealing">("idle");
  const [searchStep, setSearchStep] = useState(0);
  const [selected, setSelected] = useState<Creator | null>(null);
  const [contactReason, setContactReason] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [visibleCount, setVisibleCount] = useState(LOAD_SIZE);
  const [mobileFiltersOpen, setMobileFiltersOpen] = useState(false);
  const searchTimers = useRef<number[]>([]);
  const creators = useMemo(() => demoData.creators.filter((creator) => `${creator.handle}${creator.fullName}`.toLowerCase().includes(query.toLowerCase())), [query]);
  const visibleCreators = creators.slice(0, visibleCount);
  const hasMore = visibleCount < creators.length;

  const run = () => {
    searchTimers.current.forEach((timer) => window.clearTimeout(timer));
    setQuery("");
    setVisibleCount(LOAD_SIZE);
    setMobileFiltersOpen(false);
    setSearchStep(0);
    setPhase("searching");
    [1, 2, 3, 4].forEach((step) => searchTimers.current.push(window.setTimeout(() => setSearchStep(step), 440 + step * 430)));
    searchTimers.current.push(window.setTimeout(() => setPhase("revealing"), 2550));
    searchTimers.current.push(window.setTimeout(() => setPhase("ready"), 3400));
  };

  useEffect(() => () => searchTimers.current.forEach((timer) => window.clearTimeout(timer)), []);

  return <div className="app-shell">
    <header className="topbar"><a className="brand" href="/"><span className="brand-mark"><i/><i/><i/></span><strong>COLLECTOR</strong></a><div className="topbar-center"><a className="topbar-link" href="/">产品首页</a><span className="nav-active">Discovery</span></div><div className="topbar-right"><span className="fresh-dot"/> 数据更新于 2026-08-11 <button onClick={() => setContactReason("咨询定制 Discovery 服务")}>联系我们</button></div></header>
    <div className="workspace">
      <FilterPanel onRun={run} onCustom={setContactReason} mobileOpen={mobileFiltersOpen} onMobileToggle={() => setMobileFiltersOpen((open) => !open)}/>
      <main className={`results-panel phase-${phase}`}>
        {(phase === "ready" || phase === "revealing") && <><section className="results-head"><div><p className="eyebrow">DISCOVERY RESULTS</p><h2>为你匹配的创作者</h2><p>资料更完整的创作者优先展示，点击即可查看受众、评论与商业证据。</p></div><div className="results-tools"><label><Icon name="search" size={16}/><input value={query} onChange={(event) => { setQuery(event.target.value); setVisibleCount(LOAD_SIZE); }} placeholder="搜索全部创作者" /></label><span className="data-badge"><Icon name="database" size={14}/> {demoData.sourceRun}</span></div></section>
        <section className="active-filters" aria-label="当前 Discovery 条件">
          <div className="filter-summary-title"><small>当前任务</small><strong>默认体验条件</strong></div>
          <span><small>平台</small><strong>Instagram</strong></span>
          <span><small>粉丝规模</small><strong>10K-150K</strong></span>
          <span><small>目标市场受众</small><strong>≥ 30%</strong></span>
          <span><small>虚假粉丝率</small><strong>&lt; 25%</strong></span>
          <button onClick={() => setContactReason("调整默认 Discovery 条件")}><Icon name="tune" size={14}/>调整条件</button>
        </section></>}
        {phase === "idle" ? <DiscoveryIdle onRun={run}/> : phase === "searching" ? <DiscoveryAnimation step={searchStep}/> : <>
          <div className="results-table">
            <div className="table-key"><span>达人</span><span>表现</span><span>购买意向评论（谁说了什么）</span><span>电商 / 赞助</span><span>粉丝质量</span><span>核心受众</span><span>报价</span><span/></div>
            <section className={`results-list ${phase === "revealing" ? "revealing" : ""}`}>{visibleCreators.map((creator, index) => <div className="result-reveal" style={{ "--row-index": index } as React.CSSProperties} key={creator.handle}><ResultRow creator={creator} onOpen={() => setSelected(creator)}/></div>)}</section>
          </div>
          {!creators.length && <div className="empty-data">没有匹配的达人</div>}
          {hasMore && <button className="load-more-results" onClick={() => setVisibleCount((count) => count + LOAD_SIZE)}>加载更多创作者</button>}
          {!!creators.length && <footer className="results-footer"><span>向下浏览，按需加载更多创作者</span><span>点击任意达人查看完整画像、评论与原帖证据</span></footer>}
        </>}
      </main>
    </div>
    {selected && <CreatorDetail creator={selected} onClose={() => setSelected(null)}/>}
    {contactReason && <ContactModal reason={contactReason} onClose={() => setContactReason(null)}/>}
  </div>;
}
